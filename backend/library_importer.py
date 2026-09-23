"""Imports a folder of the user's own media files (a local path, or an SMB/NFS
share mounted into the container) into the VOD pool -- the file-backed
counterpart to emby_vod_importer.py.

The provider row's base_url is the root path as this container sees it. Each
video file becomes a movie or episode source whose local_file_path is served
by xc_server's existing local-file branch. Titles come from library_parser
and are resolved against TMDB by library_matcher (persisted, one lookup per
show), then written through vod_db's provider-agnostic bulk_import_plex_*
with preserve_existing_detail so a rescan never blanks TMDB-enriched detail.

Safety: an unmounted NAS share usually shows up as an EMPTY directory, which
would look like "every file was deleted". The stale-source cleanup is skipped
whenever the scan looks implausibly small compared to what is already
imported.
"""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import config
import library_matcher
import library_parser
import rclone_client
import vod_db
import vod_importer

logger = logging.getLogger(__name__)

_MATCH_CONCURRENCY = 6
# Directories that hold extras/system files rather than the main feature.
_SKIP_DIRS = {"extras", "featurettes", "trailers", "behind the scenes", "deleted scenes", "samples", "sample",
              "@eaDir", "#recycle", "$RECYCLE.BIN", "lost+found", ".Trash-1000"}
# NFS mounts default to "hard": I/O blocks FOREVER when the server is gone,
# and a blocked thread can't be cancelled. So every filesystem walk runs on a
# small dedicated pool under a deadline -- an offline share costs at most these
# two stranded threads (which self-heal when the server returns) and fails the
# import with a clear error, instead of hanging the import queue and starving
# the app's shared thread pool.
_FS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="library-fs")
_PROBE_TIMEOUT_SECONDS = 15
_SCAN_TIMEOUT_SECONDS = 3600
_MIN_SAFE_FRACTION = 0.5   # scan must find at least this share of already-imported sources...
_MIN_GUARD_COUNT = 20      # ...once the provider has at least this many


def _scan_root(root: str) -> tuple[list[tuple[str, int]], list[str]]:
    """([(relative posix path, size bytes)], read_errors) for every video file
    under root. Symlinked directories are not followed (avoids loops and
    escaping the root).

    os.walk silently SKIPS a directory it can't list unless given onerror, so
    without read_errors an unreadable folder (permission change, transient
    SMB/NFS error) would look exactly like "every file in it was deleted" and
    the removal cleanup would purge that whole show/folder from the catalog.
    Any error is reported so the caller can refuse to remove anything."""
    found: list[tuple[str, int]] = []
    errors: list[str] = []

    def _on_error(exc: OSError) -> None:
        errors.append(f"{getattr(exc, 'filename', None) or '?'}: {exc.strerror or exc}")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_on_error):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith(".") or not library_parser.is_video_file(fn):
                continue
            stem = os.path.splitext(fn)[0].lower()
            if stem.endswith("-trailer") or stem.endswith(".trailer") or stem == "sample":
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError as exc:
                errors.append(f"{full}: {exc.strerror or exc}")   # exists but unreadable != deleted
                continue
            found.append((os.path.relpath(full, root).replace("\\", "/"), size))
    found.sort()
    return found, errors


def _probe_root(root: str) -> bool:
    if not os.path.isdir(root):
        return False
    with os.scandir(root) as it:   # forces a real directory read, not just a cached stat
        next(it, None)
    return True


async def _fs_call(fn, timeout: float, what: str):
    try:
        return await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(_FS_EXECUTOR, fn), timeout)
    except asyncio.TimeoutError:
        raise ValueError(
            f"{what} timed out after {timeout:.0f}s -- the share is not responding "
            "(NFS/SMB server offline or unreachable?)") from None


def _episode_name(parsed: library_parser.ParsedPath) -> str:
    return f"Episode {parsed.episode}"


_IMPORT_LOCKS: dict[int, asyncio.Lock] = {}


async def import_library(provider_id: int) -> dict:
    """Serialised per provider (same as dispatcharr_dvr_importer): the
    scheduler and a manual "Import catalog" click can otherwise run two scans
    of one library at once, racing on writes and on the removal cleanup."""
    lock = _IMPORT_LOCKS.setdefault(provider_id, asyncio.Lock())
    async with lock:
        return await _import_library_locked(provider_id)


async def _import_library_locked(provider_id: int) -> dict:
    provider = await asyncio.to_thread(vod_db.get_provider, provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")
    started = time.monotonic()
    backend = provider.get("library_backend") or "local"
    previous_count = await asyncio.to_thread(vod_db.count_provider_sources, provider_id)

    if backend == "local":
        root = (provider.get("base_url") or "").strip()
        if not root:
            raise ValueError("library root is not set")
        root = os.path.normpath(root)
        if not await _fs_call(lambda: _probe_root(root), _PROBE_TIMEOUT_SECONDS, f"checking library root {root!r}"):
            raise ValueError(f"library root {root!r} is not a directory this container can read")
        files, scan_errors = await _fs_call(lambda: _scan_root(root), _SCAN_TIMEOUT_SECONDS, f"scanning {root!r}")
    else:
        # rclone-mediated remote (smb/sftp/s3/gdrive/dropbox/box) -- see
        # rclone_client.py. Same [(rel_path, size)] shape as the local scan,
        # so everything below this point is identical for both.
        root = None
        remote_config = vod_db.get_library_remote_config(provider)
        try:
            await rclone_client.probe(provider, remote_config)
        except rclone_client.RcloneError as exc:
            raise ValueError(f"can't reach the {backend} remote: {exc}") from None
        try:
            files = await rclone_client.list_remote(provider, remote_config)
        except rclone_client.RcloneError as exc:
            raise ValueError(f"listing the {backend} remote failed: {exc}") from None
        scan_errors = []

    logger.info("[library_importer] provider=%s: found %d video file(s) (backend=%s, previously %d imported)",
                provider["name"], len(files), backend, previous_count)

    parsed_by_rel = {rel: library_parser.parse_path(rel) for rel, _ in files}
    cache = library_matcher.ShowMatchCache()

    # Resolve each distinct movie file / show once, concurrently, before the
    # per-file pass below reads the memoised results.
    reps: dict[tuple, str] = {}
    for rel, parsed in parsed_by_rel.items():
        key = ("show", parsed.show_key or parsed.title.lower()) if parsed.kind == "episode" else ("movie", rel)
        reps.setdefault(key, rel)
    sem = asyncio.Semaphore(_MATCH_CONCURRENCY)

    async def _resolve(rel: str):
        async with sem:
            return await library_matcher.resolve(provider_id, rel, parsed_by_rel[rel], cache)
    await asyncio.gather(*(_resolve(rel) for rel in reps.values()))

    exclude_categories = provider.get("import_exclude_categories") or []
    exclude_uncategorized = bool(provider.get("import_exclude_uncategorized"))
    lang = config.get_import_language_exclusion()
    country = config.get_import_country_exclusion()

    movie_items: list[dict] = []
    series_items: dict[str, dict] = {}
    local_paths: dict[str, str] = {}
    counts = {"matched": 0, "ambiguous": 0, "unmatched": 0, "unparseable": 0}
    seen_movie_ids: set[str] = set()
    seen_episode_ids: set[str] = set()
    seen_series_ids: set[str] = set()

    for rel, size in files:
        parsed = parsed_by_rel[rel]
        if not parsed.title:
            counts["unparseable"] += 1
            logger.warning("[library_importer] provider=%s: no usable title for %r -- skipped", provider["name"], rel)
            continue
        result = await library_matcher.resolve(provider_id, rel, parsed, cache)
        counts[result.status] += 1
        matched = result.status == "matched"
        name = (result.title if matched and result.title else parsed.title)
        year = (result.year if matched and result.year else parsed.year)
        tmdb_id = result.tmdb_id if matched else None
        ext = os.path.splitext(rel)[1].lstrip(".").lower() or "mkv"
        if backend == "local":
            local_paths[rel] = os.path.join(root, *rel.split("/"))
        # else: no real local path -- xc_server resolves playback for a
        # remote-backend source dynamically (provider_id + provider_stream_id
        # -> that provider's rclone serve http daemon), so local_file_path
        # simply stays NULL for these rows. That NULL is also what keeps
        # them out of _delete_file_if_present's reach entirely -- nothing to
        # accidentally delete since there never was a local file.
        archive = vod_importer._should_auto_archive(name, None, exclude_categories, exclude_uncategorized, lang, country)

        if parsed.kind == "movie":
            seen_movie_ids.add(rel)
            movie_items.append({
                "name": name, "year": year, "provider_stream_id": rel, "container_extension": ext,
                "tmdb_id": tmdb_id, "file_size_bytes": size, "auto_archive": archive,
                "preserve_existing_detail": True,
                # last_enriched_at deliberately unset: the lazy enrichment pass
                # (vod_importer.enrich_movie) fills description/poster/cast from
                # TMDB, going straight to it when tmdb_id is known.
            })
            continue

        show_key = parsed.show_key or parsed.title.lower()
        series_id = f"lib-{show_key}"
        seen_series_ids.add(series_id)
        item = series_items.setdefault(series_id, {
            "name": name, "year": year, "provider_series_id": series_id, "tmdb_id": tmdb_id,
            "auto_archive": archive, "preserve_existing_detail": True, "episodes": [],
        })
        seen_episode_ids.add(rel)
        item["episodes"].append({
            "season_number": parsed.season or 0, "episode_number": parsed.episode or 0,
            "name": _episode_name(parsed), "provider_stream_id": rel, "container_extension": ext,
            "file_size_bytes": size,
        })

    movie_result = await asyncio.to_thread(vod_db.bulk_import_plex_movies, provider_id, movie_items)
    await asyncio.to_thread(vod_db.set_movie_source_local_paths, provider_id, local_paths)
    series_result = await asyncio.to_thread(vod_db.bulk_import_plex_series, provider_id, list(series_items.values()))
    await asyncio.to_thread(vod_db.set_episode_source_local_paths, provider_id, local_paths)

    # Removal cleanup, guarded against an unmounted/partial share.
    reconcile_skipped = None
    total_seen = len(seen_movie_ids) + len(seen_episode_ids)
    if scan_errors:
        reconcile_skipped = f"{len(scan_errors)} path(s) could not be read, e.g. {scan_errors[0]}"
    elif not files and previous_count:
        reconcile_skipped = "scan found no files (share unmounted?)"
    elif previous_count >= _MIN_GUARD_COUNT and total_seen < previous_count * _MIN_SAFE_FRACTION:
        reconcile_skipped = (
            f"scan found only {total_seen} of {previous_count} previously imported files -- treated as a "
            "possibly unmounted or partial share, so nothing was removed (if you really deleted most of "
            "the library, remove and re-add this source to reset it)")
    removed = {"movie_sources_removed": 0, "series_sources_removed": 0, "episode_sources_removed": 0}
    if reconcile_skipped:
        logger.warning("[library_importer] provider=%s: skipping removal cleanup -- %s", provider["name"], reconcile_skipped)
    else:
        removed = await asyncio.to_thread(
            vod_db.reconcile_provider_catalog_sources, provider_id,
            seen_movie_stream_ids=seen_movie_ids, seen_series_ids=seen_series_ids,
        )
        removed["episode_sources_removed"] += await asyncio.to_thread(
            vod_db.remove_stale_episode_sources, provider_id, seen_episode_ids)
        await asyncio.to_thread(vod_db.delete_library_matches_not_in, provider_id, "movie", seen_movie_ids)
        await asyncio.to_thread(vod_db.delete_library_matches_not_in, provider_id, "series",
                                {(p.show_key or p.title.lower()) for p in parsed_by_rel.values() if p.kind == "episode"})

    await asyncio.to_thread(vod_db.set_provider_import_totals, provider_id, len(seen_movie_ids), len(series_items))
    result = {
        "provider": provider["name"], "files_found": len(files),
        "movies_created": movie_result.get("movies_created", 0), "movies_matched": movie_result.get("movies_matched", 0),
        "series_created": series_result.get("series_created", 0), "series_matched": series_result.get("series_matched", 0),
        "episodes_imported": series_result.get("episodes_imported", 0),
        "tmdb": counts, "removed": removed, "removal_cleanup_skipped": reconcile_skipped,
        "scan_errors": len(scan_errors),
    }
    logger.info("[library_importer] provider=%s result=%s (%.1fs)", provider["name"], result, time.monotonic() - started)
    return result

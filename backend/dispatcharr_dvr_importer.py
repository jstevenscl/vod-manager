"""
Imports finished Dispatcharr DVR recordings into the VOD pool -- the DVR
counterpart to plex_importer.py/emby_vod_importer.py.

Two modes, chosen per-provider by whether dvr_local_path is set:

- Phase 1a, same-host: a shared/bind-mounted volume. A recording is
  playable the moment it's imported -- movie_sources/episode_sources.
  local_file_path points straight at the file on disk (no bytes ever
  copied), and xc_server.py serves/transcodes it from there with no
  further Dispatcharr involvement.
- Phase 1b, cross-host (dvr_local_path is None): downloads each
  recording's bytes once into VOD Manager's own storage
  (DATA_DIR/dvr_recordings/{provider_id}/...) via
  dispatcharr_dvr_client.download_recording_file, then serves that local
  copy identically to Phase 1a from then on. Confirmed live (dispatch-test,
  v0.27.2, 2026-07-26) that the file-download endpoint takes the same
  X-API-Key auth as everything else -- the one open question blocking this
  is resolved. A recording already present locally (by path) is never
  re-downloaded; a failed download is retried on the next poll cycle
  rather than failing the whole import pass.

Unlike XC/Plex/Emby, a DVR recording arrives with almost no metadata --
only whatever Dispatcharr's EPG match captured (title/sub_title/description/
season/episode/poster_url when matched) plus the output file's own path.
season/episode ARE available as structured ints on custom_properties
(confirmed live, dispatch-test v0.27.2, 2026-07-25 -- earlier research said
otherwise) and are always preferred; path-pattern parsing is a fallback for
whenever they're ever missing (an EPG-unmatched recording, or an older
Dispatcharr version). A real (but conservative -- exact-title-only) TMDB
search fills in genre-adjacent detail XC/Plex/Emby get for free, only for
fields Dispatcharr's own EPG match didn't already provide. Anything not
confidently resolved lands in the existing Missing Artwork / Needs Review
queues for a human (or AI-assist) to finish, the same as every other
import path already does -- no new review flow invented here.

Writes go through vod_db's bulk_import_plex_movies/bulk_import_plex_series
unmodified (same functions Plex/Emby already share) -- local_file_path is
DVR-specific and deliberately kept out of those shared functions, applied
in a separate small pass afterward instead (set_movie_source_local_paths/
set_episode_source_local_paths), so nothing about the Plex/Emby import path
has to change to support this.
"""

import asyncio
import logging
import os
import re

import config
import dispatcharr_dvr_client
import tmdb_sync
import vod_db

logger = logging.getLogger(__name__)

# Serializes a given provider's own import against itself -- the background
# _dispatcharr_dvr_poller loop and a manual "Import catalog" click (or the
# "Apply rules now" job) can otherwise overlap, and in download mode
# (Phase 1b) two concurrent calls racing to download the SAME recording to
# the SAME .part path is a real bug, not just a Windows-testing artifact:
# confirmed live (WinError 32 on Windows; on Linux the second writer would
# instead silently corrupt the first's in-progress download, arguably
# worse). One lock per provider_id, not a single global lock, so unrelated
# providers still import concurrently.
_import_locks: dict[int, asyncio.Lock] = {}


def _get_import_lock(provider_id: int) -> asyncio.Lock:
    if provider_id not in _import_locks:
        _import_locks[provider_id] = asyncio.Lock()
    return _import_locks[provider_id]

# Matches "S01E05", "s1e5", "S01.E05", etc. anywhere in a recording's file
# path -- fallback only now (see _resolve_season_episode); Dispatcharr's own
# structured season/episode fields are preferred whenever present. Kept
# deliberately loose (not tied to one exact folder shape) since a provider's
# TV Path Template is itself admin-configurable. A recording with no match
# (structured or path) is treated as a movie.
_SEASON_EPISODE_RE = re.compile(r"[Ss](\d{1,2})[._\- ]?[Ee](\d{1,3})")


def _resolve_season_episode(program: dict, file_path: str | None) -> tuple[int, int] | None:
    """Dispatcharr's own custom_properties.season/episode (structured ints)
    win whenever both are present and valid -- confirmed live as the
    authoritative source dispatch-test actually populates, strictly more
    reliable than regex-guessing the path. Falls back to path parsing only
    when either is missing (e.g. a recording with no EPG match at all)."""
    season, episode = program.get("season"), program.get("episode")
    if isinstance(season, int) and isinstance(episode, int) and season > 0 and episode > 0:
        return season, episode
    if not file_path:
        return None
    m = _SEASON_EPISODE_RE.search(file_path)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# Dispatcharr's recordings API reports file_path as an ABSOLUTE path from
# its OWN container filesystem's perspective (confirmed live: e.g.
# "/data/recordings/TV_Shows/Show/S01E01.mkv"), not a path relative to some
# implicit recordings root -- an admin's dvr_local_path bind-mount already
# corresponds to whatever Dispatcharr itself calls this root, so it must be
# stripped before joining with dvr_local_path, or the mapped path silently
# double-nests it (dvr_local_path + "data/recordings/..." instead of just
# dvr_local_path + the part actually under that root).
_DEFAULT_REMOTE_RECORDINGS_ROOT = "/data/recordings"


def _strip_remote_root(file_path: str, remote_root: str) -> str:
    normalized = file_path.replace("\\", "/")
    root = remote_root.rstrip("/") + "/"
    if normalized.startswith(root):
        return normalized[len(root):]
    # Root didn't match (a differently-configured Dispatcharr instance, or a
    # future/older version with a different layout) -- best-effort fall back
    # to the old lstrip-only behavior rather than erroring the whole import.
    return normalized.lstrip("/")


def _guess_title(recording: dict, program: dict, file_path: str | None) -> str:
    """EPG program title is higher-confidence than anything derived from
    the file path (Dispatcharr already matched it against the channel's
    real guide data), so it wins whenever present. Falls back to whatever
    name Dispatcharr always sets on the recording itself, and finally the
    bare filename, so this never returns an empty title even for a
    completely EPG-unmatched recording."""
    if program.get("title"):
        return program["title"]
    name = recording.get("name") or recording.get("channel_name")
    if name:
        return name
    if file_path:
        base = os.path.splitext(os.path.basename(file_path))[0]
        if base:
            return base
    return f"Recording {recording.get('id')}"


async def _enrich_from_tmdb(title: str, content_type: str) -> dict:
    """Conservative: only auto-applies when a candidate's title matches
    exactly (case-insensitive) -- search_title() already sorts exact
    matches first, so the top candidate is checked directly rather than
    picking whichever TMDB ranks highest by popularity. No match found (or
    TMDB not configured) just means less detail, never an error -- a DVR
    recording without a confident TMDB match still imports, same as any
    provider item that fails enrichment. Note: search_title doesn't resolve
    genre names (TMDB's search endpoint only returns numeric genre ids),
    so genre is deliberately not set here -- left for a human/AI-assist
    pass later, same as any other import with no genre available."""
    try:
        candidates = await tmdb_sync.search_title(title, content_type)
    except Exception as exc:
        logger.warning("[dispatcharr_dvr_importer] TMDB search failed for %r: %s", title, exc)
        return {}
    if not candidates:
        return {}
    top = candidates[0]
    if (top.get("name") or "").strip().lower() != title.strip().lower():
        return {}
    return {
        "tmdb_id": top.get("tmdb_id"),
        "description": top.get("overview"),
        "poster_url": top.get("poster_url"),
        "cast_list": ", ".join(top.get("cast") or []) or None,
        "year": top.get("year"),
    }


async def import_dvr_recordings(provider_id: int) -> dict:
    async with _get_import_lock(provider_id):
        return await _import_dvr_recordings_locked(provider_id)


async def _import_dvr_recordings_locked(provider_id: int) -> dict:
    provider = vod_db.get_provider(provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")
    if not provider.get("dispatcharr_connection_id"):
        raise ValueError(f"provider {provider_id} has no linked Dispatcharr connection configured")
    connection = vod_db.get_dispatcharr_connection(provider["dispatcharr_connection_id"])
    if not connection:
        raise ValueError(f"provider {provider_id}'s linked Dispatcharr connection no longer exists")

    local_path = provider.get("dvr_local_path")
    download_mode = not local_path
    if download_mode:
        # Phase 1b: no shared bind-mount, so VOD Manager owns a local copy
        # under its own data dir instead -- same shape dvr_local_path would
        # point at for Phase 1a, just VOD Manager-managed rather than
        # admin-configured.
        local_path = str(config.DATA_DIR / "dvr_recordings" / str(provider_id))

    recordings = await dispatcharr_dvr_client.list_completed_recordings(connection)
    remote_root = provider.get("dvr_remote_recordings_root") or _DEFAULT_REMOTE_RECORDINGS_ROOT

    movie_items = []
    series_items: dict[str, dict] = {}
    local_paths_by_stream_id: dict[str, str] = {}
    profile_by_stream_id: dict[str, list[dict]] = {}
    skipped = 0
    downloaded = 0
    download_errors = 0

    for recording in recordings:
        file_info = dispatcharr_dvr_client.recording_file_info(recording)
        file_path = file_info["file_path"]
        if not file_path:
            skipped += 1
            continue
        recording_id = str(recording["id"])
        target_path = os.path.join(local_path, _strip_remote_root(file_path, remote_root))

        if download_mode and not os.path.isfile(target_path):
            try:
                await dispatcharr_dvr_client.download_recording_file(connection, recording["id"], target_path)
                downloaded += 1
            except Exception as exc:
                download_errors += 1
                logger.warning("[dispatcharr_dvr_importer] failed to download recording=%s (%r): %s",
                                recording["id"], file_info.get("file_name"), exc)
                # Retried on the next poll cycle rather than registering a
                # source that points at a file that doesn't actually exist.
                continue

        local_paths_by_stream_id[recording_id] = target_path

        program = dispatcharr_dvr_client.recording_program_info(recording)
        title = _guess_title(recording, program, file_path)
        # _guess_title already prefers program["title"] (the real EPG title)
        # whenever present, so `title` here IS that EPG title in the common
        # case -- matching against it (rather than a separately-fetched EPG
        # title) is correct and avoids a redundant lookup. Can be more than
        # one profile (e.g. two people each set up their own "Seinfeld"
        # profile) -- see vod_db.match_recording_profiles' docstring.
        profile_by_stream_id[recording_id] = vod_db.match_recording_profiles(provider_id, title, program.get("tvg_id"))
        season_episode = _resolve_season_episode(program, file_path)
        container_extension = os.path.splitext(file_path)[1].lstrip(".") or "mkv"

        if season_episode:
            season_number, episode_number = season_episode
            # One series per distinct show title -- every recorded episode
            # of the same show folds into the same series_items entry, same
            # as Plex/Emby's own "one API call already grouped by show"
            # shape, just reconstructed here since Dispatcharr's recordings
            # list is flat (one row per recording, not per show).
            if title not in series_items:
                series_items[title] = {
                    "name": title, "year": None, "provider_series_id": f"dvr-{provider_id}-{title}",
                    "genre": None, "description": program.get("description"), "director": None,
                    "cast_list": None, "poster_url": program.get("poster_url"), "last_enriched_at": None, "episodes": [],
                }
            series_items[title]["episodes"].append({
                "season_number": season_number, "episode_number": episode_number,
                "name": program.get("sub_title") or f"Episode {episode_number}",
                "description": program.get("description"), "duration_secs": None,
                "provider_stream_id": recording_id, "container_extension": container_extension,
            })
        else:
            movie_items.append({
                "name": title, "year": None, "provider_stream_id": recording_id,
                "container_extension": container_extension,
                "description": program.get("description"),
                "genre": None, "director": None, "cast_list": None, "poster_url": program.get("poster_url"),
                "last_enriched_at": None,
            })

    # TMDB enrichment pass -- one search per distinct title, not per
    # recording, since multiple episodes/movies can share a title. Only
    # fills gaps (item.get(k) falsy) rather than overwriting unconditionally
    # -- Dispatcharr's own EPG match (description, poster_url) is specific
    # to this exact episode/instance, a higher-confidence source than a
    # conservative exact-title search against the general show/movie, so it
    # must win when both are present.
    for item in movie_items:
        detail = await _enrich_from_tmdb(item["name"], "movie")
        for k, v in detail.items():
            if v and not item.get(k):
                item[k] = v
    for item in series_items.values():
        detail = await _enrich_from_tmdb(item["name"], "series")
        for k, v in detail.items():
            if v and k != "year" and not item.get(k):  # a series' year comes from its earliest season, not one TMDB lookup here
                item[k] = v

    movie_result = vod_db.bulk_import_plex_movies(provider_id, movie_items)
    movie_id_by_stream_id = vod_db.set_movie_source_local_paths(provider_id, local_paths_by_stream_id)

    series_result = vod_db.bulk_import_plex_series(provider_id, list(series_items.values()))
    series_id_by_stream_id = vod_db.set_episode_source_local_paths(provider_id, local_paths_by_stream_id)

    # Phase 2: a recording matched to one or more profiles (see
    # profile_by_stream_id above) routes into the UNION of those profiles'
    # own target categories instead of the provider-level default -- e.g.
    # two people who each set up their own profile for the same show both
    # get their own copy-into-category out of the one recording. Falls back
    # to the provider default only when nothing matched at all (or the
    # matched profile(s) left that content-type's category blank). Grouped
    # by category first so multiple recordings/profiles landing in the same
    # category collapse into one bulk_place_*_in_category call rather than
    # one call per recording.
    movie_ids_by_category: dict[int, set[int]] = {}
    for stream_id, movie_id in movie_id_by_stream_id.items():
        profiles = profile_by_stream_id.get(stream_id) or []
        category_ids = {p["target_movie_category_id"] for p in profiles if p.get("target_movie_category_id")}
        if not category_ids and provider.get("dvr_movie_category_id"):
            category_ids = {provider["dvr_movie_category_id"]}
        for category_id in category_ids:
            movie_ids_by_category.setdefault(category_id, set()).add(movie_id)
    for category_id, ids in movie_ids_by_category.items():
        vod_db.bulk_place_movies_in_category(list(ids), category_id)

    series_ids_by_category: dict[int, set[int]] = {}
    for stream_id, series_id in series_id_by_stream_id.items():
        profiles = profile_by_stream_id.get(stream_id) or []
        category_ids = {p["target_series_category_id"] for p in profiles if p.get("target_series_category_id")}
        if not category_ids and provider.get("dvr_series_category_id"):
            category_ids = {provider["dvr_series_category_id"]}
        for category_id in category_ids:
            series_ids_by_category.setdefault(category_id, set()).add(series_id)
    for category_id, ids in series_ids_by_category.items():
        vod_db.bulk_place_series_in_category(list(ids), category_id)

    logger.info("[dispatcharr_dvr_importer] provider=%s movies=%s series=%s skipped=%d downloaded=%d download_errors=%d",
                provider["name"], movie_result, series_result, skipped, downloaded, download_errors)

    return {
        "provider": provider["name"],
        **movie_result,
        **series_result,
        "skipped_no_file_path": skipped,
        "downloaded": downloaded,
        "download_errors": download_errors,
    }

"""
Imports finished Dispatcharr DVR recordings into the VOD pool -- the DVR
counterpart to plex_importer.py/emby_vod_importer.py.

Phase 1a only: same-host ingestion via a shared/bind-mounted volume
(providers.dvr_local_path). A recording is playable the moment it's
imported -- movie_sources/episode_sources.local_file_path points straight
at the file on disk, and xc_server.py serves/transcodes it from there with
no further Dispatcharr involvement. Phase 1b (network download, for when
dvr_local_path is None) is NOT implemented here -- it needs Dispatcharr's
DVR file-endpoint auth verified live first (see vod_manager-f09's notes);
a provider with no local path configured is currently just skipped, not
queued for later download.

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

import logging
import os
import re

import dispatcharr_dvr_client
import tmdb_sync
import vod_db

logger = logging.getLogger(__name__)

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
    provider = vod_db.get_provider(provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")
    if not provider.get("dispatcharr_connection_id"):
        raise ValueError(f"provider {provider_id} has no linked Dispatcharr connection configured")
    connection = vod_db.get_dispatcharr_connection(provider["dispatcharr_connection_id"])
    if not connection:
        raise ValueError(f"provider {provider_id}'s linked Dispatcharr connection no longer exists")

    local_path = provider.get("dvr_local_path")
    if not local_path:
        logger.info("[dispatcharr_dvr_importer] provider=%s has no dvr_local_path set -- "
                     "network download (Phase 1b) isn't implemented yet, skipping", provider["name"])
        return {"movies_created": 0, "movies_matched": 0, "series_created": 0, "series_matched": 0,
                "episodes_imported": 0, "skipped_no_local_path": True}

    recordings = await dispatcharr_dvr_client.list_completed_recordings(connection)
    remote_root = provider.get("dvr_remote_recordings_root") or _DEFAULT_REMOTE_RECORDINGS_ROOT

    movie_items = []
    series_items: dict[str, dict] = {}
    local_paths_by_stream_id: dict[str, str] = {}
    skipped = 0

    for recording in recordings:
        file_info = dispatcharr_dvr_client.recording_file_info(recording)
        file_path = file_info["file_path"]
        if not file_path:
            skipped += 1
            continue
        recording_id = str(recording["id"])
        local_paths_by_stream_id[recording_id] = os.path.join(local_path, _strip_remote_root(file_path, remote_root))

        program = dispatcharr_dvr_client.recording_program_info(recording)
        title = _guess_title(recording, program, file_path)
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
    touched_movie_ids = vod_db.set_movie_source_local_paths(provider_id, local_paths_by_stream_id)

    series_result = vod_db.bulk_import_plex_series(provider_id, list(series_items.values()))
    touched_series_ids = vod_db.set_episode_source_local_paths(provider_id, local_paths_by_stream_id)

    if provider.get("dvr_movie_category_id") and touched_movie_ids:
        vod_db.bulk_place_movies_in_category(touched_movie_ids, provider["dvr_movie_category_id"])
    if provider.get("dvr_series_category_id") and touched_series_ids:
        vod_db.bulk_place_series_in_category(touched_series_ids, provider["dvr_series_category_id"])

    logger.info("[dispatcharr_dvr_importer] provider=%s movies=%s series=%s skipped=%d",
                provider["name"], movie_result, series_result, skipped)

    return {
        "provider": provider["name"],
        **movie_result,
        **series_result,
        "skipped_no_file_path": skipped,
    }

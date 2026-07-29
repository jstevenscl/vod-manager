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
  rather than failing the whole import pass. Stored under a deterministic
  hashed filename (sha256 of provider_id+recording_id), not the real
  show/episode name Dispatcharr gave it -- this is VOD Manager's own copy
  on disk (unlike Phase 1a's shared-volume reference), and a VPS host's
  automated content scan looks at filenames first. The real title lives
  only in the database.

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
import hashlib
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone

import httpx

import config
import dispatcharr_dvr_client
from dispatcharr_dvr_client import _parse_iso
import notifications
import tmdb_sync
import vod_db
import xc_server

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
# TV Path Template is itself admin-configurable. A recording with no S/E
# match anywhere (structured, path, AND no sub_title to fall back on -- see
# _resolve_season_episode's third branch) is treated as a movie.
_SEASON_EPISODE_RE = re.compile(r"[Ss](\d{1,2})[._\- ]?[Ee](\d{1,3})")


def _resolve_season_episode(program: dict, file_path: str | None, recording_start_time: str | None) -> tuple[int, int] | None:
    """Dispatcharr's own custom_properties.season/episode (structured ints)
    win whenever both are present and valid -- confirmed live as the
    authoritative source dispatch-test actually populates, strictly more
    reliable than regex-guessing the path. Falls back to path parsing only
    when either is missing (e.g. a recording with no EPG match at all).

    Third fallback -- for Dispatcharr's own "TV fallback" path template
    (`TV_Shows/{show}/{start}.mkv`, no S/E encoded). Confirmed by reading
    Dispatcharr's actual source (apps/channels/tasks.py, dispatch-test
    v0.27.2, 2026-07-26): that template is only ever chosen when
    Dispatcharr's OWN season/episode lookup (_parse_epg_tv_movie_info,
    reading the exact same EPG ProgramData.custom_properties this function
    already checks via program.get("season")/"episode") also came up empty
    -- so whenever the fallback template actually fires, the structured-field
    branch above is guaranteed to have already missed too, and the file path
    itself carries no S/E to regex out either. There is no real episode
    identity available from either source in that case. But Dispatcharr
    itself decides movie-vs-TV from the EPG's own category tags (looking
    for "movie"/"film"), a signal that never appears anywhere in the
    Recording API VOD Manager consumes -- so this can't replicate that
    decision directly. sub_title (an episode's own name) is the next best
    proxy: EPG data commonly carries an episode title without a formal S/E
    number, and a genuine movie airing essentially never has one. When
    present, synthesize a stable (season=0 "specials", episode=<start time
    as YYYYMMDDHHMM>) identity from the recording's own fixed start time --
    stable across re-imports (unlike an import-order-based counter) so
    repeat recordings of the same show still fold into one series instead
    of each becoming its own disconnected "movie" entry."""
    season, episode = program.get("season"), program.get("episode")
    if isinstance(season, int) and isinstance(episode, int) and season > 0 and episode > 0:
        return season, episode
    if file_path:
        m = _SEASON_EPISODE_RE.search(file_path)
        if m:
            return int(m.group(1)), int(m.group(2))
    if program.get("sub_title") and recording_start_time:
        try:
            dt = datetime.fromisoformat(str(recording_start_time).replace("Z", "+00:00"))
            return 0, int(dt.strftime("%Y%m%d%H%M"))
        except (ValueError, TypeError):
            pass
    return None


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


def _normalize_title_for_match(s: str) -> str:
    """Strips accents/diacritics before the exact-match comparison in
    _enrich_from_tmdb -- confirmed live 2026-07-29: Dispatcharr's own EPG
    called a real show "Crime Exposé With Nancy O'Dell" while TMDB's own
    title for the exact same show is "Crime Expose with Nancy O'Dell" (no
    accent) -- a byte-for-byte exact match would reject this as "no
    confident match" even though it plainly is one. NFKD-normalizing and
    dropping combining marks (the accent itself) before comparing makes
    "é"/"e" equivalent without loosening the match to a fuzzy/substring
    one -- still exact on every letter, just accent-insensitive."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).strip().lower()


async def _enrich_from_tmdb(title: str, content_type: str) -> dict:
    """Conservative: only auto-applies when a candidate's title matches
    exactly (case/accent-insensitive, see _normalize_title_for_match) --
    search_title() already sorts exact matches first, so the top candidate
    is checked directly rather than picking whichever TMDB ranks highest by
    popularity. No match found (or TMDB not configured) just means less
    detail, never an error -- a DVR recording without a confident TMDB
    match still imports, same as any provider item that fails enrichment.
    Note: search_title doesn't resolve genre names (TMDB's search endpoint
    only returns numeric genre ids), so genre is deliberately not set here
    -- left for a human/AI-assist pass later, same as any other import with
    no genre available."""
    try:
        candidates = await tmdb_sync.search_title(title, content_type)
    except Exception as exc:
        logger.warning("[dispatcharr_dvr_importer] TMDB search failed for %r: %s", title, exc)
        return {}
    if not candidates:
        return {}
    top = candidates[0]
    if _normalize_title_for_match(top.get("name") or "") != _normalize_title_for_match(title):
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
    vod_db.cleanup_stale_recording_claims()

    movie_items = []
    series_items: dict[str, dict] = {}
    local_paths_by_stream_id: dict[str, str] = {}
    profile_by_stream_id: dict[str, list[dict]] = {}
    # A stream_id already registered as an episode (an earlier pass whose
    # season/episode resolution succeeded, or a manual repair) must never be
    # reclassified as a movie just because THIS pass's resolution came up
    # empty -- see provider_stream_ids_with_episode_source's own docstring
    # for the real recurrence this prevents.
    known_episode_stream_ids = vod_db.provider_stream_ids_with_episode_source(provider_id)
    # Fallback attribution for a TRUE single (portal_routes.
    # portal_schedule_single's "Record this episode" -- no dvr_recording_
    # profiles row at all, so profile_by_stream_id is always empty for it).
    # create_recording already stamps custom_properties.scheduled_by onto
    # the Dispatcharr Recording itself; this just reads it back so the
    # attribution pass below has something to fall back to when no profile
    # matched. Real gap found live 2026-07-28: every single ever recorded
    # was invisible in its own portal user's Library until this existed.
    scheduled_by_user_by_stream_id: dict[str, int | None] = {}
    # Identity/channel per recording -- needed to consume any pending_
    # recording_claims (portal_schedule_single's "Bill scheduled this while
    # it was still recording" path) the moment each recording actually
    # imports. Computed the exact same way find_existing_recording does for
    # a live not-yet-completed Recording, so a claim made against the live
    # API and the same recording read back here after completion always
    # produce an identical key.
    channel_id_by_stream_id: dict[str, int | None] = {}
    identity_key_by_stream_id: dict[str, str] = {}
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
        scheduled_by_user_by_stream_id[recording_id] = (
            (recording.get("custom_properties") or {}).get("scheduled_by") or {}
        ).get("dispatcharr_user_id")
        channel_id_by_stream_id[recording_id] = recording.get("channel")
        identity_key_by_stream_id[recording_id] = dispatcharr_dvr_client.episode_identity_key(
            (recording.get("custom_properties") or {}).get("program") or {}
        )
        if download_mode:
            # This is VOD Manager's own copy (Phase 1b), fully under our
            # control, unlike Phase 1a's shared-volume reference to a file
            # Dispatcharr itself owns and names -- so this is the one case
            # where renaming is both safe and worth doing. A VPS host doing
            # an automated content scan looks at filenames first; a real
            # show/episode name sitting in DATA_DIR/dvr_recordings is a much
            # easier target than an opaque hash with the actual title living
            # only in our own database. Deterministic (provider_id +
            # recording_id), not random, so a re-import after a restart
            # still resolves to the same path instead of re-downloading.
            ext = os.path.splitext(file_path)[1] or ".mkv"
            hashed_name = hashlib.sha256(f"{provider_id}:{recording_id}".encode()).hexdigest() + ext
            target_path = os.path.join(local_path, hashed_name)
        else:
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
        season_episode = _resolve_season_episode(program, file_path, recording.get("start_time"))
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
                "file_size_bytes": file_info.get("bytes_written"),
            })
        elif recording_id in known_episode_stream_ids:
            # This exact recording already lives correctly as an episode --
            # this pass's season/episode resolution just came up empty (a
            # stale Dispatcharr-side Recording predating a since-fixed
            # capture bug, most commonly), not a sign it's actually a movie.
            # Leave the existing episode alone rather than duplicating it.
            pass
        else:
            movie_items.append({
                "name": title, "year": None, "provider_stream_id": recording_id,
                "container_extension": container_extension,
                "description": program.get("description"),
                "genre": None, "director": None, "cast_list": None, "poster_url": program.get("poster_url"),
                "last_enriched_at": None, "file_size_bytes": file_info.get("bytes_written"),
            })

    # TMDB enrichment pass -- one search per distinct title, not per
    # recording, since multiple episodes/movies can share a title. Only
    # fills gaps (item.get(k) falsy) rather than overwriting unconditionally
    # -- Dispatcharr's own EPG match (description) is specific to this
    # exact episode/instance, a higher-confidence source than a conservative
    # exact-title search against the general show/movie, so it must win
    # when both are present.
    #
    # poster_url is the one deliberate exception -- TMDB always wins there
    # when it has a confident match, even overwriting an EPG poster_url that
    # was already set. Real gap found live 2026-07-29: Dispatcharr's own EPG
    # poster_url is a live-TV listing thumbnail (channel logo, or -- for
    # General Hospital specifically -- a wide cast-photo montage), not
    # actual portrait poster art; TMDB had a real poster for it the whole
    # time but the old gap-fill-only rule meant it could never win once the
    # EPG had supplied ANYTHING at all, which is effectively always.
    for item in movie_items:
        detail = await _enrich_from_tmdb(item["name"], "movie")
        for k, v in detail.items():
            if not v:
                continue
            if k == "poster_url" or not item.get(k):
                item[k] = v
    for item in series_items.values():
        detail = await _enrich_from_tmdb(item["name"], "series")
        for k, v in detail.items():
            if not v or k == "year":  # a series' year comes from its earliest season, not one TMDB lookup here
                continue
            if k == "poster_url" or not item.get(k):
                item[k] = v

    movie_result = vod_db.bulk_import_plex_movies(provider_id, movie_items)
    movie_id_by_stream_id = vod_db.set_movie_source_local_paths(provider_id, local_paths_by_stream_id)

    series_result = vod_db.bulk_import_plex_series(provider_id, list(series_items.values()))
    series_id_by_stream_id = vod_db.set_episode_source_local_paths(provider_id, local_paths_by_stream_id)

    # Attribute each recording to EVERY profile owner that matched it, not
    # just one -- match_recording_profiles' own docstring already documented
    # that two different people can each have their own profile match the
    # very same airing (Dispatcharr only ever produces one physical
    # Recording regardless), and category placement below has always
    # unioned all of their target categories. Library OWNERSHIP never did
    # the same thing until now -- it kept a single recording_profile_id
    # "winner" (profiles[0]) that decided who could see this in their
    # portal Library, so the second person's profile matched for category
    # purposes but their Library silently never got it. Real requirement
    # from the user, 2026-07-28: if Bill schedules something Emby already
    # has, Bill needs it in HIS library too, attached to the same file, and
    # each of them removing it from their own Library must never affect the
    # other's -- see movie_source_owners' own table comment. The old single
    # recording_profile_id column is still set (to profiles[0]) purely as
    # informal "which profile literally created this" metadata; it no
    # longer decides who can see this recording.
    for stream_id, profiles in profile_by_stream_id.items():
        is_movie = stream_id in movie_id_by_stream_id
        is_episode = stream_id in series_id_by_stream_id
        if profiles:
            owner_profile_id = profiles[0]["id"]
            if is_movie:
                vod_db.set_movie_source_recording_profile(provider_id, stream_id, owner_profile_id)
            elif is_episode:
                vod_db.set_episode_source_recording_profile(provider_id, stream_id, owner_profile_id)
            owner_user_ids = {p["dispatcharr_user_id"] for p in profiles if p.get("dispatcharr_user_id") is not None}
            for user_id in owner_user_ids:
                if is_movie:
                    vod_db.add_movie_source_owner(provider_id, stream_id, user_id)
                elif is_episode:
                    vod_db.add_episode_source_owner(provider_id, stream_id, user_id)
        else:
            # No profile matched at all -- a true single (see
            # scheduled_by_user_by_stream_id's own comment above). Falls
            # back to whoever's own portal login actually scheduled it, if
            # known; unattributable (e.g. admin-scheduled, or scheduled_by
            # never set) just means it never shows in anyone's portal
            # Library, same as today's behavior for every recording before
            # this fix existed.
            scheduled_by_user = scheduled_by_user_by_stream_id.get(stream_id)
            if scheduled_by_user is not None:
                if is_movie:
                    vod_db.set_movie_source_dispatcharr_user_id(provider_id, stream_id, scheduled_by_user)
                    vod_db.add_movie_source_owner(provider_id, stream_id, scheduled_by_user)
                elif is_episode:
                    vod_db.set_episode_source_dispatcharr_user_id(provider_id, stream_id, scheduled_by_user)
                    vod_db.add_episode_source_owner(provider_id, stream_id, scheduled_by_user)
        # Claim consumption runs for every stream_id regardless of the
        # profile/scheduled_by branches above -- a second person's claim
        # (portal_schedule_single's "Bill scheduled this while it was still
        # recording" path, see pending_recording_claims' own table comment)
        # is independent of however the FIRST person ended up attributed.
        if is_movie or is_episode:
            channel_id = channel_id_by_stream_id.get(stream_id)
            identity_key = identity_key_by_stream_id.get(stream_id)
            if channel_id is not None and identity_key:
                claimant_user_ids = vod_db.consume_pending_recording_claims(provider_id, channel_id, identity_key)
                for user_id in claimant_user_ids:
                    if is_movie:
                        vod_db.add_movie_source_owner(provider_id, stream_id, user_id)
                    elif is_episode:
                        vod_db.add_episode_source_owner(provider_id, stream_id, user_id)

    # Disk quota (opt-in, same dvr_user_limits row this loop also reads
    # default categories from) -- a person at/over their disk_quota_bytes has
    # NEW category placements withheld from this point forward -- existing
    # placements and the underlying imported file are never touched/deleted,
    # only further growth attributable to them is blocked. Computed once per
    # import pass (not per recording) since it only needs to reflect state as
    # of the start of this pass. default_{movie,series}_category_id is this
    # person's own standing default -- e.g. set on a portal user who has no
    # way to pick a category themselves (PortalRecordingRuleRequest has no
    # category field), so without this every one of their recordings would
    # only ever reach the provider-level default, never anything personal to
    # them.
    over_quota_user_ids: set[int] = set()
    user_default_categories: dict[int, dict] = {}
    QUOTA_WARNING_THRESHOLDS = (80, 90, 100)
    for limit_row in vod_db.list_dvr_user_limits(provider_id):
        user_default_categories[limit_row["dispatcharr_user_id"]] = {
            "movie": limit_row.get("default_movie_category_id"),
            "series": limit_row.get("default_series_category_id"),
        }
        if limit_row.get("disk_quota_bytes") is None:
            continue
        user_id = limit_row["dispatcharr_user_id"]
        quota_bytes = limit_row["disk_quota_bytes"]
        usage = vod_db.dvr_user_disk_usage_bytes(provider_id, user_id)
        # quota_policy='delete_oldest' (the OTHER real requirement from the
        # user, 2026-07-28, alongside 'hard_fail' -- see portal_routes.
        # portal_schedule_single/portal_create_recording_rule for that half)
        # -- evicts this person's own oldest-owned recordings, oldest first
        # regardless of movie/episode, via the same reference-counted
        # remove_movie/episode_library_owner the portal's own "remove from
        # my library" button uses, so a recording someone else also owns
        # survives intact for them even while being evicted here. Runs
        # BEFORE the over-quota check below so a person who evicts back
        # under quota this same pass isn't needlessly withheld from new
        # category placements too.
        if usage["total_bytes"] >= quota_bytes and limit_row.get("quota_policy") == "delete_oldest":
            owned = sorted(
                [{"kind": "movie", **m} for m in vod_db.list_owned_movies_oldest_first(provider_id, user_id)] +
                [{"kind": "episode", **e} for e in vod_db.list_owned_episodes_oldest_first(provider_id, user_id)],
                key=lambda x: x["added_at"],
            )
            for item in owned:
                if usage["total_bytes"] < quota_bytes:
                    break
                if item["kind"] == "movie":
                    result = vod_db.remove_movie_library_owner(item["movie_id"], provider_id, user_id)
                else:
                    result = vod_db.remove_episode_library_owner(item["episode_id"], provider_id, user_id)
                logger.info("[dispatcharr_dvr_importer] quota_policy=delete_oldest: %s evicted a %s "
                            "(fully_deleted=%s) to make room", limit_row["dispatcharr_username"],
                            item["kind"], result.get("fully_deleted"))
                usage = vod_db.dvr_user_disk_usage_bytes(provider_id, user_id)
        if usage["total_bytes"] >= quota_bytes:
            over_quota_user_ids.add(user_id)
            logger.info("[dispatcharr_dvr_importer] %s is over their DVR disk quota (%d >= %d bytes, "
                        "%d actual / %d virtual) -- withholding new category placements for them this pass",
                        limit_row["dispatcharr_username"], usage["total_bytes"], quota_bytes,
                        usage["actual_bytes"], usage["virtual_bytes"])
        # Threshold warnings -- independent of quota_policy, fires for both
        # 'hard_fail' and 'delete_oldest' alike (delete_oldest still means
        # "you're accumulating enough to keep hitting this," worth knowing
        # about even though it self-resolves). sync_quota_warnings_sent
        # returns only newly-crossed thresholds this pass -- see its own
        # docstring for the reset behavior.
        pct = int(usage["total_bytes"] * 100 / quota_bytes) if quota_bytes else 0
        met = {t for t in QUOTA_WARNING_THRESHOLDS if pct >= t}
        newly_crossed = vod_db.sync_quota_warnings_sent(provider_id, user_id, met)
        if newly_crossed:
            account = next((a for a in vod_db.list_portal_accounts(provider_id) if a["dispatcharr_user_id"] == user_id), None)
            smtp = config.get_smtp_settings()
            notifications.notify_quota_threshold(
                smtp.get("admin_recipients") or [], account.get("email") if account else None,
                limit_row["dispatcharr_username"], provider["name"], max(newly_crossed),
                usage["total_bytes"], quota_bytes,
            )

    # Phase 2: a recording matched to one or more profiles (see
    # profile_by_stream_id above) routes into the UNION of those profiles'
    # own effective target categories instead of the provider-level default
    # -- e.g. two people who each set up their own profile for the same show
    # both get their own copy-into-category out of the one recording. Each
    # profile's effective category is its own target_*_category_id if set,
    # else its owner's personal default_*_category_id (user_default_categories
    # above), else nothing from that profile -- only when NO matched profile
    # contributes any category at all does this fall back to the provider
    # default. A profile whose owner is over quota (see above) doesn't
    # contribute its category here, but other matched profiles still do.
    # Grouped by category first so multiple recordings/profiles landing in
    # the same category collapse into one bulk_place_*_in_category call
    # rather than one call per recording.
    # A true single (no profile at all -- portal_schedule_single's "Record
    # this episode") never entered this loop's per-profile branch at all
    # before this fix, so a person's own default_movie/series_category_id
    # was silently ignored for every single they ever recorded even when
    # correctly set (real gap found live 2026-07-28). Falls back to whoever
    # scheduled_by_user_by_stream_id says scheduled it (same source
    # set_movie/episode_source_dispatcharr_user_id already uses), respecting
    # the same over-quota withholding as the profile path.
    def _single_owner_category(stream_id: str, kind: str) -> int | None:
        owner = scheduled_by_user_by_stream_id.get(stream_id)
        if owner is None or owner in over_quota_user_ids:
            return None
        return user_default_categories.get(owner, {}).get(kind)

    movie_ids_by_category: dict[int, set[int]] = {}
    for stream_id, movie_id in movie_id_by_stream_id.items():
        profiles = profile_by_stream_id.get(stream_id) or []
        category_ids = set()
        for p in profiles:
            if p.get("dispatcharr_user_id") in over_quota_user_ids:
                continue
            effective = p.get("target_movie_category_id") or \
                user_default_categories.get(p.get("dispatcharr_user_id"), {}).get("movie")
            if effective:
                category_ids.add(effective)
        if not category_ids:
            single_owner_cat = _single_owner_category(stream_id, "movie")
            if single_owner_cat:
                category_ids = {single_owner_cat}
        if not category_ids and provider.get("dvr_movie_category_id"):
            category_ids = {provider["dvr_movie_category_id"]}
        for category_id in category_ids:
            movie_ids_by_category.setdefault(category_id, set()).add(movie_id)
    for category_id, ids in movie_ids_by_category.items():
        vod_db.bulk_place_movies_in_category(list(ids), category_id)

    series_ids_by_category: dict[int, set[int]] = {}
    for stream_id, series_id in series_id_by_stream_id.items():
        profiles = profile_by_stream_id.get(stream_id) or []
        category_ids = set()
        for p in profiles:
            if p.get("dispatcharr_user_id") in over_quota_user_ids:
                continue
            effective = p.get("target_series_category_id") or \
                user_default_categories.get(p.get("dispatcharr_user_id"), {}).get("series")
            if effective:
                category_ids.add(effective)
        if not category_ids:
            single_owner_cat = _single_owner_category(stream_id, "series")
            if single_owner_cat:
                category_ids = {single_owner_cat}
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


async def _probe_content_length(url: str) -> int | None:
    """Pointer-mode backfill's virtual byte accounting -- a plain HEAD isn't
    reliable against real XC/Plex/Emby upstreams (some 404 a HEAD while GET
    works fine), so this opens a real streamed GET, reads Content-Length,
    then closes without ever consuming the body -- same header source
    xc_server._proxy_vod_stream already trusts for these same upstreams."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return None
                length = resp.headers.get("content-length")
                return int(length) if length else None
    except Exception as exc:
        logger.warning("[dispatcharr_dvr_importer] backfill content-length probe failed for %s: %s",
                        xc_server._redact_upstream_url(url), exc)
        return None


async def _download_url_to_file(url: str, dest_path: str) -> None:
    """Same shape as dispatcharr_dvr_client.download_recording_file
    (stream to a .part file, atomic os.replace on success) -- generalized
    to any provider's own resolved stream URL rather than Dispatcharr's
    recording-file endpoint specifically, for download-mode backfill."""
    tmp_path = dest_path + ".part"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
    os.replace(tmp_path, dest_path)


async def _apply_pointer_backfill(match: dict) -> None:
    """No new disk cost -- just makes sure the matched source's
    file_size_bytes is populated (a regular provider's source never gets
    this at import time, only DVR-recorded sources do) so it counts toward
    the target person's quota as virtual usage once placed. local_file_path
    is deliberately left untouched -- this stays a pointer into the source
    provider's own stream."""
    source = match["source"]
    if source.get("file_size_bytes") is not None:
        return
    provider = vod_db.get_provider(source["provider_id"])
    if not provider:
        return
    url = xc_server._build_upstream_url("movie" if match["type"] == "movie" else "series", provider, source)
    size = await _probe_content_length(url)
    if size is None:
        return
    if match["type"] == "movie":
        vod_db.set_movie_source_file_size_bytes(source["id"], size)
    else:
        vod_db.set_episode_source_file_size_bytes(source["id"], size)


async def _apply_download_backfill(match: dict, dvr_provider_id: int) -> None:
    """Downloads the matched source's bytes into a genuinely new local copy
    (same hashed-filename-under-DATA_DIR/dvr_recordings convention as
    completed DVR recordings, see this module's own docstring) and
    registers it as an additional source on the SAME movie/episode, owned
    by the recording rule's own DVR provider_id -- reusing the
    dispatcharr_dvr playback branch (_build_upstream_url,
    _proxy_vod_stream) that already knows how to serve a local_file_path
    untouched, rather than teaching regular-provider sources a second
    "maybe local" code path. Deterministic filename (movie/episode id, not
    the source's provider_stream_id) so re-running backfill for the same
    item is idempotent even if which source provider originally supplied it
    later changes."""
    source = match["source"]
    provider = vod_db.get_provider(source["provider_id"])
    if not provider:
        raise ValueError(f"source provider {source['provider_id']} not found")
    kind = "movie" if match["type"] == "movie" else "series"
    url = xc_server._build_upstream_url(kind, provider, source)
    ext = source.get("container_extension") or "mp4"
    item_id = match.get("movie_id") or match["episode_id"]
    key = f"{match['type']}-{item_id}"
    hashed_name = hashlib.sha256(key.encode()).hexdigest() + "." + ext
    dest_dir = str(config.DATA_DIR / "dvr_recordings" / "_backfill")
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, hashed_name)
    if not os.path.isfile(dest_path):
        await _download_url_to_file(url, dest_path)
    file_size = os.path.getsize(dest_path)
    stream_id = f"backfill-{key}"
    if match["type"] == "movie":
        vod_db.add_movie_source(match["movie_id"], dvr_provider_id, stream_id, ext,
                                 file_size_bytes=file_size, local_file_path=dest_path)
    else:
        vod_db.add_episode_source(match["episode_id"], dvr_provider_id, stream_id, ext,
                                   file_size_bytes=file_size, local_file_path=dest_path)


async def _try_backfill(program: dict, profile: dict) -> bool:
    """The backfill_check callback passed into schedule_channel_recordings
    for any profile with backfill_mode set -- returns True (handled, don't
    record) when this airing's title/episode already exists in the pool
    from a regular provider and the backfill itself (pointer's Content-
    Length probe, or download's real byte transfer) succeeds; False in
    every other case, including a failed backfill attempt, which falls
    through to schedule_channel_recordings' normal create_recording path
    so a transient provider hiccup never silently loses the recording."""
    mode = profile.get("backfill_mode")
    if not mode:
        return False
    match = vod_db.find_pool_backfill_match(profile["title"], program)
    if not match:
        return False
    try:
        if mode == "download":
            await _apply_download_backfill(match, profile["provider_id"])
        elif mode == "pointer":
            await _apply_pointer_backfill(match)
        else:
            return False
        if match["type"] == "movie" and profile.get("target_movie_category_id"):
            vod_db.place_movie_in_category(match["movie_id"], profile["target_movie_category_id"])
        elif match["type"] == "series" and profile.get("target_series_category_id"):
            vod_db.place_series_in_category(match["series_id"], profile["target_series_category_id"])
    except Exception as exc:
        logger.warning("[dispatcharr_dvr_importer] backfill (%s) failed for %r, falling back to a normal "
                        "DVR recording for this one: %s", mode, profile["title"], exc)
        return False
    logger.info("[dispatcharr_dvr_importer] backfilled (%s) %r from the pool instead of recording it -- profile=%r",
                mode, program.get("title"), profile["label"])
    return True


async def rescan_recording_profiles(provider_id: int) -> dict:
    """Re-runs every channel-scoped recording profile's own EPG search and
    schedules any newly-visible episode as a real Recording -- see
    dispatcharr_dvr_client.schedule_channel_recordings. This is VOD
    Manager's own replacement for Dispatcharr's recurring series-rules
    re-evaluation (confirmed broken for channel-scoped matching, see
    schedule_channel_recordings' docstring) -- new episodes only enter
    Dispatcharr's 7-day EPG search horizon as time passes, so something has
    to periodically re-check for them; called from main.py's
    _dispatcharr_dvr_poller loop on the same cadence as the completed-
    recordings import, since both are "did anything change on Dispatcharr's
    side" checks for the same provider.

    Profiles with no channel_id are skipped entirely -- see
    vod_routes.create_recording_profile for why a channel is required for
    every new profile; a pre-existing row without one (from before this
    redesign) has nothing safe to re-scan against and is left alone rather
    than guessed at."""
    provider = vod_db.get_provider(provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")
    if not provider.get("dispatcharr_connection_id"):
        raise ValueError(f"provider {provider_id} has no linked Dispatcharr connection configured")
    connection = vod_db.get_dispatcharr_connection(provider["dispatcharr_connection_id"])
    if not connection:
        raise ValueError(f"provider {provider_id}'s linked Dispatcharr connection no longer exists")

    profiles = [p for p in vod_db.list_recording_profiles(provider_id) if p.get("channel_id")]
    username_by_id = {}
    if any(p.get("dispatcharr_user_id") for p in profiles):
        try:
            users = await dispatcharr_dvr_client.list_users(connection)
            username_by_id = {u["id"]: u["username"] for u in users}
        except Exception as exc:
            logger.warning("[dispatcharr_dvr_importer] rescan_recording_profiles: couldn't resolve usernames: %s", exc)

    scheduled_total = 0
    backfilled_total = 0
    results = []
    for profile in profiles:
        scheduled_by = None
        if profile.get("dispatcharr_user_id"):
            scheduled_by = {
                "dispatcharr_user_id": profile["dispatcharr_user_id"],
                "dispatcharr_username": username_by_id.get(profile["dispatcharr_user_id"]),
                "profile_label": profile["label"],
            }
        backfill_check = (lambda program, profile=profile: _try_backfill(program, profile)) if profile.get("backfill_mode") else None
        try:
            result = await dispatcharr_dvr_client.schedule_channel_recordings(
                connection, profile["channel_id"], profile["title"], profile.get("mode", "all"),
                scheduled_by=scheduled_by, backfill_check=backfill_check,
            )
        except Exception as exc:
            logger.warning("[dispatcharr_dvr_importer] rescan_recording_profiles: profile=%r failed: %s",
                            profile["label"], exc)
            continue
        scheduled_total += result["scheduled"]
        backfilled_total += result.get("backfilled", 0)
        if result["scheduled"] or result.get("backfilled"):
            results.append({"profile": profile["label"], **result})

    if results:
        logger.info("[dispatcharr_dvr_importer] provider=%s rescanned %d profile(s), %d new recording(s) scheduled, "
                     "%d backfilled from the pool instead: %s",
                     provider["name"], len(profiles), scheduled_total, backfilled_total, results)
    return {"profiles_scanned": len(profiles), "scheduled": scheduled_total, "backfilled": backfilled_total, "details": results}


def _same_episode(candidate: dict, season_number: int | None, episode_number: int | None, sub_title: str | None) -> bool:
    """Cross-channel "is this the same episode" check for
    reschedule_failed_recordings below -- deliberately NOT
    dispatcharr_dvr_client.episode_identity_key, which folds tvg_id into
    its key specifically to dedup two listings of the SAME channel (see
    is_already_scheduled) -- a replacement on a different channel
    legitimately has a different tvg_id, so that key would never match
    across channels. search_epg_programs is already title-scoped, so this
    only needs season+episode (preferred) or an exact sub_title match as a
    fallback for titles with no structured season/episode.

    Deliberately more conservative than vod_routes._matches_episode (the
    admin's manual-pick Missing Episodes flow), which falls back to "any
    airing of this title counts" when neither signal is available -- that's
    fine when a human picks from a list, but this path auto-schedules with
    no review, and silently grabbing the wrong episode of a long-running
    show is worse than leaving it unresolved for another poll cycle."""
    props = candidate.get("custom_properties") or {}
    c_season, c_episode = props.get("season"), props.get("episode")
    if season_number is not None and episode_number is not None:
        return c_season == season_number and c_episode == episode_number
    if sub_title:
        return (candidate.get("sub_title") or "").strip().lower() == sub_title.strip().lower()
    return False


async def reschedule_failed_recordings(provider_id: int) -> dict:
    """Dispatcharr never retries a recording that never started, or whose
    ~80s mid-recording outage-retry window ran out (confirmed live,
    dispatch-test v0.28.2 -- see dispatcharr_dvr_client.list_failed_
    recordings' docstring) -- this is VOD Manager's own replacement, same
    reasoning as rescan_recording_profiles above being the replacement for
    Dispatcharr's own broken series-rules re-evaluation. For each genuine
    failure: finds the owning dvr_recording_profiles row (for its
    dispatcharr_user_id, to scope candidates to that person's own channel
    lineup and to tag scheduled_by the same way every other auto-scheduling
    path here does), searches the EPG for the same title, keeps only
    candidates that are the SAME episode (_same_episode) and still in the
    future, and schedules the single earliest one that isn't already
    scheduled -- "the next airing, on any channel" is literally the
    earliest surviving candidate after that filtering, which is exactly
    what catches a delayed West Coast feed or a same-channel rerun later
    the same night.

    A profile-less title (no dvr_recording_profiles row matches, e.g. an
    ad-hoc Recording created outside VOD Manager entirely) has no "how
    would this normally be scheduled" to go on, so it's searched unscoped
    -- same as resolve_missing_episode's own admin-side cross-channel
    fallback when there's no rule/channel to check first.

    Outcomes are persisted via vod_db.upsert_recording_failure:
    'rescheduled' rows are never revisited; 'unresolved' ones are retried
    every call, since a new EPG entry can enter the 7-day search horizon as
    time passes."""
    provider = vod_db.get_provider(provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")
    if not provider.get("dispatcharr_connection_id"):
        raise ValueError(f"provider {provider_id} has no linked Dispatcharr connection configured")
    connection = vod_db.get_dispatcharr_connection(provider["dispatcharr_connection_id"])
    if not connection:
        raise ValueError(f"provider {provider_id}'s linked Dispatcharr connection no longer exists")

    try:
        failed = await dispatcharr_dvr_client.list_failed_recordings(connection)
    except Exception as exc:
        logger.warning("[dispatcharr_dvr_importer] reschedule_failed_recordings: couldn't list failed recordings: %s", exc)
        return {"checked": 0, "rescheduled": 0, "unresolved": 0, "details": []}
    if not failed:
        return {"checked": 0, "rescheduled": 0, "unresolved": 0, "details": []}

    username_by_id = {}
    try:
        users = await dispatcharr_dvr_client.list_users(connection)
        username_by_id = {u["id"]: u["username"] for u in users}
    except Exception as exc:
        logger.warning("[dispatcharr_dvr_importer] reschedule_failed_recordings: couldn't resolve usernames: %s", exc)

    now = datetime.now(timezone.utc)
    rescheduled_total = 0
    unresolved_total = 0
    details = []

    for recording in failed:
        recording_id = recording.get("id")
        if recording_id is None:
            continue
        existing = vod_db.get_recording_failure(provider_id, recording_id)
        if existing and existing["outcome"] == "rescheduled":
            continue  # already handled, never revisit

        program = (recording.get("custom_properties") or {}).get("program") or {}
        title = program.get("title")
        if not title:
            continue
        props = program.get("custom_properties") or {}
        season_number, episode_number = props.get("season"), props.get("episode")
        sub_title = program.get("sub_title")
        original_channel_id = recording.get("channel")
        interrupted_reason = (recording.get("custom_properties") or {}).get("interrupted_reason")

        try:
            rule = vod_db.find_recording_profile_for_title(provider_id, title)
            dispatcharr_user_id = rule.get("dispatcharr_user_id") if rule else None
            visible = None
            if dispatcharr_user_id:
                visible = await dispatcharr_dvr_client.visible_channel_ids(connection, dispatcharr_user_id)

            matches = await dispatcharr_dvr_client.search_epg_programs(connection, title)
            candidates = []
            for m in matches:
                if not _same_episode(m, season_number, episode_number, sub_title):
                    continue
                start = _parse_iso(m.get("start_time"))
                if not start or start <= now:
                    continue
                for ch in m.get("channels") or []:
                    ch_id = ch.get("id")
                    if ch_id is None or (visible is not None and ch_id not in visible):
                        continue
                    candidates.append((start, ch_id, m))
            candidates.sort(key=lambda c: c[0])

            chosen = None
            for start, ch_id, m in candidates:
                if await dispatcharr_dvr_client.is_already_scheduled(connection, ch_id, m):
                    continue
                chosen = (ch_id, m)
                break

            if chosen:
                ch_id, m = chosen
                scheduled_by = None
                if dispatcharr_user_id:
                    scheduled_by = {
                        "dispatcharr_user_id": dispatcharr_user_id,
                        "dispatcharr_username": username_by_id.get(dispatcharr_user_id),
                        "profile_label": rule["label"] if rule else None,
                    }
                await dispatcharr_dvr_client.create_recording(connection, ch_id, m, scheduled_by)
                vod_db.upsert_recording_failure(
                    provider_id, recording_id, title, season_number, episode_number,
                    original_channel_id, interrupted_reason, "rescheduled", ch_id,
                )
                rescheduled_total += 1
                details.append({"title": title, "season": season_number, "episode": episode_number,
                                 "outcome": "rescheduled", "channel": ch_id})
                logger.info("[dispatcharr_dvr_importer] reschedule_failed_recordings: %r S%sE%s rescheduled on "
                            "channel %s (was channel %s, %s)",
                            title, season_number, episode_number, ch_id, original_channel_id, interrupted_reason)
            else:
                vod_db.upsert_recording_failure(
                    provider_id, recording_id, title, season_number, episode_number,
                    original_channel_id, interrupted_reason, "unresolved", None,
                )
                unresolved_total += 1
        except Exception as exc:
            logger.warning("[dispatcharr_dvr_importer] reschedule_failed_recordings: recording=%s (%r) failed: %s",
                            recording_id, title, exc)
            continue

    if rescheduled_total or unresolved_total:
        logger.info("[dispatcharr_dvr_importer] provider=%s: %d failed recording(s) checked, %d rescheduled, %d unresolved",
                     provider["name"], len(failed), rescheduled_total, unresolved_total)
    return {"checked": len(failed), "rescheduled": rescheduled_total, "unresolved": unresolved_total, "details": details}


async def poll_watch_sessions(connection: dict) -> dict:
    """One polling cycle against a single Dispatcharr connection's live
    /proxy/stats/ -- turns Dispatcharr's real-time-only VOD connection state
    (confirmed live, dispatch-test v0.28.2, 2026-07-27: a real per-person
    user_id is present on every active VOD connection, but Dispatcharr
    itself never persists it once the connection ends -- see
    dispatcharr_dvr_client.get_proxy_stats) into VOD Manager's own history
    by upserting/closing watch_sessions rows every time this runs.

    Not DVR-specific -- covers any VOD content served through this
    connection's relay, DVR-recorded or not; lives here anyway rather than
    a new module, matching rescan_recording_profiles' precedent that
    connection/provider-level Dispatcharr polling orchestration belongs in
    the importer, next to vod_db."""
    users = await dispatcharr_dvr_client.list_users(connection)
    username_by_id = {u["id"]: u["username"] for u in users}

    stats = await dispatcharr_dvr_client.get_proxy_stats(connection)
    active_client_ids = []
    for group in (stats.get("vod") or {}).get("vod_connections", []):
        for c in group.get("connections", []):
            client_id = c.get("client_id")
            if not client_id:
                continue
            active_client_ids.append(client_id)
            try:
                user_id = int(c["user_id"]) if c.get("user_id") not in (None, "") else None
            except (TypeError, ValueError):
                user_id = None
            try:
                position_seconds = float(c["position_seconds"]) if c.get("position_seconds") not in (None, "") else None
            except (TypeError, ValueError):
                position_seconds = None
            vod_db.upsert_watch_session(
                connection["id"], client_id, user_id, username_by_id.get(user_id),
                c.get("content_type"), c.get("content_name"), c.get("content_uuid"),
                c.get("client_ip"), int(c.get("bytes_sent") or 0), position_seconds,
            )
    closed = vod_db.close_stale_watch_sessions(connection["id"], active_client_ids)
    return {"active": len(active_client_ids), "closed": closed}

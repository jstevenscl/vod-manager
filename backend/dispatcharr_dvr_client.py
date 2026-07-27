"""
Thin client over Dispatcharr's own DVR REST API — used by
dispatcharr_dvr_importer.py to pull finished recordings into the VOD pool.

Reuses dispatcharr_client.DispatcharrClient as-is (X-API-Key header auth
against a dispatcharr_connections row) — the same auth already proven for
the live-viewer-count feature (xc_server._live_viewer_count) and for
pushing VOD sync profiles (vod_sync.py). Confirmed live (dispatch-test,
v0.27.2, 2026-07-26) that the same X-API-Key auth also covers the
completed-recording file-download endpoint (GET
/api/channels/recordings/{id}/file/, see download_recording_file below) --
no separate JWT or other auth scheme needed, resolving Phase 1b's one open
question.
"""

import logging
import os

import httpx

from dispatcharr_client import DispatcharrClient

logger = logging.getLogger(__name__)


def _is_completed(recording: dict) -> bool:
    return (recording.get("custom_properties") or {}).get("status") == "completed"


async def _list_all_recordings(connection: dict) -> list[dict]:
    client = DispatcharrClient(connection["url"], connection["token"])
    data = await client.get("/api/channels/recordings/")
    return data if isinstance(data, list) else data.get("results", [])


async def list_completed_recordings(connection: dict) -> list[dict]:
    """Every recording Dispatcharr currently reports as finished. No
    confirmed completion webhook/event is exposed externally (see
    vod_manager-f09's research notes) -- poll and filter client-side rather
    than assume the API can do it server-side, since custom_properties is
    an opaque JSON blob Dispatcharr doesn't expose as a queryable column."""
    recordings = await _list_all_recordings(connection)
    completed = [r for r in recordings if _is_completed(r)]
    logger.info("[dispatcharr_dvr_client] connection=%s: %d recording(s), %d completed",
                connection["label"], len(recordings), len(completed))
    return completed


async def list_scheduled_recordings(connection: dict) -> list[dict]:
    """Every recording Dispatcharr has scheduled but not yet started or
    finished -- confirmed live a genuinely-upcoming recording's own status
    is None/absent (not some 'scheduled' string), so 'not completed and not
    currently recording' is the correct upcoming test, matching the same
    custom_properties.status field list_completed_recordings/_is_completed
    already reads. Powers the DVR tab's upcoming-recordings agenda view."""
    recordings = await _list_all_recordings(connection)
    return [
        r for r in recordings
        if (r.get("custom_properties") or {}).get("status") not in ("completed", "recording")
    ]


def recording_program_info(recording: dict) -> dict:
    """EPG-matched program detail, when Dispatcharr had a match at record
    time -- higher-confidence than parsing the output filename, but not
    always present (an unmatched channel/timeslot still records, just
    without this).

    season/episode ARE available -- confirmed live against a real instance
    (dispatch-test, v0.27.2, 2026-07-25): Dispatcharr stamps them as plain
    ints directly on custom_properties (not nested under "program"), e.g.
    {"season": 3, "episode": 13, ...}. Earlier research said otherwise (no
    structured season/episode anywhere, path-parsing required) -- that was
    wrong, or true of an older version; either way, prefer these directly
    over dispatcharr_dvr_importer.py's regex path-parsing whenever present,
    falling back to the path only if they're ever missing (an unmatched
    recording, or an older Dispatcharr version).

    poster_url is also directly available and reflects Dispatcharr's own
    EPG match for this specific episode -- higher-confidence than the
    importer's conservative TMDB exact-title fallback search, and free (no
    extra API call), so callers should prefer it when present."""
    props = recording.get("custom_properties") or {}
    program = props.get("program") or {}
    return {
        "title": program.get("title") or None,
        "sub_title": program.get("sub_title") or None,
        "description": program.get("description") or None,
        "tvg_id": program.get("tvg_id") or None,
        "season": props.get("season"),
        "episode": props.get("episode"),
        "poster_url": props.get("poster_url") or None,
    }


def recording_file_info(recording: dict) -> dict:
    props = recording.get("custom_properties") or {}
    return {
        "file_path": props.get("file_path") or None,
        "file_name": props.get("file_name") or None,
        "file_url": props.get("file_url") or None,
        "bytes_written": props.get("bytes_written"),
        "remux_success": props.get("remux_success"),
    }


async def download_recording_file(connection: dict, recording_id: int, dest_path: str) -> None:
    """Phase 1b: streams a completed recording's bytes to a local path for
    a cross-host deployment (no shared bind-mount available) -- confirmed
    live that GET /api/channels/recordings/{id}/file/ takes the same
    X-API-Key header as everything else and supports Range (not used here;
    a plain sequential GET is simplest and this is a one-time pull, not
    scrub-and-seek playback).

    Streams straight to disk rather than buffering in memory -- recordings
    are commonly several hundred MB to multiple GB, and DispatcharrClient's
    existing get_bytes() would hold the whole thing in RAM at once.
    Downloads to a ".part" sibling and atomically renames into place only
    once fully written, so a concurrent reader (the importer's own
    isfile() check, or xc_server serving playback) can never observe a
    truncated file at the final path -- a real risk unique to Phase 1b that
    Phase 1a's same-host reference never had (nothing there is ever
    partially written from VOD Manager's point of view)."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp_path = dest_path + ".part"
    url = f"{connection['url'].rstrip('/')}/api/channels/recordings/{recording_id}/file/"
    headers = {"X-API-Key": connection["token"]}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
    os.replace(tmp_path, dest_path)


async def search_epg_programs(
    connection: dict, title: str, limit: int = 50, channel_id: int | None = None,
) -> list[dict]:
    """Real upcoming EPG airings for a title -- GET /api/epg/programs/search/,
    confirmed live (dispatch-test v0.27.2 and v0.28.2, 2026-07-26). Bounded to
    the same 7-day horizon Dispatcharr itself schedules against so results
    only ever show things that could actually be recorded. title_whole_words
    =true avoids the same over-broad substring matching a caller wouldn't
    want when they typed a real show name.

    channel_id, when passed, scopes results to just that one real Channel --
    confirmed live this filters correctly via the channel's own actual
    assignment (unlike Dispatcharr's own Series Rules feature, which
    resolves an ambiguous tvg_id string instead -- see create_recording's
    docstring), which is exactly why schedule_channel_recordings below uses
    this instead of Series Rules for the real scheduling work. Without
    channel_id, results span every channel
    carrying the title, each result's own 'channels' array of real Channel
    objects ({id, name, channel_number, channel_group, tvg_id}) -- confirmed
    live this is a genuine array despite the OpenAPI schema documenting it as
    a plain string, so don't trust that doc field's type."""
    from datetime import datetime, timedelta, timezone as _tz
    now = datetime.now(_tz.utc)
    client = DispatcharrClient(connection["url"], connection["token"])
    params = {
        "title": title, "title_whole_words": "true",
        "start_after": now.isoformat(), "start_before": (now + timedelta(days=7)).isoformat(),
        "page_size": limit,
    }
    if channel_id:
        params["channel_id"] = channel_id
    data = await client.get("/api/epg/programs/search/", params=params)
    return data.get("results", []) if isinstance(data, dict) else data


def _episode_identity_key(program: dict) -> str:
    """Same identity heuristic Dispatcharr's own series-rules evaluator uses
    to tell two airings of the same episode apart from two genuinely
    different programs (apps/channels/tasks.py's _episode_key, dispatch-test
    v0.28.2): season+episode when known, else onscreen_episode, else
    sub_title -- all scoped by (tvg_id, title) -- else this specific
    airing's own (start_time, end_time) so two programs are never collapsed
    together just because neither carries season/episode metadata.

    Works identically whether called on a fresh search_epg_programs() result
    or on an existing Recording's custom_properties.program (see
    create_recording below) -- both use the same nested shape
    {tvg_id, title, sub_title, start_time, end_time, custom_properties:
    {season, episode, onscreen_episode}} deliberately, so
    schedule_channel_recordings' dedup check compares like with like."""
    props = program.get("custom_properties") or {}
    season = props.get("season")
    episode = props.get("episode")
    onscreen = props.get("onscreen_episode")
    tvg_id = program.get("tvg_id") or ""
    title = (program.get("title") or "").strip().lower()
    base = f"{tvg_id}|{title}"
    if season is not None and episode is not None:
        return f"{base}|s{season}e{episode}"
    if onscreen:
        return f"{base}|{str(onscreen).strip().lower()}"
    sub_title = program.get("sub_title")
    if sub_title:
        return f"{base}|{sub_title.strip().lower()}"
    return f"{base}|{program.get('start_time')}|{program.get('end_time')}"


async def create_recording(connection: dict, channel_id: int, program: dict) -> dict:
    """Creates a single one-off Recording directly against Dispatcharr's
    plain Recording model (POST /api/channels/recordings/) -- channel id +
    start/end time only, with NO tvg_id-based matching anywhere in the path.
    This is the fix for a real tvg_id-collision bug in Dispatcharr's own
    Series Rules feature: confirmed live (dispatch-test v0.28.2,
    2026-07-26, both via direct API calls and by driving Dispatcharr's own
    native "Customize Rule" UI and capturing its real network requests) that
    scoping a Series Rule to one channel can silently schedule 0 recordings
    even when a real matching program exists on that exact channel right
    now -- Dispatcharr's own series-rules evaluator re-derives the EPG
    source from an ambiguous tvg_id STRING lookup (EPGData.objects.filter(
    tvg_id=...).first(), no order_by, picks whichever of several same-string
    rows comes first, dead or not) instead of the channel's own already-
    resolved epg_data_id. Confirmed live in Django shell: the buggy lookup
    resolved to a dead EPGData row with 0 programs; Channel.epg_data_id
    resolved to the correct one with 4 real upcoming episodes, all correctly
    scoped to just that channel. This function bypasses the ambiguous path
    entirely -- the caller already has the real numeric channel id (from a
    channel_id-scoped search_epg_programs call, itself confirmed correct),
    so there's nothing left to disambiguate. Works regardless of whether the
    underlying EPG source is set up via tvg_id, Gracenote station id
    (Channel.tvc_guide_stationid), or both, since none of that is ever
    touched here.

    custom_properties.program mirrors the shape Dispatcharr's own evaluator
    writes (confirmed live -- RecordingSerializer.validate only applies the
    global pre/post-roll offsets when custom_properties.program is present
    as a dict), nested the same way search_epg_programs' own results are, so
    _episode_identity_key produces the same key whether it's reading a fresh
    search result or an already-created Recording."""
    props = program.get("custom_properties") or {}
    client = DispatcharrClient(connection["url"], connection["token"])
    body = {
        "channel": channel_id,
        "start_time": program["start_time"],
        "end_time": program["end_time"],
        "custom_properties": {
            "program": {
                "tvg_id": program.get("tvg_id"),
                "title": program.get("title"),
                "sub_title": program.get("sub_title"),
                "start_time": program["start_time"],
                "end_time": program["end_time"],
                "custom_properties": {
                    "season": props.get("season"),
                    "episode": props.get("episode"),
                    "onscreen_episode": props.get("onscreen_episode"),
                },
            },
        },
    }
    return await client.post("/api/channels/recordings/", body)


async def delete_recording(connection: dict, recording_id: int) -> None:
    client = DispatcharrClient(connection["url"], connection["token"])
    await client.delete(f"/api/channels/recordings/{recording_id}/")


async def schedule_channel_recordings(
    connection: dict, channel_id: int, title: str, mode: str = "all", limit: int = 50,
) -> dict:
    """Replaces Dispatcharr's own Series Rules feature entirely -- searches
    this one channel's own real EPG (search_epg_programs, channel-scoped,
    confirmed correct) for upcoming airings of title, and directly creates a
    Recording (create_recording, above) for each one not already scheduled
    from a previous call. Safe to call repeatedly -- e.g. from a periodic
    background scan, since Dispatcharr's own recurring series-rules
    re-evaluation is what's being bypassed here, so VOD Manager now owns
    rediscovering newly-visible episodes as the EPG horizon rolls forward.
    Already-scheduled airings are skipped via _episode_identity_key
    (compared against every existing Recording on this same channel, not
    just ones VOD Manager itself created), not re-created.

    mode="new" mirrors Dispatcharr's own series-rules "new episodes only"
    semantics -- only programs whose own EPG data marks custom_properties.
    new truthy are considered, the same field _evaluate_series_rules_locked
    checks (apps/channels/tasks.py, dispatch-test v0.28.2)."""
    matches = await search_epg_programs(connection, title, limit=limit, channel_id=channel_id)
    if mode == "new":
        matches = [m for m in matches if (m.get("custom_properties") or {}).get("new")]

    existing = await _list_all_recordings(connection)
    existing_keys = {
        _episode_identity_key((r.get("custom_properties") or {}).get("program") or {})
        for r in existing
        if r.get("channel") == channel_id
    }

    created, skipped = [], 0
    for program in matches:
        key = _episode_identity_key(program)
        if key in existing_keys:
            skipped += 1
            continue
        try:
            recording = await create_recording(connection, channel_id, program)
        except Exception as exc:
            logger.warning(
                "[dispatcharr_dvr_client] schedule_channel_recordings: failed to create recording for %r at %s: %s",
                title, program.get("start_time"), exc,
            )
            continue
        existing_keys.add(key)
        created.append(recording)

    return {"total_matches": len(matches), "scheduled": len(created), "skipped_existing": skipped, "created": created}


async def list_channel_profiles(connection: dict) -> list[dict]:
    """Real Dispatcharr Channel Profiles (apps/dispatcharr_channels -- a
    person doesn't always have the full channel lineup, confirmed live:
    dispatch-test's real 'Emby' profile has 2395 total channel memberships
    but only 81 actually enabled) -- GET /api/channels/profiles/, confirmed
    live, returns [{"id", "name", "channels": [<channel id>, ...]}, ...].
    Cross-reference a specific User's own .channel_profiles (from
    list_users) against this to know which channel ids that person can
    actually see, so the EPG search picker can prioritize/mark those."""
    client = DispatcharrClient(connection["url"], connection["token"])
    data = await client.get("/api/channels/profiles/")
    return data if isinstance(data, list) else data.get("results", [])


async def list_users(connection: dict) -> list[dict]:
    """Real Dispatcharr login accounts (apps/accounts/models.py's User model,
    confirmed live -- GET /api/accounts/users/, same X-API-Key auth as
    everything else, requires admin-level permission which the existing
    connection token already has since it's already used for account/M3U
    management elsewhere). Each carries a real stream_limit -- confirmed live
    that Dispatcharr only enforces this for authenticated live/VOD viewing
    sessions (apps/proxy/live_proxy/views.py's check_user_stream_limits), NOT
    for DVR recordings (run_recording's own stream pull isn't an authenticated
    user request) -- so VOD Manager reads it here to run its own best-effort
    prediction (see vod_routes.py's create_recording_profile), not because
    Dispatcharr enforces it for us."""
    client = DispatcharrClient(connection["url"], connection["token"])
    data = await client.get("/api/accounts/users/")
    return data if isinstance(data, list) else data.get("results", [])


async def get_proxy_stats(connection: dict) -> dict:
    """Dispatcharr's own real-time connection stats -- GET /proxy/stats/
    (root-mounted, NOT under /api/ -- confirmed live, dispatch-test v0.28.2,
    2026-07-27), same X-API-Key auth as everything else despite Dispatcharr's
    own view requiring IsAdmin. Returns {"live": {...}, "vod":
    {"vod_connections": [...], "total_connections": int}, "catchup": {...},
    "timestamp": float}.

    Each entry in vod.vod_connections[].connections carries a real
    Dispatcharr user_id for who's actually watching -- confirmed live by
    logging into dispatch-test as a dedicated test account, playing a real
    VOD movie sourced from VOD Manager's own relay, and checking this exact
    response while it played. This is Dispatcharr's CURRENT STATE only
    (Redis-TTL backed on Dispatcharr's own side, no persisted history) -- a
    caller has to poll repeatedly and build its own history from the deltas,
    see dispatcharr_dvr_importer.poll_watch_sessions and main.py's
    _watch_session_poller."""
    client = DispatcharrClient(connection["url"], connection["token"])
    return await client.get("/proxy/stats/")

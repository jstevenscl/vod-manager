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


def _series_rule_body(
    title: str, tvg_id: str | None, title_mode: str, description: str | None,
    description_mode: str, mode: str, channel_id: int | None,
) -> dict:
    """Field names match Dispatcharr's own SeriesRuleRequest body exactly
    (confirmed via its OpenAPI schema, GET /api/schema/, dispatch-test
    v0.27.2, 2026-07-26) -- no translation layer needed. Blank/None tvg_id
    genuinely means "omit the field" (matches across all EPG channels,
    Dispatcharr's own default), not an empty string sent over the wire."""
    body = {"title": title, "title_mode": title_mode, "description_mode": description_mode, "mode": mode}
    if tvg_id:
        body["tvg_id"] = tvg_id
    if description:
        body["description"] = description
    if channel_id:
        body["channel_id"] = channel_id
    return body


async def create_series_rule(
    connection: dict, title: str, tvg_id: str | None = None, title_mode: str = "exact",
    description: str | None = None, description_mode: str = "contains",
    mode: str = "all", channel_id: int | None = None,
) -> dict:
    """Creates (or updates, per Dispatcharr's own description: "Add a new
    series recording rule or update an existing one") a recurring Series
    Rule. No id is returned or needed: see vod_db.match_recording_profiles'
    docstring for why VOD Manager's own dvr_recording_profiles keys on
    (title, tvg_id) instead, the same pair Dispatcharr's own
    delete_series_rule below uses.

    channel_id, when known, should always be passed -- pinning by
    Dispatcharr's own numeric Channel id is an unambiguous direct lookup.
    tvg_id alone is NOT safe: confirmed live (dispatch-test, 2026-07-26,
    via docker exec into its Django shell) that a single tvg_id STRING can
    be shared by multiple EPGData rows (one per EPG source), only one of
    which may actually have a Channel wired to it -- Dispatcharr's own
    tvg_id-scoped evaluation (EPGData.objects.filter(tvg_id=...).first())
    has no way to disambiguate and can silently pick a dead one. A blank
    rule (matches any channel) sidesteps this because it resolves each
    match's channel per-program instead, which is exactly why a title-only
    rule "just worked" immediately after a channel-scoped one for the same
    content failed with no visible error. tvg_id is still passed/stored
    for VOD Manager's own later matching in match_recording_profiles.

    Saving the rule does NOT by itself schedule anything -- confirmed by
    reading Dispatcharr's own source (apps/channels/api_views.py,
    SeriesRulesAPIView.post, dispatch-test v0.27.2, 2026-07-26): it only
    writes the rule into CoreSettings, with an explicit code comment that
    its own frontend calls the separate evaluate endpoint right after
    saving ("do NOT fire evaluate_series_rules.delay() here"). Verified
    live: without the second call below, a saved rule produced zero
    scheduled recordings; calling POST .../series-rules/evaluate/
    immediately created real ones. So this function does both steps,
    mirroring Dispatcharr's own frontend -- a caller here should never have
    to know evaluation is a separate step. Returns the evaluate response
    ({"success", "scheduled", "details": [...]}) so a caller can tell the
    difference between "saved and 0 currently schedulable" (e.g. a
    dead/ambiguous channel, or a "new episodes only" rule with nothing new
    airing right now) and a genuine failure, instead of reporting bare
    success either way."""
    client = DispatcharrClient(connection["url"], connection["token"])
    body = _series_rule_body(title, tvg_id, title_mode, description, description_mode, mode, channel_id)
    await client.post("/api/channels/series-rules/", body)
    # Scoped to this rule's own channel (or, when blank, to the other
    # blank-tvg_id "any channel" rules) rather than every rule on the
    # instance -- evaluation is idempotent either way (Dispatcharr's own
    # dedup is keyed by stable program attributes, confirmed via its test
    # suite), this just avoids doing unrelated work on every single
    # profile creation.
    return await client.post("/api/channels/series-rules/evaluate/", {"tvg_id": tvg_id} if tvg_id else {})


async def delete_series_rule(connection: dict, title: str, tvg_id: str | None = None) -> None:
    """Confirmed via the OpenAPI schema: series rules are identified purely
    by (title, tvg_id) -- there's no synthetic id for this resource at all."""
    params = {"title": title}
    if tvg_id:
        params["tvg_id"] = tvg_id
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{connection['url'].rstrip('/')}/api/channels/series-rules/",
            headers={"X-API-Key": connection["token"]}, params=params,
        )
        resp.raise_for_status()


async def preview_series_rule(
    connection: dict, title: str, tvg_id: str | None = None, title_mode: str = "exact",
    description: str | None = None, description_mode: str = "contains", limit: int = 25,
) -> dict:
    """Upcoming programs this rule would match, without persisting it --
    powers the same live preview Dispatcharr's own "Customize rule..." UI
    shows (confirmed live). Used by VOD Manager's own recording-profile form
    to show the same preview before saving. Returns the full response dict
    as-is: {"matches": [...], "total": int, "limit": int, "epg_found": bool,
    "warn": bool} (confirmed live -- the list itself is under "matches", not
    "results" like most of Dispatcharr's other list endpoints); epg_found in
    particular is a real, useful signal a caller can't derive from an empty
    "matches" list alone -- False means Dispatcharr has no EPG data at all
    for this title, not just "nothing airing soon"."""
    client = DispatcharrClient(connection["url"], connection["token"])
    body = {"title": title, "title_mode": title_mode, "description_mode": description_mode, "limit": limit}
    if tvg_id:
        body["tvg_id"] = tvg_id
    if description:
        body["description"] = description
    return await client.post("/api/channels/series-rules/preview/", body)


async def search_epg_programs(connection: dict, title: str, limit: int = 50) -> list[dict]:
    """Real upcoming EPG airings for a title, across every channel that
    carries it -- GET /api/epg/programs/search/, confirmed live (dispatch-
    test v0.27.2, 2026-07-26). Bounded to the same 7-day horizon Dispatcharr
    itself schedules against so results only ever show things that could
    actually be recorded. title_whole_words=true avoids the same over-broad
    substring matching a caller wouldn't want when they typed a real show
    name. Each result includes tvg_id (this specific airing's own EPG
    channel string) and a 'channels' array of real Channel objects
    ({id, name, channel_number, channel_group, tvg_id}) -- confirmed live
    this is a genuine array despite the OpenAPI schema documenting it as a
    plain string, so don't trust that doc field's type. The channel id in
    that array is what create_series_rule's channel_id param needs (see its
    docstring for why tvg_id alone isn't a safe pin)."""
    from datetime import datetime, timedelta, timezone as _tz
    now = datetime.now(_tz.utc)
    client = DispatcharrClient(connection["url"], connection["token"])
    data = await client.get("/api/epg/programs/search/", params={
        "title": title, "title_whole_words": "true",
        "start_after": now.isoformat(), "start_before": (now + timedelta(days=7)).isoformat(),
        "page_size": limit,
    })
    return data.get("results", []) if isinstance(data, dict) else data


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

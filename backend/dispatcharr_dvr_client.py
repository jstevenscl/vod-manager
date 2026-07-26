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


async def list_completed_recordings(connection: dict) -> list[dict]:
    """Every recording Dispatcharr currently reports as finished. No
    confirmed completion webhook/event is exposed externally (see
    vod_manager-f09's research notes) -- poll and filter client-side rather
    than assume the API can do it server-side, since custom_properties is
    an opaque JSON blob Dispatcharr doesn't expose as a queryable column."""
    client = DispatcharrClient(connection["url"], connection["token"])
    data = await client.get("/api/channels/recordings/")
    recordings = data if isinstance(data, list) else data.get("results", [])
    completed = [r for r in recordings if _is_completed(r)]
    logger.info("[dispatcharr_dvr_client] connection=%s: %d recording(s), %d completed",
                connection["label"], len(recordings), len(completed))
    return completed


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
) -> None:
    """Creates (or updates, per Dispatcharr's own description: "Add a new
    series recording rule or update an existing one") a recurring Series
    Rule -- Dispatcharr evaluates it immediately against the current EPG to
    find and schedule matching episodes. No id is returned or needed: see
    vod_db.match_recording_profiles' docstring for why VOD Manager's own
    dvr_recording_profiles keys on (title, tvg_id) instead, the same pair
    Dispatcharr's own delete_series_rule below uses."""
    client = DispatcharrClient(connection["url"], connection["token"])
    body = _series_rule_body(title, tvg_id, title_mode, description, description_mode, mode, channel_id)
    await client.post("/api/channels/series-rules/", body)


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

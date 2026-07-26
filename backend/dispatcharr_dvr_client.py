"""
Thin client over Dispatcharr's own DVR REST API — used by
dispatcharr_dvr_importer.py to pull finished recordings into the VOD pool.

Reuses dispatcharr_client.DispatcharrClient as-is (X-API-Key header auth
against a dispatcharr_connections row) — the same auth already proven for
the live-viewer-count feature (xc_server._live_viewer_count) and for
pushing VOD sync profiles (vod_sync.py). Listing/reading recordings only
needs this; downloading a finished recording's file bytes may need a
different auth scheme Dispatcharr hasn't been asked to prove yet (see
dispatcharr_dvr_importer.py's Phase 1b notes) -- deliberately not attempted
here.
"""

import logging

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

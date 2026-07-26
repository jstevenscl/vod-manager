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

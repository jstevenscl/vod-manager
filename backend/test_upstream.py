"""
Local stand-in for a real XC provider's movie stream endpoint — used only to
validate xc_server's real proxy logic (Range/206 passthrough, true streaming
without full buffering) without needing real provider credentials.

Not part of the XC catalog protocol; just serves a fixed, deterministic fake
"video" payload with proper HTTP Range support.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response

logger = logging.getLogger("vod_manager.test_upstream")

router = APIRouter(tags=["test-upstream"])

_FAKE_VIDEO = bytes((i % 256) for i in range(5 * 1024 * 1024))  # 5MB deterministic payload


async def _upstream_stream(stream_id_ext: str, request: Request):
    total = len(_FAKE_VIDEO)
    range_header = request.headers.get("range")

    if range_header:
        try:
            unit, rng = range_header.split("=")
            start_s, end_s = rng.split("-")
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
        except ValueError:
            return Response(status_code=400, content="bad range")
        end = min(end, total - 1)
        chunk = _FAKE_VIDEO[start:end + 1]
        logger.info("[test_upstream] %s range=%s-%s (%d bytes)", stream_id_ext, start, end, len(chunk))
        return Response(
            content=chunk,
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(chunk)),
            },
        )

    logger.info("[test_upstream] %s full request (%d bytes)", stream_id_ext, total)
    return Response(
        content=_FAKE_VIDEO,
        status_code=200,
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes", "Content-Length": str(total)},
    )


@router.get("/test-upstream/movie/{username}/{password}/{stream_id_ext}")
async def upstream_movie(username: str, password: str, stream_id_ext: str, request: Request):
    return await _upstream_stream(stream_id_ext, request)


@router.get("/test-upstream/series/{username}/{password}/{stream_id_ext}")
async def upstream_series(username: str, password: str, stream_id_ext: str, request: Request):
    return await _upstream_stream(stream_id_ext, request)

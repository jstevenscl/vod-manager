"""
MDBList public list sync -- same purpose as tmdb_sync.py's TMDB List sync
(organizes what the catalog already has according to an external public
list, never pulls in new content), just a different list source. See
vod_list_sync.py for the shared, provider-agnostic matching/placement logic
both plug into.

MDBList's real response shape (verified against their published OpenAPI
spec, https://github.com/linaspurinis/api.mdblist.com, not just docs prose)
for GET /lists/{listid}/items: a *split* object, not a flat array --
{"movies": [...], "shows": [...]}, each item carrying a nested
`ids: {tmdb, imdb, tvdb, mdblist}` object.
"""

import logging
import re

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.mdblist.com"
_API_KEY_RE = re.compile(r"(apikey=)[^&\s'\"]+")


def _redact(exc: Exception) -> str:
    """See tmdb_sync._redact's identical reasoning -- str(exc) on an httpx
    error embeds the full request URL, api key included."""
    return _API_KEY_RE.sub(r"\1***", str(exc))


async def fetch_list_items(api_key: str, list_id: str) -> list[dict]:
    """Returns a flat list of {"media_type": "movie"|"tv", "tmdb_id": int|None,
    "title": str|None, "year": int|None} -- already normalized to the same
    shape tmdb_sync.fetch_list_items' own items use (after vod_list_sync
    normalizes those too), so the shared sync loop doesn't need to know
    which provider a source came from. Raises on a bad key/id/network
    failure rather than returning an empty list, so a broken source is an
    obvious sync error, not a silent zero-match no-op."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            r = await client.get(
                f"{_API_BASE}/lists/{list_id}/items",
                params={"apikey": api_key, "limit": 500},
            )
            r.raise_for_status()
        except Exception as exc:
            raise ValueError(f"MDBList request failed: {_redact(exc)}") from exc
        data = r.json()

    out: list[dict] = []
    for media_type, bucket_key in (("movie", "movies"), ("tv", "shows")):
        for item in data.get(bucket_key, []) or []:
            ids = item.get("ids") or {}
            tmdb_id = ids.get("tmdb")
            out.append({
                "media_type": media_type,
                "tmdb_id": int(tmdb_id) if isinstance(tmdb_id, (int, float)) else None,
                "title": item.get("title"),
                "year": item.get("release_year"),
            })
    return out

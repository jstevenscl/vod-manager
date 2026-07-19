"""
Thin client over the Plex Media Server API — used by plex_importer.py to pull
a user's own library into the VOD pool, and by xc_server.py to build a
direct-play stream URL at playback time.

Auth model differs from XC providers (single token vs username/password), so
the Plex "token" is stored in the providers.password column and providers.
username is left blank — same shape reuse as everywhere else in this app,
just a different meaning for that one column.
"""

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 20.0

# A stable identifier so Plex treats every VOD-Manager-relayed session as
# coming from the same logical "client" — we don't track individual end
# viewers, so there's no finer-grained identity to give it.
CLIENT_IDENTIFIER = "vod-manager-relay-6f1e9c3a"


class PlexClient:
    """Use as `async with PlexClient(provider) as client:` for anything making
    more than one call (import flow) — reuses one pooled connection instead of
    a fresh TLS handshake per request, which matters once a library import is
    firing several concurrent calls at the same Cloudflare-fronted host.
    Falls back to a one-off connection if used without the context manager
    (fine for single-call callers like xc_server's capacity checks, if any)."""

    def __init__(self, provider: dict):
        self.provider = provider
        self.base_url = provider["base_url"].rstrip("/")
        self.token = provider["password"]
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "PlexClient":
        self._client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, follow_redirects=True)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict | None = None) -> dict:
        query = {"X-Plex-Token": self.token}
        if params:
            query.update(params)

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, follow_redirects=True)
        t0 = time.monotonic()
        try:
            # Defense in depth on top of httpx's own timeout= — a hang here
            # once blocked the whole import indefinitely; belt-and-suspenders
            # against whatever edge case caused that.
            r = await asyncio.wait_for(
                client.get(f"{self.base_url}{path}", params=query, headers={"Accept": "application/json"}),
                timeout=_REQUEST_TIMEOUT + 5.0,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            logger.warning("[plex_client] GET %s failed after %.1fs", path, time.monotonic() - t0)
            raise
        finally:
            if owns_client:
                await client.aclose()

    async def test_connection(self) -> dict:
        data = await self._get("/identity")
        return data.get("MediaContainer", {})

    async def list_libraries(self) -> list[dict]:
        """Returns library sections, e.g. [{"key": "1", "title": "Movies", "type": "movie"}, ...]."""
        data = await self._get("/library/sections")
        return (data.get("MediaContainer", {}) or {}).get("Directory", []) or []

    async def list_movies(self, section_key: str) -> list[dict]:
        data = await self._get(f"/library/sections/{section_key}/all", params={"type": 1})
        return (data.get("MediaContainer", {}) or {}).get("Metadata", []) or []

    async def list_shows(self, section_key: str) -> list[dict]:
        data = await self._get(f"/library/sections/{section_key}/all", params={"type": 2})
        return (data.get("MediaContainer", {}) or {}).get("Metadata", []) or []

    async def list_episodes(self, show_rating_key: str) -> list[dict]:
        """allLeaves returns every episode across every season for a show in
        one call — Plex's answer to XC's separate lazy get_series_info fetch."""
        data = await self._get(f"/library/metadata/{show_rating_key}/allLeaves")
        return (data.get("MediaContainer", {}) or {}).get("Metadata", []) or []

    async def refresh_item(self, rating_key: str) -> dict:
        """Re-fetch a single item's current detail — used to keep enrichment
        fresh without re-walking the whole library."""
        data = await self._get(f"/library/metadata/{rating_key}")
        items = (data.get("MediaContainer", {}) or {}).get("Metadata", []) or []
        return items[0] if items else {}

    async def report_timeline(self, rating_key: str, state: str, time_ms: int, duration_ms: int) -> None:
        """Tells Plex's session manager this client is playing something, so
        it shows up in Now Playing / Activity. The raw file-part endpoint
        used for actual playback (see xc_server.py) never registers a
        session on its own — real Plex apps always pair direct-play with
        these heartbeat calls. Best-effort: a failed heartbeat shouldn't
        interrupt the actual video relay, so this swallows its own errors."""
        try:
            await self._get("/:/timeline", params={
                "ratingKey": rating_key,
                "key": f"/library/metadata/{rating_key}",
                "state": state,
                "time": max(int(time_ms), 0),
                "duration": max(int(duration_ms), 0),
                "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
                "X-Plex-Device-Name": "VOD Manager",
                "X-Plex-Product": "VOD Manager",
            })
        except Exception as exc:
            logger.warning("[plex_client] timeline report failed: %s", exc)


def extract_part(item: dict) -> tuple[str | None, str]:
    """Pulls the playable file's Part key (e.g. "/library/parts/12345/file.mkv")
    and container extension off a Plex Metadata item. Used both at import time
    (to store as provider_stream_id) and, indirectly, at playback time (the
    stored key is replayed straight back at the server)."""
    media = item.get("Media") or []
    if not media:
        return None, "mp4"
    parts = media[0].get("Part") or []
    if not parts:
        return None, "mp4"
    part = parts[0]
    return part.get("key"), (media[0].get("container") or "mp4")


def extract_common_fields(item: dict) -> dict:
    """Metadata fields shared by movies and shows — Plex hands these back
    fully populated in the library listing itself, no separate detail call
    needed the way XC's lazy get_vod_info/get_series_info requires."""
    genres = [g["tag"] for g in (item.get("Genre") or []) if g.get("tag")]
    directors = [d["tag"] for d in (item.get("Director") or []) if d.get("tag")]
    cast = [r["tag"] for r in (item.get("Role") or []) if r.get("tag")]
    return {
        "genre": ", ".join(genres) or None,
        "description": item.get("summary") or None,
        "director": ", ".join(directors) or None,
        "cast_list": ", ".join(cast) or None,
        "poster_url": item.get("thumb") or None,
    }

"""
Thin client over the Emby / Jellyfin API — used by emby_vod_importer.py to
pull a user's own Emby or Jellyfin library into the VOD pool, and by
xc_server.py to build a direct-play stream URL at playback time.

Distinct from the repo's existing emby_client.py, which talks to a *different*
Emby instance for the unrelated Live TV / Gracenote channel-matching feature
(main.py's "Emby Sync" tab) — do not conflate the two. This one is scoped to
rows in the VOD providers table (provider_type='emby'|'jellyfin'), same shape
as plex_client.py: the API key lives in providers.password, providers.
username is unused.

Emby and Jellyfin share the same core API surface (Jellyfin forked from
Emby), including the /emby/* path aliases Jellyfin kept for client
compatibility — so one client covers both; nothing here branches on
provider_type.
"""

import asyncio
import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 20.0
# list_movies/list_series each do ONE unpaginated /emby/Items call for the
# whole library (Recursive=true plus a heavy Fields= list) -- fine for a
# small library, but a large one (confirmed live: a user's movie library
# that never once completed within 20s, every ~5min refresh, for days) needs
# real headroom rather than retrying forever against the same wall.
_CATALOG_REQUEST_TIMEOUT = 120.0
# Page size for list_movies/list_series -- keeps each individual request
# fast (and each one still gets the full _CATALOG_REQUEST_TIMEOUT) no matter
# how large the library grows, instead of relying on one ever-larger call.
_CATALOG_PAGE_SIZE = 500

_API_KEY_RE = re.compile(r"(api_key=)[^&\s'\"]+", re.IGNORECASE)


def _redact(exc: Exception) -> str:
    """str(exc) on an httpx.HTTPStatusError embeds the full request URL,
    api_key included -- this must wrap every logged Emby/Jellyfin exception
    or a real API key ends up in plaintext in container logs."""
    return _API_KEY_RE.sub(r"\1***", str(exc))

# Identifies VOD-Manager-relayed sessions to Emby's Now Playing / dashboard —
# the /Videos/{id}/stream direct-play endpoint never registers a session on
# its own, so these headers plus the Sessions/Playing calls below are what
# make an active relay show up there at all (same role as Plex's timeline
# heartbeat + X-Plex-Client-Identifier).
_DEVICE_ID = "vod-manager-relay-4d8f2b17"
_SESSION_HEADERS = {
    "X-Emby-Client": "VOD & DVR Manager",
    "X-Emby-Device-Name": "VOD & DVR Manager",
    "X-Emby-Device-Id": _DEVICE_ID,
    "X-Emby-Client-Version": "1.0.0",
}


class EmbyVodClient:
    """Use as `async with EmbyVodClient(provider) as client:` for anything
    making more than one call (import flow) — reuses one pooled connection
    instead of a fresh TLS handshake per request. Falls back to a one-off
    connection if used without the context manager."""

    def __init__(self, provider: dict):
        self.provider = provider
        self.base_url = provider["base_url"].rstrip("/")
        self.api_key = provider["password"]
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "EmbyVodClient":
        self._client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict | None = None, timeout: float = _REQUEST_TIMEOUT) -> dict:
        query = {"api_key": self.api_key}
        if params:
            query.update(params)

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=timeout)
        t0 = time.monotonic()
        try:
            r = await asyncio.wait_for(
                client.get(f"{self.base_url}{path}", params=query, timeout=timeout),
                timeout=timeout + 5.0,
            )
            r.raise_for_status()
            return r.json() if r.content else {}
        except Exception:
            logger.warning("[emby_vod_client] GET %s failed after %.1fs", path, time.monotonic() - t0)
            raise
        finally:
            if owns_client:
                await client.aclose()

    async def _post_session(self, path: str, body: dict) -> None:
        """Best-effort: a failed session report shouldn't interrupt the
        actual video relay in xc_server.py, so this swallows its own
        errors (same contract as plex_client.report_timeline)."""
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
        try:
            await client.post(
                f"{self.base_url}{path}", params={"api_key": self.api_key},
                json=body, headers=_SESSION_HEADERS,
            )
        except Exception as exc:
            logger.warning("[emby_vod_client] POST %s failed: %s", path, _redact(exc))
        finally:
            if owns_client:
                await client.aclose()

    async def report_playing(self, item_id: str, media_source_id: str, play_session_id: str, position_ticks: int = 0) -> None:
        await self._post_session("/emby/Sessions/Playing", {
            "ItemId": item_id, "MediaSourceId": media_source_id, "PlaySessionId": play_session_id,
            "CanSeek": True, "PlayMethod": "DirectStream", "PositionTicks": position_ticks,
        })

    async def report_progress(self, item_id: str, media_source_id: str, play_session_id: str, position_ticks: int) -> None:
        await self._post_session("/emby/Sessions/Playing/Progress", {
            "ItemId": item_id, "MediaSourceId": media_source_id, "PlaySessionId": play_session_id,
            "CanSeek": True, "PlayMethod": "DirectStream", "PositionTicks": position_ticks,
        })

    async def report_stopped(self, item_id: str, media_source_id: str, play_session_id: str, position_ticks: int) -> None:
        await self._post_session("/emby/Sessions/Playing/Stopped", {
            "ItemId": item_id, "MediaSourceId": media_source_id, "PlaySessionId": play_session_id,
            "PositionTicks": position_ticks,
        })

    async def test_connection(self) -> dict:
        return await self._get("/emby/System/Info")

    async def list_libraries(self) -> list[dict]:
        """Physical library folders, e.g. [{"ItemId": "3", "Name": "Movies",
        "CollectionType": "movies"}, ...]."""
        data = await self._get("/emby/Library/VirtualFolders")
        return data or []

    async def _list_items_paged(self, params: dict) -> list[dict]:
        """/emby/Items in pages of _CATALOG_PAGE_SIZE rather than one
        Recursive=true call for the whole library -- belt-and-suspenders on
        top of _CATALOG_REQUEST_TIMEOUT: a single giant library can still
        outgrow even 120s, but each individual page request stays fast
        regardless of total library size."""
        items: list[dict] = []
        start = 0
        while True:
            data = await self._get("/emby/Items", params={
                **params, "StartIndex": start, "Limit": _CATALOG_PAGE_SIZE,
            }, timeout=_CATALOG_REQUEST_TIMEOUT)
            page = (data or {}).get("Items", []) or []
            items.extend(page)
            total = (data or {}).get("TotalRecordCount")
            start += len(page)
            if len(page) < _CATALOG_PAGE_SIZE or not page or (total is not None and start >= total):
                break
        return items

    async def list_movies(self, library_id: str) -> list[dict]:
        return await self._list_items_paged({
            "ParentId": library_id,
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            # CommunityRating added 2026-07-31 -- real bug found live: it was
            # never requested at all, so every Emby-sourced movie showed 0.0
            # regardless of enrichment, no matter what Emby itself had.
            # PremiereDate added same day, same bug (release date was always
            # blank for Emby-sourced content, found doing a completeness
            # pass after the rating fix).
            #
            # Path replaces MediaSources 2026-08-29 -- real bug found live:
            # MediaSources makes Emby/Jellyfin probe the actual video file
            # per item (confirmed ~0.2-0.4s/item from this same server's own
            # per-episode MediaSources fetch timings), so a movies library
            # never once finished even a single _CATALOG_PAGE_SIZE page
            # within _CATALOG_REQUEST_TIMEOUT -- unlike list_series, which
            # never requested MediaSources at all. Path is plain indexed
            # metadata (no probing), and extract_stream_id() only ever used
            # MediaSources for its Container field, which the file extension
            # in Path gives for free.
            "Fields": "Overview,Genres,ProductionYear,People,Path,ProviderIds,CommunityRating,PremiereDate",
        })

    async def list_series(self, library_id: str) -> list[dict]:
        return await self._list_items_paged({
            "ParentId": library_id,
            "IncludeItemTypes": "Series",
            "Recursive": "true",
            "Fields": "Overview,Genres,ProductionYear,People,ProviderIds,CommunityRating,PremiereDate",
        })

    async def list_episodes(self, series_id: str) -> list[dict]:
        """All episodes for a series in one call — Emby's answer to XC's
        separate lazy get_series_info fetch."""
        data = await self._get(f"/emby/Shows/{series_id}/Episodes", params={
            "Fields": "Overview,MediaSources",
        })
        return (data or {}).get("Items", []) or []


def extract_stream_id(item: dict) -> tuple[str | None, str]:
    """Pulls the item's Id and container extension off an Items/Episodes
    response entry. The Id is what gets replayed back to
    /Videos/{Id}/stream as both the path segment and MediaSourceId at
    playback time (see xc_server.py) — correct for the common case of one
    media file per item, which covers a typical home library.

    Container comes from MediaSources when present (episodes) or, failing
    that, from the file extension in Path (movies -- see list_movies for why
    MediaSources isn't requested there)."""
    item_id = item.get("Id")
    if not item_id:
        return None, "mp4"
    sources = item.get("MediaSources") or []
    container = sources[0].get("Container") if sources else None
    if not container:
        path = item.get("Path") or ""
        filename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if "." in filename:
            container = filename.rsplit(".", 1)[-1]
    return item_id, (container or "mp4")


def extract_common_fields(item: dict) -> dict:
    """Metadata fields shared by movies and series — Emby hands these back
    fully populated in the library listing itself when Fields= is set, no
    separate detail call needed."""
    genres = item.get("Genres") or []
    people = item.get("People") or []
    directors = [p["Name"] for p in people if p.get("Type") == "Director" and p.get("Name")]
    cast = [p["Name"] for p in people if p.get("Type") == "Actor" and p.get("Name")]
    tmdb_raw = (item.get("ProviderIds") or {}).get("Tmdb")
    # See list_movies/list_series -- CommunityRating has to be explicitly
    # requested via Fields= or Emby omits it entirely from the response.
    community_rating = item.get("CommunityRating")
    # PremiereDate is a full ISO datetime ("2020-05-15T00:00:00.0000000Z") --
    # trimmed to just the date portion to match XC's own releasedate shape
    # (plain "YYYY-MM-DD") and Plex's originallyAvailableAt, which is
    # already date-only.
    premiere_date = item.get("PremiereDate")
    return {
        "genre": ", ".join(genres) or None,
        "description": item.get("Overview") or None,
        "director": ", ".join(directors) or None,
        "cast_list": ", ".join(cast) or None,
        "tmdb_id": str(tmdb_raw) if tmdb_raw and str(tmdb_raw).isdigit() else None,
        "rating": str(community_rating) if community_rating is not None else None,
        "release_date": premiere_date.split("T")[0] if premiere_date else None,
    }


def build_poster_url(provider: dict, item_id: str) -> str:
    base_url = provider["base_url"].rstrip("/")
    return f"{base_url}/emby/Items/{item_id}/Images/Primary?api_key={provider['password']}"

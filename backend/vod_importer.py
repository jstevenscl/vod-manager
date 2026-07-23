"""
Imports a real provider's VOD catalog into our own pool.

Two-phase, matching how Dispatcharr itself (and every XC client) actually
handles this: a cheap bulk list import (name/year/category/stream_id — one
call for the whole catalog) now, and expensive per-item detail enrichment
(genre, cast, tmdb_id, poster, description) fetched lazily on demand and
cached (see vod_db.ENRICHMENT_TTL_SECONDS). bulk_enrich_all() below covers
the whole pool at once, still one item at a time under the hood, just with
bounded concurrency instead of a human clicking one movie at a time.
"""

import asyncio
import logging
import re
import time

import httpx

import vod_db


def _as_dict(value) -> dict:
    """get_vod_info/get_series_info are documented as returning an object,
    but at least one real provider returns a bare list (e.g. `[]`) instead
    of `{}` for "no data" -- either at the top level or nested under
    "info" -- which crashed every .get() downstream with 'list' object has
    no attribute 'get', silently failing that item's whole enrichment (and,
    for series, its episodes -- see enrich_series). Treat anything that
    isn't actually a dict as "no data" instead of raising."""
    return value if isinstance(value, dict) else {}

logger = logging.getLogger(__name__)

_YEAR_SUFFIX_RE = re.compile(
    # (?<!\d) keeps this from firing inside a genuine in-title year range like
    # "... (1987-1997)" or "Wartorn: 1861-2010" -- without it, the dash/paren
    # right before the second year in the range looks identical to a real
    # trailing year suffix and the title gets mangled.
    r"^(.*?)\s*(?<!\d)[-(]\s*(19\d{2}|20\d{2})\)?\s*(?:\[[^\]]*\]|[A-Z][A-Z\- ]{2,})?\s*$"
)

# Some real XC providers silently drop the connection -- no HTTP response
# at all -- for requests without a browser-like User-Agent,
# httpx's default ("python-httpx/x.y.z") included. A generic desktop-browser
# UA is enough to get a normal response.
_UPSTREAM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}


def _coerce_year(value) -> int | None:
    """Some XC providers send the series "year" field as a string (or an
    empty string, or junk like "N/A") rather than a number -- SQLite's
    INTEGER column affinity happens to silently coerce a clean numeric
    string on insert, which is exactly why this went unnoticed here, but
    anything that doesn't look like a plain year would still get stored
    as-is and quietly break every exact (name, year) match downstream
    (series import's own dedup lookup, needs_year_review, the duplicate
    finder's group scan)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_name_year(raw_name: str) -> tuple[str, int | None]:
    """Real XC providers commonly bake the year into the title string itself,
    not always as a clean trailing "(YYYY)" -- also seen: "Title - YYYY",
    "Title (YYYY) [MULTI-SUB]", "Title (YYYY) HINDI", and even an unclosed
    "Title (YYYY". Some catalogs duplicate it (e.g. "1 1 (2018) (2018)") --
    strip every trailing year layer, not just one, or the leftover copy in
    the name doubles up with the year we display alongside it."""
    name = raw_name.strip()
    year = None
    while True:
        m = _YEAR_SUFFIX_RE.match(name)
        if not m:
            break
        new_name = m.group(1).strip()
        if new_name == name:
            break
        name, year = new_name, int(m.group(2))
    return name, year


class XCProviderClient:
    def __init__(self, provider: dict):
        self.provider = provider
        self.base_url = provider["base_url"].rstrip("/")
        self.username = provider["username"]
        self.password = provider["password"]
        custom_ua = provider.get("custom_user_agent")
        self.headers = {"User-Agent": custom_ua} if custom_ua else _UPSTREAM_HEADERS

    async def _call(self, action: str | None = None, **params) -> object:
        query = {"username": self.username, "password": self.password}
        if action:
            query["action"] = action
        query.update(params)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=self.headers) as client:
            r = await client.get(f"{self.base_url}/player_api.php", params=query)
            r.raise_for_status()
            return r.json()

    async def auth(self) -> dict:
        return await self._call()

    async def get_vod_categories(self) -> list[dict]:
        return await self._call("get_vod_categories")

    async def get_vod_streams(self) -> list[dict]:
        return await self._call("get_vod_streams")

    async def get_vod_info(self, vod_id: str) -> dict:
        return await self._call("get_vod_info", vod_id=vod_id)

    async def get_series_categories(self) -> list[dict]:
        return await self._call("get_series_categories")

    async def get_series(self) -> list[dict]:
        return await self._call("get_series")

    async def get_series_info(self, series_id: str) -> dict:
        return await self._call("get_series_info", series_id=series_id)


async def import_provider_catalog(provider_id: int) -> dict:
    provider = await asyncio.to_thread(vod_db.get_provider, provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")

    client = XCProviderClient(provider)

    categories = await client.get_vod_categories()
    category_names = {str(c["category_id"]): c["category_name"] for c in categories}

    streams = await client.get_vod_streams()
    movie_name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "movie", "name")
    movie_items = []
    for s in streams:
        name, year = parse_name_year(s.get("name", ""))
        name = vod_db.apply_rules_to_value(name, movie_name_rules)
        movie_items.append({
            "name": name,
            "year": year,
            "provider_stream_id": str(s["stream_id"]),
            "container_extension": s.get("container_extension") or "mp4",
            "provider_category_name": category_names.get(str(s.get("category_id"))),
        })
    movie_result = await asyncio.to_thread(vod_db.bulk_import_movies, provider_id, movie_items)
    logger.info("[vod_importer] provider=%s movies: %s", provider["name"], movie_result)

    series_categories = await client.get_series_categories()
    series_category_names = {str(c["category_id"]): c["category_name"] for c in series_categories}

    series_list = await client.get_series()
    series_name_rules = await asyncio.to_thread(vod_db.get_active_rules_for_field, "series", "name")
    series_items = []
    for s in series_list:
        name, year = parse_name_year(s.get("name", ""))
        name = vod_db.apply_rules_to_value(name, series_name_rules)
        series_items.append({
            "name": name,
            "year": year or _coerce_year(s.get("year")),
            "provider_series_id": str(s["series_id"]),
            "provider_category_name": series_category_names.get(str(s.get("category_id"))),
        })
    series_result = await asyncio.to_thread(vod_db.bulk_import_series, provider_id, series_items)
    logger.info("[vod_importer] provider=%s series: %s", provider["name"], series_result)

    return {
        "provider": provider["name"],
        "movie_categories": len(categories),
        "series_categories": len(series_categories),
        **movie_result,
        **series_result,
    }


def _apply_field_rules(content_type: str, fields: dict) -> dict:
    """Applies each field's active metadata_rules (regex find/replace) to the
    freshly-fetched enrichment value before it's persisted."""
    result = {}
    for field, value in fields.items():
        rules = vod_db.get_active_rules_for_field(content_type, field)
        result[field] = vod_db.apply_rules_to_value(value, rules)
    return result


async def enrich_movie(movie_id: int, *, force: bool = False) -> bool:
    """Fetch get_vod_info for this movie's best source and persist detail
    fields. Returns False without a network call if already fresh (unless
    force=True) — the on-demand-and-cache pattern from the module docstring."""
    if not force and not await asyncio.to_thread(vod_db.movie_needs_enrichment, movie_id):
        return False

    sources = await asyncio.to_thread(vod_db.list_movie_sources, movie_id)
    if not sources:
        return False
    source = sources[0]
    provider = await asyncio.to_thread(vod_db.get_provider, source["provider_id"])
    if not provider:
        return False

    if provider.get("provider_type") == "plex":
        # Plex's library listing already hands back full detail at import
        # time (see plex_importer.py) — nothing more to lazily fetch here,
        # just refresh the TTL stamp so the scheduler leaves it alone.
        await asyncio.to_thread(vod_db.set_movie_enrichment, movie_id)
        return True

    client = XCProviderClient(provider)

    info = _as_dict(await client.get_vod_info(source["provider_stream_id"]))
    detail = _as_dict(info.get("info"))

    await asyncio.to_thread(
        vod_db.set_movie_enrichment,
        movie_id,
        **_apply_field_rules("movie", {
            "genre": detail.get("genre") or None,
            "description": detail.get("plot") or detail.get("description") or None,
            "cast_list": detail.get("cast") or detail.get("actors") or None,
            "director": detail.get("director") or None,
            "country": detail.get("country") or None,
        }),
        tmdb_id=detail.get("tmdb_id") or None,
        poster_url=detail.get("cover_big") or detail.get("movie_image") or None,
        duration_secs=detail.get("duration_secs") or None,
    )
    return True


async def enrich_series(series_id: int, *, force: bool = False) -> dict:
    """Fetch get_series_info — this is also where episodes come from (the
    bulk get_series list is series-metadata-only, no episodes), so this call
    is load-bearing even just to populate episodes, not only for detail.

    Returns {"fetched": bool, "reason": str | None} rather than a bare bool
    -- every False outcome used to look identical (nothing happened, no
    error), which meant a real problem (the provider this series was
    imported from got deleted since) was indistinguishable from "already up
    to date, nothing to do" from the caller's side. A caller like the
    year-review panel's "fetch episodes to preview" button needs to tell
    those apart to show something better than a spinner that just resets.

    Every vod_db call in here (and in enrich_movie above) is offloaded via
    asyncio.to_thread — these are plain synchronous sqlite3 calls, and
    calling them directly on the event loop thread means any lock
    contention (very real: bulk_enrich_all runs 8 of these concurrently
    against the same db file) freezes the ENTIRE process, including
    unrelated concurrent work like a video stream relay. That's what was
    causing playback to stall mid-stream even though the network path to
    the source was fine."""
    if not force and not await asyncio.to_thread(vod_db.series_needs_enrichment, series_id):
        return {"fetched": False, "reason": "already up to date"}

    series = await asyncio.to_thread(vod_db.get_series, series_id)
    if not series:
        return {"fetched": False, "reason": "series not found"}
    if not series.get("import_provider_id"):
        return {"fetched": False, "reason": "no source provider recorded for this series"}

    provider = await asyncio.to_thread(vod_db.get_provider, series["import_provider_id"])
    if not provider:
        return {"fetched": False, "reason": "the provider this series was originally imported from no longer exists"}

    if provider.get("provider_type") == "plex":
        # Same reasoning as enrich_movie: Plex already gave us full detail
        # and every episode at import time (plex_importer.py) — episodes
        # aren't lazily discovered here the way XC's are.
        await asyncio.to_thread(vod_db.set_series_enrichment, series_id)
        return {"fetched": True, "reason": None}

    client = XCProviderClient(provider)
    info = _as_dict(await client.get_series_info(str(series["import_provider_series_id"])))
    detail = _as_dict(info.get("info"))

    await asyncio.to_thread(
        vod_db.set_series_enrichment,
        series_id,
        **_apply_field_rules("series", {
            "genre": detail.get("genre") or None,
            "description": detail.get("plot") or None,
            "cast_list": detail.get("cast") or None,
            "director": detail.get("director") or None,
            "country": detail.get("country") or None,
        }),
        # This provider sends the series' TMDB id under "tmdb", not "tmdb_id"
        # (unlike its own movie endpoint, which does use "tmdb_id") -- check
        # both since key naming isn't consistent even within one provider,
        # let alone across others.
        tmdb_id=detail.get("tmdb") or detail.get("tmdb_id") or None,
        poster_url=detail.get("cover") or None,
    )

    # get_series_info's "episodes" field is documented as {season_key: [ep, ...]}
    # (standard XC shape), but at least one real provider returns a plain
    # list of per-season lists instead — [[ep,...], [ep,...]].
    # Each episode also carries its own "season" field regardless of shape, so
    # trust that over the dict key / list index, falling back to the latter
    # only if a provider omits it.
    episodes_raw = info.get("episodes") or {}
    season_groups = episodes_raw.items() if isinstance(episodes_raw, dict) else enumerate(episodes_raw)

    for season_key, episodes in season_groups:
        for ep in episodes:
            season_number = ep.get("season", season_key)
            episode_id = await asyncio.to_thread(
                vod_db.add_episode,
                series_id,
                season_number=int(season_number),
                episode_number=int(ep.get("episode_num", 0)),
                name=ep.get("title") or f"Episode {ep.get('episode_num', '?')}",
                description=(ep.get("info") or {}).get("plot") or None,
                duration_secs=(ep.get("info") or {}).get("duration_secs") or None,
            )
            await asyncio.to_thread(
                vod_db.add_episode_source,
                episode_id, provider["id"], str(ep["id"]),
                ep.get("container_extension") or "mp4",
            )

    return {"fetched": True, "reason": None}


# ── Bulk enrichment ──────────────────────────────────────────────────────────
# On-demand-and-cache (above) only ever touches one item per click. Bulk mode
# walks the whole pool with bounded concurrency so it doesn't hammer a real
# provider's API — progress is tracked in-process (single-instance app, no
# need for anything heavier) and polled from the UI rather than blocking a
# single request for what can be a multi-minute run across a large pool.

_ENRICH_PROGRESS: dict = {
    "running": False,
    "movies_total": 0, "movies_done": 0, "movies_errors": 0,
    "series_total": 0, "series_done": 0, "series_errors": 0,
    "started_at": None, "finished_at": None,
}


def get_enrich_progress() -> dict:
    return dict(_ENRICH_PROGRESS)


_PROGRESS_PREFIX = {"movie": "movies", "series": "series"}  # "series" pluralizes to itself, not "seriess"


async def _enrich_one(kind: str, sem: asyncio.Semaphore, item_id: int, force: bool) -> None:
    prefix = _PROGRESS_PREFIX[kind]
    async with sem:
        try:
            if kind == "movie":
                await enrich_movie(item_id, force=force)
            else:
                await enrich_series(item_id, force=force)
        except Exception as exc:
            logger.warning("[vod_importer] bulk enrich %s=%s failed: %s", kind, item_id, exc)
            _ENRICH_PROGRESS[f"{prefix}_errors"] += 1
        finally:
            _ENRICH_PROGRESS[f"{prefix}_done"] += 1


async def bulk_enrich_all(concurrency: int = 8, force: bool = False) -> None:
    """Enriches every movie and series in the pool. Movies first, then series
    — each batch runs at bounded concurrency so we're never hitting a single
    provider with more than `concurrency` simultaneous requests."""
    if _ENRICH_PROGRESS["running"]:
        return

    movie_ids  = await asyncio.to_thread(vod_db.list_all_movie_ids)
    series_ids = await asyncio.to_thread(vod_db.list_all_series_ids)
    _ENRICH_PROGRESS.update({
        "running": True,
        "movies_total": len(movie_ids), "movies_done": 0, "movies_errors": 0,
        "series_total": len(series_ids), "series_done": 0, "series_errors": 0,
        "started_at": time.time(), "finished_at": None,
    })
    logger.info("[vod_importer] bulk enrich starting: %d movies, %d series, concurrency=%d",
                len(movie_ids), len(series_ids), concurrency)

    sem = asyncio.Semaphore(concurrency)
    try:
        # return_exceptions=True: _enrich_one already catches everything it can
        # anticipate, but a single unanticipated exception must not abort the
        # rest of the batch (gather() without this re-raises immediately on
        # the first failure, leaving every other in-flight task orphaned).
        await asyncio.gather(*(_enrich_one("movie", sem, mid, force) for mid in movie_ids), return_exceptions=True)
        await asyncio.gather(*(_enrich_one("series", sem, sid, force) for sid in series_ids), return_exceptions=True)
    finally:
        _ENRICH_PROGRESS["running"] = False
        _ENRICH_PROGRESS["finished_at"] = time.time()
        elapsed = _ENRICH_PROGRESS["finished_at"] - _ENRICH_PROGRESS["started_at"]
        logger.info(
            "[vod_importer] bulk enrich done in %.1fs: movies %d/%d (%d errors), series %d/%d (%d errors)",
            elapsed,
            _ENRICH_PROGRESS["movies_done"], _ENRICH_PROGRESS["movies_total"], _ENRICH_PROGRESS["movies_errors"],
            _ENRICH_PROGRESS["series_done"], _ENRICH_PROGRESS["series_total"], _ENRICH_PROGRESS["series_errors"],
        )

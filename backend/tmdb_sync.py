"""
Syncs TMDB public Lists into VOD categories — e.g. a user's own TMDB
watchlist-style list becomes their own named category, auto-populated by
matching each list entry's TMDB id against our own pool (movies.tmdb_id /
series.tmdb_id, already captured during enrichment from the provider's own
TMDB-quality metadata). Exact-id matching only — no fuzzy name/year guessing,
since both sides agree on the same TMDB id space.

A category's sync_source column holds a string like "tmdb_list:1234567".
Only items already present in our pool (i.e. actually available from a real
provider) can ever get placed — this doesn't pull in new content, it just
organizes what's already there according to an external list.
"""

import asyncio
import logging
import re
import time

import httpx

from config import get_tmdb_api_key
import vod_db

logger = logging.getLogger(__name__)

_API_BASE = "https://api.themoviedb.org/3"
_YEAR_LOOKUP_CONCURRENCY = 10

# KNM: added 2026-09-08 -- prompted by a provider import running concurrently
# with TMDB enrichment; not an observed failure this time, but nothing
# previously stopped two TMDB-calling features from running at once and
# stacking their concurrency against the same TMDB API key's rate limit.
# Process-wide cap on concurrent TMDB requests, shared across every caller in
# this module (bulk_enrich_all, provider import's tmdb_id-shortcut path,
# Duplicate Finder/Missing Artwork's search_title, etc). Each caller already
# bounds its OWN concurrency (e.g. bulk_enrich_all's per-kind semaphore), but
# nothing previously stopped two features from running at once and adding
# their concurrency together against TMDB's shared per-key rate limit.
# KNM: raised 20 -> 40 2026-09-09 -- still comfortably under TMDB's ~50 req/s
# limit even if every caller is maxed out simultaneously, but the old value
# of 20 was picked before connection reuse existed and was leaving real
# throughput on the table once handshake overhead stopped being the limiter.
_GLOBAL_TMDB_CONCURRENCY = 40
_tmdb_semaphore = asyncio.Semaphore(_GLOBAL_TMDB_CONCURRENCY)

# KNM: added 2026-09-09 -- one persistent client shared by every function in
# this module instead of each opening/closing its own httpx.AsyncClient per
# call (a fresh TCP+TLS handshake per single request). keepalive pool sized
# to _GLOBAL_TMDB_CONCURRENCY so every concurrent slot can hold a warm reused
# connection instead of racing to open a new socket. Never explicitly closed
# -- lives for the process lifetime.
_tmdb_client = httpx.AsyncClient(
    timeout=30.0,
    follow_redirects=True,
    limits=httpx.Limits(max_connections=_GLOBAL_TMDB_CONCURRENCY, max_keepalive_connections=_GLOBAL_TMDB_CONCURRENCY),
)

# KNM: added 2026-09-09 -- TMDB is well-behaved and rarely rate-limits, but a
# bulk enrichment run at 40-wide concurrency can trip it. Much lighter than
# the provider backoff (vod_importer._PROVIDER_BACKOFF): no adaptive
# concurrency halving, just a short pause before the next call after a
# 429/503 so a rate-limit hit doesn't get hammered through immediately.
_TMDB_BACKOFF_STATUS_CODES = {429, 503}
_TMDB_BACKOFF_SECONDS = 5.0
_tmdb_backoff_until = 0.0


async def _tmdb_get(url: str, params: dict) -> httpx.Response:
    """GET through the shared TMDB client, honoring/setting a brief
    process-wide backoff on 429/503 -- callers still do their own
    raise_for_status() and exception handling on the returned response."""
    global _tmdb_backoff_until
    now = time.time()
    if now < _tmdb_backoff_until:
        await asyncio.sleep(_tmdb_backoff_until - now)
    r = await _tmdb_client.get(url, params=params)
    if r.status_code in _TMDB_BACKOFF_STATUS_CODES:
        _tmdb_backoff_until = time.time() + _TMDB_BACKOFF_SECONDS
    return r

_API_KEY_RE = re.compile(r"(api_key=)[^&\s'\"]+")


def _redact(exc: Exception) -> str:
    """str(exc) on an httpx.HTTPStatusError embeds the full request URL,
    api_key included -- this must wrap every logged/returned exception from
    a TMDB call, or a real API key ends up in plaintext in container logs
    (and, via sync_all's error dict, in an API response body)."""
    return _API_KEY_RE.sub(r"\1***", str(exc))


async def fetch_list_items(list_id: str) -> list[dict]:
    """GET /list/{id} paginates its "items" array (20/page) behind a "page"
    param, separate from the "item_count" total it reports up front -- a
    single unpaged call silently truncated any list over 20 items to its
    first page (GH issue #3's second half: v0.1.14's title/year fallback fixed
    matching, but a 250-item list still only ever placed 20, because that's
    all fetch_list_items ever saw)."""
    api_key = get_tmdb_api_key()
    if not api_key:
        raise ValueError("TMDB API key not configured")

    items: list[dict] = []
    item_count: int | None = None
    page = 1
    while True:
        async with _tmdb_semaphore:
            r = await _tmdb_get(f"{_API_BASE}/list/{list_id}", params={"api_key": api_key, "page": page})
        r.raise_for_status()
        data = r.json()
        page_items = data.get("items", [])
        items.extend(page_items)
        if item_count is None:
            item_count = data.get("item_count")
        if not page_items or (item_count is not None and len(items) >= item_count):
            break
        page += 1

    return items


def normalize_list_items(raw_items: list[dict]) -> list[dict]:
    """TMDB's own /list/{id} item shape (media_type, id, title/
    original_title, release_date, name/original_name, first_air_date) ->
    the common {"media_type": "movie"|"tv", "tmdb_id": int|None,
    "title": str|None, "year": int|None} shape vod_list_sync.py's shared
    sync loop expects, matching mdblist_sync.fetch_list_items' own already-
    normalized output -- the shared loop never needs to know which provider
    a source came from."""
    out: list[dict] = []
    for item in raw_items:
        media_type = item.get("media_type")
        tmdb_id = item.get("id")
        if media_type == "movie":
            title = item.get("title") or item.get("original_title")
            date = item.get("release_date") or ""
        elif media_type == "tv":
            title = item.get("name") or item.get("original_name")
            date = item.get("first_air_date") or ""
        else:
            continue
        year = int(date[:4]) if date[:4].isdigit() else None
        out.append({
            "media_type": media_type,
            "tmdb_id": int(tmdb_id) if tmdb_id is not None else None,
            "title": title,
            "year": year,
        })
    return out


async def search_title(query: str, content_type: str) -> list[dict]:
    """Real TMDB search results for a query -- used by the year-review flow so
    a user picks from actual candidates (title/year/poster/tmdb_id/cast)
    instead of researching each one themselves. content_type is 'movie' or
    'series' (mapped to TMDB's own 'movie'/'tv' search endpoints). query is
    caller-supplied rather than always the pool item's own stored name --
    the same title is sometimes released under a different name in a
    different region (e.g. a film's international title vs. its North
    American one), and TMDB's search only finds what actually matches the
    query string, so a fixed auto-derived query can't be fixed in code —
    letting the reviewer type what they think it's actually called is the
    real fix. See the /needs-review/.../suggestions/ route's q param.

    Includes overview/rating/cast (and, for series, season/episode counts)
    so a reviewer has more than a bare name+year to go on -- the search
    endpoint alone doesn't return any of that, so it's one extra detail call
    per candidate (cast comes along for free on the same call via
    append_to_response=credits, no separate request needed), fetched
    concurrently to keep this fast. Capped at 5 candidates specifically to
    bound how many of those extra calls one lookup makes.

    TMDB's own search is fuzzy, not exact-title-only -- searching a short,
    common word like "Action" returns 150+ results, and most aren't actually
    titled "Action" (e.g. "Action Man", "Justice League Action", "World in
    Action"). Left in TMDB's own popularity-ranked order, those often
    outrank an exact-title match that's just less well-known, pushing it
    past the cap entirely (a real case: an exact "Action" (2024) ranked 6th,
    one past the cutoff). Re-sorted so exact (case-insensitive) title
    matches come first, before applying the cap -- TMDB's relative ordering
    is preserved within each group, only the exact/non-exact split is
    forced to the front."""
    api_key = get_tmdb_api_key()
    if not api_key:
        raise ValueError("TMDB API key not configured")

    endpoint = "movie" if content_type == "movie" else "tv"
    async with _tmdb_semaphore:
        r = await _tmdb_get(
            f"{_API_BASE}/search/{endpoint}",
            params={"api_key": api_key, "query": query},
        )
    r.raise_for_status()
    data = r.json()

    async def _build(item: dict) -> dict:
        date = item.get("release_date") if content_type == "movie" else item.get("first_air_date")
        year = int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None
        out = {
            "tmdb_id": str(item["id"]),
            "name": item.get("title") if content_type == "movie" else item.get("name"),
            "year": year,
            "poster_url": f"https://image.tmdb.org/t/p/w185{item['poster_path']}" if item.get("poster_path") else None,
            "overview": item.get("overview") or None,
            "vote_average": item.get("vote_average"),
            "season_count": None,
            "episode_count": None,
            "cast": [],
        }
        try:
            async with _tmdb_semaphore:
                dr = await _tmdb_get(
                    f"{_API_BASE}/{endpoint}/{item['id']}",
                    params={"api_key": api_key, "append_to_response": "credits"},
                )
            dr.raise_for_status()
            dd = dr.json()
            if content_type == "series":
                out["season_count"] = dd.get("number_of_seasons")
                out["episode_count"] = dd.get("number_of_episodes")
            out["cast"] = [c["name"] for c in dd.get("credits", {}).get("cast", [])[:4]]
        except Exception as exc:
            logger.warning("[tmdb_sync] failed to fetch detail for tmdb_id=%s: %s", item["id"], _redact(exc))
        return out

    results = data.get("results", [])
    query_lower = query.strip().lower()

    def _not_exact(item: dict) -> bool:
        title = item.get("title") if content_type == "movie" else item.get("name")
        return (title or "").strip().lower() != query_lower

    results.sort(key=_not_exact)  # stable sort: exact matches (False) float ahead of fuzzy ones (True)
    candidates = results[:5]
    return list(await asyncio.gather(*[_build(item) for item in candidates]))


async def get_series_episode_list(tmdb_id: str) -> list[dict]:
    """Every canonical episode TMDB knows about for a series -- the DVR
    Library's Sonarr/Radarr-style missing-episode view diffs this against
    what's actually in the pool (vod_db.list_episodes_for_series_ids) to
    find gaps, then offers a one-click way to fill them (backfill from the
    pool, or an EPG search/schedule) rather than requiring an admin to
    notice and go look for a specific episode themselves.

    One /tv/{id} call for the season list, then one /tv/{id}/season/{n}
    call per real season (TMDB's season 0 is "Specials", excluded --
    Dispatcharr recordings/EPG data essentially never carry a specials
    numbering VOD Manager could match against), fetched concurrently."""
    api_key = get_tmdb_api_key()
    if not api_key:
        raise ValueError("TMDB API key not configured")
    async with _tmdb_semaphore:
        r = await _tmdb_get(f"{_API_BASE}/tv/{tmdb_id}", params={"api_key": api_key})
    r.raise_for_status()
    seasons = [s["season_number"] for s in r.json().get("seasons", []) if s.get("season_number")]

    async def _season(season_number: int) -> list[dict]:
        try:
            async with _tmdb_semaphore:
                sr = await _tmdb_get(f"{_API_BASE}/tv/{tmdb_id}/season/{season_number}", params={"api_key": api_key})
            sr.raise_for_status()
            return [
                {
                    "season_number": season_number,
                    "episode_number": ep["episode_number"],
                    "name": ep.get("name"),
                    "air_date": ep.get("air_date"),
                }
                for ep in sr.json().get("episodes", [])
            ]
        except Exception as exc:
            logger.warning("[tmdb_sync] failed to fetch season %d for tmdb_id=%s: %s", season_number, tmdb_id, _redact(exc))
            return []

    results = await asyncio.gather(*[_season(n) for n in seasons])
    return [ep for season_eps in results for ep in season_eps]


_episode_list_cache: dict[str, tuple[float, list[dict]]] = {}
_EPISODE_LIST_CACHE_TTL = 3600  # 1 hour -- a show's full history barely ever
# changes; only the newest handful of episodes do. Added 2026-07-29 for the
# portal Library's per-show episode view (season pill selector) -- a real,
# long-running show (General Hospital: 63 seasons, ~10.8k episodes) takes
# ~3s to fetch fresh every time (confirmed live), which is fine for an
# occasional admin action but too slow to re-pay on every portal page open.


async def get_series_episode_list_cached(tmdb_id: str) -> list[dict]:
    now = time.time()
    cached = _episode_list_cache.get(tmdb_id)
    if cached and now - cached[0] < _EPISODE_LIST_CACHE_TTL:
        return cached[1]
    episodes = await get_series_episode_list(tmdb_id)
    _episode_list_cache[tmdb_id] = (now, episodes)
    return episodes


async def get_tmdb_details_for_ids(tmdb_ids: list[str], content_type: str) -> dict[str, dict]:
    """TMDB's own canonical title and release year per id. Two Duplicate
    Finder candidates sharing a tmdb_id confirms they're the same real
    title, but doesn't say which candidate's OWN name/year fields are
    actually correct -- a provider-mislabeled year or a punctuation-variant
    name still carries a valid tmdb_id, just matched by title, so the id
    alone can't distinguish which candidate is the "true" one. The title is
    what lets Duplicate Finder auto-suggest a merge target with confidence
    (exact string match against TMDB's own title) instead of falling back
    to a weaker heuristic like source count. No bulk-lookup-by-ids endpoint
    exists on TMDB, so this is one real GET per distinct id, capped at
    modest concurrency since this only ever runs against a small, bounded
    set (the ids actually surfaced by one Duplicate Finder scan), never the
    whole catalog."""
    api_key = get_tmdb_api_key()
    if not api_key:
        raise ValueError("TMDB API key not configured")

    endpoint = "movie" if content_type == "movie" else "tv"
    semaphore = asyncio.Semaphore(_YEAR_LOOKUP_CONCURRENCY)

    async def _fetch(tmdb_id: str) -> tuple[str, dict]:
        async with semaphore, _tmdb_semaphore:
            try:
                r = await _tmdb_get(f"{_API_BASE}/{endpoint}/{tmdb_id}", params={"api_key": api_key})
                r.raise_for_status()
                data = r.json()
                date = data.get("release_date") if content_type == "movie" else data.get("first_air_date")
                year = int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None
                title = data.get("title") if content_type == "movie" else data.get("name")
                return tmdb_id, {"year": year, "title": title or None}
            except Exception as exc:
                logger.warning("[tmdb_sync] failed to fetch detail for tmdb_id=%s: %s", tmdb_id, _redact(exc))
                return tmdb_id, {"year": None, "title": None}

    results = await asyncio.gather(*[_fetch(tid) for tid in set(tmdb_ids)])
    return dict(results)


async def get_movie_full_details(tmdb_id: str) -> dict | None:
    """Full detail fields for one movie by TMDB id, shaped to match what
    vod_importer.enrich_movie fills in from a provider's get_vod_info (genre,
    description, cast_list, director, country, poster_url, duration_secs,
    rating, release_date) -- lets enrich_movie skip the provider entirely
    once a movie already carries a confirmed tmdb_id, trading the provider's
    own per-account rate limit for TMDB's (much higher, and not shared with
    anything else this app does against that provider). Returns None on any
    failure (bad id, TMDB down, no API key) so the caller can fall back to
    the provider rather than leaving the movie unenriched."""
    api_key = get_tmdb_api_key()
    if not api_key:
        return None

    try:
        async with _tmdb_semaphore:
            r = await _tmdb_get(
                f"{_API_BASE}/movie/{tmdb_id}",
                params={"api_key": api_key, "append_to_response": "credits,release_dates"},
            )
        r.raise_for_status()
    except Exception as exc:
        logger.warning("[tmdb_sync] failed to fetch movie detail for tmdb_id=%s: %s", tmdb_id, _redact(exc))
        return None
    data = r.json()

    director = next(
        (c["name"] for c in data.get("credits", {}).get("crew", []) if c.get("job") == "Director"),
        None,
    )
    cast = [c["name"] for c in data.get("credits", {}).get("cast", [])[:10]]
    runtime = data.get("runtime")
    return {
        "name": data.get("title") or None,
        "genre": ", ".join(g["name"] for g in data.get("genres", [])) or None,
        "description": data.get("overview") or None,
        "cast_list": ", ".join(cast) or None,
        "director": director,
        "country": ", ".join(c["name"] for c in data.get("production_countries", [])) or None,
        "poster_url": f"https://image.tmdb.org/t/p/w500{data['poster_path']}" if data.get("poster_path") else None,
        "duration_secs": runtime * 60 if runtime else None,
        "rating": data.get("vote_average") or None,
        "release_date": data.get("release_date") or None,
        "content_rating": _extract_us_movie_certification(data),
    }


def _extract_us_movie_certification(data: dict) -> str | None:
    """US MPAA rating (G/PG/PG-13/R/NC-17) from a /movie/{id}?append_to_
    response=release_dates payload -- TMDB nests this per-country, per-
    release-type, and routinely leaves it blank for many release entries
    even when a real certification exists elsewhere in the same country's
    list, so this takes the first non-empty one rather than just index 0."""
    for country in data.get("release_dates", {}).get("results", []):
        if country.get("iso_3166_1") != "US":
            continue
        for release in country.get("release_dates", []):
            cert = (release.get("certification") or "").strip()
            if cert:
                return cert
    return None


async def get_tv_content_rating(tmdb_id: str) -> str | None:
    """US TV content rating (TV-Y/TV-Y7/TV-G/TV-PG/TV-14/TV-MA) for a series --
    see get_movie_full_details' identical certification handling. Kept
    separate/minimal (not folded into a full series-detail fetch) since
    nothing else calls TMDB for series detail today -- enrich_series only
    ever asked the provider for episodes/detail. This is deliberately the
    smallest addition that makes a real content-rating field possible for
    series smart categories, not a rework of series enrichment."""
    api_key = get_tmdb_api_key()
    if not api_key:
        return None
    try:
        async with _tmdb_semaphore:
            r = await _tmdb_get(
                f"{_API_BASE}/tv/{tmdb_id}/content_ratings",
                params={"api_key": api_key},
            )
        r.raise_for_status()
    except Exception as exc:
        logger.warning("[tmdb_sync] failed to fetch content rating for tv tmdb_id=%s: %s", tmdb_id, _redact(exc))
        return None
    data = r.json()

    for country in data.get("results", []):
        if country.get("iso_3166_1") == "US":
            rating = (country.get("rating") or "").strip()
            return rating or None
    return None


# sync_category/sync_all moved to vod_list_sync.py 2026-09-07, generalized
# to support more than one list source per category (TMDB Lists + MDBList,
# any mix) -- see that module for the current fetch/match/place logic.
# normalize_list_items above is this module's contribution to that shared
# loop (mdblist_sync.fetch_list_items returns the same normalized shape).

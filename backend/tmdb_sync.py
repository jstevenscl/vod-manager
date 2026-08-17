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
_YEAR_LOOKUP_CONCURRENCY = 6

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
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        page = 1
        while True:
            r = await client.get(f"{_API_BASE}/list/{list_id}", params={"api_key": api_key, "page": page})
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
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        r = await client.get(
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
                dr = await client.get(
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
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        r = await client.get(f"{_API_BASE}/tv/{tmdb_id}", params={"api_key": api_key})
        r.raise_for_status()
        seasons = [s["season_number"] for s in r.json().get("seasons", []) if s.get("season_number")]

        async def _season(season_number: int) -> list[dict]:
            try:
                sr = await client.get(f"{_API_BASE}/tv/{tmdb_id}/season/{season_number}", params={"api_key": api_key})
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

    async def _fetch(client: httpx.AsyncClient, tmdb_id: str) -> tuple[str, dict]:
        async with semaphore:
            try:
                r = await client.get(f"{_API_BASE}/{endpoint}/{tmdb_id}", params={"api_key": api_key})
                r.raise_for_status()
                data = r.json()
                date = data.get("release_date") if content_type == "movie" else data.get("first_air_date")
                year = int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None
                title = data.get("title") if content_type == "movie" else data.get("name")
                return tmdb_id, {"year": year, "title": title or None}
            except Exception as exc:
                logger.warning("[tmdb_sync] failed to fetch detail for tmdb_id=%s: %s", tmdb_id, _redact(exc))
                return tmdb_id, {"year": None, "title": None}

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        results = await asyncio.gather(*[_fetch(client, tid) for tid in set(tmdb_ids)])
    return dict(results)


def _parse_sync_source(sync_source: str) -> tuple[str, str] | None:
    if not sync_source or ":" not in sync_source:
        return None
    kind, ref = sync_source.split(":", 1)
    return kind, ref


async def sync_category(category_id: int) -> dict:
    category = vod_db.get_category(category_id)
    if not category:
        raise ValueError(f"category {category_id} not found")

    parsed = _parse_sync_source(category.get("sync_source") or "")
    if not parsed or parsed[0] != "tmdb_list":
        raise ValueError(f"category {category_id} has no tmdb_list sync_source configured")
    _, list_id = parsed

    items = await fetch_list_items(list_id)

    matched_movie_ids: list[int] = []
    matched_series_ids: list[int] = []
    unmatched = 0

    for item in items:
        media_type = item.get("media_type")
        tmdb_id = item.get("id")
        if tmdb_id is None:
            continue

        if media_type == "movie" and category["content_type"] == "movie":
            movie = vod_db.get_movie_by_tmdb_id(tmdb_id)
            if not movie:
                # Most pool movies never get a tmdb_id at import time (only
                # set when the provider's own metadata happens to include
                # one) -- fall back to a normalized title+year match against
                # the list's own title/date fields (GH issue #3: curated
                # lists like IMDB Top 250 were matching "very few items"
                # because this fallback didn't exist). A hit backfills the
                # tmdb_id so future syncs take the fast id-only path.
                title = item.get("title") or item.get("original_title")
                release_date = item.get("release_date") or ""
                year = int(release_date[:4]) if release_date[:4].isdigit() else None
                movie = title and vod_db.find_movie_by_title_year(title, year)
                if movie:
                    # Only source of truth for a wrong-match investigation
                    # (GH issue #6) -- backfill_tmdb_id_if_missing's own log
                    # doesn't have the list item's title, and this fallback
                    # match is the one place a title/year mismatch could
                    # silently attach the wrong id to a pool movie.
                    logger.info(
                        "[tmdb_sync] fallback match: pool movie id=%s (%r, year=%s) <- list title=%r year=%s tmdb_id=%s",
                        movie["id"], movie["name"], movie["year"], title, year, tmdb_id,
                    )
                    vod_db.backfill_tmdb_id_if_missing("movie", movie["id"], str(tmdb_id))
            if movie:
                matched_movie_ids.append(movie["id"])
            else:
                unmatched += 1
        elif media_type == "tv" and category["content_type"] == "series":
            series = vod_db.get_series_by_tmdb_id(tmdb_id)
            if not series:
                title = item.get("name") or item.get("original_name")
                first_air_date = item.get("first_air_date") or ""
                year = int(first_air_date[:4]) if first_air_date[:4].isdigit() else None
                series = title and vod_db.find_series_by_title_year(title, year)
                if series:
                    logger.info(
                        "[tmdb_sync] fallback match: pool series id=%s (%r, year=%s) <- list title=%r year=%s tmdb_id=%s",
                        series["id"], series["name"], series["year"], title, year, tmdb_id,
                    )
                    vod_db.backfill_tmdb_id_if_missing("series", series["id"], str(tmdb_id))
            if series:
                matched_series_ids.append(series["id"])
            else:
                unmatched += 1
        # media_type not matching this category's content_type is silently
        # skipped — a movie-content category ignores TV entries in the same
        # list and vice versa, rather than erroring.

    if category["content_type"] == "movie":
        newly_placed = vod_db.bulk_place_movies_in_category(matched_movie_ids, category_id)
        found = len(matched_movie_ids)
    else:
        newly_placed = vod_db.bulk_place_series_in_category(matched_series_ids, category_id)
        found = len(matched_series_ids)

    logger.info("[tmdb_sync] category=%s (%s) list=%s: %d in pool, %d newly placed, %d not in pool",
                category["name"], category["content_type"], list_id, found, newly_placed, unmatched)

    return {"list_total": len(items), "found_in_pool": found, "newly_placed": newly_placed, "not_in_pool": unmatched}


async def sync_all() -> dict:
    """Runs sync_category for every category with a sync_source configured —
    called both from the manual 'Sync now' endpoint and, if enabled in
    Settings -> Refresh Schedule, the periodic background scheduler
    (disabled by default; see main.py's _tmdb_sync_scheduler)."""
    results = {}
    for category in vod_db.list_sync_categories():
        try:
            results[category["name"]] = await sync_category(category["id"])
        except Exception as exc:
            logger.warning("[tmdb_sync] sync failed for category=%s: %s", category["name"], _redact(exc))
            results[category["name"]] = {"error": _redact(exc)}
    return results

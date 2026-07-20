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

import httpx

from config import get_tmdb_api_key
import vod_db

logger = logging.getLogger(__name__)

_API_BASE = "https://api.themoviedb.org/3"


async def fetch_list_items(list_id: str) -> list[dict]:
    api_key = get_tmdb_api_key()
    if not api_key:
        raise ValueError("TMDB API key not configured")

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(f"{_API_BASE}/list/{list_id}", params={"api_key": api_key})
        r.raise_for_status()
        data = r.json()

    return data.get("items", [])


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
                logger.warning("[tmdb_sync] failed to fetch detail for tmdb_id=%s: %s", item["id"], exc)
            return out

        results = data.get("results", [])
        query_lower = query.strip().lower()

        def _not_exact(item: dict) -> bool:
            title = item.get("title") if content_type == "movie" else item.get("name")
            return (title or "").strip().lower() != query_lower

        results.sort(key=_not_exact)  # stable sort: exact matches (False) float ahead of fuzzy ones (True)
        candidates = results[:5]
        return list(await asyncio.gather(*[_build(item) for item in candidates]))


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
            if movie:
                matched_movie_ids.append(movie["id"])
            else:
                unmatched += 1
        elif media_type == "tv" and category["content_type"] == "series":
            series = vod_db.get_series_by_tmdb_id(tmdb_id)
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
            logger.warning("[tmdb_sync] sync failed for category=%s: %s", category["name"], exc)
            results[category["name"]] = {"error": str(exc)}
    return results

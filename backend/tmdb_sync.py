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
    called both from the manual 'Sync now' endpoint and the scheduled task."""
    results = {}
    for category in vod_db.list_sync_categories():
        try:
            results[category["name"]] = await sync_category(category["id"])
        except Exception as exc:
            logger.warning("[tmdb_sync] sync failed for category=%s: %s", category["name"], exc)
            results[category["name"]] = {"error": str(exc)}
    return results

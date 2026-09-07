"""
Shared, provider-agnostic public-list sync. A category can pull from several
public lists at once (a TMDB List, an MDBList list, ...), each a "kind:ref"
string in categories.sync_sources (JSON array; categories.sync_source is the
older single-source column, still honored as a fallback for a category
linked before this existed). This module fetches+normalizes every source's
items into one common shape ({"media_type", "tmdb_id", "title", "year"} --
see tmdb_sync.normalize_list_items and mdblist_sync.fetch_list_items, both
already return this shape) and matches/places them against the pool exactly
ONCE per category, so the same title appearing in two linked lists is
naturally deduplicated by movie_category_placements' own unique constraint,
never placed twice, with no special-case dedup logic needed here.

Adding a new provider is one entry in _fetch_normalized_items' dispatch, not
a new sync loop -- e.g. a future "trakt_list:" kind just needs its own
fetch-and-normalize function plugged in here.
"""

import json
import logging

import config
import mdblist_sync
import tmdb_sync
import vod_db

logger = logging.getLogger(__name__)


def _parse_source(source: str) -> tuple[str, str] | None:
    if not source or ":" not in source:
        return None
    kind, ref = source.split(":", 1)
    return kind, ref


async def _fetch_normalized_items(source: str) -> list[dict]:
    parsed = _parse_source(source)
    if not parsed:
        raise ValueError(f"malformed list source {source!r}")
    kind, ref = parsed

    if kind == "tmdb_list":
        api_key = config.get_tmdb_api_key()
        if not api_key:
            raise ValueError("TMDB API key not configured")
        raw = await tmdb_sync.fetch_list_items(ref)
        return tmdb_sync.normalize_list_items(raw)

    if kind == "mdblist":
        api_key = config.get_mdblist_api_key()
        if not api_key:
            raise ValueError("MDBList API key not configured")
        return await mdblist_sync.fetch_list_items(api_key, ref)

    raise ValueError(f"unknown list source kind {kind!r}")


async def sync_category(category_id: int) -> dict:
    category = vod_db.get_category(category_id)
    if not category:
        raise ValueError(f"category {category_id} not found")

    # sync_source (singular) is the pre-multi-source legacy column -- still
    # honored here so a category linked before this feature keeps syncing
    # even if it was never migrated to the new array.
    sources: list[str] = []
    if category.get("sync_sources"):
        sources = json.loads(category["sync_sources"])
    if not sources and category.get("sync_source"):
        sources = [category["sync_source"]]
    if not sources:
        raise ValueError(f"category {category_id} has no list sources configured")

    matched_movie_ids: set[int] = set()
    matched_series_ids: set[int] = set()
    unmatched = 0
    list_total = 0
    source_errors: dict[str, str] = {}

    for source in sources:
        try:
            items = await _fetch_normalized_items(source)
        except Exception as exc:
            logger.warning("[vod_list_sync] source=%s failed for category=%s: %s", source, category["name"], exc)
            source_errors[source] = str(exc)
            continue

        list_total += len(items)
        for item in items:
            media_type = item.get("media_type")
            tmdb_id = item.get("tmdb_id")
            if tmdb_id is None:
                unmatched += 1
                continue

            if media_type == "movie" and category["content_type"] == "movie":
                movie = vod_db.get_movie_by_tmdb_id(tmdb_id)
                if not movie:
                    # Most pool movies never get a tmdb_id at import time --
                    # fall back to a normalized title+year match, backfilling
                    # the id on a hit so future syncs take the fast path.
                    title, year = item.get("title"), item.get("year")
                    found = title and vod_db.find_movie_by_title_year(title, year)
                    if found:
                        movie = found
                        if not found.get("tmdb_id"):
                            logger.info(
                                "[vod_list_sync] fallback match: pool movie id=%s (%r, year=%s) <- source=%s title=%r year=%s tmdb_id=%s",
                                found["id"], found["name"], found["year"], source, title, year, tmdb_id,
                            )
                            vod_db.backfill_tmdb_id_if_missing("movie", found["id"], str(tmdb_id))
                if movie:
                    matched_movie_ids.add(movie["id"])
                else:
                    unmatched += 1
            elif media_type == "tv" and category["content_type"] == "series":
                series = vod_db.get_series_by_tmdb_id(tmdb_id)
                if not series:
                    title, year = item.get("title"), item.get("year")
                    found = title and vod_db.find_series_by_title_year(title, year)
                    if found:
                        series = found
                        if not found.get("tmdb_id"):
                            logger.info(
                                "[vod_list_sync] fallback match: pool series id=%s (%r, year=%s) <- source=%s title=%r year=%s tmdb_id=%s",
                                found["id"], found["name"], found["year"], source, title, year, tmdb_id,
                            )
                            vod_db.backfill_tmdb_id_if_missing("series", found["id"], str(tmdb_id))
                if series:
                    matched_series_ids.add(series["id"])
                else:
                    unmatched += 1
            # media_type not matching this category's content_type is
            # silently skipped -- a movie-content category ignores TV
            # entries in the same list and vice versa, rather than erroring.

    if category["content_type"] == "movie":
        newly_placed = vod_db.bulk_place_movies_in_category(list(matched_movie_ids), category_id)
        found_in_pool = len(matched_movie_ids)
    else:
        newly_placed = vod_db.bulk_place_series_in_category(list(matched_series_ids), category_id)
        found_in_pool = len(matched_series_ids)

    removed = 0
    # Mirror mode: also remove anything placed here that's no longer in ANY
    # linked list -- keeps the category an exact reflection of its
    # source(s) (e.g. "Top 100 Horror Movies"), vs. the add_only default
    # where nothing already placed is ever removed just because it fell off
    # a list (e.g. a curated "Halloween - Kids" category).
    #
    # Skipped whenever ANY source failed this pass (source_errors non-
    # empty): a mirror removal is a judgment "this is no longer on the
    # list," and a transient fetch failure (rate limit, network hiccup, the
    # list host briefly down) must never look like "everything not just
    # re-confirmed is gone" -- that's the same class of bug as an XC
    # provider's short/incomplete response wiping a whole episode list
    # (see the Dispatcharr episode-relation fix this mirrors). Skipping the
    # removal this pass costs nothing real: every item actually returned by
    # the working sources is still placed above, and the next successful
    # sync reconciles normally.
    if category.get("sync_mode") == "mirror" and not source_errors:
        if category["content_type"] == "movie":
            existing_ids = {p["movie_id"] for p in vod_db.list_movie_placements_for_category(category_id)}
            for movie_id in existing_ids - matched_movie_ids:
                vod_db.remove_movie_from_category(movie_id, category_id)
                removed += 1
        else:
            existing_ids = {p["series_id"] for p in vod_db.list_series_placements_for_category(category_id)}
            for series_id in existing_ids - matched_series_ids:
                vod_db.remove_series_from_category(series_id, category_id)
                removed += 1

    logger.info(
        "[vod_list_sync] category=%s (%s) sources=%s: %d in pool, %d newly placed, %d not in pool, %d removed (mode=%s)",
        category["name"], category["content_type"], sources, found_in_pool, newly_placed, unmatched, removed, category.get("sync_mode"),
    )

    result = {
        "list_total": list_total, "found_in_pool": found_in_pool,
        "newly_placed": newly_placed, "not_in_pool": unmatched, "removed": removed,
    }
    if source_errors:
        result["source_errors"] = source_errors
    return result


async def sync_all(only_without_own_schedule: bool = False) -> dict:
    """Runs sync_category for every category with at least one list source
    configured -- called both from the manual 'Sync now'/'Sync all'
    endpoints and, if enabled in Settings -> Refresh Schedule, the periodic
    background scheduler.

    only_without_own_schedule: the global scheduler's own call site sets
    this so a category with its own schedule_interval_seconds (handled by
    the separate per-category scheduler loop, same cadence field
    categories_due_for_scheduled_evaluation uses) doesn't get synced twice
    on two different clocks -- the global interval is a true fallback
    default only for categories that haven't opted into their own cadence."""
    categories = vod_db.list_sync_categories()
    if only_without_own_schedule:
        categories = [c for c in categories if c.get("schedule_interval_seconds") is None]
    results = {}
    for category in categories:
        try:
            results[category["name"]] = await sync_category(category["id"])
        except Exception as exc:
            logger.warning("[vod_list_sync] sync failed for category=%s: %s", category["name"], exc)
            results[category["name"]] = {"error": str(exc)}
    return results

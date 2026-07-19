"""
Imports a user's own Plex library into the VOD pool — the Plex-provider
counterpart to vod_importer.py's XC catalog import.

Unlike XC (cheap bulk list now, expensive per-item detail fetched lazily
later), Plex's library listing already returns full detail (genre, summary,
cast, poster, file part) in one call, so this is single-phase: no separate
enrichment step needed at import time. last_enriched_at is stamped so the
scheduled re-enrichment pass leaves these alone until they're actually stale.

Two real-world-scale traps to avoid, both already hit and fixed elsewhere in
this app: (1) one DB connection per item times out once a library has
hundreds/thousands of rows — everything here writes through vod_db's bulk_*
functions, a single transaction for the whole batch. (2) fetching each show's
episode list one at a time, sequentially, is slow purely on round-trip time —
those fetches run at bounded concurrency instead, same pattern as
vod_importer.bulk_enrich_all.
"""

import asyncio
import logging
import time

import plex_client
import vod_db

logger = logging.getLogger(__name__)

_EPISODE_FETCH_CONCURRENCY = 4


def _poster_url(provider: dict, thumb: str | None) -> str | None:
    if not thumb:
        return None
    base_url = provider["base_url"].rstrip("/")
    return f"{base_url}{thumb}?X-Plex-Token={provider['password']}"


async def _fetch_show_episodes(
    client: plex_client.PlexClient, sem: asyncio.Semaphore, show: dict, idx: int, total: int,
) -> list[dict]:
    rating_key = show.get("ratingKey")
    title = show.get("title", "?")
    async with sem:
        t0 = time.monotonic()
        logger.info("[plex_importer] fetching episodes %d/%d: %s", idx, total, title)
        try:
            raw_episodes = await client.list_episodes(str(rating_key))
        except Exception as exc:
            logger.warning("[plex_importer] episode fetch failed for show=%s: %s", title, exc)
            return []
        logger.info("[plex_importer] fetched episodes %d/%d: %s (%.1fs, %d episodes)",
                     idx, total, title, time.monotonic() - t0, len(raw_episodes))

    episodes = []
    for ep in raw_episodes:
        part_key, container = plex_client.extract_part(ep)
        if not part_key:
            continue
        episodes.append({
            "season_number": int(ep.get("parentIndex") or 0),
            "episode_number": int(ep.get("index") or 0),
            "name": ep.get("title") or f"Episode {ep.get('index', '?')}",
            "description": ep.get("summary") or None,
            "duration_secs": int(ep["duration"] / 1000) if ep.get("duration") else None,
            "provider_stream_id": part_key,
            "container_extension": container,
            "plex_rating_key": ep.get("ratingKey"),
        })
    return episodes


async def import_plex_library(provider_id: int) -> dict:
    provider = await asyncio.to_thread(vod_db.get_provider, provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")

    now = str(time.time())

    movie_result = {"movies_created": 0, "movies_matched": 0, "total": 0}
    series_result = {"series_created": 0, "series_matched": 0, "episodes_imported": 0}

    async with plex_client.PlexClient(provider) as client:
        libraries = await client.list_libraries()
        for section in libraries:
            section_type = section.get("type")
            section_key = section.get("key")
            if section_type not in ("movie", "show") or not section_key:
                continue

            if section_type == "movie":
                raw_movies = await client.list_movies(section_key)
                movie_items = []
                for item in raw_movies:
                    part_key, container = plex_client.extract_part(item)
                    if not part_key:
                        continue
                    fields = plex_client.extract_common_fields(item)
                    movie_items.append({
                        "name": item.get("title", ""),
                        "year": item.get("year"),
                        "provider_stream_id": part_key,
                        "container_extension": container,
                        "plex_rating_key": item.get("ratingKey"),
                        "genre": fields["genre"],
                        "description": fields["description"],
                        "director": fields["director"],
                        "cast_list": fields["cast_list"],
                        "poster_url": _poster_url(provider, item.get("thumb")),
                        "last_enriched_at": now,
                    })
                r = await asyncio.to_thread(vod_db.bulk_import_plex_movies, provider_id, movie_items)
                for k in movie_result:
                    movie_result[k] += r.get(k, 0)

            else:  # show
                shows = await client.list_shows(section_key)
                logger.info("[plex_importer] section=%s: %d shows, fetching episodes at concurrency=%d",
                            section.get("title"), len(shows), _EPISODE_FETCH_CONCURRENCY)
                sem = asyncio.Semaphore(_EPISODE_FETCH_CONCURRENCY)
                all_episodes = await asyncio.gather(*(
                    _fetch_show_episodes(client, sem, s, i + 1, len(shows)) for i, s in enumerate(shows)
                ))

                series_items = []
                for show, episodes in zip(shows, all_episodes):
                    rating_key = show.get("ratingKey")
                    if not rating_key:
                        continue
                    fields = plex_client.extract_common_fields(show)
                    series_items.append({
                        "name": show.get("title", ""),
                        "year": show.get("year"),
                        "provider_series_id": str(rating_key),
                        "genre": fields["genre"],
                        "description": fields["description"],
                        "director": fields["director"],
                        "cast_list": fields["cast_list"],
                        "poster_url": _poster_url(provider, show.get("thumb")),
                        "last_enriched_at": now,
                        "episodes": episodes,
                    })
                r = await asyncio.to_thread(vod_db.bulk_import_plex_series, provider_id, series_items)
                for k in series_result:
                    series_result[k] += r.get(k, 0)

    result = {
        "provider": provider["name"],
        "movies_created": movie_result["movies_created"], "movies_matched": movie_result["movies_matched"],
        "series_created": series_result["series_created"], "series_matched": series_result["series_matched"],
        "episodes_imported": series_result["episodes_imported"],
    }
    logger.info("[plex_importer] provider=%s result=%s", provider["name"], result)
    return result

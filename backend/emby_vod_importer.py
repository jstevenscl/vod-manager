"""
Imports a user's own Emby or Jellyfin library into the VOD pool — the
Emby/Jellyfin-provider counterpart to plex_importer.py.

Single-phase like Plex: the /emby/Items listing with Fields= already returns
full detail (genre, overview, cast, year) in one call, so no separate
enrichment step is needed at import time. Writes go through vod_db's
bulk_import_plex_movies/bulk_import_plex_series — those functions are
provider-agnostic despite the name (plex_rating_key is simply left unset for
Emby/Jellyfin items), so there's no need for a parallel set of DB helpers.
"""

import asyncio
import logging
import time

import emby_vod_client
import vod_db

logger = logging.getLogger(__name__)

_EPISODE_FETCH_CONCURRENCY = 4


async def _fetch_series_episodes(
    client: emby_vod_client.EmbyVodClient, sem: asyncio.Semaphore, series: dict, idx: int, total: int,
) -> list[dict]:
    series_id = series.get("Id")
    name = series.get("Name", "?")
    async with sem:
        t0 = time.monotonic()
        logger.info("[emby_vod_importer] fetching episodes %d/%d: %s", idx, total, name)
        try:
            raw_episodes = await client.list_episodes(series_id)
        except Exception as exc:
            logger.warning("[emby_vod_importer] episode fetch failed for series=%s: %s", name, exc)
            return []
        logger.info("[emby_vod_importer] fetched episodes %d/%d: %s (%.1fs, %d episodes)",
                     idx, total, name, time.monotonic() - t0, len(raw_episodes))

    episodes = []
    for ep in raw_episodes:
        stream_id, container = emby_vod_client.extract_stream_id(ep)
        if not stream_id:
            continue
        episodes.append({
            "season_number": int(ep.get("ParentIndexNumber") or 0),
            "episode_number": int(ep.get("IndexNumber") or 0),
            "name": ep.get("Name") or f"Episode {ep.get('IndexNumber', '?')}",
            "description": ep.get("Overview") or None,
            "duration_secs": int(ep["RunTimeTicks"] / 10_000_000) if ep.get("RunTimeTicks") else None,
            "provider_stream_id": stream_id,
            "container_extension": container,
        })
    return episodes


async def import_emby_library(provider_id: int) -> dict:
    provider = await asyncio.to_thread(vod_db.get_provider, provider_id)
    if not provider:
        raise ValueError(f"provider {provider_id} not found")

    now = str(time.time())

    movie_result = {"movies_created": 0, "movies_matched": 0, "total": 0}
    series_result = {"series_created": 0, "series_matched": 0, "episodes_imported": 0}

    async with emby_vod_client.EmbyVodClient(provider) as client:
        libraries = await client.list_libraries()
        for lib in libraries:
            collection_type = lib.get("CollectionType")
            library_id = lib.get("ItemId")
            if not library_id or collection_type not in ("movies", "tvshows"):
                continue

            if collection_type == "movies":
                raw_movies = await client.list_movies(library_id)
                movie_items = []
                for item in raw_movies:
                    stream_id, container = emby_vod_client.extract_stream_id(item)
                    if not stream_id:
                        continue
                    fields = emby_vod_client.extract_common_fields(item)
                    movie_items.append({
                        "name": item.get("Name", ""),
                        "year": item.get("ProductionYear"),
                        "provider_stream_id": stream_id,
                        "container_extension": container,
                        "genre": fields["genre"],
                        "description": fields["description"],
                        "director": fields["director"],
                        "cast_list": fields["cast_list"],
                        "poster_url": emby_vod_client.build_poster_url(provider, item.get("Id")),
                        "last_enriched_at": now,
                    })
                r = await asyncio.to_thread(vod_db.bulk_import_plex_movies, provider_id, movie_items)
                for k in movie_result:
                    movie_result[k] += r.get(k, 0)

            else:  # tvshows
                series = await client.list_series(library_id)
                logger.info("[emby_vod_importer] library=%s: %d series, fetching episodes at concurrency=%d",
                            lib.get("Name"), len(series), _EPISODE_FETCH_CONCURRENCY)
                sem = asyncio.Semaphore(_EPISODE_FETCH_CONCURRENCY)
                all_episodes = await asyncio.gather(*(
                    _fetch_series_episodes(client, sem, s, i + 1, len(series)) for i, s in enumerate(series)
                ))

                series_items = []
                for show, episodes in zip(series, all_episodes):
                    series_id = show.get("Id")
                    if not series_id:
                        continue
                    fields = emby_vod_client.extract_common_fields(show)
                    series_items.append({
                        "name": show.get("Name", ""),
                        "year": show.get("ProductionYear"),
                        "provider_series_id": str(series_id),
                        "genre": fields["genre"],
                        "description": fields["description"],
                        "director": fields["director"],
                        "cast_list": fields["cast_list"],
                        "poster_url": emby_vod_client.build_poster_url(provider, series_id),
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
    logger.info("[emby_vod_importer] provider=%s result=%s", provider["name"], result)
    return result

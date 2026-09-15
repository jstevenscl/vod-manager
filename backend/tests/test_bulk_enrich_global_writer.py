"""bulk_enrich_all's per-item writes (movie fields+bitrate, series episodes)
used to go straight to vod_db via independent asyncio.to_thread calls, each
grabbing vod_db._WRITE_LOCK on its own -- with concurrency=8 for movies AND
series (up to 16 threads), plus the periodic smart-category resweep and any
concurrent manual /enrich/ call, this produced real "database is locked"
errors, live (2026-09-15) -- one of which surfaced as an uncaught 500 on the
manual enrich-series endpoint.

Fix: one run-wide asyncio.Queue + one background writer task
(vod_importer._run_global_writer) that is the ONLY thing calling
vod_db.apply_movie_enrichment_batch / vod_db.enrich_series_episodes_batch
during a bulk_enrich_all run. These tests prove the batch write functions
persist correctly, and that the writer task actually serializes writes from
concurrent producers instead of letting them race."""

import asyncio
import threading

import vod_db
import vod_importer


def test_apply_movie_enrichment_batch_writes_fields_and_bitrate(db):
    movie_id = db.upsert_movie("Test Movie", 2020)
    provider_id = db.upsert_provider("P1", "http://x", "u", "p", provider_type="xc")
    db.add_movie_source(movie_id, provider_id, "s1")
    source = db.list_movie_sources(movie_id)[0]

    db.apply_movie_enrichment_batch([
        {"movie_id": movie_id, "fields": {"genre": "Action", "tmdb_id": "123"},
         "source_id": source["id"], "bitrate": 4500},
    ])

    movie = db.get_movie(movie_id)
    assert movie["genre"] == "Action"
    assert movie["tmdb_id"] == "123"
    assert movie["last_enriched_at"] is not None
    refreshed_source = db.list_movie_sources(movie_id)[0]
    assert refreshed_source["bitrate"] == 4500


def test_apply_movie_enrichment_batch_one_bad_item_does_not_lose_the_rest(db):
    movie_id = db.upsert_movie("Good Movie", 2021)
    db.apply_movie_enrichment_batch([
        {"movie_id": 999999, "fields": {"genre": "Bogus"}, "source_id": None, "bitrate": None},
        {"movie_id": movie_id, "fields": {"genre": "Comedy"}, "source_id": None, "bitrate": None},
    ])
    assert db.get_movie(movie_id)["genre"] == "Comedy"


def test_enrich_series_episodes_batch_writes_multiple_episodes(db):
    series_id = db.upsert_series("Test Series", 2019)
    provider_id = db.upsert_provider("P1", "http://x", "u", "p", provider_type="xc")

    db.enrich_series_episodes_batch(series_id, provider_id, [
        {"season_number": 1, "episode_number": 1, "name": "Pilot", "provider_stream_id": "e1"},
        {"season_number": 1, "episode_number": 2, "name": "Ep 2", "provider_stream_id": "e2", "bitrate": 3000},
    ])

    episodes = db.list_episodes(series_id)
    assert len(episodes) == 2
    names = {e["name"] for e in episodes}
    assert names == {"Pilot", "Ep 2"}


def test_global_writer_serializes_concurrent_producers(monkeypatch):
    """Two "producers" put() movie batches onto the queue concurrently --
    the writer task draining it must never let two writes overlap, proving
    bulk_enrich_all's writes are truly serialized through one task."""
    in_writer = threading.Event()
    concurrent_write_detected = threading.Event()
    written_ids: list[int] = []

    def fake_apply_batch(items):
        if in_writer.is_set():
            concurrent_write_detected.set()
        in_writer.set()
        try:
            written_ids.extend(i["movie_id"] for i in items)
        finally:
            in_writer.clear()

    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", fake_apply_batch)

    async def run():
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        writer_task = asyncio.create_task(vod_importer._run_global_writer(queue))

        async def produce(start):
            for i in range(start, start + 25):
                done = asyncio.Event()
                await queue.put({"kind": "movie", "items": [{"movie_id": i, "fields": {}, "source_id": None, "bitrate": None}], "done": done})
                await done.wait()

        await asyncio.gather(produce(1), produce(100))
        await queue.put(None)
        await writer_task

    asyncio.run(asyncio.wait_for(run(), timeout=10))

    assert set(written_ids) == set(range(1, 26)) | set(range(100, 125))
    assert not concurrent_write_detected.is_set()

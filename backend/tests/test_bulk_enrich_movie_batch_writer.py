"""beads-ds8: enrich_movie's writes (set_movie_enrichment + optional
set_movie_source_bitrate) are still per-item, per-call, each independently
acquiring vod_db._WRITE_LOCK, opening a connection, and committing -- unlike
series, which beads-3po already fixed (see enrich_series_episodes_batch's
docstring: 20-episode series went from up to 40 separately-locked/fsynced
writes down to one connection/lock/commit per series). Under
bulk_enrich_all's 8-way concurrent movie phase this is the same class of lock
contention, just at movie-count scale instead of episode-count scale.

Two things are exercised here:
  1. vod_db.apply_movie_enrichment_batch -- the new DB-layer writer. One
     connection/lock/commit for a whole chunk of movies' enrichment fields
     (mirroring enrich_series_episodes_batch's _item_savepoint-per-item
     pattern), not one per movie.
  2. vod_importer's bulk movie phase actually USING the batch writer instead
     of calling vod_db.set_movie_enrichment per item when running under
     bulk_enrich_all (skip_auto_merge=True bulk path) -- a single/on-demand
     enrich_movie call outside bulk_enrich_all keeps writing inline
     immediately, matching enrich_series's unchanged single-item path."""

import asyncio

import pytest

import vod_importer


def _provider(pid, name):
    return {"id": pid, "name": name, "provider_type": "xc"}


# ── vod_db.apply_movie_enrichment_batch ─────────────────────────────────────

def test_apply_movie_enrichment_batch_writes_all_items_in_one_call(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "a-1", "container_extension": "mp4"},
        {"name": "Movie B", "year": 2021, "provider_stream_id": "b-1", "container_extension": "mp4"},
    ])
    movie_a = db.get_movie_by_name_year("Movie A", 2020)
    movie_b = db.get_movie_by_name_year("Movie B", 2021)
    source_a = db.list_movie_sources(movie_a["id"])[0]
    source_b = db.list_movie_sources(movie_b["id"])[0]

    db.apply_movie_enrichment_batch([
        {"movie_id": movie_a["id"], "fields": {"genre": "Action", "tmdb_id": "111"},
         "source_id": source_a["id"], "bitrate": 4000},
        {"movie_id": movie_b["id"], "fields": {"genre": "Comedy", "tmdb_id": "222"},
         "source_id": source_b["id"], "bitrate": 5000},
    ])

    refreshed_a = db.get_movie(movie_a["id"])
    refreshed_b = db.get_movie(movie_b["id"])
    assert refreshed_a["genre"] == "Action"
    assert refreshed_a["tmdb_id"] == "111"
    assert refreshed_a["last_enriched_at"] is not None
    assert refreshed_b["genre"] == "Comedy"
    assert refreshed_b["tmdb_id"] == "222"

    refreshed_source_a = db.list_movie_sources(movie_a["id"])[0]
    refreshed_source_b = db.list_movie_sources(movie_b["id"])[0]
    assert refreshed_source_a["bitrate"] == 4000
    assert refreshed_source_b["bitrate"] == 5000


def test_apply_movie_enrichment_batch_one_bad_item_does_not_lose_the_rest(db):
    """Same isolation guarantee as enrich_series_episodes_batch's
    _item_savepoint-per-episode: one malformed item in the batch (here, a
    movie_id that doesn't exist -- the UPDATE affects 0 rows, but a caller
    could also pass a field name that isn't a real column, raising) must not
    roll back or block the other, valid items in the same batch call."""
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "a-1", "container_extension": "mp4"},
    ])
    movie_a = db.get_movie_by_name_year("Movie A", 2020)

    db.apply_movie_enrichment_batch([
        {"movie_id": movie_a["id"], "fields": {"genre": "Action"}, "source_id": None, "bitrate": None},
        {"movie_id": movie_a["id"], "fields": {"not_a_real_column": "boom"}, "source_id": None, "bitrate": None},
    ])

    refreshed_a = db.get_movie(movie_a["id"])
    assert refreshed_a["genre"] == "Action"


def test_apply_movie_enrichment_batch_empty_list_is_a_noop(db):
    db.apply_movie_enrichment_batch([])  # must not raise


# ── bulk_enrich_all's movie phase uses the batch writer ─────────────────────

def test_bulk_movie_phase_calls_batch_writer_not_per_item_set_movie_enrichment(monkeypatch):
    """The whole point: under bulk_enrich_all, per-item vod_db.
    set_movie_enrichment must NOT be called at all -- writes must go through
    apply_movie_enrichment_batch instead, exactly once per chunk."""
    batch_calls = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        return {"movie_id": movie_id, "fields": {"genre": "Action"}, "source_id": None, "bitrate": None}

    def fake_set_movie_enrichment(movie_id, **fields):
        raise AssertionError("set_movie_enrichment must not be called per-item during bulk_enrich_all")

    def fake_apply_batch(items):
        batch_calls.append(list(items))

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer.vod_db, "set_movie_enrichment", fake_set_movie_enrichment)
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", fake_apply_batch)
    monkeypatch.setattr(vod_importer, "enrich_series", lambda *a, **kw: {"fetched": False, "reason": None})
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvA")])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: [10, 11, 12])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: [])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    async def fake_enrich_series(*a, **kw):
        return {"fetched": False, "reason": None}
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=5))

    all_written_ids = {item["movie_id"] for call in batch_calls for item in call}
    assert all_written_ids == {10, 11, 12}
    assert len(batch_calls) >= 1


def test_bulk_movie_phase_batches_in_chunks_of_250(monkeypatch):
    """Uses bounded chunks while avoiding per-movie SQLite commits.

    A large provider movie phase must split into multiple bounded-size
    transactions, not one single giant transaction for the whole provider
    (which would be just as bad a neighbor to concurrent readers/writers as
    the old one-connection-per-movie design was good, per _commit_with_retry's
    docstring reasoning) and not stay one-write-per-movie either."""
    batch_calls = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        return {"movie_id": movie_id, "fields": {"genre": "Action"}, "source_id": None, "bitrate": None}

    def fake_apply_batch(items):
        batch_calls.append(list(items))

    async def fake_enrich_series(*a, **kw):
        return {"fetched": False, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", fake_apply_batch)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvA")])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: list(range(1, 601)))
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: [])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=5))

    assert len(batch_calls) >= 3, "600 movies at a 250-item chunk size should need at least 3 batch calls"
    for call in batch_calls[:-1]:
        assert len(call) <= 250
    all_written_ids = {item["movie_id"] for call in batch_calls for item in call}
    assert all_written_ids == set(range(1, 601))


def test_bulk_movie_phase_merge_sweep_only_runs_after_batch_writer_flushes(monkeypatch):
    """auto_merge_movie_by_tmdb reads movies.tmdb_id back from the DB -- if
    the end-of-phase merge sweep ran before the batch writer actually
    committed a movie's tmdb_id, the merge would see stale/missing data.
    Order must be: all movie payloads collected -> batch write(s) flushed ->
    THEN the merge sweep."""
    events = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        return {"movie_id": movie_id, "fields": {"tmdb_id": str(movie_id)}, "source_id": None, "bitrate": None}

    def fake_apply_batch(items):
        events.append(("batch_write", tuple(i["movie_id"] for i in items)))

    def fake_auto_merge_movie(movie_id):
        events.append(("merge", movie_id))

    async def fake_enrich_series(*a, **kw):
        return {"fetched": False, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", fake_apply_batch)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvA")])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: [10, 11])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: [])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", fake_auto_merge_movie)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=5))

    last_batch_write_index = max(i for i, e in enumerate(events) if e[0] == "batch_write")
    first_merge_index = min(i for i, e in enumerate(events) if e[0] == "merge")
    assert last_batch_write_index < first_merge_index


def test_single_on_demand_enrich_movie_still_writes_inline(monkeypatch, db):
    """A single/on-demand enrich_movie call (outside bulk_enrich_all -- e.g.
    the UI's "re-enrich this one movie" button) must keep writing
    immediately via set_movie_enrichment, unchanged -- no batching benefit
    for exactly one item, and no caller of the single-item path expects a
    queue/flush step."""
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass", provider_type="plex")
    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "a-1", "container_extension": "mp4"},
    ])
    movie_a = db.get_movie_by_name_year("Movie A", 2020)

    monkeypatch.setattr(vod_importer, "vod_db", db)

    result = asyncio.run(vod_importer.enrich_movie(movie_a["id"]))

    assert result is True
    refreshed = db.get_movie(movie_a["id"])
    assert refreshed["last_enriched_at"] is not None

"""Plan-doc follow-up (2026-09-14): "final SQLite contention work" / one
global writer. Per-series and per-25-movie batching (beads-3po, beads-ds8)
substantially cut SQLite lock contention, but each provider lane still
flushes its own batches independently -- _run_provider_movie_phase calls
vod_db.apply_movie_enrichment_batch directly, and each series source write
calls vod_db.enrich_series_episodes_batch directly. Under bulk_enrich_all's
per-provider-phase design (beads-f7e/beads-sw9), multiple providers' movie
(or series) phases run concurrently via asyncio.gather -- so two providers'
batch-commit points CAN land on SQLite at the same instant, still not "one
global writer for the whole enrichment run."

Fix: bulk_enrich_all owns one asyncio.Queue + one background writer task for
its whole run. Every batch payload (movie chunk or series-episode write),
from every concurrently-running provider lane, is put() onto that queue
instead of being flushed inline by the lane that produced it. The single
writer task drains the queue and is the only thing that ever calls
apply_movie_enrichment_batch / enrich_series_episodes_batch -- so no two
writes are ever in flight at the same time, regardless of how many provider
lanes are enriching concurrently. A single/on-demand enrich_movie or
enrich_series call (outside bulk_enrich_all) keeps writing inline, unchanged
-- there's no run-level queue/writer outside a bulk_enrich_all call."""

import asyncio
import threading

import pytest

import vod_importer


def _provider(pid, name):
    return {"id": pid, "name": name, "provider_type": "xc"}


def test_movie_batch_writes_from_two_providers_never_run_concurrently(monkeypatch):
    """Two providers' movie phases run concurrently (that's the whole point
    of the per-provider-lane redesign) -- but their apply_movie_enrichment_batch
    calls must never overlap in time. Proven with a shared re-entrancy guard:
    if the "writer" is ever entered while already inside a call, that's two
    lanes writing to SQLite at once -- exactly what "one global writer" rules
    out."""
    in_writer = threading.Event()
    concurrent_write_detected = threading.Event()
    write_calls = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        await asyncio.sleep(0.01)
        return {"movie_id": movie_id, "fields": {"genre": "Action"}, "source_id": None, "bitrate": None}

    async def fake_enrich_series(*a, **kw):
        return {"fetched": False, "reason": None}

    def fake_apply_batch(items):
        if in_writer.is_set():
            concurrent_write_detected.set()
        in_writer.set()
        try:
            write_calls.append([i["movie_id"] for i in items])
        finally:
            in_writer.clear()

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", fake_apply_batch)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvA"), _provider(2, "ProvB")])
    monkeypatch.setattr(
        vod_importer.vod_db, "list_all_movie_ids",
        lambda provider_id=None, **kw: list(range(1, 51)) if provider_id == 1 else list(range(51, 101)),
    )
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: [])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=10))

    all_written_ids = {mid for call in write_calls for mid in call}
    assert all_written_ids == set(range(1, 101))
    assert not concurrent_write_detected.is_set(), (
        "two provider movie phases flushed apply_movie_enrichment_batch "
        "concurrently -- writes to SQLite must be serialized through one "
        "global writer for the whole bulk_enrich_all run"
    )


def test_bulk_enrich_all_uses_exactly_one_writer_task_for_the_whole_run(monkeypatch):
    """More direct than the timing-based test above: bulk_enrich_all should
    expose (or internally construct) exactly one queue-backed writer
    coroutine per run, not one per provider phase. Verified by spying on
    vod_importer._run_global_writer (the queue-draining task this fix adds)
    and asserting it is only ever started once per bulk_enrich_all call,
    regardless of how many providers/phases run."""
    writer_starts = []
    orig = getattr(vod_importer, "_run_global_writer", None)
    assert orig is not None, "bulk_enrich_all must own a single _run_global_writer coroutine function"

    async def spy_writer(*args, **kwargs):
        writer_starts.append(1)
        return await orig(*args, **kwargs)

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        return {"movie_id": movie_id, "fields": {"genre": "Action"}, "source_id": None, "bitrate": None}

    async def fake_enrich_series(*a, **kw):
        return {"fetched": False, "reason": None}

    monkeypatch.setattr(vod_importer, "_run_global_writer", spy_writer)
    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: None)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider(1, "ProvA"), _provider(2, "ProvB")])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: [10])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: [])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=10))

    assert len(writer_starts) == 1, "exactly one global writer task must run per bulk_enrich_all call"

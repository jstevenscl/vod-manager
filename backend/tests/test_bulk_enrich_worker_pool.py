"""CPU-spike follow-up (2026-09-14 session, user-supplied analysis): both
_run_provider_movie_phase and _run_provider_series_phase created one
asyncio.Task (movie phase: explicit create_task list comprehension; series
phase: asyncio.gather over a full generator, which materializes every
coroutine before any of them run) for EVERY item in a provider's catalog up
front, even though the semaphore inside _enrich_one only ever lets
`concurrency` of them actually run at once. Against the real catalog
(tens of thousands of movies/series per provider) this meant tens of
thousands of live Task/coroutine objects queued in the event loop
simultaneously -- real scheduler/memory/GC overhead for no added
parallelism, since the cap was always `concurrency` regardless.

Fix: a bounded worker-pool -- a fixed number of long-lived worker
coroutines (== concurrency) pull one item id at a time from a plain
asyncio.Queue and enrich it, instead of one Task being created per item.
Total throughput/concurrency is unchanged (still capped at `concurrency`
items in flight); only the up-front Task/coroutine pile-up goes away.

These tests prove the number of *live* Task objects tracking enrichment
work never exceeds `concurrency`, even when the id list is far larger than
that -- which the old create-everything-up-front pattern could not
guarantee (it created len(ids) tasks immediately, regardless of the
semaphore)."""

import asyncio

import pytest

import vod_importer


def _provider(pid, name):
    return {"id": pid, "name": name, "provider_type": "xc"}


def test_movie_phase_never_has_more_live_tasks_than_concurrency(monkeypatch):
    concurrency = 4
    total_items = 50
    max_observed_live = 0
    in_flight = 0

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        nonlocal in_flight, max_observed_live
        in_flight += 1
        max_observed_live = max(max_observed_live, in_flight)
        try:
            await asyncio.sleep(0.01)
        finally:
            in_flight -= 1
        return {"movie_id": movie_id, "fields": {}, "source_id": None, "bitrate": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)

    sem = asyncio.Semaphore(concurrency)
    provider = _provider(1, "ProvA")
    movie_ids = list(range(1, total_items + 1))
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: movie_ids)
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: None)

    asyncio.run(asyncio.wait_for(
        vod_importer._run_provider_movie_phase(provider, sem, force=False), timeout=10,
    ))

    assert max_observed_live <= concurrency, (
        f"observed {max_observed_live} concurrently in-flight movie enrichments, "
        f"expected at most {concurrency} -- the semaphore cap should equal the "
        f"actual number of live workers, not just gate a pile of pre-created tasks"
    )


def test_movie_phase_does_not_create_all_tasks_up_front(monkeypatch):
    """Distinguishes the worker-pool fix from the old pattern more directly:
    count how many asyncio.Task objects are alive shortly after the phase
    starts, before any item could plausibly have finished. The old
    create_task-per-id list comprehension creates len(movie_ids) tasks
    before awaiting even the first one; a worker pool creates only
    `concurrency` long-lived worker tasks regardless of len(movie_ids)."""
    concurrency = 3
    total_items = 200
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        started.set()
        await release.wait()
        return {"movie_id": movie_id, "fields": {}, "source_id": None, "bitrate": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)

    sem = asyncio.Semaphore(concurrency)
    provider = _provider(1, "ProvA")
    movie_ids = list(range(1, total_items + 1))
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: movie_ids)
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: None)

    async def _run():
        tasks_before = asyncio.all_tasks()
        phase_task = asyncio.create_task(vod_importer._run_provider_movie_phase(provider, sem, force=False))
        await asyncio.wait_for(started.wait(), timeout=5)
        # Give the event loop a moment to have created whatever it's going to create.
        await asyncio.sleep(0.05)
        tasks_during = asyncio.all_tasks() - tasks_before - {phase_task}
        release.set()
        await asyncio.wait_for(phase_task, timeout=10)
        return len(tasks_during)

    live_task_count = asyncio.run(asyncio.wait_for(_run(), timeout=15))

    assert live_task_count <= concurrency + 2, (
        f"observed {live_task_count} live tasks while only {concurrency} items should be "
        f"in flight out of {total_items} total -- tasks must not be created for every "
        f"item up front"
    )


def test_series_phase_never_has_more_live_tasks_than_concurrency(monkeypatch):
    concurrency = 4
    total_items = 50
    max_observed_live = 0
    in_flight = 0

    async def fake_enrich_series_source_only(series_id, provider_id, *, force=False, skip_auto_merge=False, write_queue=None):
        nonlocal in_flight, max_observed_live
        in_flight += 1
        max_observed_live = max(max_observed_live, in_flight)
        try:
            await asyncio.sleep(0.01)
        finally:
            in_flight -= 1
        return {"fetched": False, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", fake_enrich_series_source_only)

    sem = asyncio.Semaphore(concurrency)
    provider = _provider(1, "ProvA")
    series_ids = list(range(1, total_items + 1))
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: series_ids)

    asyncio.run(asyncio.wait_for(
        vod_importer._run_provider_series_phase(provider, sem, force=False), timeout=10,
    ))

    assert max_observed_live <= concurrency, (
        f"observed {max_observed_live} concurrently in-flight series enrichments, "
        f"expected at most {concurrency}"
    )


def test_series_phase_does_not_create_all_tasks_up_front(monkeypatch):
    concurrency = 3
    total_items = 200
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_enrich_series_source_only(series_id, provider_id, *, force=False, skip_auto_merge=False, write_queue=None):
        started.set()
        await release.wait()
        return {"fetched": False, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", fake_enrich_series_source_only)

    sem = asyncio.Semaphore(concurrency)
    provider = _provider(1, "ProvA")
    series_ids = list(range(1, total_items + 1))
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: series_ids)

    async def _run():
        tasks_before = asyncio.all_tasks()
        phase_task = asyncio.create_task(vod_importer._run_provider_series_phase(provider, sem, force=False))
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(0.05)
        tasks_during = asyncio.all_tasks() - tasks_before - {phase_task}
        release.set()
        await asyncio.wait_for(phase_task, timeout=10)
        return len(tasks_during)

    live_task_count = asyncio.run(asyncio.wait_for(_run(), timeout=15))

    assert live_task_count <= concurrency + 2, (
        f"observed {live_task_count} live tasks while only {concurrency} items should be "
        f"in flight out of {total_items} total -- tasks must not be created for every "
        f"item up front"
    )


def test_movie_phase_all_ids_still_enriched_and_ok_semantics_preserved(monkeypatch):
    """Worker-pool refactor must not change existing outcome semantics:
    every movie id still gets enriched exactly once, and a single failing
    item still flips ok to False without aborting the rest of the phase."""
    concurrency = 4
    movie_ids = list(range(1, 21))
    seen = []

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        seen.append(movie_id)
        if movie_id == 7:
            raise RuntimeError("boom")
        return {"movie_id": movie_id, "fields": {}, "source_id": None, "bitrate": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)

    sem = asyncio.Semaphore(concurrency)
    provider = _provider(1, "ProvA")
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: movie_ids)
    written = []
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: written.extend(items))

    ok, returned_ids = asyncio.run(asyncio.wait_for(
        vod_importer._run_provider_movie_phase(provider, sem, force=False), timeout=10,
    ))

    assert sorted(seen) == movie_ids
    assert returned_ids == movie_ids
    assert ok is False
    assert {i["movie_id"] for i in written} == set(movie_ids) - {7}

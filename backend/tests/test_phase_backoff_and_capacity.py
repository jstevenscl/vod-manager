"""Provider phase ok-semantics and worker sizing (upstream PR #31 review).

- A ProviderBackoffError is a deliberate deferral, not a provider failure:
  the phase must still report ok=True so bulk_enrich_all does not schedule a
  full retry or skip that provider's series phase.
- worker_count must come from the semaphore's configured capacity, not its
  currently free permits -- other providers' phases sharing the semaphore can
  be holding permits at the moment this phase starts.
"""

import asyncio

import vod_importer


def _provider(pid=1, name="ProvA"):
    return {"id": pid, "name": name, "provider_type": "xc"}


def test_movie_phase_backoff_is_ok(monkeypatch):
    async def fake_enrich_movie(movie_id, **kw):
        raise vod_importer.ProviderBackoffError("backing off")

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: [1, 2, 3])
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: None)

    ok, ids = asyncio.run(vod_importer._run_provider_movie_phase(_provider(), asyncio.Semaphore(2), force=False))

    assert ok is True
    assert ids == [1, 2, 3]


def test_series_phase_backoff_is_ok(monkeypatch):
    async def fake_source_only(series_id, provider_id, **kw):
        raise vod_importer.ProviderBackoffError("backing off")

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", fake_source_only)
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: [10, 11])

    ok, _ids = asyncio.run(vod_importer._run_provider_series_phase(_provider(), asyncio.Semaphore(2), force=False))

    assert ok is True


def test_movie_phase_real_error_is_not_ok(monkeypatch):
    async def fake_enrich_movie(movie_id, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: [1])
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: None)

    ok, _ids = asyncio.run(vod_importer._run_provider_movie_phase(_provider(), asyncio.Semaphore(2), force=False))

    assert ok is False


def test_worker_count_uses_capacity_not_free_permits(monkeypatch):
    capacity = 4
    in_flight = 0
    max_in_flight = 0

    async def fake_enrich_movie(movie_id, **kw):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return None

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: list(range(40)))
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: None)

    async def _run():
        sem = vod_importer._CapacitySemaphore(capacity)
        # Another provider's phase is holding 3 permits when this one starts.
        for _ in range(3):
            await sem.acquire()
        phase = asyncio.create_task(vod_importer._run_provider_movie_phase(_provider(), sem, force=False))
        await asyncio.sleep(0.01)
        for _ in range(3):
            sem.release()
        return await asyncio.wait_for(phase, timeout=10)

    asyncio.run(_run())

    assert max_in_flight == capacity


def test_bulk_enrich_all_resets_both_done_id_sets(monkeypatch):
    """_ENRICH_DONE_IDS and _ENRICH_DONE_SOURCE_IDS must be cleared together
    at the start of every run, or a stale source key from an earlier episode
    run under-counts series_sources_done."""
    vod_importer._ENRICH_DONE_IDS["movie"].add(999)
    vod_importer._ENRICH_DONE_SOURCE_IDS.add(999)
    seen = {}

    async def fake_enrich_movie(movie_id, **kw):
        seen["source_ids"] = set(vod_importer._ENRICH_DONE_SOURCE_IDS)
        seen["movie_ids"] = set(vod_importer._ENRICH_DONE_IDS["movie"])
        return None

    async def fake_source_only(series_id, provider_id, **kw):
        return {"fetched": True, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series_source_only", fake_source_only)
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: [_provider()])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: [10])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: [])
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(vod_importer.bulk_enrich_all(concurrency=2))

    assert 999 not in seen["movie_ids"]
    assert 999 not in seen["source_ids"]

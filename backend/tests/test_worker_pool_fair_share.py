"""2026-09-14 follow-up to the worker-pool refactor (test_bulk_enrich_worker_pool.py):
worker_count in _run_provider_movie_phase/_run_provider_series_phase was computed as
`sem._value or 1` -- the semaphore's FULL un-acquired capacity -- even though that same
semaphore is shared across every provider running concurrently in bulk_enrich_all (one
movie_sem/series_sem created once, passed into every provider's
_run_provider_enrichment call via a single asyncio.gather over all providers).

With N providers running concurrently, each independently spun up sem._value (e.g. 8)
long-lived workers, so N*8 workers ended up contending for the same 8-slot semaphore.
Each provider's own workers loop straight back to re-acquire the semaphore the instant
they release it, so a provider whose workers win a run of slots tends to keep winning
them -- producing tight per-provider request bursts against that one provider's
endpoint, unlike the old per-item create_task/gather pattern where task-creation order
naturally interleaved slot wins across all providers. This can otherwise create
avoidable per-provider request bursts despite no change to the configured
concurrency value itself.

Fix: both phase functions take an optional `provider_count` (default 1, preserving
existing callers/tests that invoke a phase directly for one provider in isolation) and
divide sem._value by it (minimum 1 worker) instead of claiming the semaphore's full
capacity for themselves alone."""

import asyncio

import vod_importer


def _provider(pid, name):
    return {"id": pid, "name": name, "provider_type": "xc"}


def test_movie_phase_worker_count_divided_by_provider_count(monkeypatch):
    concurrency = 8
    provider_count = 4
    max_observed_workers = 0
    live_workers = 0

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        nonlocal live_workers, max_observed_workers
        live_workers += 1
        max_observed_workers = max(max_observed_workers, live_workers)
        try:
            await asyncio.sleep(0.02)
        finally:
            live_workers -= 1
        return {"movie_id": movie_id, "fields": {}, "source_id": None, "bitrate": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)

    sem = asyncio.Semaphore(concurrency)
    provider = _provider(1, "ProvA")
    movie_ids = list(range(1, 51))
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: movie_ids)
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: None)

    asyncio.run(asyncio.wait_for(
        vod_importer._run_provider_movie_phase(
            provider, sem, force=False, provider_count=provider_count,
        ), timeout=10,
    ))

    fair_share = max(1, concurrency // provider_count)
    assert max_observed_workers <= fair_share, (
        f"observed {max_observed_workers} concurrent workers for one provider, expected at "
        f"most {fair_share} (= {concurrency} shared semaphore slots / {provider_count} "
        f"concurrently-running providers) -- one provider's phase must not claim the "
        f"shared semaphore's full capacity for itself"
    )


def test_movie_phase_default_provider_count_is_one_unchanged(monkeypatch):
    """Existing direct-call tests (test_bulk_enrich_worker_pool.py) invoke a phase for a
    single provider with no provider_count -- must keep behaving exactly as before
    (worker_count == sem's full capacity) when the caller doesn't say otherwise."""
    concurrency = 4
    max_observed_workers = 0
    live_workers = 0

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        nonlocal live_workers, max_observed_workers
        live_workers += 1
        max_observed_workers = max(max_observed_workers, live_workers)
        try:
            await asyncio.sleep(0.01)
        finally:
            live_workers -= 1
        return {"movie_id": movie_id, "fields": {}, "source_id": None, "bitrate": None}

    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)

    sem = asyncio.Semaphore(concurrency)
    provider = _provider(1, "ProvA")
    movie_ids = list(range(1, 51))
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: movie_ids)
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: None)

    asyncio.run(asyncio.wait_for(
        vod_importer._run_provider_movie_phase(provider, sem, force=False), timeout=10,
    ))

    assert max_observed_workers == concurrency


def test_series_phase_worker_count_divided_by_provider_count(monkeypatch):
    concurrency = 9
    provider_count = 3
    max_observed_workers = 0
    live_workers = 0

    async def fake_enrich_series_source_only(series_id, provider_id, *, force=False, skip_auto_merge=False, write_queue=None):
        nonlocal live_workers, max_observed_workers
        live_workers += 1
        max_observed_workers = max(max_observed_workers, live_workers)
        try:
            await asyncio.sleep(0.02)
        finally:
            live_workers -= 1
        return {"fetched": False, "reason": None}

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", fake_enrich_series_source_only)

    sem = asyncio.Semaphore(concurrency)
    provider = _provider(1, "ProvA")
    series_ids = list(range(1, 51))
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: series_ids)

    asyncio.run(asyncio.wait_for(
        vod_importer._run_provider_series_phase(
            provider, sem, force=False, provider_count=provider_count,
        ), timeout=10,
    ))

    fair_share = max(1, concurrency // provider_count)
    assert max_observed_workers <= fair_share, (
        f"observed {max_observed_workers} concurrent workers for one provider, expected at "
        f"most {fair_share} (= {concurrency} shared semaphore slots / {provider_count} "
        f"concurrently-running providers)"
    )


def test_bulk_enrich_all_passes_provider_count_to_phases(monkeypatch):
    """End-to-end: bulk_enrich_all knows how many providers it's running
    concurrently and must pass that count down so each provider's phase
    divides the shared semaphore fairly instead of claiming it whole."""
    captured_counts = []
    orig_movie_phase = vod_importer._run_provider_movie_phase
    orig_series_phase = vod_importer._run_provider_series_phase

    async def spy_movie_phase(provider, sem, force, write_queue=None, provider_count=1, pending_only=False):
        captured_counts.append(("movie", provider_count))
        return await orig_movie_phase(
            provider, sem, force, write_queue=write_queue,
            provider_count=provider_count, pending_only=pending_only,
        )

    async def spy_series_phase(provider, sem, force, write_queue=None, provider_count=1):
        captured_counts.append(("series", provider_count))
        return await orig_series_phase(provider, sem, force, write_queue=write_queue, provider_count=provider_count)

    async def fake_enrich_movie(movie_id, *, force=False, skip_auto_merge=False, skip_write=False):
        return {"movie_id": movie_id, "fields": {}, "source_id": None, "bitrate": None}

    async def fake_enrich_series(*a, **kw):
        return {"fetched": False, "reason": None}

    monkeypatch.setattr(vod_importer, "_run_provider_movie_phase", spy_movie_phase)
    monkeypatch.setattr(vod_importer, "_run_provider_series_phase", spy_series_phase)
    monkeypatch.setattr(vod_importer, "enrich_movie", fake_enrich_movie)
    monkeypatch.setattr(vod_importer, "enrich_series", fake_enrich_series)
    monkeypatch.setattr(vod_importer.vod_db, "apply_movie_enrichment_batch", lambda items: None)
    providers = [_provider(1, "ProvA"), _provider(2, "ProvB"), _provider(3, "ProvC")]
    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: providers)
    monkeypatch.setattr(vod_importer.vod_db, "list_all_movie_ids", lambda **kw: [10])
    monkeypatch.setattr(vod_importer.vod_db, "list_all_series_ids", lambda **kw: [])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_movie_by_tmdb", lambda movie_id: None)
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb", lambda series_id: None)

    asyncio.run(asyncio.wait_for(vod_importer.bulk_enrich_all(concurrency=8), timeout=10))

    assert captured_counts, "expected _run_provider_movie_phase to have been called"
    assert all(count == len(providers) for _, count in captured_counts), (
        f"expected every phase call to receive provider_count={len(providers)}, got {captured_counts}"
    )

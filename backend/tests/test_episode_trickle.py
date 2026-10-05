"""Paced background episode trickle: a bounded batch per provider per tick,
one request at a time, spaced out, and stopped early when a provider looks
rate-limited or keeps failing."""

import asyncio

import pytest

import apply_exclusions_job
import config
import vod_importer


def test_trickle_settings_have_safe_defaults(db):
    settings = config.get_refresh_settings()
    assert settings["episode_trickle_batch"] == 100
    assert settings["episode_trickle_interval_seconds"] == 15 * 60
    assert settings["episode_trickle_spacing_seconds"] == 3


@pytest.fixture()
def trickle_env(monkeypatch):
    providers = [{"id": 1, "name": "Provider One", "is_active": True}]
    pending = [{"id": 100 + i, "series_id": 10 + i, "provider_id": 1} for i in range(10)]
    calls: list[int] = []
    sleeps: list[float] = []

    def list_pending(provider_id, limit=None, series_ids=None):
        rows = [p for p in pending if p["provider_id"] == provider_id]
        return rows[:limit] if limit else rows

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: providers)
    monkeypatch.setattr(vod_importer.vod_db, "list_pending_series_sources", list_pending)
    monkeypatch.setattr(vod_importer.vod_db, "get_series_episode_summaries", lambda ids: {})
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb_batch", lambda ids: [])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_tmdb_collisions", lambda ids=None: [])
    monkeypatch.setattr(vod_importer, "_trickle_sleep", fake_sleep)
    monkeypatch.setattr(vod_importer, "_provider_backoff_remaining", lambda provider_id: 0.0)
    monkeypatch.setitem(vod_importer._ENRICH_PROGRESS, "running", False)
    return {"calls": calls, "sleeps": sleeps}


def test_trickle_fetches_at_most_batch_per_provider_with_spacing(trickle_env, monkeypatch):
    async def fake_enrich(series_id, provider_id, **kwargs):
        trickle_env["calls"].append(kwargs["source_id"])
        return {}

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", fake_enrich)
    asyncio.run(vod_importer.run_episode_trickle_tick(batch_size=4, spacing_seconds=3))

    assert trickle_env["calls"] == [100, 101, 102, 103]
    assert trickle_env["sleeps"].count(3) == 3


def test_trickle_stops_provider_after_consecutive_failures(trickle_env, monkeypatch):
    async def failing(series_id, provider_id, **kwargs):
        trickle_env["calls"].append(kwargs["source_id"])
        raise RuntimeError("Server error '513'")

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", failing)
    asyncio.run(vod_importer.run_episode_trickle_tick(batch_size=10, spacing_seconds=0))

    assert len(trickle_env["calls"]) == 3


def test_trickle_stops_provider_immediately_on_backoff(trickle_env, monkeypatch):
    async def backing_off(series_id, provider_id, **kwargs):
        trickle_env["calls"].append(kwargs["source_id"])
        raise vod_importer.ProviderBackoffError("backing off")

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", backing_off)
    monkeypatch.setattr(vod_importer, "_provider_backoff_remaining", lambda provider_id: 30.0)
    asyncio.run(vod_importer.run_episode_trickle_tick(batch_size=10, spacing_seconds=0))

    assert len(trickle_env["calls"]) == 1


def test_background_tmdb_pass_leaves_episodes_to_the_trickle(monkeypatch):
    called = []

    async def fake_bulk(*args, **kwargs):
        called.append(kwargs)

    monkeypatch.setattr(vod_importer, "bulk_enrich_series_episodes", fake_bulk)
    monkeypatch.setattr(vod_importer.vod_db, "count_pending_series_sources", lambda: 50)
    monkeypatch.setattr(vod_importer.vod_db, "count_movies_pending_tmdb_enrichment", lambda: 0)
    monkeypatch.setattr(vod_importer.vod_db, "count_series_pending_tmdb_metadata_enrichment", lambda: 0)
    asyncio.run(vod_importer._background_tmdb_enrichment())

    assert called == []


def test_apply_rules_job_error_hides_provider_credentials(monkeypatch):
    provider = {"id": 1, "name": "Provider One", "is_active": True, "provider_type": "xc"}

    async def failing_import(provider_id, **_kwargs):
        raise RuntimeError(
            "Server error '513 <none>' for url "
            "'http://example.invalid/player_api.php?username=realuser&password=realpass&action=get_vod_categories'"
        )

    monkeypatch.setattr(apply_exclusions_job.vod_db, "list_providers", lambda: [provider])
    monkeypatch.setattr(apply_exclusions_job.vod_importer, "import_provider_catalog", failing_import)
    job_id = "redaction-test"
    apply_exclusions_job._jobs[job_id] = {"results": [], "completed": 0, "total": 0}
    asyncio.run(apply_exclusions_job._run_job(job_id))

    error = apply_exclusions_job._jobs[job_id]["results"][0]["error"]
    assert "realuser" not in error and "realpass" not in error
    assert "513" in error


def test_trickle_skips_tick_while_catalog_import_runs(trickle_env, monkeypatch):
    async def fake_enrich(series_id, provider_id, **kwargs):
        trickle_env["calls"].append(kwargs["source_id"])
        return {}

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", fake_enrich)

    async def run():
        async with vod_importer._XC_IMPORT_LOCK:
            return await vod_importer.run_episode_trickle_tick(batch_size=4, spacing_seconds=0)

    asyncio.run(run())
    assert trickle_env["calls"] == []


def test_trickle_lane_stops_when_catalog_import_starts(trickle_env, monkeypatch):
    lock_holder = {}

    async def fake_enrich(series_id, provider_id, **kwargs):
        trickle_env["calls"].append(kwargs["source_id"])
        if len(trickle_env["calls"]) == 2:
            await vod_importer._XC_IMPORT_LOCK.acquire()
            lock_holder["held"] = True
        return {}

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", fake_enrich)

    async def run():
        try:
            return await vod_importer.run_episode_trickle_tick(batch_size=10, spacing_seconds=0)
        finally:
            if lock_holder.get("held"):
                vod_importer._XC_IMPORT_LOCK.release()

    asyncio.run(run())
    assert trickle_env["calls"] == [100, 101]


def test_apply_rules_progress_never_exceeds_total_while_finalizing(monkeypatch):
    provider = {"id": 1, "name": "Provider One", "is_active": True, "provider_type": "xc"}
    seen = {}

    async def ok_import(provider_id):
        return {}

    async def resweep():
        job = apply_exclusions_job._jobs["finalizing-test"]
        seen.update(completed=job["completed"], total=job["total"],
                    current=job["current_provider"], phase=job.get("phase"))

    monkeypatch.setattr(apply_exclusions_job.vod_db, "list_providers", lambda: [provider])
    monkeypatch.setattr(apply_exclusions_job.vod_importer, "import_provider_catalog", ok_import)
    monkeypatch.setattr(apply_exclusions_job.vod_importer, "resweep_smart_categories", resweep)
    apply_exclusions_job._jobs["finalizing-test"] = {"results": [], "completed": 0, "total": 0, "current_provider": None}
    asyncio.run(apply_exclusions_job._run_job("finalizing-test"))

    assert seen == {"completed": 1, "total": 1, "current": None, "phase": "finalizing"}

"""Bounded episode-list preloading stays paced and yields to providers/imports."""

import asyncio
import time

import config
import vod_importer


_BASE = dict(
    catalog_refresh_seconds_xc=3600, catalog_refresh_seconds_plex=3600,
    catalog_refresh_seconds_emby=3600, catalog_refresh_seconds_jellyfin=3600,
    enrichment_ttl_seconds=3600, tmdb_sync_interval_seconds=None,
    smart_category_reconciliation_enabled=True,
)


def test_trickle_defaults_and_setting_bounds(db):
    settings = config.get_refresh_settings()
    assert settings["episode_trickle_batch"] == 100
    assert settings["episode_trickle_interval_seconds"] == 15 * 60
    assert settings["episode_trickle_spacing_seconds"] == 3

    config.save_refresh_settings(**_BASE, episode_trickle_batch=900,
                                 episode_trickle_interval_seconds=60,
                                 episode_trickle_spacing_seconds=0)
    settings = config.get_refresh_settings()
    assert settings["episode_trickle_batch"] == 500
    assert settings["episode_trickle_interval_seconds"] == 300
    assert settings["episode_trickle_spacing_seconds"] == 1


def test_pending_sources_are_limited_and_recent_failures_wait(db):
    provider_id = db.upsert_provider("Test", "http://example.invalid", "user", "pass")
    db.bulk_import_series(provider_id, [{
        "name": f"Show {i}", "year": 2020, "provider_series_id": str(i),
        "provider_category_name": None, "raw_name": f"Show {i}", "_has_detail": True,
        "genre": None, "description": None, "cast_list": None, "director": None,
        "poster_url": None, "rating": None, "release_date": None, "tmdb_id": None,
        "provider_last_modified": None,
    } for i in range(4)])
    rows = db.list_pending_series_sources(provider_id)
    failed_id = rows[0]["id"]
    row = db._connect()
    row.execute("UPDATE series_sources SET consecutive_failures=1, last_failed_at=? WHERE id=?", (str(time.time()), failed_id))
    row.commit()
    row.close()

    pending = db.list_pending_series_sources(provider_id, limit=2)
    assert len(pending) == 2
    assert failed_id not in {item["id"] for item in pending}

    row = db._connect()
    row.execute("UPDATE series_sources SET last_failed_at=? WHERE id=?", (str(time.time() - 25 * 3600), failed_id))
    row.commit()
    row.close()
    pending = db.list_pending_series_sources(provider_id)
    assert len(pending) == 4
    assert pending[-1]["id"] == failed_id


def _trickle_env(monkeypatch):
    providers = [
        {"id": 1, "name": "Provider One", "provider_type": "xc", "is_active": True},
        {"id": 2, "name": "Provider Two", "provider_type": "xc", "is_active": True},
    ]
    pending = {
        provider["id"]: [{"id": 100 * provider["id"] + i, "series_id": i, "provider_id": provider["id"]}
                         for i in range(6)]
        for provider in providers
    }
    calls, sleeps = [], []

    def list_pending(provider_id, limit=None):
        rows = pending[provider_id]
        return rows[:limit] if limit is not None else rows

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(vod_importer.vod_db, "list_providers", lambda: providers)
    monkeypatch.setattr(vod_importer.vod_db, "list_pending_series_sources", list_pending)
    monkeypatch.setattr(vod_importer, "_trickle_sleep", fake_sleep)
    monkeypatch.setattr(vod_importer, "_provider_backoff_remaining", lambda provider_id: 0)
    monkeypatch.setattr(vod_importer, "_ENRICH_PROGRESS", {"running": False})
    monkeypatch.setattr(vod_importer, "_EPISODE_TRICKLE_RUNNING", False)
    return calls, sleeps


def test_trickle_limits_each_provider_batch_and_spaces_requests(monkeypatch):
    calls, sleeps = _trickle_env(monkeypatch)

    async def enrich(series_id, provider_id, **kwargs):
        calls.append((provider_id, kwargs["source_id"]))
        return {"fetched": True}

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", enrich)
    result = asyncio.run(vod_importer.run_episode_trickle_tick(batch_size=4, spacing_seconds=3))

    assert len(result) == len(calls) == 8
    assert [provider_id for provider_id, _ in calls].count(1) == 4
    assert [provider_id for provider_id, _ in calls].count(2) == 4
    assert sleeps == [3, 3, 3, 3, 3, 3]


def test_trickle_stops_provider_after_three_failures(monkeypatch):
    calls, _ = _trickle_env(monkeypatch)

    async def fail(series_id, provider_id, **kwargs):
        calls.append((provider_id, kwargs["source_id"]))
        return {"fetched": False, "reason": "provider request failed"}

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", fail)
    asyncio.run(vod_importer.run_episode_trickle_tick(batch_size=10, spacing_seconds=0))

    assert len(calls) == 6
    assert [provider_id for provider_id, _ in calls].count(1) == 3
    assert [provider_id for provider_id, _ in calls].count(2) == 3


def test_trickle_stops_provider_when_backoff_is_raised(monkeypatch):
    calls, _ = _trickle_env(monkeypatch)

    async def backing_off(series_id, provider_id, **kwargs):
        calls.append((provider_id, kwargs["source_id"]))
        raise vod_importer.ProviderBackoffError("provider is backing off")

    monkeypatch.setattr(vod_importer, "enrich_series_source_only", backing_off)
    result = asyncio.run(vod_importer.run_episode_trickle_tick(batch_size=10, spacing_seconds=0))

    assert len(calls) == 2
    assert sum(bool(row.get("backoff")) for row in result) == 2

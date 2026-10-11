"""The local TMDB library is opt-in: an upgrade with only a TMDB key saved
must make no downloads and no TMDB requests. Once turned on, it keeps its ID
list current with TMDB's daily export (upstream review of the catalog-first
library)."""

import asyncio

import pytest

import config
import tmdb_fill
import tmdb_store


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(tmdb_store, "DB_PATH", tmp_path / "tmdb.sqlite")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "_raw_cache", None)
    monkeypatch.setattr(tmdb_fill, "get_tmdb_api_key", lambda: "k")
    tmdb_store.init_db()
    ran: list[str] = []

    def job(name):
        async def fake(*_a, **_k):
            ran.append(name)
            return {}
        return fake

    for name in ("import_exports", "refresh_changes", "run_burst", "retention"):
        monkeypatch.setattr(tmdb_fill, name, job(name))
    return ran


def test_library_is_off_by_default():
    assert config._TMDB_STORE_DEFAULTS["enabled"] is False


def test_fresh_install_with_key_does_nothing(fresh):
    assert asyncio.run(tmdb_fill.run_due_jobs()) is False
    assert fresh == []


def test_enabled_scheduler_downloads_the_export_once_a_day(fresh):
    config.save_tmdb_store_settings({"enabled": True})
    assert asyncio.run(tmdb_fill.run_due_jobs()) is True
    assert "import_exports" in fresh and "run_burst" in fresh
    tmdb_store.set_meta("exports_imported_at", tmdb_fill.time.time())
    fresh.clear()
    asyncio.run(tmdb_fill.run_due_jobs())
    assert "import_exports" not in fresh


def test_prefill_top_setting_removed(fresh):
    assert "prefill_top" not in config.get_tmdb_store_settings()
    assert "prefill_top" not in config.save_tmdb_store_settings({"prefill_top": 5})


def test_unused_prefill_candidate_query_removed():
    assert not hasattr(tmdb_store, "list_fill_candidates")

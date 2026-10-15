"""Popular-title fill, keep-until-cache-limit retention, and store compaction."""

import asyncio
import time

import config
import tmdb_fill
import tmdb_sync
from test_tmdb_fill import _fake_fetch, _seed_export, store  # noqa: F401
from test_tmdb_store import _movie, _tv


def test_popular_export_ids_returns_most_popular_first(store):
    _seed_export(store, "movie", [1, 2, 3])  # popularity 99, 98, 97
    assert store.popular_export_ids("movie", 2) == {1, 2}
    assert store.popular_export_ids("movie", 0) == set()


def test_burst_fills_catalog_first_then_popular_titles(store, monkeypatch):
    config.save_tmdb_store_settings({"popular_keep": 2})
    _seed_export(store, "movie", [10, 11, 12])
    monkeypatch.setattr(tmdb_fill.vod_db, "list_catalog_tmdb_ids", lambda: {"movie": {1}, "tv": set()})
    fetched = []
    monkeypatch.setattr(tmdb_sync, "fetch_title_payload", _fake_fetch(store, fetched))
    result = asyncio.run(tmdb_fill.run_burst(size=10))
    assert fetched == [("movie", 1), ("movie", 10), ("movie", 11)]
    assert result["fetched"] == 3
    assert result["popular"] == 2


def test_burst_skips_popular_titles_already_stored_or_in_catalog(store, monkeypatch):
    config.save_tmdb_store_settings({"popular_keep": 2})
    _seed_export(store, "movie", [10, 11])
    store.upsert_payload("movie", _movie(11))
    monkeypatch.setattr(tmdb_fill.vod_db, "list_catalog_tmdb_ids", lambda: {"movie": {10}, "tv": set()})
    fetched = []
    monkeypatch.setattr(tmdb_sync, "fetch_title_payload", _fake_fetch(store, fetched))
    result = asyncio.run(tmdb_fill.run_burst(size=10))
    assert fetched == [("movie", 10)]
    assert result["popular"] == 0


def test_burst_with_popular_keep_off_fills_catalog_only(store, monkeypatch):
    config.save_tmdb_store_settings({"popular_keep": 0})
    _seed_export(store, "movie", [10])
    monkeypatch.setattr(tmdb_fill.vod_db, "list_catalog_tmdb_ids", lambda: {"movie": {1}, "tv": set()})
    fetched = []
    monkeypatch.setattr(tmdb_sync, "fetch_title_payload", _fake_fetch(store, fetched))
    asyncio.run(tmdb_fill.run_burst(size=10))
    assert fetched == [("movie", 1)]


def test_retention_keeps_unused_titles_until_cache_age_limit(store):
    store.upsert_payload("movie", _movie(1))
    store.upsert_payload("movie", _movie(2))
    store.upsert_payload("tv", _tv(10))
    store.put_season(10, 1, [{"episode_number": 1}])
    store.mark_gone("movie", 404)
    old = time.time() - store.MAX_CACHE_AGE_SECONDS - 1
    with store._conn() as conn:
        conn.execute("UPDATE titles SET fetched_at=? WHERE media_type='movie' AND tmdb_id=2", (old,))

    result = store.retention_cleanup({"movie": set(), "tv": set()}, dry_run=False)

    assert result["titles_removed"] == 1
    assert store.get_title("movie", 1) is not None
    assert store.get_title("movie", 2) is None
    assert store.get_title("tv", 10) is not None
    assert store.get_season(10, 1) is not None
    assert result["gone_removed"] == 0


def test_vacuum_reclaims_space_after_large_delete(store):
    for i in range(1, 400):
        store.upsert_payload("movie", _movie(i, "x" * 2000))
    with store._conn() as conn:
        conn.execute("DELETE FROM titles")
    assert store.free_space_ratio() > 0.25
    assert store.vacuum_if_fragmented(0.25) is True
    assert store.free_space_ratio() == 0
    assert store.vacuum_if_fragmented(0.25) is False


def test_daily_retention_compacts_the_store(store, monkeypatch):
    for i in range(1, 400):
        store.upsert_payload("movie", _movie(i, "x" * 2000))
    old = time.time() - store.MAX_CACHE_AGE_SECONDS - 1
    with store._conn() as conn:
        conn.execute("UPDATE titles SET fetched_at=?", (old,))
    monkeypatch.setattr(tmdb_fill.vod_db, "list_catalog_tmdb_ids", lambda: {"movie": set(), "tv": set()})
    result = asyncio.run(tmdb_fill.retention(dry_run=False))
    assert result["vacuumed"] is True
    assert store.free_space_ratio() == 0

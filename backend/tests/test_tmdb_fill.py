"""Background fill: burst selection/budget, changes refresh, local-first lookup."""

import asyncio

import pytest

import config
import tmdb_fill
import tmdb_store
import tmdb_sync
from test_tmdb_store import _movie, _tv


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(tmdb_store, "DB_PATH", tmp_path / "tmdb.sqlite")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "_raw_cache", None)
    monkeypatch.setattr(tmdb_sync, "get_tmdb_api_key", lambda: "k")
    monkeypatch.setattr(tmdb_fill, "get_tmdb_api_key", lambda: "k")
    tmdb_store.init_db()
    return tmdb_store


def _seed_export(store, media_type, ids):
    with store._conn() as conn:
        conn.executemany(
            "INSERT INTO export_ids (media_type, tmdb_id, name, popularity) VALUES (?,?,?,?)",
            [(media_type, i, f"name {i}", 100.0 - i) for i in ids],
        )


def _fake_fetch(store, fetched):
    async def fake(media_type, tmdb_id, *, use_store=True):
        assert use_store is False
        fetched.append((media_type, tmdb_id))
        payload = _movie(tmdb_id, f"Movie {tmdb_id}") if media_type == "movie" else _tv(tmdb_id, f"Show {tmdb_id}")
        store.upsert_payload(media_type, payload)
        return payload
    return fake


def test_burst_refreshes_stale_first_then_popular_unfilled(store, monkeypatch):
    store.upsert_payload("movie", _movie(50, "Old"))
    store.mark_changed("movie", [50])
    _seed_export(store, "movie", [1, 2, 3])
    _seed_export(store, "tv", [7])
    fetched = []
    monkeypatch.setattr(tmdb_sync, "fetch_title_payload", _fake_fetch(store, fetched))
    result = asyncio.run(tmdb_fill.run_burst(size=6))
    assert fetched[:3] == [("movie", 50), ("movie", 1), ("movie", 2)]
    assert ("tv", 7) in fetched
    assert result["fetched"] == 4


def test_burst_respects_daily_budget(store, monkeypatch):
    config.save_tmdb_store_settings({"daily_budget": 2})
    _seed_export(store, "movie", [1, 2, 3, 4])
    store.add_requests(1)
    fetched = []
    monkeypatch.setattr(tmdb_sync, "fetch_title_payload", _fake_fetch(store, fetched))
    asyncio.run(tmdb_fill.run_burst(size=10))
    assert len(fetched) == 1


def test_changes_refresh_marks_only_stored_titles(store, monkeypatch):
    store.upsert_payload("movie", _movie(1))
    store.upsert_payload("tv", _tv(10))

    class Resp:
        status_code = 200

        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    async def fake_get(url, params):
        ids = [{"id": 1}, {"id": 2}] if "/movie/changes" in url else [{"id": 99}]
        return Resp({"results": ids, "page": 1, "total_pages": 1})

    monkeypatch.setattr(tmdb_sync, "_tmdb_get", fake_get)
    asyncio.run(tmdb_fill.refresh_changes())
    assert store.list_stale_ids("movie", 10) == [1]
    assert store.list_stale_ids("tv", 10) == []


def test_lookup_local_first_then_live_search_stores(store, monkeypatch):
    store.upsert_payload("tv", _tv(10))
    result = asyncio.run(tmdb_fill.lookup("Father Knows Best", "tv"))
    assert result["source"] == "local" and result["results"][0]["tmdb_id"] == 10

    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"id": 77, "name": "Supercar"}]}

    async def fake_get(url, params):
        assert "/search/tv" in url
        return Resp()

    fetched = []
    monkeypatch.setattr(tmdb_sync, "_tmdb_get", fake_get)

    async def fake_fetch(media_type, tmdb_id, *, use_store=True, **_):
        fetched.append(tmdb_id)
        store.upsert_payload(media_type, _tv(int(tmdb_id), "Supercar", "1961-01-28"))
        return {}

    monkeypatch.setattr(tmdb_sync, "fetch_title_payload", fake_fetch)
    result = asyncio.run(tmdb_fill.lookup("Supercar", "tv"))
    assert result["source"] == "tmdb"
    assert result["results"][0]["tmdb_id"] == 77
    assert fetched == [77]
    # stored now, so the next lookup is local
    assert asyncio.run(tmdb_fill.lookup("supercar", "tv"))["source"] == "local"


def test_lookup_by_numeric_id(store, monkeypatch):
    store.upsert_payload("movie", _movie(603, "The Matrix"))
    result = asyncio.run(tmdb_fill.lookup("603", "movie"))
    assert result["results"][0]["title"] == "The Matrix"


def test_due_jobs_skip_everything_without_api_key(store, monkeypatch):
    monkeypatch.setattr(tmdb_fill, "get_tmdb_api_key", lambda: None)
    called = []

    async def nope(*_a, **_k):
        called.append(1)

    monkeypatch.setattr(tmdb_fill, "import_exports", nope)
    monkeypatch.setattr(tmdb_fill, "refresh_changes", nope)
    monkeypatch.setattr(tmdb_fill, "run_burst", nope)
    assert asyncio.run(tmdb_fill.run_due_jobs()) is False
    assert called == []

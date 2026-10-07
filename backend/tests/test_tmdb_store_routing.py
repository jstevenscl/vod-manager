"""Store on/off switch and the call sites routed through the local store:
series details, name search, library matching, IMDb lookup, episode lists."""

import asyncio
import gzip
import json
import sqlite3
import zlib

import httpx
import pytest

import config
import library_matcher
import tmdb_fill
import tmdb_store
import tmdb_sync
from test_tmdb_store import _movie, _tv


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(tmdb_store, "DB_PATH", tmp_path / "tmdb.sqlite")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "_raw_cache", None)
    for mod in (tmdb_sync, tmdb_fill, library_matcher):
        monkeypatch.setattr(mod, "get_tmdb_api_key", lambda: "k")
    tmdb_store.init_db()
    return tmdb_store


class FakeTmdb:
    """Stands in for tmdb_sync._tmdb_get: url-substring -> (status, json)."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[str] = []

    async def __call__(self, url, params):
        self.calls.append(url.replace(tmdb_sync._API_BASE, ""))
        for part, (status, data) in self.routes.items():
            if url.endswith(part):
                return httpx.Response(status, json=data, request=httpx.Request("GET", url))
        raise AssertionError(f"unexpected TMDB call: {url}")


@pytest.fixture()
def tmdb(monkeypatch):
    fake = FakeTmdb()
    monkeypatch.setattr(tmdb_sync, "_tmdb_get", fake)
    return fake


def _off():
    config.save_tmdb_store_settings({"enabled": False})


def _seed_export(media_type, rows):
    path = tmdb_store.DB_PATH.parent / f"export_{media_type}.json.gz"
    name_key = "original_title" if media_type == "movie" else "original_name"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for tmdb_id, name in rows:
            fh.write(json.dumps({"id": tmdb_id, name_key: name, "popularity": 1.0}) + "\n")
    tmdb_store.import_export_file(media_type, path)


# --- on/off switch ---------------------------------------------------------

def test_store_off_bypasses_local_read_and_write(store, tmdb):
    store.upsert_payload("movie", _movie(1, "Stored Title"))
    _off()
    tmdb.routes = {"/movie/1": (200, _movie(1, "Live Title")), "/movie/2": (200, _movie(2, "Other"))}
    assert asyncio.run(tmdb_sync.fetch_title_payload("movie", 1))["title"] == "Live Title"
    asyncio.run(tmdb_sync.fetch_title_payload("movie", 2))
    assert store.get_title("movie", 1)["title"] == "Stored Title"
    assert store.get_title("movie", 2) is None


def test_fill_scheduler_idle_when_off(store, monkeypatch):
    _off()
    assert asyncio.run(tmdb_fill.run_due_jobs()) is False


def test_lookup_when_off_goes_live_and_stores_nothing(store, tmdb):
    store.upsert_payload("tv", _tv(10))
    _off()
    tmdb.routes = {"/search/tv": (200, {"results": [
        {"id": 10, "name": "Father Knows Best", "first_air_date": "1954-10-03", "poster_path": "/x.jpg"}]})}
    result = asyncio.run(tmdb_fill.lookup("Father Knows Best", "tv"))
    assert result["source"] == "tmdb"
    assert result["results"][0]["tmdb_id"] == 10 and result["results"][0]["year"] == 1954
    assert tmdb.calls == ["/search/tv"]


def test_clear_removes_everything(store):
    store.upsert_payload("movie", _movie(1))
    store.set_meta("exports_imported_at", 123)
    store.clear()
    assert [s["entries"] for s in store.stats()] == [0, 0]
    assert store.get_meta("exports_imported_at") is None


# --- series details ----------------------------------------------------------

def test_series_full_details_served_from_store(store, tmdb):
    store.upsert_payload("tv", _tv(10))
    detail = asyncio.run(tmdb_sync.get_series_full_details("10"))
    assert detail["cast_list"] == "Robert Young"
    assert detail["release_date"] == "1954-10-03"
    assert tmdb.calls == []


def test_series_full_details_404_raises_not_found(store, tmdb):
    tmdb.routes = {"/tv/99": (404, {})}
    with pytest.raises(tmdb_sync.TmdbNotFoundError):
        asyncio.run(tmdb_sync.get_series_full_details("99"))


# --- name search ---------------------------------------------------------------

def test_search_title_local_when_every_same_name_title_is_stored(store, tmdb):
    _seed_export("movie", [(1, "Angry Boys"), (5, "Something Else")])
    store.upsert_payload("movie", _movie(1, "Angry Boys"))
    results = asyncio.run(tmdb_sync.search_title("angry boys", "movie"))
    assert tmdb.calls == []
    assert results[0]["tmdb_id"] == "1" and results[0]["name"] == "Angry Boys" and results[0]["year"] == 2011
    assert results[0]["cast"] == ["Actor 0", "Actor 1", "Actor 2", "Actor 3"]
    assert results[0]["poster_url"].endswith("/p.jpg")


def test_search_title_goes_to_tmdb_when_a_same_name_title_is_not_stored(store, tmdb):
    _seed_export("movie", [(1, "Angry Boys"), (2, "Angry Boys")])
    store.upsert_payload("movie", _movie(1, "Angry Boys"))
    tmdb.routes = {"/search/movie": (200, {"results": [{"id": 1, "title": "Angry Boys"}]})}
    asyncio.run(tmdb_sync.search_title("Angry Boys", "movie"))
    assert tmdb.calls == ["/search/movie"]


def test_search_title_goes_to_tmdb_without_a_local_exact_match(store, tmdb):
    store.upsert_payload("movie", _movie(1, "Angry Boys"))
    tmdb.routes = {"/search/movie": (200, {"results": []})}
    asyncio.run(tmdb_sync.search_title("Angry", "movie"))
    assert tmdb.calls == ["/search/movie"]


def test_search_title_store_off_always_tmdb(store, tmdb):
    _seed_export("movie", [(1, "Angry Boys")])
    store.upsert_payload("movie", _movie(1, "Angry Boys"))
    _off()
    tmdb.routes = {"/search/movie": (200, {"results": [{"id": 1, "title": "Angry Boys"}]}),
                   "/movie/1": (200, _movie(1, "Angry Boys"))}
    asyncio.run(tmdb_sync.search_title("Angry Boys", "movie"))
    assert tmdb.calls[0] == "/search/movie"


# --- library matcher -----------------------------------------------------------

def test_library_search_local_when_year_agrees(store, tmdb):
    _seed_export("movie", [(1, "Angry Boys")])
    store.upsert_payload("movie", _movie(1, "Angry Boys"))
    found = asyncio.run(library_matcher._search("Angry Boys", "movie", 2011))
    assert [(c.tmdb_id, c.year) for c in found] == [("1", 2011)]
    assert tmdb.calls == []


def test_library_search_tmdb_when_local_year_disagrees(store, tmdb):
    _seed_export("movie", [(1, "Angry Boys")])
    store.upsert_payload("movie", _movie(1, "Angry Boys"))
    tmdb.routes = {"/search/movie": (200, {"results": [{"id": 8, "title": "Angry Boys", "release_date": "1990-01-01"}]}),
                   "/movie/8": (200, _movie(8, "Angry Boys", "1990"))}
    found = asyncio.run(library_matcher._search("Angry Boys", "movie", 1990))
    assert [c.tmdb_id for c in found] == ["8"]
    assert tmdb.calls == ["/search/movie", "/movie/8"]  # TMDB's answer is saved locally


def test_imdb_lookup_answered_locally(store, tmdb):
    store.upsert_payload("movie", _movie(1, "Angry Boys", imdb_id="tt1838556"))
    store.upsert_payload("tv", {**_tv(10), "external_ids": {"imdb_id": "tt0046600"}})
    assert asyncio.run(library_matcher._lookup_by_imdb("tt1838556", "movie")).tmdb_id == "1"
    assert asyncio.run(library_matcher._lookup_by_imdb("tt0046600", "series")).tmdb_id == "10"
    assert tmdb.calls == []


def test_imdb_lookup_falls_back_to_find(store, tmdb):
    tmdb.routes = {"/find/tt9": (200, {"movie_results": [{"id": 3, "title": "X", "release_date": "2000-01-01"}]})}
    assert asyncio.run(library_matcher._lookup_by_imdb("tt9", "movie")).tmdb_id == "3"


# --- episode lists ---------------------------------------------------------------

def _season(n, count=2):
    return {"episodes": [{"episode_number": i, "name": f"S{n}E{i}", "air_date": "1955-01-01"} for i in range(1, count + 1)]}


def _show_with_seasons():
    return {**_tv(10), "seasons": [{"season_number": 0}, {"season_number": 1}, {"season_number": 2}]}


def test_episode_list_cached_per_season(store, tmdb):
    store.upsert_payload("tv", _show_with_seasons())
    tmdb.routes = {"/tv/10/season/1": (200, _season(1)), "/tv/10/season/2": (200, _season(2, 3))}
    first = asyncio.run(tmdb_sync.get_series_episode_list("10"))
    assert len(first) == 5 and sorted(tmdb.calls) == ["/tv/10/season/1", "/tv/10/season/2"]
    tmdb.calls.clear()
    assert asyncio.run(tmdb_sync.get_series_episode_list("10")) == first
    assert tmdb.calls == []


def test_episode_list_refetched_after_tmdb_change(store, tmdb):
    store.upsert_payload("tv", _show_with_seasons())
    tmdb.routes = {"/tv/10/season/1": (200, _season(1)), "/tv/10/season/2": (200, _season(2)),
                   "/tv/10": (200, _show_with_seasons())}
    asyncio.run(tmdb_sync.get_series_episode_list("10"))
    store.mark_changed("tv", [10])
    tmdb.calls.clear()
    asyncio.run(tmdb_sync.get_series_episode_list("10"))
    assert sorted(tmdb.calls) == ["/tv/10", "/tv/10/season/1", "/tv/10/season/2"]


def test_latest_season_refreshed_daily(store, tmdb):
    store.upsert_payload("tv", _show_with_seasons())
    tmdb.routes = {"/tv/10/season/1": (200, _season(1)), "/tv/10/season/2": (200, _season(2))}
    asyncio.run(tmdb_sync.get_series_episode_list("10"))
    with sqlite3.connect(store.DB_PATH) as conn:
        conn.execute("UPDATE seasons SET fetched_at = fetched_at - 2 * 86400")
    tmdb.calls.clear()
    asyncio.run(tmdb_sync.get_series_episode_list("10"))
    assert tmdb.calls == ["/tv/10/season/2"]


def test_episode_list_store_off_always_tmdb(store, tmdb):
    store.upsert_payload("tv", _show_with_seasons())
    _off()
    tmdb.routes = {"/tv/10/season/1": (200, _season(1)), "/tv/10/season/2": (200, _season(2)),
                   "/tv/10": (200, _show_with_seasons())}
    asyncio.run(tmdb_sync.get_series_episode_list("10"))
    asyncio.run(tmdb_sync.get_series_episode_list("10"))
    assert tmdb.calls.count("/tv/10/season/1") == 2


# --- schema upgrade of an existing (live) store ------------------------------------

def test_existing_store_upgraded_in_place(tmp_path, monkeypatch):
    db = tmp_path / "old.sqlite"
    monkeypatch.setattr(tmdb_store, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE titles (media_type TEXT NOT NULL, tmdb_id INTEGER NOT NULL, title TEXT,
                original_title TEXT, year INTEGER, release_date TEXT, poster_path TEXT, overview TEXT,
                vote_average REAL, popularity REAL, content_rating TEXT, top_cast TEXT, genres TEXT,
                raw BLOB, fetched_at REAL NOT NULL, changed_at REAL, PRIMARY KEY (media_type, tmdb_id));
            CREATE TABLE export_ids (media_type TEXT NOT NULL, tmdb_id INTEGER NOT NULL, name TEXT,
                popularity REAL, PRIMARY KEY (media_type, tmdb_id)) WITHOUT ROWID;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('exports_imported_at', '1700000000');
        """)
        raw = zlib.compress(json.dumps({"id": 1, "imdb_id": "tt1"}).encode())
        conn.execute("INSERT INTO titles (media_type, tmdb_id, title, raw, fetched_at) VALUES ('movie', 1, 'A', ?, 1)", (raw,))
    tmdb_store.init_db()
    assert tmdb_store.find_by_imdb("movie", "tt1")["tmdb_id"] == 1
    # old export rows have no name key, so force a re-import on the next tick
    assert float(tmdb_store.get_meta("exports_imported_at")) == 0
    tmdb_store.init_db()  # idempotent

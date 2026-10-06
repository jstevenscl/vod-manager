"""Automatic TMDB ID matching for catalog titles that have none (tmdb_automatch),
and the local-vs-TMDB lookup counters shown on the TMDB Library page."""

import asyncio

import pytest

import config
import library_matcher
import tmdb_automatch
import tmdb_store
import tmdb_sync
from test_tmdb_store import _movie, _tv
from test_tmdb_store_routing import FakeTmdb, _seed_export


@pytest.fixture()
def env(db, tmp_path, monkeypatch):
    monkeypatch.setattr(tmdb_store, "DB_PATH", tmp_path / "tmdb.sqlite")
    for mod in (tmdb_sync, library_matcher):
        monkeypatch.setattr(mod, "get_tmdb_api_key", lambda: "k")
    tmdb_store.init_db()
    fake = FakeTmdb()
    monkeypatch.setattr(tmdb_sync, "_tmdb_get", fake)
    return db, fake


def _add(db, table, name, year, tmdb_id=None):
    conn = db._connect()
    conn.execute(f"INSERT INTO {table} (name, year, tmdb_id, created_at) VALUES (?,?,?,?)",
                 (name, year, tmdb_id, db._now()))
    item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return item_id


def _tmdb_id(db, table, item_id):
    conn = db._connect()
    row = conn.execute(f"SELECT tmdb_id FROM {table} WHERE id=?", (item_id,)).fetchone()
    conn.close()
    return row["tmdb_id"] if row else None


def test_confident_local_match_assigns_id_without_tmdb_calls(env):
    db, fake = env
    tmdb_store.upsert_payload("tv", _tv(10, "Father Knows Best", "1954-10-03"))
    _seed_export("tv", [(10, "Father Knows Best")])
    item = _add(db, "series", "Father Knows Best", 1954)

    result = asyncio.run(tmdb_automatch.run_auto_match())

    assert _tmdb_id(db, "series", item) == "10"
    assert fake.calls == []
    assert result["matched"] == 1


def test_local_miss_falls_back_to_tmdb_search(env):
    db, fake = env
    fake.routes = {"/search/tv": (200, {"results": [{"id": 77, "name": "Supercar", "first_air_date": "1961-01-28"}]})}
    item = _add(db, "series", "Supercar", 1961)

    asyncio.run(tmdb_automatch.run_auto_match())

    assert _tmdb_id(db, "series", item) == "77"


def test_year_disagreement_goes_to_metadata_review_not_assigned(env):
    db, fake = env
    fake.routes = {"/search/tv": (200, {"results": [{"id": 5, "name": "Angry Boys", "first_air_date": "1990-01-01"}]})}
    item = _add(db, "series", "Angry Boys", 2011)

    result = asyncio.run(tmdb_automatch.run_auto_match())

    assert _tmdb_id(db, "series", item) is None
    assert result["ambiguous"] == 1
    review = db.list_metadata_review("series")["series"]
    assert [r["id"] for r in review] == [item]


def test_ambiguous_title_is_not_hidden_from_export(env):
    """Ambiguous results must not set needs_year_review -- that flag blocks
    every placement path, which would remove the title from Dispatcharr."""
    db, fake = env
    item = _add(db, "series", "Riptide", 2022)
    fake.routes = {"/search/tv": (200, {"results": [
        {"id": 1, "name": "Riptide", "first_air_date": "2022-01-01"},
        {"id": 2, "name": "Riptide", "first_air_date": "2022-06-01"},
    ]})}

    asyncio.run(tmdb_automatch.run_auto_match())

    conn = db._connect()
    flag = conn.execute("SELECT needs_year_review FROM series WHERE id=?", (item,)).fetchone()[0]
    conn.close()
    assert flag == 0
    assert _tmdb_id(db, "series", item) is None


def test_reviewer_resolving_without_id_settles_it(env):
    db, fake = env
    fake.routes = {"/search/tv": (200, {"results": [{"id": 5, "name": "Angry Boys", "first_air_date": "1990-01-01"}]})}
    item = _add(db, "series", "Angry Boys", 2011)
    asyncio.run(tmdb_automatch.run_auto_match())

    db.resolve_year_review("series", item, 2011)
    again = asyncio.run(tmdb_automatch.run_auto_match())

    assert db.list_metadata_review("series")["series"] == []
    assert again["checked"] == 0


def test_no_year_match_needs_name_unique_on_tmdb(env):
    db, fake = env
    # TMDB lists two different films originally titled "Kotch"; only one is
    # stored and the live search only returns that one -- still not safe.
    tmdb_store.upsert_payload("movie", _movie(1, "Kotch", "1971"))
    _seed_export("movie", [(1, "Kotch"), (2, "Kotch")])
    fake.routes = {"/search/movie": (200, {"results": [{"id": 1, "title": "Kotch", "release_date": "1971-09-17"}]})}
    item = _add(db, "movies", "Kotch", None)

    result = asyncio.run(tmdb_automatch.run_auto_match())

    assert _tmdb_id(db, "movies", item) is None
    assert result["ambiguous"] == 1


def test_no_year_unique_name_is_matched(env):
    db, fake = env
    tmdb_store.upsert_payload("movie", _movie(1, "Kotch", "1971"))
    _seed_export("movie", [(1, "Kotch")])
    item = _add(db, "movies", "Kotch", None)

    asyncio.run(tmdb_automatch.run_auto_match())

    assert _tmdb_id(db, "movies", item) == "1"


def test_unmatched_not_retried_until_retry_window(env):
    db, fake = env
    fake.routes = {"/search/movie": (200, {"results": []})}
    _add(db, "movies", "Nothing Like This", 2020)

    first = asyncio.run(tmdb_automatch.run_auto_match())
    calls = len(fake.calls)
    second = asyncio.run(tmdb_automatch.run_auto_match())

    assert first["unmatched"] == 1
    assert len(fake.calls) == calls
    assert second["checked"] == 0


def test_tmdb_error_is_not_recorded_and_retried_next_run(env):
    db, fake = env
    fake.routes = {"/search/movie": (500, {})}
    _add(db, "movies", "Flaky", 2020)

    first = asyncio.run(tmdb_automatch.run_auto_match())
    fake.routes = {"/search/movie": (200, {"results": [{"id": 9, "title": "Flaky", "release_date": "2020-01-01"}]})}
    asyncio.run(tmdb_automatch.run_auto_match())

    assert first["failed"] == 1
    assert _tmdb_id(db, "movies", db.get_movie_by_name_year("Flaky", 2020)["id"]) == "9"


def test_match_onto_existing_tmdb_id_merges(env):
    db, fake = env
    tmdb_store.upsert_payload("tv", _tv(10, "Father Knows Best", "1954-10-03"))
    _seed_export("tv", [(10, "Father Knows Best")])
    keeper = _add(db, "series", "Father Knows Best (US)", 1954, "10")
    dup = _add(db, "series", "Father Knows Best", 1954)

    asyncio.run(tmdb_automatch.run_auto_match())

    assert _tmdb_id(db, "series", dup) is None  # merged away
    assert _tmdb_id(db, "series", keeper) == "10"


def test_batch_limit(env):
    db, fake = env
    fake.routes = {"/search/movie": (200, {"results": []})}
    for i in range(3):
        _add(db, "movies", f"Unknown {i}", 2000 + i)

    result = asyncio.run(tmdb_automatch.run_auto_match(limit=2))

    assert result["checked"] == 2


# --- local vs TMDB lookup counters ------------------------------------------

def test_lookup_counters_track_local_and_tmdb(env):
    db, fake = env
    tmdb_store.upsert_payload("movie", _movie(1, "Stored"))
    fake.routes = {"/movie/2": (200, _movie(2, "Fetched"))}

    asyncio.run(tmdb_sync.fetch_title_payload("movie", 1))
    asyncio.run(tmdb_sync.fetch_title_payload("movie", 2))
    asyncio.run(tmdb_sync.fetch_title_payload("movie", 2))

    assert tmdb_store.lookups_today() == {"local": 2, "tmdb": 1}


def test_lookup_counters_skip_fill_and_store_off(env):
    db, fake = env
    fake.routes = {"/movie/3": (200, _movie(3, "Fill"))}
    asyncio.run(tmdb_sync.fetch_title_payload("movie", 3, use_store=False))
    config.save_tmdb_store_settings({"enabled": False})
    asyncio.run(tmdb_sync.fetch_title_payload("movie", 3))

    assert tmdb_store.lookups_today() == {"local": 0, "tmdb": 0}

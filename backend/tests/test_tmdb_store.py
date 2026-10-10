"""Local TMDB store: payload upsert, name search, stats, fill selection, read-through."""

import asyncio
import gzip
import json

import pytest

import config
import tmdb_store
import tmdb_sync


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(tmdb_store, "DB_PATH", tmp_path / "tmdb.sqlite")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "_raw_cache", None)
    tmdb_store.init_db()
    return tmdb_store


def _movie(tmdb_id=1, title="Angry Boys", year="2011", popularity=5.0, **extra):
    payload = {
        "id": tmdb_id, "title": title, "original_title": title,
        "release_date": f"{year}-05-11" if year else "", "poster_path": "/p.jpg",
        "overview": "Mockumentary", "vote_average": 7.1, "popularity": popularity,
        "genres": [{"id": 35, "name": "Comedy"}],
        "credits": {"cast": [{"name": f"Actor {i}"} for i in range(40)],
                    "crew": [{"name": "Dir", "job": "Director"}, {"name": "X", "job": "Grip"}]},
        "release_dates": {"results": [{"iso_3166_1": "US", "release_dates": [{"certification": ""}, {"certification": "R"}]}]},
        "alternative_titles": {"titles": [{"title": "Les Garçons en Colère", "iso_3166_1": "FR"}]},
    }
    payload.update(extra)
    return payload


def _tv(tmdb_id=10, name="Father Knows Best", first_air="1954-10-03"):
    return {
        "id": tmdb_id, "name": name, "original_name": name, "first_air_date": first_air,
        "poster_path": None, "overview": "", "vote_average": 0, "popularity": 2.0,
        "genres": [], "number_of_seasons": 6, "number_of_episodes": 203,
        "credits": {"cast": [{"name": "Robert Young"}]},
        "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-G"}]},
        "alternative_titles": {"results": [{"title": "Papa weiß es am besten", "iso_3166_1": "DE"}]},
    }


def test_upsert_and_get_payload_roundtrip(store):
    store.upsert_payload("movie", _movie())
    row = store.get_title("movie", 1)
    assert row["title"] == "Angry Boys"
    assert row["year"] == 2011
    assert row["content_rating"] == "R"
    assert row["genres"] == "Comedy"
    assert row["top_cast"].startswith("Actor 0, Actor 1")
    payload = store.get_payload("movie", 1)
    # raw payload is trimmed: cast capped, crew limited to directing/creating jobs
    assert len(payload["credits"]["cast"]) == tmdb_store.RAW_CAST_LIMIT
    assert [c["job"] for c in payload["credits"]["crew"]] == ["Director"]


def test_tv_row_uses_name_fields_and_us_rating(store):
    store.upsert_payload("tv", _tv())
    row = store.get_title("tv", 10)
    assert (row["title"], row["year"], row["content_rating"]) == ("Father Knows Best", 1954, "TV-G")


def test_search_matches_title_and_alt_title_ignoring_accents(store):
    store.upsert_payload("movie", _movie())
    store.upsert_payload("movie", _movie(2, "Angry Birds", "2016", popularity=50.0, alternative_titles={"titles": []}))
    hits = store.search("movie", "angry boys")
    assert [h["tmdb_id"] for h in hits][0] == 1
    assert store.search("movie", "garcons colere")[0]["tmdb_id"] == 1
    assert store.search("movie", "nothing like this") == []


def test_search_prefers_exact_title_then_year(store):
    store.upsert_payload("movie", _movie(1, "Riptide", "1984", popularity=1.0))
    store.upsert_payload("movie", _movie(2, "Riptide", "1949", popularity=3.0))
    store.upsert_payload("movie", _movie(3, "Riptide Rising", "2020", popularity=99.0))
    assert [h["tmdb_id"] for h in store.search("movie", "Riptide")] == [2, 1, 3]
    assert store.search("movie", "Riptide", year=1984)[0]["tmdb_id"] == 1


def test_reupsert_replaces_names(store):
    store.upsert_payload("movie", _movie())
    store.upsert_payload("movie", _movie(title="Renamed Show", alternative_titles={"titles": []}))
    assert store.search("movie", "angry boys") == []
    assert store.search("movie", "renamed")[0]["tmdb_id"] == 1


def test_changed_title_is_not_served_until_refetched(store):
    store.upsert_payload("movie", _movie())
    store.mark_changed("movie", [1, 999])
    assert store.get_payload("movie", 1) is None
    assert store.list_stale_ids("movie", 10) == [1]
    store.upsert_payload("movie", _movie())
    assert store.get_payload("movie", 1) is not None


def test_export_import_and_fill_candidates(store, tmp_path):
    path = tmp_path / "movie_ids.json.gz"
    lines = [
        {"id": 1, "original_title": "A", "popularity": 9.0, "adult": False, "video": False},
        {"id": 2, "original_title": "B", "popularity": 8.0, "adult": True, "video": False},
        {"id": 3, "original_title": "C", "popularity": 7.0, "adult": False, "video": False},
        {"id": 4, "original_title": "D", "popularity": 6.0, "adult": False, "video": False},
    ]
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(line) for line in lines) + "\n")
    assert store.import_export_file("movie", path) == 3  # adult skipped
    store.upsert_payload("movie", _movie(1))
    store.mark_gone("movie", 3)
    assert store.list_fill_candidates("movie", limit=10, top_n=100) == [4]
    assert store.list_fill_candidates("movie", limit=10, top_n=2) == []


def test_stats_counts(store):
    store.upsert_payload("movie", _movie())
    store.upsert_payload("tv", _tv())
    stats = {s["media_type"]: s for s in store.stats()}
    assert stats["movie"]["entries"] == 1
    assert stats["movie"]["with_poster"] == 1
    assert stats["tv"]["with_poster"] == 0
    assert stats["tv"]["with_overview"] == 0
    assert stats["tv"]["with_cast"] == 1


def test_daily_request_budget(store):
    assert store.requests_today() == 0
    store.add_requests(3)
    store.add_requests(2)
    assert store.requests_today() == 5


def test_movie_details_read_through_skips_network_when_stored(store, monkeypatch):
    store.upsert_payload("movie", _movie(55))

    async def boom(*_a, **_k):
        raise AssertionError("network must not be used for a stored title")

    monkeypatch.setattr(tmdb_sync, "_tmdb_get", boom)
    monkeypatch.setattr(tmdb_sync, "get_tmdb_api_key", lambda: "k")
    details = asyncio.run(tmdb_sync.get_movie_full_details("55"))
    assert details["name"] == "Angry Boys"
    assert details["content_rating"] == "R"
    assert details["director"] == "Dir"


def test_tv_details_fetch_stores_payload(store, monkeypatch):
    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return _tv(77, "Supercar", "1961-01-28")

    calls = []

    async def fake_get(url, params):
        calls.append((url, params.get("append_to_response")))
        return Resp()

    monkeypatch.setattr(tmdb_sync, "_tmdb_get", fake_get)
    monkeypatch.setattr(tmdb_sync, "get_tmdb_api_key", lambda: "k")
    first = asyncio.run(tmdb_sync.get_tv_identity("77"))
    second = asyncio.run(tmdb_sync.get_tv_identity("77"))
    assert first == second
    assert first["name"] == "Supercar" and first["year"] == 1961 and first["content_rating"] == "TV-G"
    assert len(calls) == 1
    assert "alternative_titles" in calls[0][1]
    assert store.search("tv", "supercar")[0]["tmdb_id"] == 77


def test_name_search_starts_from_the_text_index(store):
    # With the text index as an inner loop, every stored name was scanned per
    # search (tens of seconds once the library held ~200K names).
    store.upsert_payload("movie", _movie(1, "Angry Boys"))
    with store._conn() as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN " + store._NAME_SEARCH_SQL, ('"angry"', "movie")).fetchall()
    assert plan[0][3].startswith("SCAN f")

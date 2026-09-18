"""Known TMDB identities are resolved without provider detail requests."""

import asyncio

import tmdb_sync
import vod_importer


def _movie(stream_id: str, tmdb_id: str | None) -> dict:
    return {
        "name": f"Movie {stream_id}", "year": 2024,
        "provider_stream_id": stream_id, "container_extension": "mp4",
        "tmdb_id": tmdb_id,
    }


def test_pending_movie_selectors_split_tmdb_from_provider_fallback(db):
    provider_id = db.upsert_provider("Example Provider", "http://example.invalid", "user", "pass")
    db.bulk_import_movies(provider_id, [_movie("known", "123"), _movie("unknown", None)])

    assert len(db.list_movie_ids_pending_tmdb_enrichment()) == 1
    assert len(db.list_movie_ids_pending_provider_enrichment(provider_id)) == 1


def test_tmdb_job_writes_known_identity_without_provider_client(db, monkeypatch):
    provider_id = db.upsert_provider("Example Provider", "http://example.invalid", "user", "pass")
    db.bulk_import_movies(provider_id, [_movie("known", "123")])
    movie_id = db.list_movie_ids_pending_tmdb_enrichment()[0]

    async def fake_tmdb(tmdb_id):
        assert tmdb_id == "123"
        return {
            "name": "Resolved Movie", "genre": "Drama", "description": "summary",
            "cast_list": "Actor", "director": "Director", "country": "US",
            "poster_url": "https://image.example/poster.jpg", "duration_secs": 6000,
            "rating": 7.0, "release_date": "2024-01-01", "content_rating": "PG",
        }

    monkeypatch.setattr(tmdb_sync, "get_movie_full_details", fake_tmdb)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    asyncio.run(vod_importer.bulk_enrich_tmdb_movies(concurrency=1))

    movie = db.get_movie(movie_id)
    assert movie["genre"] == "Drama"
    assert movie["last_enriched_at"] is not None
    assert db.list_movie_ids_pending_tmdb_enrichment() == []


def test_tmdb_series_pass_normalizes_card_name_but_preserves_raw_source(db, monkeypatch):
    provider_id = db.upsert_provider("Example Provider", "http://example.invalid", "user", "pass")
    db.bulk_import_series(provider_id, [{
        "name": "30 Coins (ES)", "year": 2020, "provider_series_id": "series-1",
        "provider_category_name": None, "raw_name": "30 Coins (ES)", "_has_detail": True,
        "genre": None, "description": None, "cast_list": None, "director": None,
        "poster_url": None, "rating": None, "release_date": None, "tmdb_id": "123",
        "provider_last_modified": None,
    }])
    series_id = db.list_series(limit=10)[0]["id"]

    async def fake_tmdb(tmdb_id):
        assert tmdb_id == "123"
        return {"name": "30 Coins", "content_rating": "TV-MA", "year": 2020}

    monkeypatch.setattr(tmdb_sync, "get_tv_full_details", fake_tmdb)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    asyncio.run(vod_importer.bulk_enrich_tmdb_series_metadata(concurrency=1))

    series = db.get_series(series_id)
    source = db.list_series_sources(series_id)[0]
    assert series["name"] == "30 Coins"
    assert series["content_rating"] == "TV-MA"
    assert series["tmdb_metadata_enriched_at"] is not None
    assert source["raw_name"] == "30 Coins (ES)"
    assert db.list_series_pending_tmdb_metadata_enrichment() == []


def test_tmdb_series_pass_backfills_year_and_clears_review_hold(db, monkeypatch):
    provider_id = db.upsert_provider("Example Provider", "http://example.invalid", "user", "pass")
    db.bulk_import_series(provider_id, [{
        "name": "Undated Series", "year": None, "provider_series_id": "series-undated",
        "provider_category_name": None, "raw_name": "Undated Series", "_has_detail": True,
        "genre": None, "description": None, "cast_list": None, "director": None,
        "poster_url": None, "rating": None, "release_date": None, "tmdb_id": "321",
        "provider_last_modified": None,
    }])
    series_id = db.list_series(limit=10)[0]["id"]

    async def fake_tmdb(tmdb_id):
        assert tmdb_id == "321"
        return {"name": "Undated Series", "content_rating": None, "year": 2018}

    monkeypatch.setattr(tmdb_sync, "get_tv_full_details", fake_tmdb)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    asyncio.run(vod_importer.bulk_enrich_tmdb_series_metadata(concurrency=1))

    series = db.get_series(series_id)
    assert series["year"] == 2018
    assert series["needs_year_review"] == 0
    assert db.list_series_pending_tmdb_metadata_enrichment() == []


def test_metadata_review_resolution_merges_same_tmdb_series_alias(db):
    """A reviewer-confirmed ID must not wait for a later import to merge."""
    existing_id = db.upsert_series("Canonical Series", 2020, tmdb_id="900")
    review_id = db.upsert_series("Provider Alias", None)

    db.resolve_year_review("series", review_id, 2020, "900")

    survivors = [db.get_series(series_id) for series_id in (existing_id, review_id)]
    assert len([series for series in survivors if series is not None]) == 1


def test_tmdb_series_pass_reuses_one_lookup_for_shared_tmdb_id(db, monkeypatch):
    first_id = db.upsert_series("English Card", 2020, tmdb_id="shared-id")
    second_id = db.upsert_series("Spanish Card", 2020, tmdb_id="shared-id")
    calls = []

    async def fake_tmdb(tmdb_id):
        calls.append(tmdb_id)
        return {"name": "Canonical Card", "content_rating": None}

    monkeypatch.setattr(tmdb_sync, "get_tv_full_details", fake_tmdb)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    monkeypatch.setattr(vod_importer.vod_db, "auto_merge_series_by_tmdb_batch", lambda _: None)
    asyncio.run(vod_importer.bulk_enrich_tmdb_series_metadata(concurrency=8))

    assert calls == ["shared-id"]
    assert db.get_series(first_id)["name"] == "Canonical Card"
    assert db.get_series(second_id)["name"] == "Canonical Card"


def test_tmdb_404_is_persisted_for_incorrect_id_review(db, monkeypatch):
    provider_id = db.upsert_provider("Example Provider", "http://example.invalid", "user", "pass")
    db.bulk_import_movies(provider_id, [_movie("gone", "404")])
    movie_id = db.list_movie_ids_pending_tmdb_enrichment()[0]

    async def missing(_tmdb_id):
        raise tmdb_sync.TmdbNotFoundError("404")

    monkeypatch.setattr(tmdb_sync, "get_movie_full_details", missing)
    asyncio.run(vod_importer.bulk_enrich_tmdb_movies(concurrency=1))

    queued = db.list_tmdb_lookup_failures("movie")["movies"]
    assert [(row["id"], row["invalid_tmdb_id"]) for row in queued] == [(movie_id, "404")]


def test_adult_known_tmdb_ids_are_not_retried(db):
    provider_id = db.upsert_provider("Example Provider", "http://example.invalid", "user", "pass")
    db.bulk_import_movies(provider_id, [_movie("adult", "123")])
    movie_id = db.list_movie_ids_pending_tmdb_enrichment()[0]
    db.set_movie_adult(movie_id, True)
    assert db.list_movie_ids_pending_tmdb_enrichment() == []


def test_post_import_runs_series_episode_phase_after_tmdb_phases(monkeypatch):
    calls = []

    async def movie_phase():
        calls.append("movie-tmdb")

    async def series_metadata_phase():
        calls.append("series-tmdb")

    async def episode_phase():
        calls.append("series-episodes")

    monkeypatch.setattr(vod_importer, "bulk_enrich_tmdb_movies", movie_phase)
    monkeypatch.setattr(vod_importer, "bulk_enrich_tmdb_series_metadata", series_metadata_phase)
    monkeypatch.setattr(vod_importer, "bulk_enrich_series_episodes", episode_phase)

    asyncio.run(vod_importer._post_import_enrichment(track_catalog_workflow=False))

    assert calls == ["movie-tmdb", "series-tmdb", "series-episodes"]


def test_episode_only_series_phase_preserves_tmdb_metadata(db, monkeypatch):
    provider_id = db.upsert_provider("Example Provider", "http://example.invalid", "user", "pass")
    db.bulk_import_series(provider_id, [{
        "name": "TMDB Name", "year": 2020, "provider_series_id": "series-1",
        "raw_name": "Provider Name", "tmdb_id": "123", "_has_detail": True,
    }])
    series_id = db.list_series(limit=10)[0]["id"]
    db.set_series_enrichment(series_id, name="TMDB Name", tmdb_id="123")

    class FakeClient:
        def __init__(self, _provider):
            pass

        async def get_series_info(self, series_id):
            assert series_id == "series-1"
            return {"info": {"name": "Provider Replacement"}, "episodes": {
                "1": [{"id": "episode-1", "episode_num": 1, "title": "Pilot"}]
            }}

    monkeypatch.setattr(vod_importer, "XCProviderClient", FakeClient)
    result = asyncio.run(vod_importer.enrich_series_source_only(
        series_id, provider_id, episodes_only=True,
    ))

    assert result["fetched"] is True
    assert db.get_series(series_id)["name"] == "TMDB Name"
    assert len(db.list_episodes(series_id)) == 1

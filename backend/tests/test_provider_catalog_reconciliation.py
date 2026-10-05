"""Provider catalog snapshots must remove sources no longer advertised.

These tests use only the temporary SQLite database fixture; no live provider
or network request is involved.
"""

import asyncio

import vod_importer


def _movie(stream_id: str, name: str = "Shared Movie") -> dict:
    return {
        "name": name, "year": 2020, "provider_stream_id": stream_id,
        "container_extension": "mp4", "provider_category_name": "Movies",
        "raw_name": name,
    }


def _series(series_id: str, name: str = "Shared Series") -> dict:
    return {
        "name": name, "year": 2020, "provider_series_id": series_id,
        "provider_category_name": "Series", "raw_name": name,
        "_has_detail": True, "genre": None, "description": None,
        "cast_list": None, "director": None, "poster_url": None,
        "rating": None, "release_date": None, "tmdb_id": None,
        "provider_last_modified": None,
    }


def test_reconcile_provider_catalog_removes_only_missing_provider_sources(db):
    provider_a = db.upsert_provider("Provider A", "http://a.invalid", "u", "p", provider_type="xc")
    provider_b = db.upsert_provider("Provider B", "http://b.invalid", "u", "p", provider_type="xc")

    db.bulk_import_movies(provider_a, [_movie("a-keep"), _movie("a-shared-gone"), _movie("a-gone", "Gone Movie")])
    db.bulk_import_movies(provider_b, [_movie("b-keep")])
    db.bulk_import_series(provider_a, [_series("a-keep"), _series("a-shared-gone"), _series("a-gone", "Gone Series")])
    db.bulk_import_series(provider_b, [_series("b-keep")])

    shared_movie = db.get_movie_by_name_year("Shared Movie", 2020)
    shared_series = db.get_series_by_name_year("Shared Series", 2020)
    assert {s["provider_stream_id"] for s in db.list_movie_sources(shared_movie["id"])} == {"a-keep", "a-shared-gone", "b-keep"}
    assert {s["provider_series_id"] for s in db.list_series_sources(shared_series["id"])} == {"a-keep", "a-shared-gone", "b-keep"}

    result = db.reconcile_provider_catalog_sources(
        provider_a, seen_movie_stream_ids={"a-keep"}, seen_series_ids={"a-keep"},
    )

    assert result == {"movie_sources_removed": 2, "series_sources_removed": 2, "episode_sources_removed": 0}
    assert {s["provider_stream_id"] for s in db.list_movie_sources(shared_movie["id"])} == {"a-keep", "b-keep"}
    assert {s["provider_series_id"] for s in db.list_series_sources(shared_series["id"])} == {"a-keep", "b-keep"}
    assert db.get_movie_by_name_year("Gone Movie", 2020) is None
    assert db.get_series_by_name_year("Gone Series", 2020) is None


def test_reconcile_provider_catalog_empty_snapshot_removes_all_that_provider_sources(db):
    provider_id = db.upsert_provider("Provider A", "http://a.invalid", "u", "p", provider_type="xc")
    db.bulk_import_movies(provider_id, [_movie("a-1")])
    db.bulk_import_series(provider_id, [_series("a-1")])

    result = db.reconcile_provider_catalog_sources(
        provider_id, seen_movie_stream_ids=set(), seen_series_ids=set(),
    )

    assert result == {"movie_sources_removed": 1, "series_sources_removed": 1, "episode_sources_removed": 0}
    assert db.get_movie_by_name_year("Shared Movie", 2020) is None
    assert db.get_series_by_name_year("Shared Series", 2020) is None


def test_reconcile_provider_catalog_removes_episodes_for_a_vanished_series_source(db):
    provider_id = db.upsert_provider("Provider A", "http://a.invalid", "u", "p", provider_type="xc")
    db.bulk_import_series(provider_id, [_series("a-1")])
    series = db.get_series_by_name_year("Shared Series", 2020)
    db.enrich_series_episodes_batch(series["id"], provider_id, [{
        "season_number": 1, "episode_number": 1, "name": "Episode 1",
        "provider_stream_id": "episode-1", "container_extension": "mp4",
    }])

    result = db.reconcile_provider_catalog_sources(
        provider_id, seen_movie_stream_ids=set(), seen_series_ids=set(),
    )

    assert result == {"movie_sources_removed": 0, "series_sources_removed": 1, "episode_sources_removed": 1}
    assert db.get_series_by_name_year("Shared Series", 2020) is None


def test_import_reconciles_against_raw_snapshot_not_filtered_payload(db, monkeypatch):
    provider_id = db.upsert_provider("Provider A", "http://a.invalid", "u", "p", provider_type="xc")
    db.bulk_import_movies(provider_id, [_movie("stale")])
    db.bulk_import_series(provider_id, [_series("stale")])

    class FakeClient:
        def __init__(self, _provider):
            pass

        async def get_vod_categories(self):
            return [{"category_id": "1", "category_name": "Movies"}]

        async def get_series_categories(self):
            return [{"category_id": "2", "category_name": "Series"}]

        async def get_vod_streams(self):
            return [{"stream_id": "keep", "name": "Keep Movie (2020)", "category_id": "1"}]

        async def get_series(self):
            return [{"series_id": "keep", "name": "Keep Series (2020)", "category_id": "2"}]

    monkeypatch.setattr(vod_importer, "XCProviderClient", FakeClient)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    monkeypatch.setattr(vod_importer, "schedule_post_import_enrichment", lambda: False)
    monkeypatch.setattr(
        vod_importer.vod_db, "purge_excluded_archived_content",
        lambda *_: {"movies_deleted": 0, "series_deleted": 0},
    )
    monkeypatch.setattr(
        vod_importer.vod_db, "archive_disabled_language_content",
        lambda *_: {"movies_archived": 0, "series_archived": 0, "movies_unarchived": 0, "series_unarchived": 0},
    )

    asyncio.run(vod_importer.import_provider_catalog(provider_id))
    unchanged = asyncio.run(vod_importer.import_provider_catalog(provider_id))

    assert db.get_movie_by_name_year("Shared Movie", 2020) is None
    assert db.get_series_by_name_year("Shared Series", 2020) is None
    assert db.get_movie_by_name_year("Keep Movie", 2020) is not None
    assert db.get_series_by_name_year("Keep Series", 2020) is not None
    assert unchanged["catalog_changed"] is False
    assert unchanged["movies_created"] == 0
    assert unchanged["series_created"] == 0

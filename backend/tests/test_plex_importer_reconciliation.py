"""Plex imports reconcile sources against the complete raw library snapshot."""

import asyncio

import plex_client
import plex_importer


def test_plex_import_removes_source_from_deleted_library(db, monkeypatch):
    provider_id = db.upsert_provider(
        "Plex", "http://plex.invalid", "token", "token", provider_type="plex",
    )
    db.bulk_import_movies(provider_id, [{
        "name": "Old Library Copy", "year": 2025,
        "provider_stream_id": "old-part", "container_extension": "mkv",
        "provider_category_name": "Deleted Library",
    }])
    old_movie_id = db.get_movie_by_name_year("Old Library Copy", 2025)["id"]

    class FakeClient:
        def __init__(self, provider): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def list_libraries(self):
            return [{"type": "movie", "key": "1", "title": "Movies"}]
        async def list_movies(self, key):
            return [{"title": "Current Copy", "ratingKey": "22"}]

    monkeypatch.setattr(plex_client, "PlexClient", FakeClient)
    monkeypatch.setattr(plex_client, "extract_part", lambda item: ("current-part", "mkv"))
    monkeypatch.setattr(plex_client, "extract_common_fields", lambda item: {
        "genre": None, "description": None, "director": None,
        "cast_list": None, "tmdb_id": None, "rating": None, "release_date": None,
    })
    monkeypatch.setattr(plex_importer.vod_db, "bulk_import_plex_movies", lambda *_: {
        "movies_created": 0, "movies_matched": 0, "total": 1,
    })
    monkeypatch.setattr(plex_importer.vod_db, "bulk_import_plex_series", lambda *_: {
        "series_created": 0, "series_matched": 0, "episodes_imported": 0,
    })

    result = asyncio.run(plex_importer.import_plex_library(provider_id))

    assert result["removed"]["movie_sources_removed"] == 1
    assert not db.list_movie_sources(old_movie_id)


def test_plex_import_reconciles_raw_ids_even_when_category_is_excluded(db, monkeypatch):
    provider_id = db.upsert_provider(
        "Plex", "http://plex.invalid", "token", "token", provider_type="plex",
    )
    db.set_provider_import_exclude_categories(provider_id, ["Excluded Library"], False)
    captured = {}

    class FakeClient:
        def __init__(self, provider): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def list_libraries(self):
            return [{"type": "movie", "key": "1", "title": "Excluded Library"}]
        async def list_movies(self, key):
            return [{"title": "Excluded but present", "ratingKey": "22"}]

    monkeypatch.setattr(plex_client, "PlexClient", FakeClient)
    monkeypatch.setattr(plex_client, "extract_part", lambda item: ("present-part", "mkv"))
    monkeypatch.setattr(plex_client, "extract_common_fields", lambda item: {
        "genre": None, "description": None, "director": None,
        "cast_list": None, "tmdb_id": None, "rating": None, "release_date": None,
    })
    monkeypatch.setattr(plex_importer.vod_db, "bulk_import_plex_movies", lambda *_: {
        "movies_created": 0, "movies_matched": 0, "total": 0,
    })
    monkeypatch.setattr(plex_importer.vod_db, "bulk_import_plex_series", lambda *_: {
        "series_created": 0, "series_matched": 0, "episodes_imported": 0,
    })

    original_reconcile = plex_importer.vod_db.reconcile_provider_catalog_sources
    def capture_reconcile(provider_id, *, seen_movie_stream_ids, seen_series_ids):
        captured["movies"] = seen_movie_stream_ids
        return original_reconcile(
            provider_id,
            seen_movie_stream_ids=seen_movie_stream_ids,
            seen_series_ids=seen_series_ids,
        )
    monkeypatch.setattr(plex_importer.vod_db, "reconcile_provider_catalog_sources", capture_reconcile)

    asyncio.run(plex_importer.import_plex_library(provider_id))

    assert captured["movies"] == {"present-part"}


def test_plex_import_skips_cleanup_when_plex_reports_nothing(db, monkeypatch):
    provider_id = db.upsert_provider(
        "Plex", "http://plex.invalid", "token", "token", provider_type="plex",
    )
    db.bulk_import_movies(provider_id, [{
        "name": "Still There", "year": 2025,
        "provider_stream_id": "part-1", "container_extension": "mkv",
        "provider_category_name": "Movies",
    }])
    movie_id = db.get_movie_by_name_year("Still There", 2025)["id"]

    class EmptyClient:
        def __init__(self, provider): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def list_libraries(self):
            return []

    monkeypatch.setattr(plex_client, "PlexClient", EmptyClient)

    result = asyncio.run(plex_importer.import_plex_library(provider_id))

    assert result["removed"]["movie_sources_removed"] == 0
    assert db.list_movie_sources(movie_id)

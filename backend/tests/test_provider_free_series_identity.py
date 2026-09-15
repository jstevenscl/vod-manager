"""Regression coverage for provider-free known-series identity repair."""

import asyncio

import tmdb_sync
import vod_importer


def test_provider_free_tmdb_year_clears_review_and_merges_alias(db, monkeypatch):
    provider_id = db.upsert_provider("Example", "http://example.invalid", "user", "pass")
    db.bulk_import_series(provider_id, [
        {"name": "Canonical Show", "year": 2020, "provider_series_id": "known",
         "raw_name": "Canonical Show", "tmdb_id": "123", "_has_detail": True,
         "provider_category_name": None, "genre": None, "description": None, "cast_list": None,
         "director": None, "poster_url": None, "rating": None, "release_date": None,
         "provider_last_modified": None},
        {"name": "Provider Alias", "year": None, "provider_series_id": "missing-year",
         "raw_name": "Provider Alias", "tmdb_id": "123", "_has_detail": True,
         "provider_category_name": None, "genre": None, "description": None, "cast_list": None,
         "director": None, "poster_url": None, "rating": None, "release_date": None,
         "provider_last_modified": None},
    ])

    async def fake_identity(tmdb_id):
        assert tmdb_id == "123"
        return {"name": "Canonical Show", "year": 2020, "content_rating": "TV-14"}

    monkeypatch.setattr(tmdb_sync, "get_tv_identity", fake_identity)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])
    asyncio.run(vod_importer.reconcile_known_series_identities(concurrency=1))

    remaining = [series for series in db.list_series(limit=100) if series["tmdb_id"] == "123"]
    assert len(remaining) == 1
    assert remaining[0]["year"] == 2020
    assert remaining[0]["needs_year_review"] == 0


def test_metadata_review_confirmation_merges_same_tmdb_alias(db):
    existing_id = db.upsert_series("Canonical Show", 2020, tmdb_id="456")
    review_id = db.upsert_series("Provider Alias", None)

    db.resolve_year_review("series", review_id, 2020, "456")

    survivors = [db.get_series(series_id) for series_id in (existing_id, review_id)]
    assert len([series for series in survivors if series is not None]) == 1

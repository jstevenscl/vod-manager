"""Saved XC category exclusions skip intake and legacy cleanup is preview-first."""

import asyncio

import pytest
from fastapi import HTTPException

import vod_importer
import vod_routes


def _import_movie(db, provider_id, name, stream_id, category_name):
    db.bulk_import_movies(provider_id, [{
        "name": name,
        "year": 2001,
        "provider_stream_id": stream_id,
        "container_extension": "mp4",
        "provider_category_name": category_name,
        "raw_name": name,
        "auto_archive": False,
        "_has_detail": True,
    }])


def _import_series(db, provider_id, name, series_id, category_name):
    db.bulk_import_series(provider_id, [{
        "name": name,
        "year": 2001,
        "provider_series_id": series_id,
        "provider_category_name": category_name,
        "raw_name": name,
        "auto_archive": False,
        "_has_detail": True,
    }])


class FakeXCClient:
    def __init__(self, _provider):
        pass

    async def get_vod_categories(self):
        return [
            {"category_id": "1", "category_name": "Talk"},
            {"category_id": "2", "category_name": "Action"},
        ]

    async def get_series_categories(self):
        return []

    async def get_vod_streams(self):
        return [
            {"stream_id": "talk-1", "name": "Talk Movie (2001)", "category_id": "1"},
            {"stream_id": "action-1", "name": "Action Movie (2001)", "category_id": "2"},
        ]

    async def get_series(self):
        return []


def _configure_import(monkeypatch):
    monkeypatch.setattr(vod_importer, "XCProviderClient", FakeXCClient)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])


def test_saved_exclusion_is_not_created_on_import(db, monkeypatch):
    provider_id = db.upsert_provider("prov1", "http://example.invalid", "u", "p", provider_type="xc")
    db.set_provider_import_exclude_categories(provider_id, ["Talk"], False)
    _configure_import(monkeypatch)

    result = asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))

    assert result["movies_created"] == 1
    assert result["movies_skipped_excluded"] == 1
    assert db.get_movie_by_name_year("Talk Movie", 2001) is None
    assert db.get_movie_by_name_year("Action Movie", 2001) is not None


def test_preview_then_apply_removes_legacy_content_once(db, monkeypatch):
    provider_id = db.upsert_provider("prov1", "http://example.invalid", "u", "p", provider_type="xc")
    _configure_import(monkeypatch)

    first = asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))
    assert first["movies_created"] == 2
    db.set_provider_import_exclude_categories(provider_id, ["Talk"], False)

    # Import skips the saved exclusion but does not destructively clean up the
    # already-stored source without an explicit apply.
    second = asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))
    assert second["movies_created"] == 0
    assert second["movies_skipped_excluded"] == 1
    assert db.get_movie_by_name_year("Talk Movie", 2001) is not None

    preview = asyncio.run(vod_routes.purge_excluded_content(provider_id))
    assert preview["dry_run"] is True
    assert preview["movie_sources_removed"] == 1
    assert preview["movies_deleted"] == 1
    assert preview["sample_movies"] == ["Talk Movie"]
    assert db.get_movie_by_name_year("Talk Movie", 2001) is not None

    applied = asyncio.run(vod_routes.purge_excluded_content(provider_id, dry_run=False))
    assert applied["dry_run"] is False
    assert applied["movies_deleted"] == 1
    assert db.get_movie_by_name_year("Talk Movie", 2001) is None

    # The next unchanged import neither recreates nor re-purges the title.
    third = asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))
    assert third["movies_created"] == 0
    assert third["movies_skipped_excluded"] == 1
    assert asyncio.run(vod_routes.purge_excluded_content(provider_id))["movie_sources_removed"] == 0


def test_proposed_preview_does_not_save_or_allow_cleanup_before_save(db):
    provider_id = db.upsert_provider("prov1", "http://example.invalid", "u", "p", provider_type="xc")
    _import_movie(db, provider_id, "Talk Movie", "talk-1", "Talk")
    proposal = vod_routes.ProviderImportExcludeCategoriesRequest(category_names=["Talk"], exclude_uncategorized=False)

    preview = asyncio.run(vod_routes.purge_excluded_content(provider_id, body=proposal))

    assert preview["provider_id"] == provider_id
    assert preview["saved_category_names"] == []
    assert preview["proposed_category_names"] == ["Talk"]
    assert preview["movies_deleted"] == 1
    assert db.get_provider(provider_id)["import_exclude_categories"] == []
    assert db.get_movie_by_name_year("Talk Movie", 2001) is not None
    with pytest.raises(HTTPException) as exc:
        asyncio.run(vod_routes.purge_excluded_content(provider_id, dry_run=False, body=proposal))
    assert exc.value.status_code == 400


def test_archive_new_category_is_stored_archived_not_skipped(db, monkeypatch):
    provider_id = db.upsert_provider("prov1", "http://example.invalid", "u", "p", provider_type="xc")
    db.set_provider_archive_new_categories(provider_id, True)
    db.set_provider_known_import_categories(provider_id, ["Action"])
    _configure_import(monkeypatch)

    result = asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))

    movie = db.get_movie_by_name_year("Talk Movie", 2001)
    assert result["movies_skipped_excluded"] == 0
    assert movie is not None
    assert movie["review_excluded"] == 1


def test_apply_keeps_card_backed_by_another_provider(db):
    provider_a = db.upsert_provider("prov1", "http://a.invalid", "u", "p")
    provider_b = db.upsert_provider("prov2", "http://b.invalid", "u", "p")
    _import_movie(db, provider_a, "Shared Movie", "a-1", "Talk")
    _import_movie(db, provider_b, "Shared Movie", "b-1", "Action")
    movie = db.get_movie_by_name_year("Shared Movie", 2001)

    result = db.purge_excluded_category_sources(provider_a, ["Talk"], False, dry_run=False)

    assert result["movie_sources_removed"] == 1
    assert result["movies_deleted"] == 0
    assert {source["provider_stream_id"] for source in db.list_movie_sources(movie["id"])} == {"b-1"}


def test_apply_skips_manually_curated_card(db):
    provider_id = db.upsert_provider("prov1", "http://example.invalid", "u", "p")
    _import_movie(db, provider_id, "Hand Kept", "s-1", "Talk")
    movie = db.get_movie_by_name_year("Hand Kept", 2001)
    db.bulk_set_review_excluded("movie", [movie["id"]], False)

    result = db.purge_excluded_category_sources(provider_id, ["Talk"], False, dry_run=False)

    assert result["movie_sources_removed"] == 0
    assert db.get_movie(movie["id"]) is not None


def test_apply_removes_series_episode_sources_and_sourceless_card(db):
    provider_id = db.upsert_provider("prov1", "http://example.invalid", "u", "p")
    _import_series(db, provider_id, "Talk Show", "series-1", "Talk")
    series = db.get_series_by_name_year("Talk Show", 2001)
    db.enrich_series_episodes_batch(series["id"], provider_id, [{
        "season_number": 1,
        "episode_number": 1,
        "name": "Episode 1",
        "provider_stream_id": "episode-1",
        "container_extension": "mp4",
    }])

    result = db.purge_excluded_category_sources(provider_id, ["Talk"], False, dry_run=False)

    assert result["series_sources_removed"] == 1
    assert result["episode_sources_removed"] == 1
    assert result["series_deleted"] == 1
    assert db.get_series_by_name_year("Talk Show", 2001) is None


def test_purge_route_rejects_non_xc_provider(db):
    provider_id = db.upsert_provider(
        "plex1", "http://plex.invalid", "u", "p", provider_type="plex",
    )
    db.set_provider_import_exclude_categories(provider_id, [], True)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(vod_routes.purge_excluded_content(provider_id, dry_run=False))

    assert exc.value.status_code == 400

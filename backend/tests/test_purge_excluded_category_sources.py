"""Content imported BEFORE a category was added to a provider's
import_exclude_categories is never removed: reconcile_provider_catalog_sources
compares against the raw (unfiltered) snapshot, so excluded items still count
as "seen", and purge_excluded_archived_content only touches archived rows.
purge_excluded_category_sources removes the provider's sources in excluded
categories and deletes only cards left with no source at all."""

import vod_db


def _import_movie(db, provider_id, name, stream_id, category_name=None):
    db.bulk_import_movies(provider_id, [{
        "name": name, "year": 2001, "provider_stream_id": stream_id,
        "container_extension": "mp4", "provider_category_name": category_name,
        "raw_name": name, "auto_archive": False, "_has_detail": True,
    }])


def _import_series(db, provider_id, name, series_id, category_name=None):
    db.bulk_import_series(provider_id, [{
        "name": name, "year": 2001, "provider_series_id": series_id,
        "provider_category_name": category_name, "raw_name": name,
        "auto_archive": False, "_has_detail": True,
    }])


def test_deletes_active_movie_whose_only_source_is_in_excluded_category(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "u", "p")
    _import_movie(db, provider_id, "Nordic Music Video", "s-1", "NORDIC MUSIC")

    result = vod_db.purge_excluded_category_sources(provider_id, ["NORDIC MUSIC"], False)

    assert result["movie_sources_removed"] == 1
    assert result["movies_deleted"] == 1
    assert db.get_movie_by_name_year("Nordic Music Video", 2001) is None


def test_keeps_card_that_still_has_a_source_in_a_kept_category(db):
    provider_a = db.upsert_provider("prov1", "http://a.invalid", "u", "p")
    provider_b = db.upsert_provider("prov2", "http://b.invalid", "u", "p")
    _import_movie(db, provider_a, "Shared Movie", "a-1", "NORDIC MOVIES")
    _import_movie(db, provider_b, "Shared Movie", "b-1", "Action")
    movie = db.get_movie_by_name_year("Shared Movie", 2001)

    result = vod_db.purge_excluded_category_sources(provider_a, ["NORDIC MOVIES"], False)

    assert result["movie_sources_removed"] == 1
    assert result["movies_deleted"] == 0
    assert {s["provider_stream_id"] for s in db.list_movie_sources(movie["id"])} == {"b-1"}


def test_does_not_touch_sources_in_non_excluded_categories(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "u", "p")
    _import_movie(db, provider_id, "Action Flick", "s-1", "Action")

    result = vod_db.purge_excluded_category_sources(provider_id, ["NORDIC MUSIC"], False)

    assert result["movie_sources_removed"] == 0
    assert db.get_movie_by_name_year("Action Flick", 2001) is not None


def test_uncategorized_sources_removed_only_when_exclude_uncategorized(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "u", "p")
    _import_movie(db, provider_id, "No Category", "s-1", None)

    kept = vod_db.purge_excluded_category_sources(provider_id, [], False)
    assert kept["movie_sources_removed"] == 0
    assert db.get_movie_by_name_year("No Category", 2001) is not None

    removed = vod_db.purge_excluded_category_sources(provider_id, [], True)
    assert removed["movies_deleted"] == 1
    assert db.get_movie_by_name_year("No Category", 2001) is None


def test_skips_manually_curated_cards(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "u", "p")
    _import_movie(db, provider_id, "Hand Kept", "s-1", "NORDIC MUSIC")
    movie = db.get_movie_by_name_year("Hand Kept", 2001)
    db.bulk_set_review_excluded("movie", [movie["id"]], False)  # stamps review_excluded_manual=1

    result = vod_db.purge_excluded_category_sources(provider_id, ["NORDIC MUSIC"], False)

    assert result["movie_sources_removed"] == 0
    assert db.get_movie_by_name_year("Hand Kept", 2001) is not None


def test_only_this_providers_sources_are_considered(db):
    provider_a = db.upsert_provider("prov1", "http://a.invalid", "u", "p")
    provider_b = db.upsert_provider("prov2", "http://b.invalid", "u", "p")
    _import_movie(db, provider_b, "Other Provider Nordic", "b-1", "NORDIC MUSIC")

    result = vod_db.purge_excluded_category_sources(provider_a, ["NORDIC MUSIC"], False)

    assert result["movie_sources_removed"] == 0
    assert db.get_movie_by_name_year("Other Provider Nordic", 2001) is not None


def test_deletes_series_whose_only_source_is_in_excluded_category(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "u", "p")
    _import_series(db, provider_id, "Nordic Show", "ser-1", "NORDIC SERIES")
    series = db.get_series_by_name_year("Nordic Show", 2001)
    db.enrich_series_episodes_batch(series["id"], provider_id, [{
        "season_number": 1, "episode_number": 1, "name": "Ep 1",
        "provider_stream_id": "ep-1", "container_extension": "mp4",
    }])

    result = vod_db.purge_excluded_category_sources(provider_id, ["NORDIC SERIES"], False)

    assert result["series_sources_removed"] == 1
    assert result["episode_sources_removed"] == 1
    assert result["series_deleted"] == 1
    assert db.get_series_by_name_year("Nordic Show", 2001) is None


def test_dry_run_reports_exact_counts_and_samples_without_writing(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "u", "p")
    _import_movie(db, provider_id, "Nordic Music Video", "s-1", "NORDIC MUSIC")
    _import_series(db, provider_id, "Nordic Show", "ser-1", "NORDIC SERIES")

    preview = vod_db.purge_excluded_category_sources(
        provider_id, ["NORDIC MUSIC", "NORDIC SERIES"], False, dry_run=True,
    )

    assert preview["dry_run"] is True
    assert preview["movies_deleted"] == 1
    assert preview["series_deleted"] == 1
    assert preview["sample_movies"] == ["Nordic Music Video"]
    assert preview["sample_series"] == ["Nordic Show"]
    assert db.get_movie_by_name_year("Nordic Music Video", 2001) is not None
    assert db.get_series_by_name_year("Nordic Show", 2001) is not None


def test_import_purges_previously_imported_content_in_newly_excluded_category(db, monkeypatch):
    import asyncio
    import vod_importer

    provider_id = db.upsert_provider("prov1", "http://a.invalid", "u", "p", provider_type="xc")

    class FakeClient:
        def __init__(self, _provider):
            pass

        async def get_vod_categories(self):
            return [{"category_id": "1", "category_name": "NORDIC MUSIC"},
                    {"category_id": "2", "category_name": "Action"}]

        async def get_series_categories(self):
            return []

        async def get_vod_streams(self):
            return [{"stream_id": "nordic", "name": "Nordic Video (2001)", "category_id": "1"},
                    {"stream_id": "action", "name": "Action Flick (2001)", "category_id": "2"}]

        async def get_series(self):
            return []

    monkeypatch.setattr(vod_importer, "XCProviderClient", FakeClient)
    monkeypatch.setattr(vod_importer.vod_db, "get_active_rules_for_field", lambda *_: [])

    asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))
    assert db.get_movie_by_name_year("Nordic Video", 2001) is not None

    db.set_provider_import_exclude_categories(provider_id, ["NORDIC MUSIC"], False)
    result = asyncio.run(vod_importer.import_provider_catalog(provider_id, schedule_enrichment=False))

    assert db.get_movie_by_name_year("Nordic Video", 2001) is None
    assert db.get_movie_by_name_year("Action Flick", 2001) is not None
    assert result["excluded_category_purge"]["movies_deleted"] == 1


def test_route_previews_then_applies_using_saved_exclusions(db):
    import asyncio
    import vod_routes

    provider_id = db.upsert_provider("prov1", "http://example.com", "u", "p")
    _import_movie(db, provider_id, "Nordic Music Video", "s-1", "NORDIC MUSIC")
    db.set_provider_import_exclude_categories(provider_id, ["NORDIC MUSIC"], False)

    preview = asyncio.run(vod_routes.purge_excluded_content(provider_id, dry_run=True))
    assert preview["dry_run"] is True
    assert preview["movies_deleted"] == 1
    assert db.get_movie_by_name_year("Nordic Music Video", 2001) is not None

    applied = asyncio.run(vod_routes.purge_excluded_content(provider_id, dry_run=False))
    assert applied["movies_deleted"] == 1
    assert "affected_movie_ids" not in applied
    assert db.get_movie_by_name_year("Nordic Music Video", 2001) is None

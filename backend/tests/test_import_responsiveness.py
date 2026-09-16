"""Regression coverage for responsive, serialized catalog imports."""

import asyncio

import vod_importer


def test_catalog_item_builders_keep_raw_snapshot_when_items_are_filtered(monkeypatch):
    monkeypatch.setattr(vod_importer, "_should_exclude_from_import", lambda name, *_args, **_kwargs: name == "Skip")

    movies, movie_ids = vod_importer._build_movie_import_items(
        [
            {"stream_id": "keep", "name": "Keep (2020)", "category_id": "1"},
            {"stream_id": "skip", "name": "Skip (2020)", "category_id": "1"},
        ],
        {"1": "Movies"}, [], False, {"enabled_languages": ["EN"], "exclude_non_latin": False}, [],
    )
    series, series_ids = vod_importer._build_series_import_items(
        [
            {"series_id": "keep", "name": "Keep (2020)", "category_id": "1"},
            {"series_id": "skip", "name": "Skip (2020)", "category_id": "1"},
        ],
        {"1": "Series"}, [], False, {"enabled_languages": ["EN"], "exclude_non_latin": False}, [],
        {field: [] for field in ("genre", "description", "cast_list", "director")},
    )

    assert [item["provider_stream_id"] for item in movies] == ["keep"]
    assert movie_ids == {"keep", "skip"}
    assert [item["provider_series_id"] for item in series] == ["keep"]
    assert series_ids == {"keep", "skip"}


def test_xc_imports_are_serialized(monkeypatch):
    active = 0
    peak = 0

    async def fake_impl(_provider_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"ok": True}

    monkeypatch.setattr(vod_importer.vod_db, "get_provider", lambda provider_id: {"id": provider_id, "name": str(provider_id)})
    monkeypatch.setattr(vod_importer, "_import_provider_catalog_impl", fake_impl)

    async def run():
        await asyncio.gather(
            vod_importer.import_provider_catalog(1, schedule_enrichment=False),
            vod_importer.import_provider_catalog(2, schedule_enrichment=False),
        )

    asyncio.run(run())
    assert peak == 1


def test_non_xc_import_lifecycle_updates_shared_sidebar_status():
    previous = vod_importer.get_import_progress()
    previous_workflow = vod_importer.get_catalog_workflow_progress()
    try:
        vod_importer.mark_import_queued(42, "Plex", 1)
        assert vod_importer.get_import_progress()["queued"] is True
        assert vod_importer.get_catalog_workflow_progress()["state"] == "queued"

        vod_importer.mark_import_running(42, "Plex")
        running = vod_importer.get_import_progress()
        assert running["running"] is True
        assert running["queued"] is False
        assert running["provider_name"] == "Plex"
        assert vod_importer.get_catalog_workflow_progress()["phase"] == "Importing provider catalog"

        vod_importer.mark_import_finished(42)
        finished = vod_importer.get_import_progress()
        assert finished["running"] is False
        assert finished["queued"] is False
        assert finished["error"] is None

        vod_importer.mark_catalog_workflow_ready()
        ready = vod_importer.get_catalog_workflow_progress()
        assert ready["state"] == "ready"
        assert ready["finished_at"] is not None
    finally:
        vod_importer._IMPORT_PROGRESS.clear()
        vod_importer._IMPORT_PROGRESS.update(previous)
        vod_importer._CATALOG_WORKFLOW_PROGRESS.clear()
        vod_importer._CATALOG_WORKFLOW_PROGRESS.update(previous_workflow)


def test_review_summary_matches_visible_metadata_review_counts(db):
    movie_id = db.upsert_movie("Needs identity", None)
    series_id = db.upsert_series("Series needs identity", None)
    adult_id = db.upsert_movie("Hidden adult", None)
    db.set_movie_adult(adult_id, True)
    invalid_id = db.upsert_movie("Incorrect TMDB", 2024, tmdb_id="gone")
    db.record_tmdb_lookup_failure("movie", invalid_id, "gone")

    assert movie_id and series_id
    assert db.get_review_summary() == {
        "missing_identity": {"movies": 1, "series": 1},
        "invalid_tmdb": {"movies": 1, "series": 0},
    }


def test_unchanged_sources_do_not_rewrite_movies_or_series(db):
    provider_id = db.upsert_provider("Provider", "http://provider.invalid", "u", "p", provider_type="xc")
    movie = {
        "name": "Movie", "year": 2020, "provider_stream_id": "movie-1",
        "container_extension": "mp4", "provider_category_name": "Movies",
        "raw_name": "Movie (2020)", "catalog_fingerprint": "movie-v1",
    }
    series = {
        "name": "Series", "year": 2020, "provider_series_id": "series-1",
        "provider_category_name": "Series", "raw_name": "Series (2020)",
        "catalog_fingerprint": "series-v1", "_has_detail": True,
        "genre": "Drama", "description": "Description", "cast_list": None,
        "director": None, "poster_url": None, "rating": None,
        "release_date": None, "tmdb_id": None, "provider_last_modified": "100",
    }

    assert db.bulk_import_movies(provider_id, [movie])["sources_changed"] == 1
    assert db.bulk_import_series(provider_id, [series])["sources_changed"] == 1

    second_movies = db.bulk_import_movies(provider_id, [movie])
    second_series = db.bulk_import_series(provider_id, [series])

    assert second_movies["sources_changed"] == 0
    assert second_movies["changed_movie_ids"] == []
    assert second_series["sources_changed"] == 0
    assert second_series["changed_series_ids"] == []

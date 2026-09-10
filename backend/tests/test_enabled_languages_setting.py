import config


def _make_foreign_movie(db, provider_id, name, year, stream_id, prefix):
    db.bulk_import_movies(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_stream_id": stream_id,
            "container_extension": "mp4",
            "provider_category_name": "Drama",
            "auto_archive": False,
            "raw_name": f"{prefix} - {name}",
        },
    ])


def test_streaming_sources_respect_custom_enabled_languages(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _make_foreign_movie(db, provider_id, "Frost", 2020, "se-1", "SE")

    movie = db.get_movie_by_name_year("Frost", 2020)

    # Default allow-list (EN/ES) excludes the Swedish-tagged source.
    assert db.list_movie_sources_for_streaming(movie["id"]) == []

    # Widening the allow-list to include SE makes it eligible immediately --
    # no re-import, just a live query-time filter.
    config.save_enabled_languages(["EN", "ES", "SE"])
    sources = db.list_movie_sources_for_streaming(movie["id"])
    assert {s["provider_stream_id"] for s in sources} == {"se-1"}


def test_streaming_sources_exclude_language_removed_from_enabled_set(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {
            "name": "Coco", "year": 2017, "provider_stream_id": "es-1",
            "container_extension": "mp4", "raw_name": "ES - Coco",
        },
    ])
    movie = db.get_movie_by_name_year("Coco", 2017)

    assert {s["provider_stream_id"] for s in db.list_movie_sources_for_streaming(movie["id"])} == {"es-1"}

    # Removing ES from the enabled set excludes it, even though it was
    # allowed under the old hardcoded default.
    config.save_enabled_languages(["EN"])
    assert db.list_movie_sources_for_streaming(movie["id"]) == []


def test_episode_streaming_sources_respect_custom_enabled_languages(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    series_id = db.upsert_series("Some Show", 2020)
    episode_id = db.add_episode(series_id, 1, 1, "Pilot")
    db.add_episode_source(episode_id, provider_id, "se-1", raw_name="SE - Pilot")

    assert db.list_episode_sources_for_streaming(episode_id) == []

    config.save_enabled_languages(["EN", "ES", "SE"])
    sources = db.list_episode_sources_for_streaming(episode_id)
    assert {s["provider_stream_id"] for s in sources} == {"se-1"}


def test_export_rows_respect_custom_enabled_languages(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _make_foreign_movie(db, provider_id, "Frost", 2020, "se-1", "SE")
    movie = db.get_movie_by_name_year("Frost", 2020)
    category_id = db.upsert_category("Drama Movies", "movie")
    db.place_movie_in_category(movie["id"], category_id)

    assert all(r["movie_id"] != movie["id"] for r in db.get_movie_export_rows())

    config.save_enabled_languages(["EN", "ES", "SE"])
    rows = db.get_movie_export_rows()
    assert any(r["movie_id"] == movie["id"] for r in rows)


def test_language_impact_preview_counts_movies_and_episodes_that_would_lose_access(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _make_foreign_movie(db, provider_id, "Frost", 2020, "se-1", "SE")
    db.bulk_import_movies(provider_id, [
        {
            "name": "Coco", "year": 2017, "provider_stream_id": "es-1",
            "container_extension": "mp4", "raw_name": "ES - Coco",
        },
    ])
    series_id = db.upsert_series("Some Show", 2020)
    episode_id = db.add_episode(series_id, 1, 1, "Pilot")
    db.add_episode_source(episode_id, provider_id, "es-2", raw_name="ES - Pilot")

    # Proposing to drop ES from the currently-enabled default (EN, ES):
    # Coco (movie) and the episode both currently rely on an ES-only source.
    impact = db.preview_enabled_languages_impact(["EN"])

    assert impact["movies_losing_access"] == 1
    assert impact["episodes_losing_access"] == 1

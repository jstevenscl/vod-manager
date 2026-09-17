"""Provider-supplied trailer preservation regression coverage.

Scoped to what actually got ported: capturing a trailer value a provider's
own bulk catalog list already supplies (movie_items/series_items' "trailer"
key in vod_importer.py, applied via apply_provider_trailers). The TMDB-
lookup fallback for titles with no provider-supplied trailer exists as
schema-only groundwork on knmplace's own fork too (trailer_status/attempts/
checked_at/last_error columns with no wired caller as of 2026-09-16) --
not ported here since there's nothing working yet to port."""


def test_provider_trailer_is_saved_for_movie_and_series(db):
    provider = db.upsert_provider("Synthetic Provider", "http://example.invalid", "u", "p")
    db.bulk_import_movies(provider, [{
        "name": "Fixture Movie", "year": 2024, "provider_stream_id": "m1",
        "container_extension": "mp4", "tmdb_id": "101",
    }])
    db.bulk_import_series(provider, [{
        "name": "Fixture Series", "year": 2024, "provider_series_id": "s1", "tmdb_id": "202",
        "provider_category_name": None, "raw_name": "Fixture Series", "_has_detail": True,
    }])
    assert db.apply_provider_trailers(provider, "movie", [{"provider_stream_id": "m1", "trailer": "movie-key"}]) == 1
    assert db.apply_provider_trailers(provider, "series", [{"provider_series_id": "s1", "youtube_trailer": "series-key"}]) == 1

    movie_id = db.list_all_movie_ids()[0]
    assert db.get_movie(movie_id)["trailer_key"] == "movie-key"
    series_row = db.list_series(limit=10)[0]
    assert series_row["trailer_key"] == "series-key"


def test_provider_trailer_missing_or_blank_is_skipped(db):
    provider = db.upsert_provider("Synthetic Provider", "http://example.invalid", "u", "p")
    db.bulk_import_movies(provider, [{
        "name": "Fixture Movie", "year": 2024, "provider_stream_id": "m1",
        "container_extension": "mp4", "tmdb_id": "101",
    }])
    assert db.apply_provider_trailers(provider, "movie", [{"provider_stream_id": "m1"}]) == 0
    assert db.apply_provider_trailers(provider, "movie", [{"provider_stream_id": "m1", "trailer": "   "}]) == 0
    movie_id = db.list_all_movie_ids()[0]
    assert db.get_movie(movie_id)["trailer_key"] is None
    assert db.get_movie(movie_id)["trailer_status"] == "unknown"


def test_provider_trailer_exposed_in_xc_export(db):
    provider = db.upsert_provider("Synthetic Provider", "http://example.invalid", "u", "p")
    db.bulk_import_movies(provider, [{
        "name": "Fixture Movie", "year": 2024, "provider_stream_id": "m1",
        "container_extension": "mp4", "tmdb_id": "101",
    }])
    db.apply_provider_trailers(provider, "movie", [{"provider_stream_id": "m1", "trailer": "movie-key"}])
    movie_id = db.list_all_movie_ids()[0]
    category_id = db.upsert_category("Movies", content_type="movie")
    db.place_movie_in_category(movie_id, category_id)

    rows = db.get_movie_export_rows()
    assert rows[0]["trailer_key"] == "movie-key"
    row_by_stream = db.get_movie_export_row_by_stream_id(rows[0]["export_stream_id"])
    assert row_by_stream["trailer_key"] == "movie-key"

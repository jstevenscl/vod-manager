"""Provider-supplied trailer preservation regression coverage."""

def test_provider_trailer_is_saved_for_movie_and_series(db):
    provider = db.upsert_provider("Synthetic Provider", "http://example.invalid", "u", "p")
    db.bulk_import_movies(provider, [{"name": "Fixture Movie", "year": 2024,
        "provider_stream_id": "m1", "container_extension": "mp4", "tmdb_id": "101"}])
    db.bulk_import_series(provider, [{"name": "Fixture Series", "year": 2024,
        "provider_series_id": "s1", "tmdb_id": "202", "provider_category_name": None,
        "raw_name": "Fixture Series", "_has_detail": True}])
    assert db.apply_provider_trailers(provider, "movie", [{"provider_stream_id": "m1", "trailer": "movie-key"}]) == 1
    assert db.apply_provider_trailers(provider, "series", [{"provider_series_id": "s1", "youtube_trailer": "series-key"}]) == 1
    assert db.get_movie(db.list_all_movie_ids()[0])["trailer_key"] == "movie-key"
    assert db.list_series(limit=10)[0]["trailer_key"] == "series-key"


def test_provider_trailer_queue_excludes_saved_rows(db):
    provider = db.upsert_provider("Synthetic Provider", "http://example.invalid", "u", "p")
    db.bulk_import_movies(provider, [{"name": "Fixture Movie", "year": 2024,
        "provider_stream_id": "m1", "container_extension": "mp4", "tmdb_id": "101"}])
    db.apply_provider_trailers(provider, "movie", [{"provider_stream_id": "m1", "trailer": "movie-key"}])
    assert db.list_pending_trailer_enrichment(10) == []

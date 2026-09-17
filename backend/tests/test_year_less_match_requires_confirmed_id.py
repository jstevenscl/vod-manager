"""A year-less import item matching an existing row by name alone must not
silently attach to it unless that existing row already carries a confirmed
tmdb_id -- otherwise two different real titles that happen to share a name
could get merged as if they were sources of the same movie/series (found
live on @Knm's fork, "Inherit confirmed identities across providers",
2026-09-16)."""


def test_year_less_match_skipped_without_confirmed_tmdb_id(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Ambiguous Title", "year": 2020, "provider_stream_id": "1", "container_extension": "mkv"},
    ])
    assert len(db.list_all_movie_ids()) == 1

    # Second provider supplies the exact same name but NO year, and the
    # existing row has no tmdb_id -- must NOT attach, must create its own row.
    result = db.bulk_import_movies(provider_id, [
        {"name": "Ambiguous Title", "year": None, "provider_stream_id": "2", "container_extension": "mkv"},
    ])
    assert result["movies_created"] == 1
    assert len(db.list_all_movie_ids()) == 2


def test_year_less_match_attaches_when_candidate_has_confirmed_tmdb_id(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Known Title", "year": 2020, "provider_stream_id": "1",
         "container_extension": "mkv", "tmdb_id": "555"},
    ])
    assert len(db.list_all_movie_ids()) == 1

    result = db.bulk_import_movies(provider_id, [
        {"name": "Known Title", "year": None, "provider_stream_id": "2", "container_extension": "mkv"},
    ])
    assert result["movies_matched"] == 1
    assert result["movies_created"] == 0
    assert len(db.list_all_movie_ids()) == 1


def test_series_year_less_match_requires_confirmed_tmdb_id(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_series(provider_id, [
        {"name": "Ambiguous Series", "year": 2020, "provider_series_id": "1",
         "provider_category_name": None, "raw_name": "Ambiguous Series", "_has_detail": True},
    ])
    assert len(db.list_series(limit=10)) == 1

    db.bulk_import_series(provider_id, [
        {"name": "Ambiguous Series", "year": None, "provider_series_id": "2",
         "provider_category_name": None, "raw_name": "Ambiguous Series", "_has_detail": True},
    ])
    assert len(db.list_series(limit=10)) == 2

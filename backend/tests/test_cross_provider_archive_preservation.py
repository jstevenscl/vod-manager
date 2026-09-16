"""A matching source must not resurrect an archived catalog item."""


def test_archived_series_stays_archived_when_second_provider_matches(db):
    first = db.upsert_provider("Provider One", "http://one.invalid", "u", "p")
    second = db.upsert_provider("Provider Two", "http://two.invalid", "u", "p")
    db.bulk_import_series(first, [{
        "name": "Archived Show", "year": 2020, "provider_series_id": "one-1",
        "tmdb_id": "789", "auto_archive": True, "_has_detail": True,
    }])
    db.bulk_import_series(second, [{
        "name": "Archived Show", "year": None, "provider_series_id": "two-1",
        "tmdb_id": None, "auto_archive": False, "_has_detail": True,
    }])
    rows = db.list_series(limit=10, archived=True)
    assert len(rows) == 1
    assert rows[0]["review_excluded"] == 1


def test_archived_movie_stays_archived_when_second_provider_matches(db):
    first = db.upsert_provider("Provider One", "http://one.invalid", "u", "p")
    second = db.upsert_provider("Provider Two", "http://two.invalid", "u", "p")
    db.bulk_import_movies(first, [{
        "name": "Archived Movie", "year": 2020, "provider_stream_id": "one-1",
        "tmdb_id": "987", "auto_archive": True, "_has_detail": True,
    }])
    db.bulk_import_movies(second, [{
        "name": "Archived Movie", "year": None, "provider_stream_id": "two-1",
        "tmdb_id": None, "auto_archive": False, "_has_detail": True,
    }])
    rows = db.list_movies(limit=10, archived=True)
    assert len(rows) == 1
    assert rows[0]["review_excluded"] == 1

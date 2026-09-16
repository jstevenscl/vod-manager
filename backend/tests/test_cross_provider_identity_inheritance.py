"""An undated second provider should inherit one confirmed canonical identity."""

def test_undated_series_inherits_unique_confirmed_tmdb_row(db):
    first = db.upsert_provider("Provider One", "http://one.invalid", "u", "p")
    second = db.upsert_provider("Provider Two", "http://two.invalid", "u", "p")
    db.bulk_import_series(first, [{"name": "Example Show", "year": 2020,
        "provider_series_id": "one-1", "tmdb_id": "123", "provider_category_name": None,
        "raw_name": "Example Show", "_has_detail": True}])
    db.bulk_import_series(second, [{"name": "Example Show", "year": None,
        "provider_series_id": "two-1", "tmdb_id": None, "provider_category_name": None,
        "raw_name": "Example Show", "_has_detail": True}])
    rows = db.list_series(limit=10)
    assert len(rows) == 1
    assert rows[0]["tmdb_id"] == "123"
    assert len(db.list_series_sources(rows[0]["id"])) == 2


def test_undated_series_with_ambiguous_candidates_stays_separate(db):
    first = db.upsert_provider("Provider One", "http://one.invalid", "u", "p")
    second = db.upsert_provider("Provider Two", "http://two.invalid", "u", "p")
    db.bulk_import_series(first, [
        {"name": "Example Show", "year": 2020, "provider_series_id": "one-1", "tmdb_id": "123", "_has_detail": True},
        {"name": "Example Show", "year": 2021, "provider_series_id": "one-2", "tmdb_id": "456", "_has_detail": True},
    ])
    db.bulk_import_series(second, [{"name": "Example Show", "year": None,
        "provider_series_id": "two-1", "tmdb_id": None, "_has_detail": True}])
    assert len(db.list_series(limit=10)) == 3

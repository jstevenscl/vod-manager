"""Regression coverage for provider-free known-series identity repair."""

def test_metadata_review_confirmation_merges_same_tmdb_alias(db):
    existing_id = db.upsert_series("Canonical Show", 2020, tmdb_id="456")
    review_id = db.upsert_series("Provider Alias", None)

    db.resolve_year_review("series", review_id, 2020, "456")

    survivors = [db.get_series(series_id) for series_id in (existing_id, review_id)]
    assert len([series for series in survivors if series is not None]) == 1

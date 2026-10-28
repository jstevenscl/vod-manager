"""list_metadata_review surfaces actionable identity gaps."""


def test_actionable_gap_without_year_review_flag_is_included(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "No Identity At All", "year": None, "provider_stream_id": "1", "container_extension": "mkv"},
    ])

    review = db.list_metadata_review("movie")
    names = {m["name"] for m in review["movies"]}
    assert "No Identity At All" in names


def test_row_with_year_but_no_tmdb_id_is_included(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Has A Year", "year": 2020, "provider_stream_id": "1", "container_extension": "mkv"},
    ])

    review = db.list_metadata_review("movie")
    names = {m["name"] for m in review["movies"]}
    assert "Has A Year" in names


def test_archived_row_is_excluded(db):
    movie_id = db.upsert_movie("Gapped But Archived", None)
    db.bulk_set_review_excluded("movie", [movie_id], True)

    review = db.list_metadata_review("movie")
    names = {m["name"] for m in review["movies"]}
    assert "Gapped But Archived" not in names


def test_content_type_filter_scopes_to_one_table(db):
    db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.upsert_movie("Gapped Movie", None)
    db.upsert_series("Gapped Series", None)

    movies_only = db.list_metadata_review("movie")
    assert "movies" in movies_only
    assert "series" not in movies_only

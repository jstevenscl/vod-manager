"""Incorrect TMDB ID review queue (vod_db.record_tmdb_lookup_failure /
clear_tmdb_lookup_failure / list_tmdb_lookup_failures) -- for a stored
tmdb_id that TMDB itself has confirmed (via a 404) no longer exists.
Distinct from Metadata Review, which is for items with no id at all."""


def test_record_and_list_lookup_failure(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Stale ID Movie", "year": 2020, "provider_stream_id": "1",
         "container_extension": "mkv", "tmdb_id": "999999"},
    ])
    movie_id = db.list_all_movie_ids()[0]

    db.record_tmdb_lookup_failure("movie", movie_id, "999999")

    queue = db.list_tmdb_lookup_failures("movie")
    assert len(queue["movies"]) == 1
    assert queue["movies"][0]["id"] == movie_id
    assert queue["movies"][0]["invalid_tmdb_id"] == "999999"
    assert queue["movies"][0]["attempts"] == 1


def test_repeated_failure_increments_attempts_not_duplicates(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Stale ID Movie", "year": 2020, "provider_stream_id": "1",
         "container_extension": "mkv", "tmdb_id": "999999"},
    ])
    movie_id = db.list_all_movie_ids()[0]

    db.record_tmdb_lookup_failure("movie", movie_id, "999999")
    db.record_tmdb_lookup_failure("movie", movie_id, "999999")

    queue = db.list_tmdb_lookup_failures("movie")
    assert len(queue["movies"]) == 1
    assert queue["movies"][0]["attempts"] == 2


def test_clear_lookup_failure_removes_from_queue(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Stale ID Movie", "year": 2020, "provider_stream_id": "1",
         "container_extension": "mkv", "tmdb_id": "999999"},
    ])
    movie_id = db.list_all_movie_ids()[0]
    db.record_tmdb_lookup_failure("movie", movie_id, "999999")

    db.clear_tmdb_lookup_failure("movie", movie_id)

    assert db.list_tmdb_lookup_failures("movie")["movies"] == []


def test_set_tmdb_id_clears_lookup_failure(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Stale ID Movie", "year": 2020, "provider_stream_id": "1",
         "container_extension": "mkv", "tmdb_id": "999999"},
    ])
    movie_id = db.list_all_movie_ids()[0]
    db.record_tmdb_lookup_failure("movie", movie_id, "999999")

    db.set_tmdb_id("movie", movie_id, 12345)

    assert db.list_tmdb_lookup_failures("movie")["movies"] == []
    assert db.get_movie(movie_id)["tmdb_id"] == "12345"


def test_clear_tmdb_id_clears_lookup_failure(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Stale ID Movie", "year": 2020, "provider_stream_id": "1",
         "container_extension": "mkv", "tmdb_id": "999999"},
    ])
    movie_id = db.list_all_movie_ids()[0]
    db.record_tmdb_lookup_failure("movie", movie_id, "999999")

    db.clear_tmdb_id("movie", movie_id)

    assert db.list_tmdb_lookup_failures("movie")["movies"] == []


def test_archived_item_excluded_from_queue(db):
    provider_id = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [
        {"name": "Stale ID Movie", "year": 2020, "provider_stream_id": "1",
         "container_extension": "mkv", "tmdb_id": "999999"},
    ])
    movie_id = db.list_all_movie_ids()[0]
    db.record_tmdb_lookup_failure("movie", movie_id, "999999")
    db.bulk_set_review_excluded("movie", [movie_id], True)

    assert db.list_tmdb_lookup_failures("movie")["movies"] == []

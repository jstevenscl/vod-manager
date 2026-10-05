def test_dry_run_report_finds_rows_whose_stored_language_disagrees_with_recompute(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "en-1",
         "container_extension": "mp4", "raw_name": "Movie A"},
        {"name": "Movie B", "year": 2021, "provider_stream_id": "ir-1",
         "container_extension": "mp4", "raw_name": "IR - Movie B"},
        {"name": "Movie C", "year": 2022, "provider_stream_id": "ir-2",
         "container_extension": "mp4", "raw_name": "IR - Movie C"},
    ])

    # Simulate rows written before IR was a known language code: they were
    # classified (not NULL) but landed on the wrong value, "EN", because the
    # prefix wasn't recognized at write time.
    conn = db._connect()
    conn.execute("UPDATE movie_sources SET language = 'EN' WHERE raw_name LIKE 'IR - %'")
    conn.commit()
    conn.close()

    report = db.language_recompute_dry_run_report()

    assert report["movie_sources"]["IR"]["count"] == 2
    assert "Movie B" in report["movie_sources"]["IR"]["sample_titles"]
    assert "Movie C" in report["movie_sources"]["IR"]["sample_titles"]
    # Movie A's stored "EN" already agrees with a fresh computation, so it
    # must not show up as a mismatch.
    assert "EN" not in report["movie_sources"]


def test_dry_run_report_ignores_rows_with_null_language(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "ir-1",
         "container_extension": "mp4", "raw_name": "IR - Movie A"},
    ])

    conn = db._connect()
    conn.execute("UPDATE movie_sources SET language = NULL")
    conn.commit()
    conn.close()

    report = db.language_recompute_dry_run_report()

    # NULL rows belong to language_backfill_dry_run_report, not this tool.
    assert "movie_sources" not in report


def test_apply_recompute_writes_corrected_language_onto_mismatched_rows(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "en-1",
         "container_extension": "mp4", "raw_name": "Movie A"},
        {"name": "Movie B", "year": 2021, "provider_stream_id": "ir-1",
         "container_extension": "mp4", "raw_name": "IR - Movie B"},
    ])

    conn = db._connect()
    conn.execute("UPDATE movie_sources SET language = 'EN' WHERE raw_name = 'IR - Movie B'")
    conn.commit()
    conn.close()

    updated = db.apply_language_recompute()

    assert updated == 1

    conn = db._connect()
    row = conn.execute(
        "SELECT language FROM movie_sources WHERE raw_name = 'IR - Movie B'"
    ).fetchone()
    conn.close()
    assert row["language"] == "IR"


def test_apply_recompute_is_a_no_op_when_nothing_disagrees(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "en-1",
         "container_extension": "mp4", "raw_name": "Movie A"},
    ])

    updated = db.apply_language_recompute()

    assert updated == 0

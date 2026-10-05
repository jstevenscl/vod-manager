def test_dry_run_report_counts_and_samples_by_detected_language(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "en-1",
         "container_extension": "mp4", "raw_name": "Movie A"},
        {"name": "Movie B", "year": 2021, "provider_stream_id": "nl-1",
         "container_extension": "mp4", "raw_name": "NL - Movie B"},
        {"name": "Movie C", "year": 2022, "provider_stream_id": "nl-2",
         "container_extension": "mp4", "raw_name": "NL - Movie C"},
    ])

    # Simulate pre-existing rows imported before the language column existed:
    # NULL language, never touched by the fixed bulk_import_movies insert.
    conn = db._connect()
    conn.execute("UPDATE movie_sources SET language = NULL")
    conn.commit()
    conn.close()

    report = db.language_backfill_dry_run_report()

    assert report["movie_sources"]["EN"]["count"] == 1
    assert report["movie_sources"]["NL"]["count"] == 2
    assert "Movie B" in report["movie_sources"]["NL"]["sample_titles"]
    assert "Movie C" in report["movie_sources"]["NL"]["sample_titles"]


def test_apply_backfill_writes_computed_language_onto_existing_rows(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_movies(provider_id, [
        {"name": "Movie A", "year": 2020, "provider_stream_id": "en-1",
         "container_extension": "mp4", "raw_name": "Movie A"},
        {"name": "Movie B", "year": 2021, "provider_stream_id": "nl-1",
         "container_extension": "mp4", "raw_name": "NL - Movie B"},
    ])

    conn = db._connect()
    conn.execute("UPDATE movie_sources SET language = NULL")
    conn.commit()
    conn.close()

    updated = db.apply_language_backfill()

    assert updated == 2

    movie_b = db.get_movie_by_name_year("Movie B", 2021)
    sources = db.list_movie_sources_for_streaming(movie_b["id"])
    assert sources == []

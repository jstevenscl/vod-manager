"""The metadata workspace must include only actionable identity gaps."""


def _movie(stream_id: str, name: str, year: int | None, tmdb_id: str | None = None) -> dict:
    return {
        "name": name,
        "year": year,
        "tmdb_id": tmdb_id,
        "provider_stream_id": stream_id,
        "container_extension": "mp4",
        "provider_category_name": "Movies",
        "raw_name": name,
    }


def test_metadata_review_includes_missing_identity_and_year_flags(db):
    provider_id = db.upsert_provider("Provider", "http://provider.invalid", "u", "p", provider_type="xc")
    db.bulk_import_movies(provider_id, [
        _movie("missing-both", "No Identity", None),
        _movie("missing-id", "No TMDB", 2024),
        _movie("missing-year", "No Year", None, "123"),
        _movie("complete", "Complete", 2023, "456"),
    ])
    complete = db.get_movie_by_name_year("Complete", 2023)
    conn = db._connect()
    conn.execute("UPDATE movies SET needs_year_review=1 WHERE id=?", (complete["id"],))
    conn.commit()
    conn.close()

    queue = db.list_metadata_review()

    assert {row["name"] for row in queue["movies"]} == {"No Identity", "Complete"}
    assert queue["series"] == []


def test_metadata_review_existing_match_is_ranked_and_reports_sources(db):
    first = db.upsert_provider("First", "http://first.invalid", "u", "p", provider_type="xc")
    second = db.upsert_provider("Second", "http://second.invalid", "u", "p", provider_type="xc")
    now = db._now()
    conn = db._connect()
    conn.execute("INSERT INTO movies (name, year, created_at) VALUES (?, ?, ?)", ("Night Shift", None, now))
    review_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO movies (name, year, tmdb_id, created_at) VALUES (?, ?, ?, ?)", ("Night: Shift", 2024, "123", now))
    match_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for provider_id, stream_id in ((first, "review"), (second, "match")):
        movie_id = review_id if provider_id == first else match_id
        conn.execute(
            "INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, added_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (movie_id, provider_id, stream_id, now, now),
        )
    conn.commit()
    conn.close()

    matches = db.find_existing_metadata_matches("movie", review_id)

    assert len(matches) == 1
    assert matches[0]["id"] == match_id
    assert matches[0]["tmdb_id"] == "123"
    assert matches[0]["source_count"] == 1
    assert matches[0]["match_reason"] == "same normalized title"

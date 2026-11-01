import vod_db


def _insert(conn, table, name, year, tmdb_id):
    conn.execute(f"INSERT INTO {table} (name, year, tmdb_id, created_at) VALUES (?, ?, ?, ?)",
                 (name, year, tmdb_id, vod_db._now()))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_possible_matches_include_same_year_and_year_differences_through_three(db):
    conn = db._connect()
    same = _insert(conn, "movies", "Example Title", 2020, "100")
    near = _insert(conn, "movies", "Example Title", 2023, "101")
    too_far = _insert(conn, "movies", "Example Title", 2024, "102")
    incoming = _insert(conn, "movies", "Example Title", 2020, None)
    conn.commit()
    conn.close()

    matches = db.list_possible_metadata_matches("movie")
    row = next(item for item in matches["items"] if item["id"] == incoming)
    assert [candidate["id"] for candidate in row["candidates"]] == [same, near]
    assert row["candidates"][0]["match_reason"] == "same title and year"
    assert "differs by 3" in row["candidates"][1]["match_reason"]
    assert too_far not in [candidate["id"] for candidate in row["candidates"]]


def test_possible_matches_include_incoming_and_candidate_posters(db):
    conn = db._connect()
    conn.execute(
        "INSERT INTO movies (name, year, tmdb_id, poster_url, created_at) VALUES (?, ?, ?, ?, ?)",
        ("Poster Match", 2020, "88", "https://images.invalid/existing.jpg", db._now()),
    )
    candidate_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO movies (name, year, tmdb_id, poster_url, created_at) VALUES (?, ?, ?, ?, ?)",
        ("Poster Match", 2020, None, "https://images.invalid/incoming.jpg", db._now()),
    )
    incoming_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    row = next(item for item in db.list_possible_metadata_matches("movie")["items"] if item["id"] == incoming_id)
    assert row["poster_url"] == "https://images.invalid/incoming.jpg"
    assert row["candidates"][0]["id"] == candidate_id
    assert row["candidates"][0]["poster_url"] == "https://images.invalid/existing.jpg"


def test_possible_matches_explain_missing_incoming_year(db):
    conn = db._connect()
    incoming = _insert(conn, "series", "Example Show", None, None)
    existing = _insert(conn, "series", "Example Show", 2018, "200")
    conn.commit()
    conn.close()

    page = db.list_possible_metadata_matches("series")
    row = next(item for item in page["items"] if item["id"] == incoming)
    assert row["candidates"][0]["id"] == existing
    assert row["candidates"][0]["match_reason"] == "same normalized title; incoming year missing"


def test_keep_separate_dismisses_candidate_pair_and_preserves_catalog_entry(db):
    conn = db._connect()
    incoming = _insert(conn, "movies", "Separate Example", None, None)
    existing = _insert(conn, "movies", "Separate Example", 2021, "300")
    conn.commit()
    conn.close()

    assert db.bulk_ignore_possible_metadata_matches("movie", [{
        "item_id": incoming, "candidate_ids": [existing],
    }]) == 1
    assert db.list_possible_metadata_matches("movie")["total"] == 0
    assert db.get_movie(incoming) is not None


def test_bulk_merge_rechecks_title_and_three_year_limit(db):
    conn = db._connect()
    existing = _insert(conn, "movies", "Merge Example", 2020, "400")
    incoming = _insert(conn, "movies", "Merge Example", 2023, None)
    conn.commit()
    conn.close()

    result = db.bulk_merge_possible_metadata_matches("movie", [{"item_id": incoming, "candidate_id": existing}])
    assert result == {"merged": 1, "skipped": []}
    assert db.get_movie(incoming) is None
    assert db.get_movie(existing)["tmdb_id"] == "400"


def test_metadata_review_count_matches_page_and_default_adult_filter(db):
    db.upsert_movie("Example movie", None)
    adult = db.upsert_movie("Adult example", None)
    db.set_movie_adult(adult, True)
    db.upsert_series("Example show", None)

    counts = db.count_metadata_review()
    movie_page = db.list_metadata_review("movie")["movies"]
    series_page = db.list_metadata_review("series")["series"]
    assert counts["movies"] == {"total": len(movie_page), "adult": 1}
    assert counts["series"] == {"total": len(series_page), "adult": 0}
    assert counts["movies"]["total"] - counts["movies"]["adult"] == 1

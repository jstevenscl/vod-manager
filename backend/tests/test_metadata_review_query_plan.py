"""list_metadata_review must use the partial review indexes (upstream PR #35
review: the OR form is a full table scan, 8s+ on ~115k rows)."""

import vod_db


def test_metadata_review_rows(db):
    pid = db.upsert_provider("P", "http://p.invalid", "u", "p")
    db.bulk_import_movies(pid, [
        {"name": n, "year": y, "provider_stream_id": s, "container_extension": "mp4",
         "provider_category_name": "Movies", "raw_name": n}
        for n, y, s in (("No Year", None, "1"), ("Flagged", 2020, "2"), ("Clean", 2020, "3"), ("Has Id", None, "4"))
    ])
    conn = vod_db._connect()
    conn.execute("UPDATE movies SET needs_year_review=1 WHERE name='Flagged'")
    conn.execute("UPDATE movies SET tmdb_id='9' WHERE name='Has Id'")
    conn.commit()
    conn.close()

    names = [r["name"] for r in db.list_metadata_review("movie")["movies"]]

    assert names == ["Flagged", "No Year"]


def test_metadata_review_query_uses_partial_indexes(db):
    conn = vod_db._connect()
    plan = " ".join(r[-1] for r in conn.execute(
        "EXPLAIN QUERY PLAN " + vod_db._metadata_review_sql("movies")).fetchall())
    conn.close()

    assert "idx_movies_metadata_review_flag" in plan
    assert "idx_movies_metadata_review_missing" in plan

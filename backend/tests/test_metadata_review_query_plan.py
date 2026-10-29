"""Metadata Review must include year-bearing rows with no TMDB identity."""

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

    assert names == ["Flagged", "Clean", "No Year"]



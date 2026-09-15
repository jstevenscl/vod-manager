"""The metadata workspace must include unflagged, provider-incomplete rows."""


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

    assert {row["name"] for row in queue["movies"]} == {"No TMDB", "No Year", "Complete"}
    assert queue["series"] == []

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


def test_metadata_review_only_keeps_missing_tmdb_identity(db):
    provider_id = db.upsert_provider("Provider", "http://provider.invalid", "u", "p", provider_type="xc")
    db.bulk_import_movies(provider_id, [
        _movie("missing-both", "No Identity", None),
        _movie("missing-id", "No TMDB", 2024),
        _movie("missing-year", "No Year", None, "123"),
        _movie("complete", "Complete", 2023, "456"),
    ])
    queue = db.list_metadata_review()

    # A TMDB-backed row with only a missing/ambiguous year is not an identity
    # review item. A no-ID row is actionable even when the provider supplied a
    # year, because the same TMDB picker can resolve both fields.
    assert {row["name"] for row in queue["movies"]} == {"No Identity", "No TMDB"}
    assert queue["series"] == []

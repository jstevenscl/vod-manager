"""Repeatedly failing every movie fallback must suppress the dead title."""


def test_all_failed_movie_sources_are_blocked_and_a_success_restores_the_title(db):
    provider_a = db.upsert_provider("Provider A", "http://a.invalid", "u", "p", provider_type="xc")
    provider_b = db.upsert_provider("Provider B", "http://b.invalid", "u", "p", provider_type="xc")
    item = {
        "name": "Broken Movie", "year": 2020, "container_extension": "mp4",
        "provider_category_name": "Movies", "raw_name": "Broken Movie",
    }
    db.bulk_import_movies(provider_a, [{**item, "provider_stream_id": "a-1"}])
    db.bulk_import_movies(provider_b, [{**item, "provider_stream_id": "b-1"}])
    movie = db.get_movie_by_name_year("Broken Movie", 2020)
    category_id = db.upsert_category("Movies", "movie")
    db.place_movie_in_category(movie["id"], category_id)
    sources = db.list_movie_sources_for_streaming(movie["id"])

    for source in sources:
        for _ in range(3):
            db.record_source_failure("movie", source["source_id"])

    conn = db._connect()
    try:
        assert conn.execute("SELECT stream_blocked FROM movies WHERE id=?", (movie["id"],)).fetchone()["stream_blocked"] == 1
    finally:
        conn.close()
    assert db.get_movie_export_rows() == []
    blocked = db.list_blocked_movies()
    assert [row["id"] for row in blocked] == [movie["id"]]
    assert {source["provider_name"] for source in blocked[0]["sources"]} == {"Provider A", "Provider B"}

    db.record_source_success("movie", sources[0]["source_id"])
    assert [row["movie_id"] for row in db.get_movie_export_rows()] == [movie["id"]]
    assert db.list_blocked_movies() == []

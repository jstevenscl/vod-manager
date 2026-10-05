def _make_provider(db, name="prov1"):
    return db.upsert_provider(name, "http://example.com", "user", "pass")


def test_export_rows_use_english_source_not_foreign_when_both_exist(db):
    en_provider_id = db.upsert_provider("prov_en", "http://example.com", "user", "pass", priority=0)
    nl_provider_id = db.upsert_provider("prov_nl", "http://example.com", "user", "pass", priority=10)

    db.bulk_import_movies(en_provider_id, [
        {
            "name": "White House Down",
            "year": 2013,
            "provider_stream_id": "en-1",
            "container_extension": "mp4",
            "provider_category_name": "Action",
            "auto_archive": False,
            "raw_name": "White House Down",
        },
    ])
    # Higher-priority provider's source is NL-tagged -- without a language
    # gate, _source_order_by would rank this one first (provider priority
    # wins ties), proving the CTE's language filter (not luck) excludes it.
    db.bulk_import_movies(nl_provider_id, [
        {
            "name": "White House Down",
            "year": 2013,
            "provider_stream_id": "nl-1",
            "container_extension": "mp4",
            "provider_category_name": "Action",
            "auto_archive": False,
            "raw_name": "NL - White House Down",
        },
    ])
    movie = db.get_movie_by_name_year("White House Down", 2013)
    category_id = db.upsert_category("Action Movies", "movie")
    db.place_movie_in_category(movie["id"], category_id)

    rows = db.get_movie_export_rows()
    row = next(r for r in rows if r["movie_id"] == movie["id"])

    assert row["provider_stream_id"] == "en-1"


def test_export_rows_omit_movie_with_no_english_or_spanish_source(db):
    provider_id = db.upsert_provider("prov_nl", "http://example.com", "user", "pass")

    db.bulk_import_movies(provider_id, [
        {
            "name": "Some Foreign Film",
            "year": 2020,
            "provider_stream_id": "nl-1",
            "container_extension": "mp4",
            "provider_category_name": "Drama",
            "auto_archive": False,
            "raw_name": "NL - Some Foreign Film",
        },
    ])
    movie = db.get_movie_by_name_year("Some Foreign Film", 2020)
    category_id = db.upsert_category("Drama Movies", "movie")
    db.place_movie_in_category(movie["id"], category_id)

    rows = db.get_movie_export_rows()

    assert all(r["movie_id"] != movie["id"] for r in rows)

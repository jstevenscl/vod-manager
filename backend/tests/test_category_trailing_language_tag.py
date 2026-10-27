# KNM: 2026-10-27 -- some providers put the category's language tag at the END
# ("Series Korean [KR]"); only a leading "[KR]" was read, so these defaulted to EN.


def _series(db, provider_id, category):
    db.bulk_import_series(provider_id, [{
        "name": "Missing", "year": 2020, "provider_series_id": "s-1",
        "raw_name": "Missing", "provider_category_name": category,
        "_has_detail": True,
    }])


def _lang(db, table="series_sources"):
    conn = db._connect()
    try:
        return conn.execute(f"SELECT language FROM {table}").fetchone()["language"]
    finally:
        conn.close()


def _set_lang(db, value):
    conn = db._connect()
    conn.execute("UPDATE series_sources SET language=?", (value,))
    conn.commit()
    conn.close()


def test_series_import_reads_trailing_category_tag(db):
    _series(db, db.upsert_provider("p", "http://x", "u", "p"), "Series Korean [KR]")
    assert _lang(db) == "KR"


def test_movie_import_reads_trailing_category_tag(db):
    pid = db.upsert_provider("p", "http://x", "u", "p")
    db.bulk_import_movies(pid, [{
        "name": "Movie A", "year": 2020, "provider_stream_id": "m-1", "container_extension": "mp4",
        "raw_name": "Movie A", "provider_category_name": "Movies French [FR]",
    }])
    assert _lang(db, "movie_sources") == "FR"


def test_recompute_fixes_trailing_category_tag(db):
    _series(db, db.upsert_provider("p", "http://x", "u", "p"), "Series German [DE]")
    _set_lang(db, "EN")
    assert db.apply_language_recompute() == 1
    assert _lang(db) == "DE"


def test_leading_category_tag_still_read(db):
    _series(db, db.upsert_provider("p", "http://x", "u", "p"), "[KR] Series")
    assert _lang(db) == "KR"


def test_trailing_non_language_tag_stays_en(db):
    _series(db, db.upsert_provider("p", "http://x", "u", "p"), "Series Action [4K]")
    assert _lang(db) == "EN"

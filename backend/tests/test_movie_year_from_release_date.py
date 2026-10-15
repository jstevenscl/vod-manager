"""Held (needs_year_review=1) yearless movies take their year from the TMDB release date."""


def _movie(db, name="Flashback", tmdb_id="123", flagged=1, year=None, enriched=False, release_date=None, excluded=0):
    conn = db._connect()
    cur = conn.execute(
        "INSERT INTO movies (name, year, tmdb_id, needs_year_review, release_date, last_enriched_at, review_excluded, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (name, year, tmdb_id, flagged, release_date, db._now() if enriched else None, excluded, db._now()),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def test_enrichment_sets_year_from_release_date_and_clears_flag(db):
    mid = _movie(db)
    db.apply_movie_enrichment_batch([{"movie_id": mid, "fields": {"release_date": "1990-02-02"}}])
    row = db.get_movie(mid)
    assert row["year"] == 1990
    assert row["needs_year_review"] == 0


def test_single_enrichment_sets_year_from_release_date(db):
    mid = _movie(db)
    db.set_movie_enrichment(mid, release_date="2004-05-06")
    assert db.get_movie(mid)["year"] == 2004


def test_enrichment_without_release_date_keeps_flag(db):
    mid = _movie(db)
    db.apply_movie_enrichment_batch([{"movie_id": mid, "fields": {"release_date": None}}])
    row = db.get_movie(mid)
    assert row["year"] is None
    assert row["needs_year_review"] == 1


def test_enrichment_leaves_unflagged_or_dated_movies_alone(db):
    unflagged = _movie(db, name="A", flagged=0)
    dated = _movie(db, name="B", year=2001)
    db.apply_movie_enrichment_batch([
        {"movie_id": unflagged, "fields": {"release_date": "1990-02-02"}},
        {"movie_id": dated, "fields": {"release_date": "1990-02-02"}},
    ])
    assert db.get_movie(unflagged)["year"] is None
    assert db.get_movie(dated)["year"] == 2001
    assert db.get_movie(dated)["needs_year_review"] == 1


def test_startup_heals_already_enriched_stuck_movies(db):
    stuck = _movie(db, enriched=True, release_date="1990-02-02")
    archived = _movie(db, name="War", enriched=True, release_date="2019-10-02", excluded=1)
    blank = _movie(db, name="Blank", enriched=True, release_date="")
    db.init_db()
    assert db.get_movie(stuck)["year"] == 1990
    assert db.get_movie(stuck)["needs_year_review"] == 0
    assert db.get_movie(archived)["year"] == 2019
    assert db.get_movie(archived)["review_excluded"] == 1
    assert db.get_movie(blank)["needs_year_review"] == 1

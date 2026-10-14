"""vod_manager-58a: the Movies/TV 'No TMDB ID' filter, which (unlike Metadata
Review) also lists year-bearing items that were never matched."""


def _seed(db):
    a = db.upsert_series("Matched Show", 2020)
    db.backfill_tmdb_id_if_missing("series", a, "111")
    b = db.upsert_series("Unmatched Show", 2019)  # has a year, no TMDB id
    c = db.upsert_series("Another Unmatched", 2018)
    m1 = db.upsert_movie("Matched Film", 2000)
    db.backfill_tmdb_id_if_missing("movie", m1, "222")
    m2 = db.upsert_movie("Unmatched Film", 2001)
    return {"matched": a, "un1": b, "un2": c, "m1": m1, "m2": m2}


def test_series_no_tmdb_filter(db):
    ids = _seed(db)
    everything = {s["id"] for s in db.list_series()}
    assert everything == {ids["matched"], ids["un1"], ids["un2"]}
    only = {s["id"] for s in db.list_series(no_tmdb=True)}
    assert only == {ids["un1"], ids["un2"]}
    assert db.count_series(no_tmdb=True) == 2
    assert set(db.list_all_series_ids(no_tmdb=True)) == only


def test_filter_combines_with_search(db):
    ids = _seed(db)
    assert {s["id"] for s in db.list_series(search="Another", no_tmdb=True)} == {ids["un2"]}
    assert db.count_series(search="Matched", no_tmdb=False) >= 1
    # case-insensitive substring: "Unmatched Show" matches, the tmdb-matched "Matched Show" must not
    assert {s["id"] for s in db.list_series(search="Matched Show", no_tmdb=True)} == {ids["un1"]}


def test_movie_no_tmdb_filter(db):
    ids = _seed(db)
    assert {m["id"] for m in db.list_movies(no_tmdb=True)} == {ids["m2"]}
    assert db.count_movies(no_tmdb=True) == 1
    assert db.list_all_movie_ids(no_tmdb=True) == [ids["m2"]]
    assert db.count_movies() == 2


def test_unmatched_with_year_is_not_in_metadata_review(db):
    """The gap the filter exists to cover."""
    ids = _seed(db)
    review = {s["id"] for s in db.list_metadata_review()["series"]}
    assert ids["un1"] not in review

"""Regression tests for the dedicated artwork review queue."""


def test_missing_artwork_treats_bare_tmdb_urls_as_missing_and_hides_adults_by_default(db):
    bare = db.upsert_movie("Bare TMDB", 2020)
    adult = db.upsert_movie("Adult Missing", 2020)
    valid = db.upsert_movie("Has Poster", 2020)
    conn = db._connect()
    conn.execute(
        "UPDATE movies SET poster_url='https://image.tmdb.org/t/p/w500', is_adult=0 WHERE id=?",
        (bare,),
    )
    conn.execute("UPDATE movies SET is_adult=1 WHERE id=?", (adult,))
    conn.execute(
        "UPDATE movies SET poster_url='https://image.tmdb.org/t/p/w500/poster.jpg' WHERE id=?",
        (valid,),
    )
    conn.commit()
    conn.close()

    assert {row["id"] for row in db.list_missing_artwork("movie")} == {bare}
    assert {row["id"] for row in db.list_missing_artwork("movie", hide_adult=False)} == {bare, adult}

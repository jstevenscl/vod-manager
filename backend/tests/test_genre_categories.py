"""One-click genre categories: genre counts, smart-rule creation, collision skips."""

import json


def _seed(db):
    for name, genre in (("Funny One", "Comedy, Drama"), ("Funny Two", "Comedy"), ("Sad One", "Drama"), ("No Genre", None)):
        mid = db.upsert_movie(name, 2000)
        if genre:
            conn = db._connect()
            try:
                conn.execute("UPDATE movies SET genre=? WHERE id=?", (genre, mid))
                conn.commit()
            finally:
                conn.close()
    sid = db.upsert_series("A Show", 2010)
    conn = db._connect()
    try:
        conn.execute("UPDATE series SET genre='Comedy' WHERE id=?", (sid,))
        conn.commit()
    finally:
        conn.close()


def test_genre_counts_split_and_merge_case(db):
    _seed(db)
    counts = db.list_genre_counts()
    movies = {g["genre"]: g["count"] for g in counts["movie"]}
    assert movies == {"Comedy": 2, "Drama": 2}
    assert {g["genre"]: g["count"] for g in counts["series"]} == {"Comedy": 1}


def test_create_genre_categories_builds_smart_rules_and_skips_existing(db):
    _seed(db)
    db.upsert_category("Drama", "movie")  # user already has a plain "Drama" movie category
    result = db.create_genre_categories(["Comedy", "Drama", "Western"], ["movie", "series"])
    created = {(c["name"], c["content_type"]) for c in result["created"]}
    assert created == {("Comedy", "movie"), ("Comedy", "series"), ("Drama", "series")} - {("Drama", "series")}
    reasons = {(s["name"], s["content_type"]): s["reason"] for s in result["skipped"]}
    assert "already exists" in reasons[("Drama", "movie")]
    assert "no titles" in reasons[("Western", "movie")]
    assert "no titles" in reasons[("Drama", "series")]  # no series carries Drama

    comedy = next(c for c in result["created"] if c["content_type"] == "movie")
    cat = db.get_category(comedy["id"])
    assert cat["is_smart"] == 1
    assert json.loads(cat["rule_json"])["conditions"] == [{"field": "genre", "op": "contains", "value": "Comedy"}]
    assert db.evaluate_smart_category(comedy["id"])["matched"] == 2


def test_running_twice_does_not_duplicate(db):
    _seed(db)
    first = db.create_genre_categories(["Comedy"], ["movie"])
    second = db.create_genre_categories(["Comedy"], ["movie"])
    assert len(first["created"]) == 1 and second["created"] == []


def test_slash_separated_genres_and_min_items(db):
    mid = db.upsert_movie("Mixed", 2001)
    other = db.upsert_movie("Other", 2002)
    conn = db._connect()
    try:
        conn.execute("UPDATE movies SET genre='Comedia / Drama' WHERE id=?", (mid,))
        conn.execute("UPDATE movies SET genre='Drama|Thriller' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()
    assert {g["genre"]: g["count"] for g in db.list_genre_counts()["movie"]} == {"Comedia": 1, "Drama": 2, "Thriller": 1}
    assert [g["genre"] for g in db.list_genre_counts(min_items=2)["movie"]] == ["Drama"]

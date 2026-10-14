"""Removing titles from a category (single + bulk) keeps them in the pool, and the
removal sticks against smart-rule re-evaluation until the title is placed by hand."""

import asyncio
import json

import vod_routes


def _smart_comedy(db):
    ids = {}
    for key, name in (("a", "Funny A"), ("b", "Funny B"), ("c", "Funny C")):
        mid = db.upsert_movie(name, 2000)
        conn = db._connect()
        try:
            conn.execute("UPDATE movies SET genre='Comedy' WHERE id=?", (mid,))
            conn.commit()
        finally:
            conn.close()
        ids[key] = mid
    rule = json.dumps({"match": "all", "conditions": [{"field": "genre", "op": "contains", "value": "Comedy"}]})
    cat = db.upsert_category("Comedy", "movie", True, 0, rule)
    db.evaluate_smart_category(cat)
    return cat, ids


def _placed(db, cat):
    conn = db._connect()
    try:
        return {r["movie_id"] for r in conn.execute("SELECT movie_id FROM movie_category_placements WHERE category_id=?", (cat,))}
    finally:
        conn.close()


def test_removed_title_stays_out_after_reevaluation(db):
    cat, ids = _smart_comedy(db)
    assert _placed(db, cat) == set(ids.values())

    db.remove_movie_from_category(ids["a"], cat)
    assert _placed(db, cat) == {ids["b"], ids["c"]}
    assert db.get_movie(ids["a"])  # still in the pool, not archived
    assert db.get_movie(ids["a"])["review_excluded"] == 0

    db.evaluate_smart_category(cat)  # the rule still matches A, but the removal sticks
    assert _placed(db, cat) == {ids["b"], ids["c"]}
    assert db.count_category_exclusions(cat) == {"movie": 1}


def test_placing_by_hand_lifts_the_exclusion(db):
    cat, ids = _smart_comedy(db)
    db.remove_movie_from_category(ids["a"], cat)
    db.place_movie_in_category(ids["a"], cat)
    assert ids["a"] in _placed(db, cat)
    assert db.count_category_exclusions(cat) == {}


def test_clear_exclusions_lets_the_rule_place_again(db):
    cat, ids = _smart_comedy(db)
    db.remove_items_from_category("movie", [ids["a"], ids["b"]], cat)
    assert db.clear_category_exclusions(cat) == 2
    db.evaluate_smart_category(cat)
    assert _placed(db, cat) == set(ids.values())


def test_bulk_unplace_routes_by_ids_and_by_filter(db):
    cat, ids = _smart_comedy(db)
    r = asyncio.run(vod_routes.bulk_unplace_movies(vod_routes.BulkUnplaceRequest(category_id=cat, ids=[ids["a"], ids["b"]])))
    assert r == {"matched": 2, "removed": 2}
    assert _placed(db, cat) == {ids["c"]}

    # everything left in the category that matches a search
    r = asyncio.run(vod_routes.bulk_unplace_movies(vod_routes.BulkUnplaceRequest(category_id=cat, search="Funny C")))
    assert r == {"matched": 1, "removed": 1}
    assert _placed(db, cat) == set()

    # manual bulk-place of the same ids puts them back and lifts the exclusions
    asyncio.run(vod_routes.bulk_place_movies(vod_routes.BulkPlaceRequest(category_id=cat, ids=[ids["a"], ids["c"]])))
    assert _placed(db, cat) == {ids["a"], ids["c"]}
    assert db.count_category_exclusions(cat) == {"movie": 1}  # B is still excluded


def test_series_unplace_and_category_delete_cleans_up(db):
    sid = db.upsert_series("Show", 2020)
    cat = db.upsert_category("Drama", "series")
    db.place_series_in_category(sid, cat)
    r = asyncio.run(vod_routes.bulk_unplace_series(vod_routes.BulkUnplaceRequest(category_id=cat, ids=[sid])))
    assert r["removed"] == 1 and db.get_series(sid)["review_excluded"] == 0
    assert db.count_category_exclusions(cat) == {"series": 1}
    db.delete_category(cat)
    conn = db._connect()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM category_exclusions").fetchone()["c"] == 0
    finally:
        conn.close()

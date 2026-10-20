"""Editing a smart category's rule, and a rule with several values ("any of" / "all of")."""

import asyncio
import json

import pytest
from fastapi import HTTPException

import vod_routes


def _movie(db, name, genre):
    mid = db.upsert_movie(name, 2000)
    conn = db._connect()
    try:
        conn.execute("UPDATE movies SET genre=? WHERE id=?", (genre, mid))
        conn.commit()
    finally:
        conn.close()
    return mid


def _rule(values, match="any", op="contains"):
    return json.dumps({"match": match, "conditions": [{"field": "genre", "op": op, "value": v} for v in values]})


def _placed(db, cat):
    conn = db._connect()
    try:
        return {r["movie_id"] for r in conn.execute("SELECT movie_id FROM movie_category_placements WHERE category_id=?", (cat,))}
    finally:
        conn.close()


def _edit(cat, rule, prune=True):
    return asyncio.run(vod_routes.set_category_rule(cat, vod_routes.CategoryRuleRequest(rule_json=rule, prune=prune)))


@pytest.fixture
def pool(db):
    return {g: _movie(db, f"Film {g}", g) for g in ("Action", "Comedy", "Drama", "Action, Comedy")}


def test_any_of_several_values(db, pool):
    cat = db.upsert_category("A or C", "movie", True, 0, _rule(["Action", "Comedy"]))
    db.evaluate_smart_category(cat)
    assert _placed(db, cat) == {pool["Action"], pool["Comedy"], pool["Action, Comedy"]}


def test_all_of_several_values(db, pool):
    cat = db.upsert_category("A and C", "movie", True, 0, _rule(["Action", "Comedy"], match="all"))
    db.evaluate_smart_category(cat)
    assert _placed(db, cat) == {pool["Action, Comedy"]}


def test_edit_rule_prunes_titles_that_no_longer_match(db, pool):
    cat = db.upsert_category("Mixed", "movie", True, 0, _rule(["Action", "Comedy"]))
    db.evaluate_smart_category(cat)
    res = _edit(cat, _rule(["Drama"]))
    assert _placed(db, cat) == {pool["Drama"]}
    assert res["matched"] == 1 and res["newly_placed"] == 1 and res["pruned"] == 3
    assert json.loads(db.get_category(cat)["rule_json"])["conditions"][0]["value"] == "Drama"


def test_edit_rule_without_prune_keeps_old_placements(db, pool):
    cat = db.upsert_category("Mixed", "movie", True, 0, _rule(["Action"]))
    db.evaluate_smart_category(cat)
    res = _edit(cat, _rule(["Drama"]), prune=False)
    assert _placed(db, cat) == {pool["Action"], pool["Action, Comedy"], pool["Drama"]}
    assert "pruned" not in res


def test_prune_leaves_manual_removals_excluded(db, pool):
    cat = db.upsert_category("Act", "movie", True, 0, _rule(["Action"]))
    db.evaluate_smart_category(cat)
    db.remove_movie_from_category(pool["Action"], cat)          # removed on purpose
    _edit(cat, _rule(["Action", "Drama"]))
    assert pool["Action"] not in _placed(db, cat) and pool["Drama"] in _placed(db, cat)


@pytest.mark.parametrize("rule", [
    "not json", json.dumps({"match_all": True}), json.dumps({"match": "any", "conditions": []}),
    json.dumps({"match": "some", "conditions": [{"field": "genre", "op": "contains", "value": "x"}]}),
    json.dumps({"match": "any", "conditions": [{"field": "nope", "op": "contains", "value": "x"}]}),
    json.dumps({"match": "any", "conditions": [{"field": "genre", "op": "regex", "value": "x"}]}),
    json.dumps({"match": "any", "conditions": [{"field": "genre", "op": "contains", "value": "  "}]}),
])
def test_bad_rule_is_refused_and_not_saved(db, pool, rule):
    original = _rule(["Action"])
    cat = db.upsert_category("Act", "movie", True, 0, original)
    with pytest.raises(HTTPException) as e:
        _edit(cat, rule)
    assert e.value.status_code == 400
    assert db.get_category(cat)["rule_json"] == original


def test_only_smart_categories_have_a_rule(db, pool):
    cat = db.upsert_category("Manual", "movie", False, 0, None)
    with pytest.raises(HTTPException) as e:
        _edit(cat, _rule(["Action"]))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _edit(99999, _rule(["Action"]))
    assert e.value.status_code == 404


def test_series_category_rule_edit_prunes(db):
    sid = db.upsert_series("Show Drama", 2000) if hasattr(db, "upsert_series") else None
    if sid is None:
        pytest.skip("no upsert_series helper")
    conn = db._connect()
    try:
        conn.execute("UPDATE series SET genre='Drama' WHERE id=?", (sid,))
        conn.commit()
    finally:
        conn.close()
    cat = db.upsert_category("Series Drama", "series", True, 0, _rule(["Drama"]))
    db.evaluate_smart_category(cat)
    res = _edit(cat, _rule(["Comedy"]))
    assert res["pruned"] == 1 and res["matched"] == 0

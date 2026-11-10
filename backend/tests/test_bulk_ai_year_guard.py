# KNM: 2026-11-06 - bulk AI auto-applied a 2013 short to "A Hard Place" (2025): a
# high-confidence AI pick was trusted even though its year disagreed with ours. Now
# bulk AI only applies a pick whose year fits what we have (+/-1), and leaves the
# title for the user when more than 3 candidates fit.
import asyncio

import pytest

import ai_assist
import tmdb_sync
import vod_bulk_ai_service as svc
import vod_db


def _flagged_id(name):
    conn = vod_db._connect()
    conn.execute("UPDATE movies SET needs_year_review=1 WHERE name=?", (name,))
    conn.commit()
    return conn.execute("SELECT id FROM movies WHERE name=?", (name,)).fetchone()[0]


def _cand(tmdb_id, year, poster=True):
    return {"tmdb_id": str(tmdb_id), "name": "A Hard Place", "year": year,
            "poster_url": f"https://image.tmdb.org/t/p/w185/{tmdb_id}.jpg" if poster else None}


@pytest.fixture()
def setup(db, monkeypatch):
    pid = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(pid, [{"name": "A Hard Place", "year": 2025, "provider_stream_id": "1",
                                  "container_extension": "mkv"}])
    item_id = _flagged_id("A Hard Place")
    state = {"candidates": [], "pick": 0}

    async def search(query, content_type, *a, **k):
        return list(state["candidates"])

    async def suggest(*a, **k):
        return {"best_match_index": state["pick"], "confidence": "high", "reasoning": "looks right"}
    monkeypatch.setattr(tmdb_sync, "search_title", search)
    monkeypatch.setattr(ai_assist, "suggest_year_review_match", suggest)
    return db, item_id, state


def test_needs_review_skips_pick_from_another_year(setup):
    db, item_id, state = setup
    state["candidates"] = [_cand(1573480, 2013)]
    out = asyncio.run(svc._resolve_one_needs_review("movie", item_id))
    assert out["status"] == "skipped" and "2013" in out["detail"] and "2025" in out["detail"]
    m = db.get_movie(item_id)
    assert (m["year"], m["tmdb_id"]) == (2025, None)


def test_needs_review_applies_pick_within_a_year(setup):
    db, item_id, state = setup
    state["candidates"] = [_cand(1104999, 2024)]
    out = asyncio.run(svc._resolve_one_needs_review("movie", item_id))
    assert out["status"] == "resolved"
    assert db.get_movie(item_id)["tmdb_id"] == "1104999"


def test_needs_review_skips_when_more_than_three_fit(setup):
    db, item_id, state = setup
    state["candidates"] = [_cand(i, 2025) for i in range(1, 5)]
    out = asyncio.run(svc._resolve_one_needs_review("movie", item_id))
    assert out["status"] == "skipped" and "4" in out["detail"]
    assert db.get_movie(item_id)["tmdb_id"] is None


def test_three_fitting_candidates_still_auto_apply(setup):
    db, item_id, state = setup
    state["candidates"] = [_cand(i, 2025) for i in range(1, 4)] + [_cand(9, 1990)]
    assert asyncio.run(svc._resolve_one_needs_review("movie", item_id))["status"] == "resolved"


def test_missing_artwork_skips_pick_from_another_year(setup):
    db, item_id, state = setup
    state["candidates"] = [_cand(1573480, 2013)]
    out = asyncio.run(svc._resolve_one_missing_artwork("movie", item_id))
    assert out["status"] == "skipped"
    m = db.get_movie(item_id)
    assert (m["year"], m["poster_url"]) == (2025, None)


def test_unknown_year_is_not_blocked(db, monkeypatch):
    pid = db.upsert_provider("XC-Test", "http://example.com", "user", "pass")
    db.bulk_import_movies(pid, [{"name": "No Year Film", "year": None, "provider_stream_id": "1",
                                  "container_extension": "mkv"}])
    item_id = _flagged_id("No Year Film")

    async def search(*a, **k):
        return [_cand(5, 2013)]

    async def suggest(*a, **k):
        return {"best_match_index": 0, "confidence": "high", "reasoning": "x"}
    monkeypatch.setattr(tmdb_sync, "search_title", search)
    monkeypatch.setattr(ai_assist, "suggest_year_review_match", suggest)
    assert asyncio.run(svc._resolve_one_needs_review("movie", item_id))["status"] == "resolved"

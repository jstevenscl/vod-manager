import asyncio

import tmdb_sync
import vod_bulk_ai_service


def test_user_merge_applies_the_only_tmdb_candidate_without_ai(db, monkeypatch):
    keep_id = db.upsert_movie("Known Title", 2020, tmdb_id="123")
    incoming_id = db.upsert_movie("Imported Alias", None)

    async def only_candidate(query, content_type):
        return [{"tmdb_id": "123", "name": "Known Title", "year": 2020}]

    monkeypatch.setattr(tmdb_sync, "search_title", only_candidate)
    monkeypatch.setattr(
        "ai_assist.suggest_year_review_match",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("User Merge must not call AI")),
    )

    result = asyncio.run(vod_bulk_ai_service._user_merge_one_needs_review("movie", incoming_id))

    assert result["status"] == "resolved"
    assert f"#{keep_id}" in result["detail"]
    assert db.get_movie(incoming_id) is None
    assert db.get_movie(keep_id) is not None


def test_user_merge_applies_unique_candidate_when_no_catalog_duplicate_exists(db, monkeypatch):
    item_id = db.upsert_movie("Single Result", None)

    async def only_candidate(query, content_type):
        return [{"tmdb_id": "456", "name": "Single Result", "year": 2019}]

    monkeypatch.setattr(tmdb_sync, "search_title", only_candidate)

    result = asyncio.run(vod_bulk_ai_service._user_merge_one_needs_review("movie", item_id))

    assert result["status"] == "resolved"
    assert result["detail"] == "user-approved TMDB candidate applied; no existing duplicate was merged"
    assert db.get_movie(item_id)["year"] == 2019
    assert str(db.get_movie(item_id)["tmdb_id"]) == "456"


def test_user_merge_accepts_year_bearing_no_id_items_from_review_queue(db, monkeypatch):
    item_id = db.upsert_movie("Blood White and Blue", 2025)
    assert item_id in [row["id"] for row in db.list_metadata_review("movie")["movies"]]
    assert db.get_movie(item_id)["needs_year_review"] == 0

    async def only_candidate(query, content_type):
        return [{"tmdb_id": "789", "name": "Blood, White & Blue", "year": 2022}]

    monkeypatch.setattr(tmdb_sync, "search_title", only_candidate)

    result = asyncio.run(vod_bulk_ai_service._user_merge_one_needs_review("movie", item_id))

    assert result["status"] == "resolved"
    assert db.get_movie(item_id)["year"] == 2022
    assert str(db.get_movie(item_id)["tmdb_id"]) == "789"


def test_ai_bulk_resolve_accepts_year_bearing_no_id_items_from_review_queue(db, monkeypatch):
    import ai_assist

    item_id = db.upsert_movie("Year Bearing No ID", 2025)

    async def only_candidate(query, content_type):
        return [{"tmdb_id": "790", "name": "Year Bearing No ID", "year": 2025}]

    async def confident_match(*args, **kwargs):
        return {"best_match_index": 0, "confidence": "high", "reasoning": "single result"}

    monkeypatch.setattr(tmdb_sync, "search_title", only_candidate)
    monkeypatch.setattr(ai_assist, "suggest_year_review_match", confident_match)

    result = asyncio.run(vod_bulk_ai_service._resolve_one_needs_review("movie", item_id))

    assert result["status"] == "resolved"
    assert str(db.get_movie(item_id)["tmdb_id"]) == "790"


def test_user_merge_skips_ambiguous_candidates_without_changing_identity(db, monkeypatch):
    item_id = db.upsert_movie("Ambiguous Title", None)

    async def two_candidates(query, content_type):
        return [
            {"tmdb_id": "201", "name": "Ambiguous Title", "year": 2020},
            {"tmdb_id": "202", "name": "Ambiguous Title", "year": 2021},
        ]

    monkeypatch.setattr(tmdb_sync, "search_title", two_candidates)

    result = asyncio.run(vod_bulk_ai_service._user_merge_one_needs_review("movie", item_id))

    assert result["status"] == "skipped"
    assert "found 2" in result["detail"]
    assert db.get_movie(item_id)["tmdb_id"] is None


def test_user_merge_skips_when_tmdb_has_no_candidate(db, monkeypatch):
    item_id = db.upsert_movie("No Candidate", None)

    async def no_candidates(query, content_type):
        return []

    monkeypatch.setattr(tmdb_sync, "search_title", no_candidates)

    result = asyncio.run(vod_bulk_ai_service._user_merge_one_needs_review("movie", item_id))

    assert result["status"] == "skipped"
    assert result["detail"] == "no TMDB candidates found"
    assert db.get_movie(item_id)["tmdb_id"] is None

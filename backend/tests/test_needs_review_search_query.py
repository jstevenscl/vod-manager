"""Needs-review TMDB search used the raw stored name, so
"Title (2014) (US)" returned no candidates for suggestions or AI suggest."""

import asyncio

import tmdb_sync
import vod_routes


def test_default_review_search_strips_year_and_country_suffix(db, monkeypatch):
    series_id = db.upsert_series("BoJack Horseman (2014) (US)", 2014)
    queries = []

    async def fake_search(query, content_type):
        queries.append((query, content_type))
        return []

    monkeypatch.setattr(tmdb_sync, "search_title", fake_search)
    asyncio.run(vod_routes.year_review_suggestions("series", series_id))
    asyncio.run(vod_routes.year_review_ai_suggest("series", series_id))
    asyncio.run(vod_routes.year_review_suggestions("series", series_id, q="Bojack (2014)"))
    assert queries == [
        ("BoJack Horseman", "series"),
        ("BoJack Horseman", "series"),
        ("Bojack (2014)", "series"),
    ]


def test_default_review_search_strips_quality_prefix_but_preserves_manual_query(db, monkeypatch):
    movie_id = db.upsert_movie("4K: Copper Harbor", None)
    queries = []

    async def fake_search(query, content_type):
        queries.append((query, content_type))
        return []

    monkeypatch.setattr(tmdb_sync, "search_title", fake_search)
    asyncio.run(vod_routes.year_review_suggestions("movie", movie_id))
    asyncio.run(vod_routes.year_review_suggestions("movie", movie_id, q="  4K: A manually entered title  "))

    assert queries == [
        ("Copper Harbor", "movie"),
        ("4K: A manually entered title", "movie"),
    ]


def test_review_candidates_keep_exact_normalized_title_and_catalog_year():
    candidates = [
        {"name": "A Christmas Romance", "year": 1994},
        {"name": "A Royal Date for Christmas", "year": 2023},
        {"name": "A Christmas Romance", "year": 2024},
    ]

    assert vod_routes._filter_review_candidates(candidates, "A Christmas-Romance", 1994) == [candidates[0]]


def test_review_candidates_keep_all_exact_years_when_catalog_year_has_no_exact_release():
    candidates = [
        {"name": "3:10 to Yuma", "year": 2007},
        {"name": "3:10 to Yuma", "year": 1957},
        {"name": "3:10 to Yuma: Revisited", "year": 2017},
    ]

    assert vod_routes._filter_review_candidates(candidates, "3:10 to Yuma", 2026) == candidates[:2]


def test_review_candidates_remain_available_when_no_exact_title_exists():
    candidates = [{"name": "Moon Beyond: Chapter Two", "year": 2024}]

    assert vod_routes._filter_review_candidates(candidates, "Moon Beyond", None) == candidates


def test_review_search_query_helper(db):
    assert db.tmdb_review_search_query("Severance (2022) (US)") == "Severance"
    assert db.tmdb_review_search_query("4K: Copper Harbor") == "Copper Harbor"
    assert db.tmdb_review_search_query("[1080p] - Harborlight Story") == "Harborlight Story"
    assert db.tmdb_review_search_query("4K-DE - Harborlight Story (2022)") == "Harborlight Story"
    assert db.tmdb_review_search_query("4K Collection") == "4K Collection"
    assert db.tmdb_review_search_query("Plain Title") == "Plain Title"
    assert db.tmdb_review_search_query("Plain Title", "  Other  ") == "Other"
    assert db.tmdb_review_search_query("(2014)") == "(2014)"

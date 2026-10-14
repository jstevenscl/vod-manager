"""Every TMDB answer is kept locally: /search and /find responses (including
"nothing found", which expires after a week), plus full details for the top
search results and for titles a TMDB list returns. The same question is never
sent to TMDB twice while the library is on."""

import asyncio
import sqlite3

import pytest

import library_matcher
import tmdb_fill
import tmdb_store
import tmdb_sync
from test_tmdb_store import _movie, _tv
from test_tmdb_store_routing import _off, store, tmdb  # noqa: F401 (fixtures)

WEEK = 7 * 86400


def _age_searches(seconds):
    with sqlite3.connect(tmdb_store.DB_PATH) as conn:
        conn.execute("UPDATE searches SET fetched_at = fetched_at - ?", (seconds,))


def _search_hit(*ids):
    return {"results": [{"id": i, "title": "Angry Boys", "release_date": "2011-05-11"} for i in ids]}


# --- library matcher (auto-match, library import) ------------------------------

def test_library_search_answer_kept_and_reused(store, tmdb):
    tmdb.routes = {"/search/movie": (200, _search_hit(1)), "/movie/1": (200, _movie(1, "Angry Boys"))}
    first = asyncio.run(library_matcher._search("Angry Boys", "movie", 2011))
    assert tmdb.calls == ["/search/movie", "/movie/1"]
    assert store.get_title("movie", 1)["title"] == "Angry Boys"  # details saved too
    tmdb.calls.clear()
    second = asyncio.run(library_matcher._search("angry  boys", "movie", 2011))
    assert tmdb.calls == []
    assert [c.tmdb_id for c in second] == [c.tmdb_id for c in first] == ["1"]


def test_library_search_saves_details_for_top_five_only(store, tmdb):
    tmdb.routes = {"/search/movie": (200, _search_hit(*range(1, 9)))}
    tmdb.routes.update({f"/movie/{i}": (200, _movie(i, "Angry Boys")) for i in range(1, 9)})
    found = asyncio.run(library_matcher._search("Angry Boys", "movie", 2011))
    assert len(found) == 8
    assert sorted(c for c in tmdb.calls if c != "/search/movie") == [f"/movie/{i}" for i in range(1, 6)]


def test_library_search_skips_details_already_stored(store, tmdb):
    store.upsert_payload("movie", _movie(1, "Angry Boys"))
    tmdb.routes = {"/search/movie": (200, _search_hit(1))}
    asyncio.run(library_matcher._search("Angry Boys", "movie", 1990))
    assert tmdb.calls == ["/search/movie"]


def test_library_search_not_found_kept_for_a_week(store, tmdb):
    tmdb.routes = {"/search/tv": (200, {"results": []})}
    assert asyncio.run(library_matcher._search("Nothing Here", "series", 1960)) == []
    assert asyncio.run(library_matcher._search("Nothing Here", "series", 1960)) == []
    assert tmdb.calls == ["/search/tv"]
    _age_searches(WEEK + 60)
    asyncio.run(library_matcher._search("Nothing Here", "series", 1960))
    assert tmdb.calls == ["/search/tv", "/search/tv"]


def test_library_search_found_answer_does_not_expire(store, tmdb):
    tmdb.routes = {"/search/movie": (200, _search_hit(1)), "/movie/1": (200, _movie(1))}
    asyncio.run(library_matcher._search("Angry Boys", "movie", 2011))
    _age_searches(60 * 86400)
    tmdb.calls.clear()
    asyncio.run(library_matcher._search("Angry Boys", "movie", 2011))
    assert tmdb.calls == []


def test_library_search_year_is_part_of_the_question(store, tmdb):
    tmdb.routes = {"/search/movie": (200, {"results": []})}
    asyncio.run(library_matcher._search("Angry Boys", "movie", 2011))
    asyncio.run(library_matcher._search("Angry Boys", "movie", 1990))
    assert tmdb.calls == ["/search/movie", "/search/movie"]


def test_kept_answer_counts_as_local_lookup(store, tmdb):
    tmdb.routes = {"/search/tv": (200, {"results": []})}
    asyncio.run(library_matcher._search("Nothing Here", "series", 1960))
    asyncio.run(library_matcher._search("Nothing Here", "series", 1960))
    assert store.lookups_today() == {"local": 1, "tmdb": 1}


def test_search_requests_count_against_budget(store, tmdb):
    tmdb.routes = {"/search/tv": (200, {"results": []})}
    asyncio.run(library_matcher._search("Nothing Here", "series", 1960))
    assert store.requests_today() == 1


def test_library_search_store_off_keeps_nothing(store, tmdb):
    _off()
    tmdb.routes = {"/search/movie": (200, {"results": []})}
    asyncio.run(library_matcher._search("Angry Boys", "movie", 2011))
    asyncio.run(library_matcher._search("Angry Boys", "movie", 2011))
    assert tmdb.calls == ["/search/movie", "/search/movie"]


# --- IMDb lookup ---------------------------------------------------------------

def test_imdb_find_answer_and_title_kept(store, tmdb):
    tmdb.routes = {"/find/tt9": (200, {"movie_results": [{"id": 3, "title": "X", "release_date": "2000-01-01"}]}),
                   "/movie/3": (200, _movie(3, "X", imdb_id="tt9"))}
    assert asyncio.run(library_matcher._lookup_by_imdb("tt9", "movie")).tmdb_id == "3"
    assert store.get_title("movie", 3) is not None
    tmdb.calls.clear()
    assert asyncio.run(library_matcher._lookup_by_imdb("tt9", "movie")).tmdb_id == "3"
    assert tmdb.calls == []


def test_imdb_find_nothing_kept_for_a_week(store, tmdb):
    tmdb.routes = {"/find/tt8": (200, {"movie_results": [], "tv_results": []})}
    assert asyncio.run(library_matcher._lookup_by_imdb("tt8", "movie")) is None
    assert asyncio.run(library_matcher._lookup_by_imdb("tt8", "series")) is None
    assert tmdb.calls == ["/find/tt8"]
    _age_searches(WEEK + 60)
    asyncio.run(library_matcher._lookup_by_imdb("tt8", "movie"))
    assert tmdb.calls == ["/find/tt8", "/find/tt8"]


# --- review search (search_title) ------------------------------------------------

def test_search_title_answer_kept_and_reused(store, tmdb):
    tmdb.routes = {"/search/movie": (200, _search_hit(1, 2)),
                   "/movie/1": (200, _movie(1, "Angry Boys")), "/movie/2": (200, _movie(2, "Angry Boys", "1990"))}
    first = asyncio.run(tmdb_sync.search_title("Angry Boys", "movie"))
    tmdb.calls.clear()
    second = asyncio.run(tmdb_sync.search_title("Angry Boys", "movie"))
    assert tmdb.calls == []
    assert [r["tmdb_id"] for r in second] == [r["tmdb_id"] for r in first] == ["1", "2"]
    assert second[1]["year"] == 2011  # same answer TMDB gave the first time


# --- TMDB Library page lookup ---------------------------------------------------

def test_library_page_lookup_answer_kept(store, tmdb):
    # A query that only matches TMDB's fuzzy search, not a stored name.
    tmdb.routes = {"/search/tv": (200, {"results": [{"id": 10, "name": "Father Knows Best"}]}),
                   "/tv/10": (200, _tv(10))}
    first = asyncio.run(tmdb_fill.lookup("fkb", "tv"))
    assert first["source"] == "tmdb"
    tmdb.calls.clear()
    second = asyncio.run(tmdb_fill.lookup("fkb", "tv"))
    assert tmdb.calls == []
    assert second["source"] == "local"
    assert [r["tmdb_id"] for r in second["results"]] == [10]


# --- TMDB list sync ------------------------------------------------------------------

def test_list_sync_stays_live_but_saves_titles(store, tmdb):
    tmdb.routes = {"/list/7": (200, {"item_count": 2, "items": [
        {"media_type": "movie", "id": 1, "title": "Angry Boys"},
        {"media_type": "tv", "id": 10, "name": "Father Knows Best"}]}),
        "/movie/1": (200, _movie(1)), "/tv/10": (200, _tv(10))}
    asyncio.run(tmdb_sync.fetch_list_items("7"))
    assert store.get_title("movie", 1) is not None and store.get_title("tv", 10) is not None
    tmdb.calls.clear()
    asyncio.run(tmdb_sync.fetch_list_items("7"))
    assert tmdb.calls == ["/list/7"]  # membership re-checked, titles not re-fetched

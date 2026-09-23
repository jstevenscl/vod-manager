"""library_matcher.decide() is the no-guessing scoring step; match() adds the
explicit-id shortcuts and the per-show cache. TMDB itself is stubbed out."""

import asyncio

import library_matcher as lm
from library_parser import parse_path


def C(tmdb_id, title, year, pop=1.0):
    return lm.Candidate(tmdb_id, title, year, pop)


def test_normalize_title():
    assert lm.normalize_title("Amélie") == lm.normalize_title("amelie")
    assert lm.normalize_title("Spider-Man: Homecoming") == "spider man homecoming"
    assert lm.normalize_title("Tom & Jerry") == lm.normalize_title("Tom and Jerry")
    assert lm.normalize_title("The Office") == lm.normalize_title("Office")


def test_exact_title_and_year_matches():
    r = lm.decide("The Matrix", 1999, [C("603", "The Matrix", 1999), C("999", "The Matrix Reloaded", 2003)])
    assert (r.status, r.tmdb_id, r.confidence) == ("matched", "603", "title_year")


def test_year_off_by_one_is_tolerated():
    r = lm.decide("Heat", 1996, [C("949", "Heat", 1995)])
    assert r.status == "matched" and r.tmdb_id == "949"


def test_year_disagrees_goes_to_review_not_matched():
    r = lm.decide("Heat", 2010, [C("949", "Heat", 1995)])
    assert r.status == "ambiguous"
    assert r.reason == "exact title but year disagrees"


def test_remake_within_a_year_prefers_exact_year_if_unique():
    r = lm.decide("Thing", 2011, [C("1", "Thing", 2010), C("2", "Thing", 2011)])
    assert (r.status, r.tmdb_id) == ("matched", "2")


def test_no_year_unique_exact_title_is_title_only():
    r = lm.decide("Breaking Bad", None, [C("1396", "Breaking Bad", 2008), C("5", "Breaking Bad: Extras", 2010)])
    assert (r.status, r.tmdb_id, r.confidence) == ("matched", "1396", "title_only")


def test_no_year_multiple_exact_titles_is_ambiguous():
    r = lm.decide("Dracula", None, [C("1", "Dracula", 1931), C("2", "Dracula", 1992)])
    assert r.status == "ambiguous" and len(r.candidates) == 2


def test_fuzzy_only_is_never_auto_matched():
    r = lm.decide("Blade Runer", 2017, [C("335984", "Blade Runner 2049", 2017)])
    assert r.status == "ambiguous" and r.reason == "no exact title match"


def test_no_results_is_unmatched():
    assert lm.decide("zzzz", 2001, []).status == "unmatched"


def test_explicit_tmdb_id_skips_search(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("search must not run when an id is in the path")
    monkeypatch.setattr(lm, "_search", boom)
    r = asyncio.run(lm.match(parse_path("Heat (1995) {tmdb-949}/Heat.mkv")))
    assert (r.status, r.tmdb_id, r.confidence) == ("matched", "949", "explicit_id")


def test_year_filtered_miss_retries_unfiltered(monkeypatch):
    calls = []

    async def fake_search(query, content_type, year):
        calls.append(year)
        return [] if year else [C("949", "Heat", 1995)]
    monkeypatch.setattr(lm, "_search", fake_search)
    r = asyncio.run(lm.match(parse_path("Heat (1995).mkv")))
    assert calls == [1995, None]
    assert r.status == "matched"


def test_tmdb_failure_is_unmatched_not_an_error(monkeypatch):
    async def fail(*a, **k):
        raise ValueError("TMDB API key not configured")
    monkeypatch.setattr(lm, "_search", fail)
    r = asyncio.run(lm.match(parse_path("Heat (1995).mkv")))
    assert r.status == "unmatched" and r.reason == "TMDB lookup failed"


def test_show_cache_resolves_once_per_show(monkeypatch):
    n = 0

    async def fake_search(query, content_type, year):
        nonlocal n
        n += 1
        return [C("1396", "Breaking Bad", 2008)]
    monkeypatch.setattr(lm, "_search", fake_search)
    cache = lm.ShowMatchCache()

    async def run():
        for ep in range(1, 6):
            r = await cache.match_episode(parse_path(f"TV/Breaking Bad (2008)/Season 1/S01E0{ep}.mkv"))
            assert r.tmdb_id == "1396"
    asyncio.run(run())
    assert n == 1


# --- persistence (resolve) -------------------------------------------------

def _stub_search(monkeypatch, results, counter):
    async def fake_search(query, content_type, year):
        counter.append(query)
        return results
    monkeypatch.setattr(lm, "_search", fake_search)


def test_resolve_persists_and_reuses_decision(db, monkeypatch):
    pid = db.upsert_provider("lib", "/mnt/movies", "", "", provider_type="library")
    calls = []
    _stub_search(monkeypatch, [C("603", "The Matrix", 1999)], calls)
    path = "The.Matrix.1999.1080p.mkv"

    first = asyncio.run(lm.resolve(pid, path, parse_path(path), lm.ShowMatchCache()))
    second = asyncio.run(lm.resolve(pid, path, parse_path(path), lm.ShowMatchCache()))  # fresh scan
    assert first.tmdb_id == second.tmdb_id == "603"
    assert len(calls) == 1                                  # rescan hit the DB, not TMDB
    assert db.get_library_match(pid, path, "movie")["source"] == "auto"


def test_resolve_rematches_when_path_changes_query(db, monkeypatch):
    pid = db.upsert_provider("lib", "/mnt/movies", "", "", provider_type="library")
    calls = []
    _stub_search(monkeypatch, [C("603", "The Matrix", 1999)], calls)
    asyncio.run(lm.resolve(pid, "a/movie.mkv", parse_path("The.Matrix.1999.mkv"), lm.ShowMatchCache()))
    asyncio.run(lm.resolve(pid, "a/movie.mkv", parse_path("The.Matrix.Reloaded.2003.mkv"), lm.ShowMatchCache()))
    assert len(calls) == 2


def test_manual_match_survives_rescan_and_wins(db, monkeypatch):
    pid = db.upsert_provider("lib", "/mnt/movies", "", "", provider_type="library")
    db.upsert_library_match(pid, "x.mkv", "movie", status="matched", query="Dracula|", tmdb_id="138", title="Dracula",
                            year=1992, confidence="explicit_id", source="manual")
    calls = []
    _stub_search(monkeypatch, [C("1", "Dracula", 1931)], calls)
    r = asyncio.run(lm.resolve(pid, "x.mkv", parse_path("Dracula (1931).mkv"), lm.ShowMatchCache()))
    assert r.tmdb_id == "138" and calls == []
    # and an auto write can never clobber it
    assert db.upsert_library_match(pid, "x.mkv", "movie", status="matched", query="q", tmdb_id="1") is False
    assert db.get_library_match(pid, "x.mkv", "movie")["tmdb_id"] == "138"


def test_transient_failure_is_not_persisted(db, monkeypatch):
    pid = db.upsert_provider("lib", "/mnt/movies", "", "", provider_type="library")

    async def fail(*a, **k):
        raise ValueError("TMDB API key not configured")
    monkeypatch.setattr(lm, "_search", fail)
    r = asyncio.run(lm.resolve(pid, "h.mkv", parse_path("Heat (1995).mkv"), lm.ShowMatchCache()))
    assert r.transient and db.get_library_match(pid, "h.mkv", "movie") is None


def test_tv_resolves_once_per_show_and_persists_by_folder(db, monkeypatch):
    pid = db.upsert_provider("lib", "/mnt/tv", "", "", provider_type="library")
    calls = []
    _stub_search(monkeypatch, [C("1396", "Breaking Bad", 2008)], calls)
    cache = lm.ShowMatchCache()
    for ep in (1, 2, 3):
        rel = f"TV/Breaking Bad (2008)/Season 1/S01E0{ep}.mkv"
        assert asyncio.run(lm.resolve(pid, rel, parse_path(rel), cache)).tmdb_id == "1396"
    assert len(calls) == 1
    assert db.get_library_match(pid, "TV/Breaking Bad (2008)", "series")["tmdb_id"] == "1396"


def test_ambiguous_candidates_round_trip(db, monkeypatch):
    pid = db.upsert_provider("lib", "/mnt/tv", "", "", provider_type="library")
    _stub_search(monkeypatch, [C("1", "Dracula", 1931), C("2", "Dracula", 1992)], [])
    asyncio.run(lm.resolve(pid, "d.mkv", parse_path("Dracula.mkv"), lm.ShowMatchCache()))
    again = asyncio.run(lm.resolve(pid, "d.mkv", parse_path("Dracula.mkv"), lm.ShowMatchCache()))
    assert again.status == "ambiguous" and [c.tmdb_id for c in again.candidates] == ["1", "2"]


def test_prune_removes_only_missing_keys(db):
    pid = db.upsert_provider("lib", "/mnt/movies", "", "", provider_type="library")
    for k in ("a.mkv", "b.mkv"):
        db.upsert_library_match(pid, k, "movie", status="unmatched", query="q")
    assert db.delete_library_matches_not_in(pid, "movie", {"a.mkv"}) == 1
    assert db.get_library_match(pid, "a.mkv", "movie") and not db.get_library_match(pid, "b.mkv", "movie")

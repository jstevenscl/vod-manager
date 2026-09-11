"""beads-974 (follow-up): bulk_import_movies/bulk_import_series previously
matched an incoming item to an existing movie/series purely by
provider_stream_id / (name, year) / single-candidate name -- with no check
that the item's own language (derived from raw_name via _source_language)
was compatible with the languages already sitting on that row's sources.

This let a provider's re-scan silently re-attach a foreign-language stream
onto a movie/series that apply_movie_language_split/apply_series_language_split
had already cleaned up, re-mixing it on the very next import cycle (confirmed
live 2026-09-10/11: movie "Foe" (2023) picked up a second GR source this way
minutes after its split ran).

Fix: each match branch now also requires the item's language to share at
least one language (COALESCE-to-EN, overlap not equality -- same rule as
_shares_a_language) with the matched row's current sources before accepting
the match. A same-name/year/stream-id match with a conflicting language is
treated as no match: it attaches to an existing same-language sibling row if
one already exists (so repeated imports of an already-split-off language
don't create a new duplicate row every cycle), else creates a new row."""

import config


def _import_movie(db, provider_id, name, stream_id, raw_name, tmdb_id=None, year=2001):
    db.bulk_import_movies(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_stream_id": stream_id,
            "container_extension": "mp4",
            "raw_name": raw_name,
            "tmdb_id": tmdb_id,
            "_has_detail": True,
        },
    ])


def _import_series(db, provider_id, name, provider_series_id, raw_name, tmdb_id=None, year=2001):
    db.bulk_import_series(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_series_id": provider_series_id,
            "raw_name": raw_name,
            "tmdb_id": tmdb_id,
            "_has_detail": True,
        },
    ])


# -- movies: match by (name, year) --------------------------------------

def test_conflicting_language_by_name_year_creates_new_row_not_reuse(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Amelie", "en-1", "EN - Amelie", tmdb_id=194)
    original = db.get_movie_by_name_year("Amelie", 2001)

    # Different provider_stream_id, same (name, year), but a conflicting
    # (GR) language -- must NOT attach to the EN row.
    _import_movie(db, provider_id, "Amelie", "gr-1", "GR - Amelie", tmdb_id=194)

    all_movies = db.list_movies(limit=1000)
    amelie_rows = [m for m in all_movies if m["name"] == "Amelie" and m["year"] == 2001]
    assert len(amelie_rows) == 2

    original_sources = db.list_movie_sources(original["id"])
    assert {s["provider_stream_id"] for s in original_sources} == {"en-1"}

    new_row = next(m for m in amelie_rows if m["id"] != original["id"])
    new_sources = db.list_movie_sources(new_row["id"])
    assert {s["provider_stream_id"] for s in new_sources} == {"gr-1"}
    assert {s["language"] for s in new_sources} == {"GR"}


def test_same_language_by_name_year_still_matches_as_before(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Amelie", "en-1", "EN - Amelie", tmdb_id=194)
    original = db.get_movie_by_name_year("Amelie", 2001)

    # Second EN source, same (name, year) -- should still attach to the same row.
    _import_movie(db, provider_id, "Amelie", "en-2", "AMZ - Amelie", tmdb_id=194)

    all_movies = db.list_movies(limit=1000)
    amelie_rows = [m for m in all_movies if m["name"] == "Amelie" and m["year"] == 2001]
    assert len(amelie_rows) == 1

    sources = db.list_movie_sources(original["id"])
    assert {s["provider_stream_id"] for s in sources} == {"en-1", "en-2"}


def test_untagged_language_defaults_to_en_and_still_matches(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Amelie", "en-1", "EN - Amelie", tmdb_id=194)
    original = db.get_movie_by_name_year("Amelie", 2001)

    # raw_name with no recognizable language prefix -- _source_language
    # defaults it to EN, so this must still match the EN row.
    _import_movie(db, provider_id, "Amelie", "en-2", "Amelie", tmdb_id=194)

    all_movies = db.list_movies(limit=1000)
    amelie_rows = [m for m in all_movies if m["name"] == "Amelie" and m["year"] == 2001]
    assert len(amelie_rows) == 1

    sources = db.list_movie_sources(original["id"])
    assert {s["provider_stream_id"] for s in sources} == {"en-1", "en-2"}


def test_repeated_conflicting_import_reuses_split_sibling_not_new_row_each_time(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Amelie", "en-1", "EN - Amelie", tmdb_id=194)

    # First GR import creates the sibling row.
    _import_movie(db, provider_id, "Amelie", "gr-1", "GR - Amelie", tmdb_id=194)
    # A later import cycle re-sees the SAME GR stream (upsert path) plus a
    # second, new GR stream_id -- neither should create ANOTHER new row;
    # both must land on the one GR sibling already created.
    _import_movie(db, provider_id, "Amelie", "gr-1", "GR - Amelie", tmdb_id=194)
    _import_movie(db, provider_id, "Amelie", "gr-2", "GR - Amelie (alt)", tmdb_id=194)

    all_movies = db.list_movies(limit=1000)
    amelie_rows = [m for m in all_movies if m["name"] == "Amelie" and m["year"] == 2001]
    assert len(amelie_rows) == 2  # original EN row + exactly one GR sibling

    # Identify the GR row by its sources rather than assuming ordering.
    gr_rows = [m for m in amelie_rows if {s["language"] for s in db.list_movie_sources(m["id"])} == {"GR"}]
    assert len(gr_rows) == 1
    gr_sources = db.list_movie_sources(gr_rows[0]["id"])
    assert {s["provider_stream_id"] for s in gr_sources} == {"gr-1", "gr-2"}


def test_conflicting_language_reattach_via_existing_stream_id_does_not_pull_back(db):
    """Reproduces the exact live regression: an existing movie_sources row
    (already re-seen by its provider before) must not silently drag its
    movie_id back onto a language-conflicting target on ON CONFLICT upsert."""
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Amelie", "en-1", "EN - Amelie", tmdb_id=194)
    _import_movie(db, provider_id, "Amelie", "gr-1", "GR - Amelie", tmdb_id=194)

    original = db.get_movie_by_name_year("Amelie", 2001)

    # Re-import (re-scan) of the same GR stream_id -- ON CONFLICT DO UPDATE
    # path. Must keep landing on the GR sibling, not the EN original.
    _import_movie(db, provider_id, "Amelie", "gr-1", "GR - Amelie", tmdb_id=194)

    original_sources_after = db.list_movie_sources(original["id"])
    assert {s["provider_stream_id"] for s in original_sources_after} == {"en-1"}


# -- movies: match by single-candidate name (year is None) --------------

def test_conflicting_language_by_single_candidate_name_creates_new_row(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Amelie", "en-1", "EN - Amelie", tmdb_id=194, year=None)

    _import_movie(db, provider_id, "Amelie", "gr-1", "GR - Amelie", tmdb_id=194, year=None)

    all_movies = db.list_movies(limit=1000)
    amelie_rows = [m for m in all_movies if m["name"] == "Amelie"]
    assert len(amelie_rows) == 2


# -- series: mirror coverage ----------------------------------------------

def test_series_conflicting_language_by_name_year_creates_new_row(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_series(db, provider_id, "Dark", "s-en-1", "EN - Dark", tmdb_id=772)
    original = db.get_series_by_name_year("Dark", 2001)

    _import_series(db, provider_id, "Dark", "s-gr-1", "GR - Dark", tmdb_id=772)

    all_series = db.list_series(limit=1000)
    dark_rows = [s for s in all_series if s["name"] == "Dark" and s["year"] == 2001]
    assert len(dark_rows) == 2

    original_sources = db.list_series_sources(original["id"])
    assert {s["provider_series_id"] for s in original_sources} == {"s-en-1"}


def test_series_same_language_by_name_year_still_matches(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_series(db, provider_id, "Dark", "s-en-1", "EN - Dark", tmdb_id=772)
    original = db.get_series_by_name_year("Dark", 2001)

    _import_series(db, provider_id, "Dark", "s-en-2", "AMZ - Dark", tmdb_id=772)

    all_series = db.list_series(limit=1000)
    dark_rows = [s for s in all_series if s["name"] == "Dark" and s["year"] == 2001]
    assert len(dark_rows) == 1

    sources = db.list_series_sources(original["id"])
    assert {s["provider_series_id"] for s in sources} == {"s-en-1", "s-en-2"}


def test_series_repeated_conflicting_import_reuses_split_sibling(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_series(db, provider_id, "Dark", "s-en-1", "EN - Dark", tmdb_id=772)
    _import_series(db, provider_id, "Dark", "s-gr-1", "GR - Dark", tmdb_id=772)
    _import_series(db, provider_id, "Dark", "s-gr-1", "GR - Dark", tmdb_id=772)

    all_series = db.list_series(limit=1000)
    dark_rows = [s for s in all_series if s["name"] == "Dark" and s["year"] == 2001]
    assert len(dark_rows) == 2

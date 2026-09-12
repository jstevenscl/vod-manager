"""Real bug found live 2026-09-12 via UI screenshots showing sibling
"duplicate" rows like "#BringBackAlice (2023)" vs "#BringBackAlice (PL)",
"12 Monkeys (2015)" vs "12 Monkeys (US)", "15 Storeys High (2002)" vs
"15 Storeys High (2002) (GB)".

GB/PL/US etc. here are NOT language codes -- confirmed live, they never
appear as a stored movie_sources.language/series_sources.language value.
They're cosmetic trailing country-of-origin tags some providers append to
the raw title. The actual language is always driven by a separate LEADING
prefix (see _source_language), unrelated to this trailing suffix.

Root cause: bulk_import_movies/bulk_import_series matched candidates on
EXACT (name, year) STRING equality with zero suffix normalization. Two
providers' rows for the same title/year/language never merged whenever one
provider's raw title carried extra literal text (a trailing "(GB)"/"(PL)"/
"(US)" tag, or a redundant literal "(YYYY)") that the other's name lacked --
even with an identical `year` column value and identical language.

Fix: _import_match_key_name() normalizes the in-memory LOOKUP KEY only
(strips one trailing known-country-code tag, then one trailing literal
year) used to bucket candidates during import matching -- the `name`
column actually stored/displayed is completely untouched, so display stays
exactly what providers/rules produced."""

import config


def _import_movie(db, provider_id, name, stream_id, raw_name, tmdb_id=None, year=2002):
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


def _import_series(db, provider_id, name, provider_series_id, raw_name, tmdb_id=None, year=2002):
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


def test_movie_country_suffix_variant_merges_into_same_row(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "12 Monkeys", "en-1", "EN - 12 Monkeys", tmdb_id=63, year=2015)

    # Same title/year/language, but this provider's raw title carries a
    # trailing "(US)" country-of-origin tag that the first provider's didn't.
    _import_movie(db, provider_id, "12 Monkeys (US)", "en-2", "EN - 12 Monkeys (US)", tmdb_id=63, year=2015)

    all_movies = db.list_movies(limit=1000)
    matches = [m for m in all_movies if m["year"] == 2015 and m["name"].startswith("12 Monkeys")]
    assert len(matches) == 1, f"expected one merged row, got {[m['name'] for m in matches]}"

    sources = db.list_movie_sources(matches[0]["id"])
    assert {s["provider_stream_id"] for s in sources} == {"en-1", "en-2"}

    # Display name is untouched -- whichever row's `name` was actually
    # stored is exactly what a provider/rule produced, not a normalized key.
    assert matches[0]["name"] in ("12 Monkeys", "12 Monkeys (US)")


def test_movie_redundant_literal_year_variant_merges_into_same_row(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "15 Storeys High", "en-1", "EN - 15 Storeys High", tmdb_id=999, year=2002)

    # Same year column value, but this provider's title also has a
    # redundant literal "(2002)" baked into the name itself.
    _import_movie(db, provider_id, "15 Storeys High (2002)", "en-2", "EN - 15 Storeys High (2002)", tmdb_id=999, year=2002)

    all_movies = db.list_movies(limit=1000)
    matches = [m for m in all_movies if m["year"] == 2002 and m["name"].startswith("15 Storeys High")]
    assert len(matches) == 1, f"expected one merged row, got {[m['name'] for m in matches]}"

    sources = db.list_movie_sources(matches[0]["id"])
    assert {s["provider_stream_id"] for s in sources} == {"en-1", "en-2"}


def test_movie_stacked_year_and_country_suffix_is_a_known_scope_limit(db):
    """Documents an accepted scope limit (see _import_match_key_name's
    docstring): the candidate-lookup query only fetches rows whose stored
    name exactly equals one of the incoming item's own (raw or normalized)
    strings -- it does not scan the table. A title stacking BOTH a literal
    year AND a country suffix on the same side normalizes to the right key
    in Python, but isn't fetched as a SQL candidate unless that exact
    string already exists as a stored name. Broadening the query to a
    same-year table scan per chunk wasn't worth it for a case that hasn't
    been seen live -- the single-layer case actually reported (plain title
    vs. title+suffix, or plain title vs. title+literal-year) is fully
    covered by the other tests in this file."""
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "15 Storeys High (2002)", "en-1", "EN - 15 Storeys High (2002)", tmdb_id=999, year=2002)

    _import_movie(db, provider_id, "15 Storeys High (2002) (GB)", "en-2", "EN - 15 Storeys High (2002) (GB)", tmdb_id=999, year=2002)

    all_movies = db.list_movies(limit=1000)
    matches = [m for m in all_movies if m["year"] == 2002 and m["name"].startswith("15 Storeys High")]
    assert len(matches) == 2, "stacked-suffix case is a known scope limit -- see docstring"


def test_movie_conflicting_language_still_creates_sibling_even_with_suffix(db):
    """The suffix-normalization fix must not weaken the existing beads-974
    language-conflict gate -- a genuinely different-language item with a
    suffix variant still must NOT merge into the other language's row."""
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "12 Monkeys", "en-1", "EN - 12 Monkeys", tmdb_id=63, year=2015)

    _import_movie(db, provider_id, "12 Monkeys (US)", "gr-1", "GR - 12 Monkeys (US)", tmdb_id=63, year=2015)

    all_movies = db.list_movies(limit=1000)
    matches = [m for m in all_movies if m["year"] == 2015 and m["name"].startswith("12 Monkeys")]
    assert len(matches) == 2, f"expected two rows (conflicting languages), got {[m['name'] for m in matches]}"


def test_series_country_suffix_variant_merges_into_same_row(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_series(db, provider_id, "15 Storeys High", "s-en-1", "EN - 15 Storeys High", tmdb_id=772, year=2002)

    _import_series(db, provider_id, "15 Storeys High (2002)", "s-en-2", "EN - 15 Storeys High (2002)", tmdb_id=772, year=2002)

    all_series = db.list_series(limit=1000)
    matches = [s for s in all_series if s["year"] == 2002 and s["name"].startswith("15 Storeys High")]
    assert len(matches) == 1, f"expected one merged row, got {[s['name'] for s in matches]}"

    sources = db.list_series_sources(matches[0]["id"])
    assert {s["provider_series_id"] for s in sources} == {"s-en-1", "s-en-2"}


def test_series_conflicting_language_still_creates_sibling_even_with_suffix(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_series(db, provider_id, "Dark", "s-en-1", "EN - Dark", tmdb_id=772, year=2017)

    _import_series(db, provider_id, "Dark (DE)", "s-gr-1", "GR - Dark (DE)", tmdb_id=772, year=2017)

    all_series = db.list_series(limit=1000)
    matches = [s for s in all_series if s["year"] == 2017 and s["name"].startswith("Dark")]
    assert len(matches) == 2, f"expected two rows (conflicting languages), got {[s['name'] for s in matches]}"

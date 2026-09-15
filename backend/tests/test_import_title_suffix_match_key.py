"""_import_match_key_name lets bulk_import_movies/bulk_import_series collapse
two providers' rows for the same title/year onto one row even when one
provider's raw name carries a trailing country-of-origin tag ("(US)"/"(GB)")
or a redundant literal "(YYYY)" the other lacks -- confirmed live via UI
screenshots showing sibling "duplicate" rows like "12 Monkeys (2015)" vs.
"12 Monkeys (US)" and "15 Storeys High (2002)" vs. "15 Storeys High (2002) (GB)".
"""

import vod_db


def test_import_match_key_strips_country_suffix():
    assert vod_db._import_match_key_name("12 Monkeys (US)") == "12 Monkeys"


def test_import_match_key_strips_trailing_literal_year():
    assert vod_db._import_match_key_name("15 Storeys High (2002)") == "15 Storeys High"


def test_import_match_key_strips_both_layers():
    # Country suffix strips first ("(GB)"), exposing a trailing literal year
    # which strips next -- same final key as either single-layer variant.
    assert vod_db._import_match_key_name("15 Storeys High (2002) (GB)") == "15 Storeys High"


def test_import_match_key_leaves_unrelated_parenthetical_alone():
    # Not a known country code -- must not be stripped as if it were one.
    assert vod_db._import_match_key_name("Some Title (Director's Cut)") == "Some Title (Director's Cut)"


def test_bulk_import_movies_collapses_country_suffix_variant(db):
    provider_a = db.upsert_provider("Provider A", "http://a.example.com", "user", "pass")
    provider_b = db.upsert_provider("Provider B", "http://b.example.com", "user", "pass")

    db.bulk_import_movies(provider_a, [
        {"name": "15 Storeys High", "year": 2002, "provider_stream_id": "100", "container_extension": "mkv"},
    ])
    result = db.bulk_import_movies(provider_b, [
        {"name": "15 Storeys High (2002)", "year": 2002, "provider_stream_id": "200", "container_extension": "mkv"},
    ])

    all_movies = db.list_movies(limit=1000)
    matching = [m for m in all_movies if m["name"] in ("15 Storeys High", "15 Storeys High (2002)")]
    assert len(matching) == 1
    assert result["movies_matched"] == 1
    assert result["movies_created"] == 0
    sources = db.list_movie_sources(matching[0]["id"])
    assert {s["provider_id"] for s in sources} == {provider_a, provider_b}


def test_bulk_import_series_collapses_country_suffix_variant(db):
    provider_a = db.upsert_provider("Provider A", "http://a.example.com", "user", "pass")
    provider_b = db.upsert_provider("Provider B", "http://b.example.com", "user", "pass")

    db.bulk_import_series(provider_a, [
        {"name": "12 Monkeys", "year": 2015, "provider_series_id": "100", "provider_category_name": "Series"},
    ])
    result = db.bulk_import_series(provider_b, [
        {"name": "12 Monkeys (US)", "year": 2015, "provider_series_id": "200", "provider_category_name": "Series"},
    ])

    all_series = db.list_series(limit=1000)
    matching = [s for s in all_series if s["name"] in ("12 Monkeys", "12 Monkeys (US)")]
    assert len(matching) == 1
    assert result["series_matched"] == 1
    assert result["series_created"] == 0


def test_bulk_import_movies_exact_match_still_wins_over_match_key(db):
    """An exact (name, year) match must never be bypassed in favor of the
    looser match-key pass -- the match-key branch is only reached once the
    exact match has already missed."""
    provider_a = db.upsert_provider("Provider A", "http://a.example.com", "user", "pass")
    provider_b = db.upsert_provider("Provider B", "http://b.example.com", "user", "pass")

    db.bulk_import_movies(provider_a, [
        {"name": "Exact Title", "year": 2019, "provider_stream_id": "100", "container_extension": "mkv"},
    ])
    result = db.bulk_import_movies(provider_b, [
        {"name": "Exact Title", "year": 2019, "provider_stream_id": "200", "container_extension": "mkv"},
    ])

    all_movies = db.list_movies(limit=1000)
    matching = [m for m in all_movies if m["name"] == "Exact Title"]
    assert len(matching) == 1
    assert result["movies_matched"] == 1

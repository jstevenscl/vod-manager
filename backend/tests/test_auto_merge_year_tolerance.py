"""Same TMDB ID cards whose years differ by at most 2 auto-merge (provider
festival vs release year, regional dates). Wider gaps and different
languages still stay separate. The Duplicate Finder explains, per group,
why its cards were not auto-merged."""

import pytest

import config


def _movie(db, provider_id, name, year, stream_id, tmdb_id, raw_name=None):
    db.bulk_import_movies(provider_id, [{
        "name": name, "year": year, "provider_stream_id": stream_id,
        "container_extension": "mp4", "raw_name": raw_name or name,
        "tmdb_id": tmdb_id, "_has_detail": True,
    }])
    return db.get_movie_by_name_year(name, year)


@pytest.fixture
def provider_id(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    return db.upsert_provider("prov1", "http://example.com", "user", "pass")


@pytest.mark.parametrize("gap", [1, 2])
def test_movie_same_tmdb_merges_within_two_years(db, provider_id, gap):
    first = _movie(db, provider_id, "Terrifier", 2018, "one", 420634)
    second = _movie(db, provider_id, "Terrifier", 2018 - gap, "two", 420634)

    db.auto_merge_movie_by_tmdb(first["id"])

    assert db.get_movie(first["id"]) is not None
    assert db.get_movie(second["id"]) is None


def test_movie_same_tmdb_alternate_title_merges_within_two_years(db, provider_id):
    first = _movie(db, provider_id, "All Roads to Pearla", 2019, "one", 576727)
    second = _movie(db, provider_id, "Sleeping in Plastic", 2020, "two", 576727)

    db.auto_merge_movie_by_tmdb(first["id"])

    assert db.get_movie(second["id"]) is None


def test_movie_same_tmdb_three_years_apart_stays_separate(db, provider_id):
    first = _movie(db, provider_id, "Burn Your Maps", 2016, "one", 352504)
    second = _movie(db, provider_id, "Burn Your Maps", 2019, "two", 352504)

    db.auto_merge_movie_by_tmdb(first["id"])

    assert db.get_movie(second["id"]) is not None


def test_movie_within_two_years_different_language_stays_separate(db, provider_id):
    config.save_enabled_languages(["EN", "FR"])
    first = _movie(db, provider_id, "Amelie", 2001, "en", 194, raw_name="EN - Amelie")
    second = _movie(db, provider_id, "FR - Amelie", 2002, "fr", 194, raw_name="FR - Amelie")

    db.auto_merge_movie_by_tmdb(first["id"])

    assert db.get_movie(second["id"]) is not None


def test_series_same_tmdb_merges_within_two_years(db, provider_id):
    db.bulk_import_series(provider_id, [
        {"name": "Example Show", "year": 2020, "provider_series_id": "one",
         "raw_name": "Example Show", "tmdb_id": 778, "_has_detail": True},
        {"name": "Example Show", "year": 2022, "provider_series_id": "two",
         "raw_name": "Example Show", "tmdb_id": 778, "_has_detail": True},
    ])
    rows = [s for s in db.list_series(limit=1000) if s["tmdb_id"] == "778"]
    assert len(rows) == 2

    db.auto_merge_series_by_tmdb(rows[0]["id"])

    assert sum(1 for row in rows if db.get_series(row["id"])) == 1


def _reasons(db, *movies):
    group = {"items": [{"id": m["id"], "name": m["name"], "year": m["year"], "tmdb_id": m["tmdb_id"]} for m in movies]}
    db.annotate_auto_merge_reasons("movie", [group])
    return " | ".join(group["auto_merge_reasons"])


def test_reason_year_gap_too_large(db, provider_id):
    a = _movie(db, provider_id, "Burn Your Maps", 2016, "one", 352504)
    b = _movie(db, provider_id, "Burn Your Maps", 2019, "two", 352504)
    assert "2016 vs 2019" in _reasons(db, a, b)


def test_reason_no_shared_language(db, provider_id):
    config.save_enabled_languages(["EN", "FR"])
    a = _movie(db, provider_id, "Amelie", 2001, "en", 194, raw_name="EN - Amelie")
    b = _movie(db, provider_id, "FR - Amelie", 2001, "fr", 194, raw_name="FR - Amelie")
    assert "No shared language" in _reasons(db, a, b)


def test_reason_missing_tmdb(db, provider_id):
    a = _movie(db, provider_id, "2050", 2020, "one", None)
    b = _movie(db, provider_id, "2050", 2019, "two", None)
    assert "No TMDB ID" in _reasons(db, a, b)


def test_reason_different_tmdb_ids(db, provider_id):
    a = _movie(db, provider_id, "Run", 2020, "one", 546121)
    b = _movie(db, provider_id, "Run", 2021, "two", 999001)
    assert "Different TMDB IDs" in _reasons(db, a, b)


def test_reason_qualifies(db, provider_id):
    a = _movie(db, provider_id, "Terrifier", 2018, "one", 420634)
    b = _movie(db, provider_id, "Terrifier", 2017, "two", 420634)
    assert "Qualifies" in _reasons(db, a, b)


def test_reason_auto_merge_off(db, provider_id):
    config.save_duplicate_finder_auto_merge_tmdb(False)
    a = _movie(db, provider_id, "Terrifier", 2018, "one", 420634)
    b = _movie(db, provider_id, "Terrifier", 2017, "two", 420634)
    assert "turned off" in _reasons(db, a, b)


def test_year_tolerance_setting_defaults_to_two_and_clamps():
    assert config.get_duplicate_finder_auto_merge_year_tolerance() == 2
    config.save_duplicate_finder_auto_merge_year_tolerance(9)
    assert config.get_duplicate_finder_auto_merge_year_tolerance() == 3
    config.save_duplicate_finder_auto_merge_year_tolerance(-1)
    assert config.get_duplicate_finder_auto_merge_year_tolerance() == 0


def test_tolerance_zero_requires_exact_year(db, provider_id):
    config.save_duplicate_finder_auto_merge_year_tolerance(0)
    first = _movie(db, provider_id, "Terrifier", 2018, "one", 420634)
    second = _movie(db, provider_id, "Terrifier", 2017, "two", 420634)

    db.auto_merge_movie_by_tmdb(first["id"])

    assert db.get_movie(second["id"]) is not None
    assert "2018 vs 2017" in _reasons(db, first, second)


def test_tolerance_three_merges_three_year_gap(db, provider_id):
    config.save_duplicate_finder_auto_merge_year_tolerance(3)
    first = _movie(db, provider_id, "Burn Your Maps", 2016, "one", 352504)
    second = _movie(db, provider_id, "Burn Your Maps", 2019, "two", 352504)

    db.auto_merge_movie_by_tmdb(first["id"])

    assert db.get_movie(second["id"]) is None


def test_reason_names_configured_tolerance(db, provider_id):
    config.save_duplicate_finder_auto_merge_year_tolerance(1)
    a = _movie(db, provider_id, "Burn Your Maps", 2016, "one", 352504)
    b = _movie(db, provider_id, "Burn Your Maps", 2018, "two", 352504)
    assert "more than 1 year apart" in _reasons(db, a, b)


def test_merge_existing_route_runs_full_catalog_merge(db, provider_id):
    import asyncio
    import vod_routes

    a = _movie(db, provider_id, "Terrifier", 2018, "one", 420634)
    b = _movie(db, provider_id, "Terrifier", 2017, "two", 420634)

    result = asyncio.run(vod_routes.merge_existing_tmdb_duplicates())

    assert result == {"movies_merged": 1, "series_merged": 0}
    assert (db.get_movie(a["id"]) is None) != (db.get_movie(b["id"]) is None)

"""beads-974 (Step 2): find_duplicate_groups must not surface same-tmdb_id
pairs as duplicate-merge candidates when they share NO source language --
mirrors the auto_merge_movie_by_tmdb / auto_merge_series_by_tmdb gate added
in Step 1 (see test_language_aware_auto_merge.py), so a human reviewing the
Duplicate Finder never sees "merge" offered for what's actually two distinct
language/dub variants of the same title.

Movies/series have no language column of their own -- language lives on
their _sources rows -- so the split compares each candidate's source
languages (COALESCE'd to EN, matching _source_language's own untagged
default), same as the Step 1 gate.

Distinct `name` values are used per language variant below (mirroring real
provider feeds -- see test_language_aware_auto_merge.py's module docstring
for why bulk_import_* would otherwise collapse two variants into one row)."""

import config


def _import_movie(db, provider_id, name, year, stream_id, raw_name, tmdb_id):
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


def _import_series(db, provider_id, name, year, series_stream_id, raw_name, tmdb_id):
    db.bulk_import_series(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_series_id": series_stream_id,
            "raw_name": raw_name,
            "tmdb_id": tmdb_id,
            "_has_detail": True,
        },
    ])


def test_duplicate_finder_splits_movies_with_different_languages(db):
    config.save_duplicate_finder_quality_prefix_matching(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    _import_movie(db, provider_id, "FR - Amelie", 2001, "fr-1", "FR - Amelie", tmdb_id=194)

    groups = db.find_duplicate_groups("movie")

    # Same tmdb_id, but zero shared source language -- must NOT be offered
    # as a single merge candidate.
    for group in groups:
        names = {i["name"] for i in group["items"]}
        assert not {"Amelie", "FR - Amelie"} <= names


def test_duplicate_finder_still_groups_movies_with_same_language(db):
    config.save_duplicate_finder_quality_prefix_matching(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    _import_movie(db, provider_id, "Amelie (Provider2)", 2001, "en-2", "EN - Amelie", tmdb_id=194)

    groups = db.find_duplicate_groups("movie")

    matching = [g for g in groups if {i["name"] for i in g["items"]} == {"Amelie", "Amelie (Provider2)"}]
    assert len(matching) == 1


def test_duplicate_finder_still_groups_movies_when_both_untagged(db):
    config.save_duplicate_finder_quality_prefix_matching(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    _import_movie(db, provider_id, "Amelie", 2001, "p1-1", "Amelie", tmdb_id=194)
    _import_movie(db, provider_id, "Amelie (Provider2)", 2001, "p1-2", "Amelie", tmdb_id=194)

    groups = db.find_duplicate_groups("movie")

    matching = [g for g in groups if {i["name"] for i in g["items"]} == {"Amelie", "Amelie (Provider2)"}]
    assert len(matching) == 1


def test_duplicate_finder_splits_series_with_different_languages(db):
    config.save_duplicate_finder_quality_prefix_matching(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    _import_series(db, provider_id, "Dark", 2017, "en-s1", "EN - Dark", tmdb_id=77)
    _import_series(db, provider_id, "DE - Dark", 2017, "de-s1", "DE - Dark", tmdb_id=77)

    groups = db.find_duplicate_groups("series")

    for group in groups:
        names = {i["name"] for i in group["items"]}
        assert not {"Dark", "DE - Dark"} <= names


def test_duplicate_finder_still_groups_series_with_same_language(db):
    config.save_duplicate_finder_quality_prefix_matching(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    _import_series(db, provider_id, "Dark", 2017, "en-s1", "EN - Dark", tmdb_id=77)
    _import_series(db, provider_id, "Dark (Provider2)", 2017, "en-s2", "EN - Dark", tmdb_id=77)

    groups = db.find_duplicate_groups("series")

    matching = [g for g in groups if {i["name"] for i in g["items"]} == {"Dark", "Dark (Provider2)"}]
    assert len(matching) == 1


def test_duplicate_finder_movies_and_series_together(db):
    """Both content types exercised in the same test run/db (user's Step 2
    instruction: 'run same tests again include movies and shows together to
    make sure both are good')."""
    config.save_duplicate_finder_quality_prefix_matching(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    _import_movie(db, provider_id, "FR - Amelie", 2001, "fr-1", "FR - Amelie", tmdb_id=194)
    _import_series(db, provider_id, "Dark", 2017, "en-s1", "EN - Dark", tmdb_id=77)
    _import_series(db, provider_id, "DE - Dark", 2017, "de-s1", "DE - Dark", tmdb_id=77)

    movie_groups = db.find_duplicate_groups("movie")
    series_groups = db.find_duplicate_groups("series")

    for group in movie_groups:
        names = {i["name"] for i in group["items"]}
        assert not {"Amelie", "FR - Amelie"} <= names
    for group in series_groups:
        names = {i["name"] for i in group["items"]}
        assert not {"Dark", "DE - Dark"} <= names

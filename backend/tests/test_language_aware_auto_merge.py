"""beads-974: auto-merge must gate on (tmdb_id, language), not tmdb_id alone,
so provider language/dub variants of the same title no longer collapse into
one card that can silently fail over to an unwanted-language stream. Movies
and series have no language column of their own -- language lives on their
_sources rows -- so the gate compares each candidate's source languages
(COALESCE'd to EN, matching _source_language's own untagged default).

Distinct movie/series `name` values are used per variant below (mirroring
real provider feeds, where a language variant typically carries a distinct
prefixed name like "FR - Amelie") -- bulk_import_movies/bulk_import_series
match existing rows by (name, year) first, so two variants sharing the exact
same name+year would collapse into ONE row/source pre-merge, never reaching
auto_merge_* as two separate rows at all."""

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
    return db.get_movie_by_name_year(name, year)


def test_auto_merge_movie_skips_same_tmdb_id_different_language(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    en_movie = _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    fr_movie = _import_movie(db, provider_id, "FR - Amelie", 2001, "fr-1", "FR - Amelie", tmdb_id=194)

    db.auto_merge_movie_by_tmdb(en_movie["id"])

    # Different languages under the same tmdb_id must stay as separate cards.
    assert db.get_movie(en_movie["id"]) is not None
    assert db.get_movie(fr_movie["id"]) is not None


def test_auto_merge_movie_merges_same_tmdb_id_same_language(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    movie_a = _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    movie_b = _import_movie(db, provider_id, "Amelie (Provider2)", 2001, "en-2", "EN - Amelie", tmdb_id=194)

    db.auto_merge_movie_by_tmdb(movie_a["id"])

    survivors = [mid for mid in (movie_a["id"], movie_b["id"]) if db.get_movie(mid)]
    assert len(survivors) == 1


def test_auto_merge_movie_merges_same_tmdb_id_when_both_untagged(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    # No recognizable language prefix on either raw_name -- both default to
    # EN per _source_language, so this is the backward-compatible case.
    movie_a = _import_movie(db, provider_id, "Amelie", 2001, "p1-1", "Amelie", tmdb_id=194)
    movie_b = _import_movie(db, provider_id, "Amelie (Provider2)", 2001, "p1-2", "Amelie", tmdb_id=194)

    db.auto_merge_movie_by_tmdb(movie_a["id"])

    survivors = [mid for mid in (movie_a["id"], movie_b["id"]) if db.get_movie(mid)]
    assert len(survivors) == 1


def test_auto_merge_movie_merges_when_one_side_untagged(db):
    """User's rule: untagged sources default to EN, so an untagged variant
    still merges with an explicitly-EN-tagged variant of the same tmdb_id."""
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    movie_a = _import_movie(db, provider_id, "Amelie", 2001, "en-1", "EN - Amelie", tmdb_id=194)
    movie_b = _import_movie(db, provider_id, "Amelie (Provider2)", 2001, "p2-1", "Amelie", tmdb_id=194)

    db.auto_merge_movie_by_tmdb(movie_a["id"])

    survivors = [mid for mid in (movie_a["id"], movie_b["id"]) if db.get_movie(mid)]
    assert len(survivors) == 1


def test_auto_merge_series_skips_same_tmdb_id_different_language(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_series(provider_id, [
        {"name": "Dark", "year": 2017, "provider_series_id": "en-s1", "raw_name": "EN - Dark", "tmdb_id": 77, "_has_detail": True}
    ])
    db.bulk_import_series(provider_id, [
        {"name": "DE - Dark", "year": 2017, "provider_series_id": "de-s1", "raw_name": "DE - Dark", "tmdb_id": 77, "_has_detail": True}
    ])
    rows = [s for s in db.list_series(limit=1000) if "Dark" in s["name"]]
    assert len(rows) == 2

    db.auto_merge_series_by_tmdb(rows[0]["id"])

    remaining = [s for s in db.list_series(limit=1000) if "Dark" in s["name"]]
    assert len(remaining) == 2


def test_auto_merge_series_merges_same_tmdb_id_same_language(db):
    config.save_duplicate_finder_auto_merge_tmdb(True)
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")

    db.bulk_import_series(provider_id, [
        {"name": "Dark", "year": 2017, "provider_series_id": "en-s1", "raw_name": "EN - Dark", "tmdb_id": 77, "_has_detail": True}
    ])
    db.bulk_import_series(provider_id, [
        {"name": "Dark (Provider2)", "year": 2017, "provider_series_id": "en-s2", "raw_name": "EN - Dark", "tmdb_id": 77, "_has_detail": True}
    ])
    rows = [s for s in db.list_series(limit=1000) if "Dark" in s["name"]]
    assert len(rows) == 2

    db.auto_merge_series_by_tmdb(rows[0]["id"])

    remaining = [s for s in db.list_series(limit=1000) if "Dark" in s["name"]]
    assert len(remaining) == 1

"""Follow-up to beads-974: one-time cleanup companion to the skip-at-import
change (see test_import_exclusion_skip.py) -- rows that were imported and
auto-archived BEFORE that change existed (review_excluded=1,
review_excluded_manual=0) shouldn't just sit there forever; per user
direction ("so they should not be there anyway, right?"), purge_excluded_
archived_content deletes them outright, matching the new "never stored"
model. A human's manual archive (review_excluded_manual=1) is never touched
-- same protection bulk_import_movies/series already give that flag."""

import config
import vod_db


def _lang():
    """2026-09-11 follow-up: _row_excluded_by_rule's language-prefix gate now
    reads lang["enabled_languages"] (config.get_enabled_languages(), an
    include-list) instead of lang["exclude_prefixes"] (an explicit
    exclude-list) -- see test_import_exclusion_skip.py for the full
    rationale. exclude_non_latin still comes from
    config.get_import_language_exclusion(), which is unchanged and still
    backs the "Import Language Exclusion" UI card's non-Latin toggle."""
    return {
        "enabled_languages": config.get_enabled_languages(),
        "exclude_non_latin": config.get_import_language_exclusion()["exclude_non_latin"],
    }


def _import_movie(db, provider_id, name, stream_id, category_name=None, year=2001):
    db.bulk_import_movies(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_stream_id": stream_id,
            "container_extension": "mp4",
            "provider_category_name": category_name,
            "raw_name": name,
            "auto_archive": False,
            "_has_detail": True,
        },
    ])


def _import_series(db, provider_id, name, series_id, category_name=None, year=2001):
    db.bulk_import_series(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_series_id": series_id,
            "provider_category_name": category_name,
            "raw_name": name,
            "auto_archive": False,
            "_has_detail": True,
        },
    ])


def test_purges_auto_archived_movie_matching_current_category_exclusion(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Foreign Flick", "s-1", category_name="Foreign Films")
    movie = db.get_movie_by_name_year("Foreign Flick", 2001)
    db.bulk_set_review_excluded("movie", [movie["id"]], True)
    # Simulate: this row was auto-archived (not a human archive) -- flip the
    # manual flag back off the way it would look pre-this-fix.
    conn = db._connect()
    conn.execute("UPDATE movies SET review_excluded_manual=0 WHERE id=?", (movie["id"],))
    conn.commit()
    conn.close()

    result = vod_db.purge_excluded_archived_content(
        provider_exclusions={provider_id: (["Foreign Films"], False)},
        lang=_lang(),
    )

    assert result["movies_deleted"] == 1
    assert db.get_movie_by_name_year("Foreign Flick", 2001) is None


def test_does_not_purge_manually_archived_movie(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Foreign Flick", "s-1", category_name="Foreign Films")
    movie = db.get_movie_by_name_year("Foreign Flick", 2001)
    db.bulk_set_review_excluded("movie", [movie["id"]], True)  # stamps review_excluded_manual=1

    result = vod_db.purge_excluded_archived_content(
        provider_exclusions={provider_id: (["Foreign Films"], False)},
        lang=_lang(),
    )

    assert result["movies_deleted"] == 0
    assert db.get_movie_by_name_year("Foreign Flick", 2001) is not None


def test_does_not_purge_non_excluded_archived_movie(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Some Flick", "s-1", category_name="Action Movies")
    movie = db.get_movie_by_name_year("Some Flick", 2001)
    db.bulk_set_review_excluded("movie", [movie["id"]], True)
    conn = db._connect()
    conn.execute("UPDATE movies SET review_excluded_manual=0 WHERE id=?", (movie["id"],))
    conn.commit()
    conn.close()

    result = vod_db.purge_excluded_archived_content(
        provider_exclusions={provider_id: (["Foreign Films"], False)},
        lang=_lang(),
    )

    assert result["movies_deleted"] == 0
    assert db.get_movie_by_name_year("Some Flick", 2001) is not None


def test_purges_auto_archived_series_matching_current_category_exclusion(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_series(db, provider_id, "Foreign Show", "sr-1", category_name="Foreign TV")
    series = db.get_series_by_name_year("Foreign Show", 2001)
    db.bulk_set_review_excluded("series", [series["id"]], True)
    conn = db._connect()
    conn.execute("UPDATE series SET review_excluded_manual=0 WHERE id=?", (series["id"],))
    conn.commit()
    conn.close()

    result = vod_db.purge_excluded_archived_content(
        provider_exclusions={provider_id: (["Foreign TV"], False)},
        lang=_lang(),
    )

    assert result["series_deleted"] == 1
    assert db.get_series_by_name_year("Foreign Show", 2001) is None


def test_is_idempotent(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Foreign Flick", "s-1", category_name="Foreign Films")
    movie = db.get_movie_by_name_year("Foreign Flick", 2001)
    db.bulk_set_review_excluded("movie", [movie["id"]], True)
    conn = db._connect()
    conn.execute("UPDATE movies SET review_excluded_manual=0 WHERE id=?", (movie["id"],))
    conn.commit()
    conn.close()

    exclusions = {provider_id: (["Foreign Films"], False)}
    first = vod_db.purge_excluded_archived_content(provider_exclusions=exclusions, lang=_lang())
    second = vod_db.purge_excluded_archived_content(provider_exclusions=exclusions, lang=_lang())

    assert first["movies_deleted"] == 1
    assert second["movies_deleted"] == 0


def test_purges_auto_archived_movie_whose_language_is_not_enabled(db):
    """2026-09-11 follow-up: _row_excluded_by_rule's language gate now reads
    lang["enabled_languages"] (an include-list) rather than
    lang["exclude_prefixes"] (an explicit exclude-list) -- a GR-tagged row
    is purged simply because GR isn't in the enabled set, with no
    exclude_prefixes entry involved at all."""
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "GR - Some Movie", "s-1")
    movie = db.get_movie_by_name_year("GR - Some Movie", 2001)
    db.bulk_set_review_excluded("movie", [movie["id"]], True)
    conn = db._connect()
    conn.execute("UPDATE movies SET review_excluded_manual=0 WHERE id=?", (movie["id"],))
    conn.commit()
    conn.close()

    result = vod_db.purge_excluded_archived_content(
        provider_exclusions={provider_id: ([], False)},
        lang={"enabled_languages": ["EN"], "exclude_non_latin": False},
    )

    assert result["movies_deleted"] == 1
    assert db.get_movie_by_name_year("GR - Some Movie", 2001) is None


def test_does_not_purge_movie_whose_language_is_enabled(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "GR - Some Movie", "s-1")
    movie = db.get_movie_by_name_year("GR - Some Movie", 2001)
    db.bulk_set_review_excluded("movie", [movie["id"]], True)
    conn = db._connect()
    conn.execute("UPDATE movies SET review_excluded_manual=0 WHERE id=?", (movie["id"],))
    conn.commit()
    conn.close()

    result = vod_db.purge_excluded_archived_content(
        provider_exclusions={provider_id: ([], False)},
        lang={"enabled_languages": ["EN", "GR"], "exclude_non_latin": False},
    )

    assert result["movies_deleted"] == 0
    assert db.get_movie_by_name_year("GR - Some Movie", 2001) is not None

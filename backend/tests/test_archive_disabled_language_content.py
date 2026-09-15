"""Catch-up cleanup for the 2026-09-13 auto-merge language gate fix (see
test_language_aware_auto_merge.py): with the merge bug fixed, movies/series
whose only real source language isn't in config.get_enabled_languages() will
correctly stay split -- but on deployments that were already running before
the fix, those rows accumulated with review_excluded=0 (never archived, just
silently filtered out of playback by _enabled_languages_clause) instead of
being flagged for review. archive_disabled_language_content is a one-time
(repeatable) catch-up, parallel to purge_excluded_archived_content, that
flips those rows to review_excluded=1 instead of deleting them -- the user's
explicit direction was "archive them", not purge, since unlike the
category-exclusion purge case these were legitimately imported content, just
not currently wanted for playback. A human's manual archive/unarchive
(review_excluded_manual=1) is never touched, same protection every other
auto-archive path in this file already gives that flag. Reversible: if the
row's language is later re-enabled, it un-archives the same way."""

import config
import vod_db


def _import_movie(db, provider_id, name, stream_id, raw_name=None, year=2001):
    db.bulk_import_movies(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_stream_id": stream_id,
            "container_extension": "mp4",
            "raw_name": raw_name if raw_name is not None else name,
            "_has_detail": True,
        },
    ])
    return db.get_movie_by_name_year(name, year)


def _import_series(db, provider_id, name, series_id, raw_name=None, year=2001):
    db.bulk_import_series(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_series_id": series_id,
            "raw_name": raw_name if raw_name is not None else name,
            "_has_detail": True,
        },
    ])
    return db.get_series_by_name_year(name, year)


def test_archives_movie_whose_only_language_is_not_enabled(db):
    config.save_enabled_languages(["EN"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = _import_movie(db, provider_id, "ES - Amelie", "es-1", raw_name="ES - Amelie")
    assert movie["review_excluded"] == 0

    result = vod_db.archive_disabled_language_content()

    assert result["movies_archived"] == 1
    updated = db.get_movie(movie["id"])
    assert updated["review_excluded"] == 1
    assert updated["review_excluded_manual"] == 0


def test_does_not_archive_movie_with_enabled_language(db):
    config.save_enabled_languages(["EN", "ES"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = _import_movie(db, provider_id, "ES - Amelie", "es-1", raw_name="ES - Amelie")

    result = vod_db.archive_disabled_language_content()

    assert result["movies_archived"] == 0
    assert db.get_movie(movie["id"])["review_excluded"] == 0


def test_does_not_archive_movie_when_any_source_language_is_enabled(db):
    """A row with sources in more than one language (e.g. merged EN+ES
    variants pre-beads-974, or any other path that can leave a row with
    mixed-language sources) must not be archived just because ES isn't
    enabled -- same "shares at least one language" spirit as the merge gate:
    archiving here is about a row having NO eligible language left, not
    about every source matching. bulk_import_movies itself won't produce
    this shape (_movie_language_ok keeps a same-name/year mismatched-language
    item from attaching to an existing row as a second source), so this is
    simulated directly at the sources-table level."""
    config.save_enabled_languages(["EN"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = _import_movie(db, provider_id, "Amelie", "en-1", raw_name="EN - Amelie")
    conn = db._connect()
    conn.execute(
        "INSERT INTO movie_sources "
        "(movie_id, provider_id, provider_stream_id, container_extension, language, added_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        (movie["id"], provider_id, "es-1", "mp4", "ES"),
    )
    conn.commit()
    conn.close()

    result = vod_db.archive_disabled_language_content()

    assert result["movies_archived"] == 0
    assert db.get_movie(movie["id"])["review_excluded"] == 0


def test_does_not_touch_manually_archived_movie(db):
    config.save_enabled_languages(["EN"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = _import_movie(db, provider_id, "ES - Amelie", "es-1", raw_name="ES - Amelie")
    db.bulk_set_review_excluded("movie", [movie["id"]], True)  # stamps review_excluded_manual=1

    result = vod_db.archive_disabled_language_content()

    assert result["movies_archived"] == 0
    updated = db.get_movie(movie["id"])
    assert updated["review_excluded"] == 1
    assert updated["review_excluded_manual"] == 1


def test_is_idempotent_for_movies(db):
    config.save_enabled_languages(["EN"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "ES - Amelie", "es-1", raw_name="ES - Amelie")

    first = vod_db.archive_disabled_language_content()
    second = vod_db.archive_disabled_language_content()

    assert first["movies_archived"] == 1
    assert second["movies_archived"] == 0


def test_archives_series_whose_only_language_is_not_enabled(db):
    config.save_enabled_languages(["EN"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    series = _import_series(db, provider_id, "ES - Dark", "es-s1", raw_name="ES - Dark")
    assert series["review_excluded"] == 0

    result = vod_db.archive_disabled_language_content()

    assert result["series_archived"] == 1
    updated = db.get_series(series["id"])
    assert updated["review_excluded"] == 1


def test_does_not_touch_manually_archived_series(db):
    config.save_enabled_languages(["EN"])
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    series = _import_series(db, provider_id, "ES - Dark", "es-s1", raw_name="ES - Dark")
    db.bulk_set_review_excluded("series", [series["id"]], True)

    result = vod_db.archive_disabled_language_content()

    assert result["series_archived"] == 0
    updated = db.get_series(series["id"])
    assert updated["review_excluded"] == 1
    assert updated["review_excluded_manual"] == 1

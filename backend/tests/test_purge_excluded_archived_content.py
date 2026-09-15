"""purge_excluded_archived_content: companion cleanup to _should_auto_archive
(ported from knmplace's fork). Deletes movies/series that are currently
auto-archived (review_excluded=1, review_excluded_manual=0) AND still match
a CURRENTLY-active provider category/language exclusion rule -- content the
admin genuinely doesn't want stored at all, as opposed to
archive_disabled_language_content's "keep it, just don't play it" case."""

import config
import vod_db


def _import_movie(db, provider_id, name, stream_id, category_name=None, raw_name=None, year=2001):
    db.bulk_import_movies(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_stream_id": stream_id,
            "container_extension": "mp4",
            "provider_category_name": category_name,
            "raw_name": raw_name if raw_name is not None else name,
            "_has_detail": True,
        },
    ])
    return db.get_movie_by_name_year(name, year)


def test_deletes_movie_matching_active_category_exclusion(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = _import_movie(db, provider_id, "Some Kids Show", "s-1", category_name="Kids")
    db.bulk_set_review_excluded("movie", [movie["id"]], True)
    # bulk_set_review_excluded stamps review_excluded_manual=1 -- reset it
    # to 0 to simulate an auto-archived row (the case this function targets).
    conn = vod_db._connect()
    conn.execute("UPDATE movies SET review_excluded_manual=0 WHERE id=?", (movie["id"],))
    conn.commit()
    conn.close()

    provider_exclusions = {provider_id: (["Kids"], False)}
    lang = {"enabled_languages": config.get_enabled_languages(), "exclude_non_latin": False}
    result = vod_db.purge_excluded_archived_content(provider_exclusions, lang)

    assert result["movies_deleted"] == 1
    assert vod_db.get_movie(movie["id"]) is None


def test_leaves_manually_archived_movie_alone(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = _import_movie(db, provider_id, "Some Kids Show", "s-1", category_name="Kids")
    db.bulk_set_review_excluded("movie", [movie["id"]], True)  # review_excluded_manual=1

    provider_exclusions = {provider_id: (["Kids"], False)}
    lang = {"enabled_languages": config.get_enabled_languages(), "exclude_non_latin": False}
    result = vod_db.purge_excluded_archived_content(provider_exclusions, lang)

    assert result["movies_deleted"] == 0
    assert vod_db.get_movie(movie["id"]) is not None


def test_leaves_archived_movie_alone_when_no_active_rule_matches(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie = _import_movie(db, provider_id, "Some Kids Show", "s-1", category_name="Kids")
    db.bulk_set_review_excluded("movie", [movie["id"]], True)
    conn = vod_db._connect()
    conn.execute("UPDATE movies SET review_excluded_manual=0 WHERE id=?", (movie["id"],))
    conn.commit()
    conn.close()

    # No exclusion rule configured for this provider at all.
    result = vod_db.purge_excluded_archived_content({}, {"enabled_languages": ["EN", "ES"], "exclude_non_latin": False})

    assert result["movies_deleted"] == 0
    assert vod_db.get_movie(movie["id"]) is not None

"""Regression coverage for removal of entries excluded by active import policy."""

import config
import vod_db


def _archived_movie(db, provider_id):
    db.bulk_import_movies(provider_id, [{
        "name": "Foreign Flick", "year": 2001, "provider_stream_id": "stream-1",
        "container_extension": "mp4", "provider_category_name": "Foreign Films",
        "raw_name": "Foreign Flick", "auto_archive": False, "_has_detail": True,
    }])
    movie = db.get_movie_by_name_year("Foreign Flick", 2001)
    db.bulk_set_review_excluded("movie", [movie["id"]], True)
    conn = vod_db._connect()
    conn.execute("UPDATE movies SET review_excluded_manual=0 WHERE id=?", (movie["id"],))
    conn.commit()
    conn.close()
    return movie


def test_purges_auto_archived_entry_matching_provider_rule(db):
    provider_id = db.upsert_provider("provider", "http://example.com", "user", "pass")
    movie = _archived_movie(db, provider_id)

    result = vod_db.purge_excluded_archived_content(
        {provider_id: (["Foreign Films"], False)}, {"enabled_languages": config.get_enabled_languages(), "exclude_non_latin": False},
    )

    assert result["movies_deleted"] == 1
    assert db.get_movie(movie["id"]) is None


def test_leaves_manual_archive_untouched(db):
    provider_id = db.upsert_provider("provider", "http://example.com", "user", "pass")
    db.bulk_import_movies(provider_id, [{
        "name": "Foreign Flick", "year": 2001, "provider_stream_id": "stream-1",
        "container_extension": "mp4", "provider_category_name": "Foreign Films",
        "raw_name": "Foreign Flick", "auto_archive": False, "_has_detail": True,
    }])
    movie = db.get_movie_by_name_year("Foreign Flick", 2001)
    db.bulk_set_review_excluded("movie", [movie["id"]], True)

    result = vod_db.purge_excluded_archived_content(
        {provider_id: (["Foreign Films"], False)}, {"enabled_languages": config.get_enabled_languages(), "exclude_non_latin": False},
    )

    assert result["movies_deleted"] == 0
    assert db.get_movie(movie["id"]) is not None

"""Catalog "changed" counts track playable identity, not provider artwork.

Posters and descriptive metadata come from TMDB after import, so a provider
rotating image URLs (or rewording a plot) is not a catalog change. Stream id,
container, TMDB id, name, category and a series' last_modified still are.
Sources stored under the old fingerprint upgrade silently instead of all
showing as changed on the first refresh after the switch."""

import vod_importer

LANG = {"enabled_languages": ["EN"], "exclude_non_latin": False}
DETAIL_RULES = {field: [] for field in ("genre", "description", "cast_list", "director")}


def _movie(**overrides):
    row = {"stream_id": "1", "name": "Known (2020)", "category_id": "1", "container_extension": "mkv",
           "tmdb": "123", "stream_icon": "http://img/a.jpg", "cover": None, "cover_big": None, "movie_image": None}
    row.update(overrides)
    return row


def _series(**overrides):
    row = {"series_id": "7", "name": "Show (2020)", "category_id": "1", "year": "2020", "tmdb": "55",
           "cover": "http://img/s.jpg", "plot": "A plot.", "genre": "Drama", "cast": "A, B", "director": "C",
           "rating": "7.1", "releaseDate": "2020-01-01", "last_modified": "1700000000"}
    row.update(overrides)
    return row


def _movie_items(rows):
    return vod_importer._build_movie_import_items(rows, {"1": "Movies", "2": "Other"}, [], False, LANG, [])[0]


def _series_items(rows):
    return vod_importer._build_series_import_items(rows, {"1": "Series", "2": "Other"}, [], False, LANG, [], DETAIL_RULES)[0]


def _fp(items):
    return items[0]["catalog_fingerprint"]


def test_movie_artwork_changes_do_not_change_fingerprint():
    base = _fp(_movie_items([_movie()]))
    for change in ({"stream_icon": "http://img/b.jpg"}, {"cover": "x"}, {"cover_big": "y"}, {"movie_image": "z"}):
        assert _fp(_movie_items([_movie(**change)])) == base, change


def test_movie_playable_changes_still_change_fingerprint():
    base = _fp(_movie_items([_movie()]))
    for change in ({"container_extension": "mp4"}, {"tmdb": "999"}, {"tmdb": None, "tmdb_id": "999"},
                   {"name": "Renamed (2020)"}, {"category_id": "2"}):
        assert _fp(_movie_items([_movie(**change)])) != base, change


def test_series_artwork_and_metadata_changes_do_not_change_fingerprint():
    base = _fp(_series_items([_series()]))
    for change in ({"cover": "http://img/t.jpg"}, {"plot": "Reworded."}, {"genre": "Crime"}, {"cast": "D"},
                   {"director": "E"}, {"rating": "8.0"}, {"releaseDate": "2020-02-02"}):
        assert _fp(_series_items([_series(**change)])) == base, change


def test_series_identity_and_new_episode_signal_still_change_fingerprint():
    base = _fp(_series_items([_series()]))
    for change in ({"last_modified": "1800000000"}, {"tmdb": "66"}, {"name": "Other Show (2020)"}, {"category_id": "2"}):
        assert _fp(_series_items([_series(**change)])) != base, change


def _stored(db, table, key_col, key):
    conn = db._connect()
    value = conn.execute(f"SELECT catalog_fingerprint FROM {table} WHERE {key_col}=?", (key,)).fetchone()[0]
    conn.close()
    return value


def _as_legacy(items):
    return [{**item, "catalog_fingerprint": item["legacy_catalog_fingerprint"]} for item in items]


def test_legacy_movie_fingerprints_upgrade_without_counting_as_changed(db):
    provider_id = db.upsert_provider("XC-Test", "http://xc.example.com", "user", "pass", provider_type="xc")
    items = _movie_items([_movie()])
    db.bulk_import_movies(provider_id, _as_legacy(items))

    result = db.bulk_import_movies(provider_id, _movie_items([_movie()]))

    assert result["sources_changed"] == 0
    assert _stored(db, "movie_sources", "provider_stream_id", "1") == _fp(items)
    assert db.bulk_import_movies(provider_id, _movie_items([_movie()]))["sources_changed"] == 0


def test_legacy_movie_with_a_real_change_is_still_counted(db):
    provider_id = db.upsert_provider("XC-Test", "http://xc.example.com", "user", "pass", provider_type="xc")
    db.bulk_import_movies(provider_id, _as_legacy(_movie_items([_movie()])))

    result = db.bulk_import_movies(provider_id, _movie_items([_movie(container_extension="mp4")]))

    assert result["sources_changed"] == 1


def test_legacy_series_fingerprints_upgrade_without_counting_as_changed(db):
    provider_id = db.upsert_provider("XC-Test", "http://xc.example.com", "user", "pass", provider_type="xc")
    items = _series_items([_series()])
    db.bulk_import_series(provider_id, _as_legacy(items))

    result = db.bulk_import_series(provider_id, _series_items([_series()]))

    assert result["sources_changed"] == 0
    assert _stored(db, "series_sources", "provider_series_id", "7") == _fp(items)

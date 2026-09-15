"""Enabled Playback Languages, part 2: the config/detection primitives from
test_enabled_playback_languages.py are only useful once they actually gate
what plays/exports. This covers the wiring added on top of that:

- bulk_import_movies/add_episode_source now stamp movie_sources.language/
  episode_sources.language at write time (derived from raw_name via
  _source_language), instead of leaving new rows NULL until a manual
  backfill runs.
- list_movie/episode_sources_for_streaming, get_movie/episode_export_rows*,
  and the *_best_source_cte helpers all now filter through
  _enabled_languages_clause, so a source in a disabled language is invisible
  to playback/export/failover -- not deleted, just filtered out of these
  read paths.

Default config is EN+ES (see config.get_enabled_languages), so an install
that has never touched the setting sees no behavior change here -- every
test below is explicit about which languages it enables to keep that
default-safe guarantee visible.
"""


def _import_movie(db, provider_id, name, stream_id, raw_name, year=2001):
    db.bulk_import_movies(provider_id, [
        {
            "name": name,
            "year": year,
            "provider_stream_id": stream_id,
            "container_extension": "mp4",
            "raw_name": raw_name,
            "_has_detail": True,
        },
    ])


def test_import_stamps_language_on_movie_source(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Amelie", "gr-1", "GR - Amelie")
    movie = db.get_movie_by_name_year("Amelie", 2001)
    sources = db.list_movie_sources(movie["id"])
    assert {s["language"] for s in sources} == {"GR"}


def test_import_defaults_untagged_movie_source_to_en(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Amelie", "en-1", "Amelie")
    movie = db.get_movie_by_name_year("Amelie", 2001)
    sources = db.list_movie_sources(movie["id"])
    assert {s["language"] for s in sources} == {"EN"}


def test_add_episode_source_stamps_language(db):
    series_id = db.upsert_series("Test Show", 2010)
    episode_id = db.add_episode(series_id, 1, 1, "Pilot")
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    db.add_episode_source(episode_id, provider_id, "fr-1", raw_name="FR - Pilot")
    sources = db.list_episode_sources_for_episode_ids([episode_id])[episode_id]
    assert {s["language"] for s in sources} == {"FR"}


def test_default_enabled_languages_include_untagged_and_es_sources(db, monkeypatch):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Amelie", "en-1", "Amelie")
    _import_movie(db, provider_id, "Volver", "es-1", "ES - Volver", year=2006)
    movie_en = db.get_movie_by_name_year("Amelie", 2001)
    movie_es = db.get_movie_by_name_year("Volver", 2006)

    assert [s["source_id"] for s in db.list_movie_sources_for_streaming(movie_en["id"])]
    assert [s["source_id"] for s in db.list_movie_sources_for_streaming(movie_es["id"])]


def test_disabling_a_language_hides_its_sources_from_streaming_and_export(db):
    import config

    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    _import_movie(db, provider_id, "Foreign Film", "gr-1", "GR - Foreign Film")
    movie = db.get_movie_by_name_year("Foreign Film", 2001)
    cat_id = db.upsert_category("Movies", "movie")
    db.place_movie_in_category(movie["id"], cat_id)

    # Default EN+ES: a GR-only source has no enabled-language source at all.
    assert db.list_movie_sources_for_streaming(movie["id"]) == []
    export_rows = [r for r in db.get_movie_export_rows() if r["movie_id"] == movie["id"]]
    assert export_rows == []

    # Enabling GR makes it visible again.
    config.save_enabled_languages(["EN", "ES", "GR"])
    assert len(db.list_movie_sources_for_streaming(movie["id"])) == 1
    export_rows = [r for r in db.get_movie_export_rows() if r["movie_id"] == movie["id"]]
    assert len(export_rows) == 1
    assert export_rows[0]["provider_stream_id"] == "gr-1"


def test_disabling_a_language_hides_episode_sources_too(db):
    import config

    series_id = db.upsert_series("Test Show", 2010)
    episode_id = db.add_episode(series_id, 1, 1, "Pilot")
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    db.add_episode_source(episode_id, provider_id, "fr-1", raw_name="FR - Pilot")

    assert db.list_episode_sources_for_streaming(episode_id) == []
    row = db.get_episode_export_row(episode_id)
    assert row["provider_id"] is None

    config.save_enabled_languages(["EN", "ES", "FR"])
    assert len(db.list_episode_sources_for_streaming(episode_id)) == 1
    row = db.get_episode_export_row(episode_id)
    assert row["provider_id"] == provider_id

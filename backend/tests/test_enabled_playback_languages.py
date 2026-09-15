"""Enabled Playback Languages foundation: config get/save, per-source
language detection (_source_language / _name_prefix_code), and the
backfill/recompute tools that populate movie_sources.language /
episode_sources.language for rows written before this column existed.

This is the first port pass of knmplace's Enabled Playback Languages
feature -- it does NOT yet cover series_sources (that table doesn't exist
in this codebase yet), the movie/series language-split UI, or wiring
_enabled_languages_clause into the actual playback/export/failover query
paths. Those are later, separate passes.
"""

import config


def test_get_enabled_languages_defaults_to_en_es(db):
    assert config.get_enabled_languages() == ["EN", "ES"]


def test_save_and_get_enabled_languages_roundtrips():
    config.save_enabled_languages(["en", " fr ", "DE"])
    assert config.get_enabled_languages() == ["EN", "FR", "DE"]


def test_source_language_detects_pipe_colon_dash_prefixes(db):
    assert db._source_language("AR| Apex") == "AR"
    assert db._source_language("FR: Movie Title") == "FR"
    assert db._source_language("FR - Movie Title") == "FR"
    assert db._source_language("Plain Untagged Title") == "EN"
    assert db._source_language(None) == "EN"


def test_source_language_ir_prefix_code(db):
    assert db._source_language("IR - Some Movie") == "IR"


def test_source_language_hi_pk_sc_real_titles_not_misdetected(db):
    # Real bare titles that collide with known language codes -- must NOT
    # be detected as language-tagged.
    assert db._source_language("HI - 2014") == "EN"
    assert db._source_language("PK - 2014") == "EN"
    assert db._source_language("SC - 3 Bed, 2 Bath, 1 Ghost") == "EN"
    # But a real HI-tagged title with more text still detects correctly.
    assert db._source_language("HI - Some Other Title") == "HI"


def test_language_backfill_dry_run_and_apply(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie_id = db.upsert_movie("Amelie", 2001)
    conn = db._connect()
    conn.execute(
        "INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, container_extension, raw_name, added_at, last_seen_at) VALUES (?,?,?,?,?,datetime('now'),datetime('now'))",
        (movie_id, provider_id, "gr-1", "mp4", "GR - Amelie"),
    )
    db._commit_with_retry(conn)
    conn.close()

    report = db.language_backfill_dry_run_report()
    assert report["movie_sources"]["GR"]["count"] == 1

    updated = db.apply_language_backfill()
    assert updated == 1

    conn = db._connect()
    row = conn.execute("SELECT language FROM movie_sources WHERE provider_stream_id='gr-1'").fetchone()
    conn.close()
    assert row["language"] == "GR"

    # Re-running is a no-op -- nothing left with language IS NULL.
    assert db.apply_language_backfill() == 0


def test_language_recompute_picks_up_fixed_prefix_codes(db):
    provider_id = db.upsert_provider("prov1", "http://example.com", "user", "pass")
    movie_id = db.upsert_movie("Some Movie", 2010)
    conn = db._connect()
    # Simulate a row classified before the IR prefix code existed: stored
    # as EN, but _source_language would now detect IR.
    conn.execute(
        "INSERT INTO movie_sources (movie_id, provider_id, provider_stream_id, container_extension, raw_name, language, added_at, last_seen_at) VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (movie_id, provider_id, "ir-1", "mp4", "IR - Some Movie", "EN"),
    )
    db._commit_with_retry(conn)
    conn.close()

    report = db.language_recompute_dry_run_report()
    assert report["movie_sources"]["IR"]["count"] == 1

    updated = db.apply_language_recompute()
    assert updated == 1

    conn = db._connect()
    row = conn.execute("SELECT language FROM movie_sources WHERE provider_stream_id='ir-1'").fetchone()
    conn.close()
    assert row["language"] == "IR"

    assert db.apply_language_recompute() == 0


def test_enabled_languages_clause_uses_configured_codes(db):
    config.save_enabled_languages(["EN", "FR"])
    clause, params = db._enabled_languages_clause("ms.language")
    assert params == ["EN", "FR"]
    assert "COALESCE(ms.language, 'EN') IN (?,?)" == clause

# KNM: 2026-11-05 - auto-clean hides an unwanted-language feed only when the probe,
# TMDB original_language and TMDB spoken_languages all agree; default off, never
# touches a decided source, reversible, and pauses while import/enrichment runs.
import asyncio

import config
import tmdb_store
import language_autoclean as ac


def _store(media_type, tmdb_id, original, spoken):
    tmdb_store.init_db()
    tmdb_store.upsert_payload(media_type, {
        "id": tmdb_id, "title": "T", "name": "T", "original_language": original,
        "spoken_languages": [{"iso_639_1": s} for s in spoken],
    })


def _movie(db, name, tmdb_id, observed, label="EN", decision=None, sibling=True):
    pid = db.upsert_provider(f"prov-{name}", "", "", "", provider_type="library")
    mid = db.upsert_movie(name, 2020)
    db.add_movie_source(mid, pid, f"s-{name}", "mkv")
    if sibling:  # a second, wanted feed keeps the title playable
        other = db.upsert_provider(f"prov-{name}-en", "", "", "", provider_type="library")
        db.add_movie_source(mid, other, f"s-{name}-en", "mkv")
    conn = db._connect()
    try:
        sid = conn.execute("SELECT id FROM movie_sources WHERE provider_stream_id=?", (f"s-{name}",)).fetchone()["id"]
        conn.execute("UPDATE movies SET tmdb_id=? WHERE id=?", (tmdb_id, mid))
        conn.execute("UPDATE movie_sources SET language=?, language_probe_decision=? WHERE id=?", (label, decision, sid))
        conn.commit()
    finally:
        conn.close()
    db.set_source_language_probe("movie", sid, observed, "observed", "embedded_audio_tags", "high")
    return sid


def _row(db, sid):
    conn = db._connect()
    try:
        return dict(conn.execute("SELECT language, language_probe_decision FROM movie_sources WHERE id=?", (sid,)).fetchone())
    finally:
        conn.close()


def test_defaults_off():
    assert config.get_language_autoclean_settings()["language_autoclean_enabled"] is False


def test_preview_requires_all_three_signals(db):
    agree = _movie(db, "agree", 501, ["JA"])
    _store("movie", 501, "ja", ["ja"])
    en_original = _movie(db, "orig", 502, ["JA"])
    _store("movie", 502, "en", ["ja"])
    en_spoken = _movie(db, "spoken", 503, ["JA"])
    _store("movie", 503, "ja", ["ja", "en"])
    no_tmdb = _movie(db, "notmdb", 504, ["JA"])
    has_en = _movie(db, "hasen", 505, ["EN", "JA"])
    _store("movie", 505, "ja", ["ja"])
    decided = _movie(db, "decided", 506, ["JA"], decision="rejected")
    _store("movie", 506, "ja", ["ja"])
    sole = _movie(db, "sole", 507, ["JA"], sibling=False)
    _store("movie", 507, "ja", ["ja"])

    preview = ac.preview(limit=50)

    assert [c["source_id"] for c in preview["candidates"]] == [agree]
    skipped = preview["skipped"]
    assert skipped["tmdb_original_language"] == 1 and skipped["tmdb_spoken_language"] == 1
    assert skipped["no_tmdb_data"] == 1 and skipped["only_source"] == 1
    for sid in (en_original, en_spoken, no_tmdb, has_en, decided, sole):
        assert _row(db, sid)["language_probe_decision"] in (None, "rejected")


def test_apply_hides_feed_and_revert_restores(db):
    sid = _movie(db, "apply", 601, ["JA"])
    _store("movie", 601, "ja", ["ja"])

    result = ac.apply_batch(limit=10)

    assert result["applied"] == 1
    assert _row(db, sid) == {"language": "JA", "language_probe_decision": "approved"}
    log = ac.list_log()
    assert log[0]["source_id"] == sid and log[0]["previous_language"] == "EN"

    assert ac.revert(log[0]["id"]) is True
    assert _row(db, sid) == {"language": "EN", "language_probe_decision": None}
    assert ac.list_log()[0]["reverted_at"]


def test_apply_skips_source_decided_after_preview(db):
    sid = _movie(db, "race", 701, ["JA"])
    _store("movie", 701, "ja", ["ja"])
    conn = db._connect()
    conn.execute("UPDATE movie_sources SET language_probe_decision='rejected' WHERE id=?", (sid,))
    conn.commit(); conn.close()

    assert ac.apply_batch(limit=10)["applied"] == 0
    assert _row(db, sid)["language"] == "EN"


def test_cycle_disabled_does_nothing(db):
    sid = _movie(db, "off", 801, ["JA"])
    _store("movie", 801, "ja", ["ja"])

    assert asyncio.run(ac.run_cycle())["status"] == "disabled"
    assert _row(db, sid)["language"] == "EN"


def test_cycle_pauses_while_import_running_and_cools_down(db, monkeypatch):
    import vod_importer
    sid = _movie(db, "busy", 901, ["JA"])
    _store("movie", 901, "ja", ["ja"])
    config.save_language_autoclean_settings(enabled=True, batch_size=10, interval_seconds=300, cooldown_seconds=600)
    monkeypatch.setitem(vod_importer._IMPORT_PROGRESS, "running", True)
    clock = [1000.0]
    monkeypatch.setattr(ac, "_now", lambda: clock[0])
    monkeypatch.setattr(ac, "_last_busy_at", 0.0)

    assert asyncio.run(ac.run_cycle())["status"] == "paused_busy"
    monkeypatch.setitem(vod_importer._IMPORT_PROGRESS, "running", False)
    clock[0] += 300
    assert asyncio.run(ac.run_cycle())["status"] == "cooldown"
    assert _row(db, sid)["language"] == "EN"
    clock[0] += 301
    assert asyncio.run(ac.run_cycle())["status"] == "applied"
    assert _row(db, sid)["language"] == "JA"


def test_routes_toggle_preview_and_revert(db):
    import pytest
    from fastapi import HTTPException
    import vod_routes
    sid = _movie(db, "route", 1001, ["JA"])
    _store("movie", 1001, "ja", ["ja"])

    view = asyncio.run(vod_routes.save_language_autoclean_settings(
        vod_routes.LanguageAutocleanSettingsRequest(enabled=True, batch_size=5)))
    assert view["language_autoclean_enabled"] is True and view["language_autoclean_batch_size"] == 5
    assert [c["source_id"] for c in asyncio.run(vod_routes.language_autoclean_preview())["candidates"]] == [sid]

    ac.apply_batch(5)
    log_id = asyncio.run(vod_routes.language_autoclean_log())["results"][0]["id"]
    assert asyncio.run(vod_routes.revert_language_autoclean(log_id)) == {"reverted": True}
    with pytest.raises(HTTPException):
        asyncio.run(vod_routes.revert_language_autoclean(log_id))
    # a reverted source is never auto-cleaned again
    assert ac.preview()["candidates"] == []


def test_log_shows_title_provider_and_readable_time(db):
    # KNM: 2026-11-05 - the log showed "episode #1681044" and a raw timestamp; users need names.
    sid = _movie(db, "Cure", 1101, ["JA"])
    db.log_language_autoclean("movie", sid, "EN", "JA", ["JA"], "ja")
    pid = db.upsert_provider("prov-show", "", "", "", provider_type="library")
    series = db.upsert_series("Dark", 2017)
    esrc = db.add_episode_source(db.add_episode(series, 1, 2, "Lies"), pid, "e-dark", "mkv")
    db.log_language_autoclean("episode", esrc, "EN", "DE", ["DE"], "de")

    rows = {r["kind"]: r for r in db.list_language_autoclean_log()}

    assert rows["movie"]["title"] == "Cure" and rows["movie"]["provider_name"] == "prov-Cure"
    assert rows["episode"]["title"] == "Dark S01E02 · Lies" and rows["episode"]["provider_name"] == "prov-show"
    assert rows["movie"]["applied_at_iso"].endswith("+00:00")


def test_middle_english_tag_counts_as_english():
    # KNM: 2026-11-05 - "enm" (Middle English) on a modern file is a mistagged "eng".
    import vod_probe
    assert vod_probe._LANGUAGE_CODES["enm"] == "EN"


def test_busy_check_sees_this_codebases_own_job_trackers(monkeypatch):
    """The getattr-based tracker names only exist in some builds; the import lock and the
    episode-preloading flag are real here and must pause auto-clean."""
    import vod_importer
    import language_autoclean

    assert language_autoclean._busy_reason() is None
    monkeypatch.setattr(vod_importer, "_EPISODE_TRICKLE_RUNNING", True)
    assert language_autoclean._busy_reason() == "episode_preloading"
    monkeypatch.setattr(vod_importer, "_EPISODE_TRICKLE_RUNNING", False)

    class _Locked:
        def locked(self):
            return True

    monkeypatch.setattr(vod_importer, "_XC_IMPORT_LOCK", _Locked())
    assert language_autoclean._busy_reason() == "import"

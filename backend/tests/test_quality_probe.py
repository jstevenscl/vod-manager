"""On-demand "Check quality": what it stores, and above all that it never leans on a provider.
ffprobe isn't needed here: the frame-size read is stubbed, the HTTP side runs against a real local server."""

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import HTTPException

import vod_db
import vod_downloader
import vod_probe
import vod_routes
import xc_server
import config

TOTAL = 20_000_000
hits: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        sid = self.path.rsplit("/", 1)[-1].split(".")[0]
        hits.append(sid)
        if sid == "limited" or (sid == "once" and hits.count("once") == 1):
            self.send_response(429)
            self.end_headers()
            return
        if sid == "busy":
            body = b"<html>Maximum connections reached for this account</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if sid == "ignores-range":  # sends the whole 9 MB with a 200: we must stop reading early
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(TOTAL))
            self.end_headers()
            try:
                self.wfile.write(b"\0" * TOTAL)
            except OSError:
                pass
            return
        start, end = 0, vod_probe.PROBE_BYTES - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start, end = int(a), min(int(b), TOTAL - 1)
        self.send_response(206)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Range", f"bytes {start}-{end}/{TOTAL}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            self.wfile.write(b"\0" * (end - start + 1))
        except OSError:
            pass


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    hits.clear()
    for d in (vod_probe._last_end, vod_probe._recent, vod_downloader._cooldown_until, vod_downloader._pushbacks,
              vod_downloader._provider_active):
        d.clear()
    vod_probe._language_provider_blocks.clear()
    vod_probe._language_claimed_sources.clear()
    vod_probe._language_trickle_day = ""
    vod_probe._language_trickle_count = 0
    vod_probe._in_flight.clear()
    vod_probe._running_total = 0
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)

    async def fake_ffprobe(path):
        import os
        if os.path.basename(path).startswith("vodprobe-") and hits and hits[-1] in ("no-video",):
            return None, None, None
        if hits and hits[-1] == "moov-at-end" and os.path.getsize(path) != TOTAL:
            return None, None, None          # the start of the file alone says nothing
        return 1920, 1080, "h264"
    monkeypatch.setattr(vod_probe, "_ffprobe_video", fake_ffprobe)


def _sources(db, base, ids=("a",), pname="p1"):
    pid = db.upsert_provider(pname, base, "u", "secretpw")
    out = []
    for i, sid in enumerate(ids):
        mid = db.upsert_movie(f"Movie {i}", 2000 + i)
        db.add_movie_source(mid, pid, sid, "mp4")
        conn = db._connect()
        out.append(conn.execute("SELECT id FROM movie_sources WHERE movie_id=?", (mid,)).fetchone()["id"])
        conn.close()
    return pid, out


def _probe(sid, force=False):
    return asyncio.run(vod_probe.probe_source("movie", sid, force))


def _refused(sid):
    with pytest.raises(vod_probe.ProbeRefused) as e:
        _probe(sid)
    return e.value


def test_quality_labels():
    assert vod_probe.quality_label(3840, 2160) == "4K"
    assert vod_probe.quality_label(1920, 1080) == "1080p"
    assert vod_probe.quality_label(1920, 800) == "1080p"     # scope picture, still 1080p wide
    assert vod_probe.quality_label(1280, 720) == "720p"
    assert vod_probe.quality_label(720, 480) == "480p"
    assert vod_probe.quality_label(None, None) is None


def test_language_probe_records_observed_tags_without_changing_configured_language(db, tmp_path, monkeypatch):
    pid = db.upsert_provider("local-language", "", "", "", provider_type="library")
    mid = db.upsert_movie("Canary Hindi Movie", 2026)
    path = tmp_path / "canary.mkv"
    path.write_bytes(b"fixture")
    db.add_movie_source(mid, pid, "canary", "mkv", local_file_path=str(path))
    conn = db._connect()
    sid = conn.execute("SELECT id FROM movie_sources WHERE movie_id=?", (mid,)).fetchone()["id"]
    conn.execute("UPDATE movie_sources SET language='EN' WHERE id=?", (sid,))
    conn.commit(); conn.close()
    monkeypatch.setattr(vod_probe, "_ffprobe_audio_languages", lambda p: asyncio.sleep(0, result=["HI"]))

    out = asyncio.run(vod_probe.probe_language_source("movie", sid))

    assert out["observed_languages"] == ["HI"]
    assert out["status"] == "observed"
    assert out["mismatch"] is True
    row = db.get_language_probe_source("movie", sid)
    assert row["language"] == "EN"
    assert row["language_probe_status"] == "observed"
    assert row["language_probe_languages"] == '["HI"]'


def test_language_probe_caches_unknown_result(db, tmp_path, monkeypatch):
    pid = db.upsert_provider("local-language-unknown", "", "", "", provider_type="library")
    mid = db.upsert_movie("Un tagged Movie", 2026)
    path = tmp_path / "unknown.mkv"
    path.write_bytes(b"fixture")
    db.add_movie_source(mid, pid, "unknown", "mkv", local_file_path=str(path))
    conn = db._connect()
    sid = conn.execute("SELECT id FROM movie_sources WHERE movie_id=?", (mid,)).fetchone()["id"]
    conn.commit(); conn.close()
    calls = []
    async def no_tags(_):
        calls.append(1)
        return []
    monkeypatch.setattr(vod_probe, "_ffprobe_audio_languages", no_tags)

    first = asyncio.run(vod_probe.probe_language_source("movie", sid))
    second = asyncio.run(vod_probe.probe_language_source("movie", sid))

    assert first["status"] == "unknown" and first["checked"]
    assert second["status"] == "unknown" and second["checked"]
    assert len(calls) == 1


def test_language_trickle_uses_experimental_limits_and_saved_playback_policy(db, monkeypatch):
    assert vod_probe.LANGUAGE_GLOBAL_MAX == 15
    assert vod_probe.LANGUAGE_TRICKLE_DAILY_MAX == 500
    assert vod_probe.LANGUAGE_WINDOW_MAX is None

    monkeypatch.setattr(config, "CONFIG_FILE", db.DB_PATH.with_name("config.json"))
    config.save_enabled_languages(["IT", "ES"])
    assert config.get_enabled_languages() == ["IT", "ES"]
    assert db.list_language_mismatches()["enabled_languages"] == ["ES", "IT"]


def test_language_trickle_runs_up_to_source_limit_per_cycle(monkeypatch):
    config.save_language_trickle_settings(15, 180, None, enabled=True)
    targets = [
        {"kind": "movie", "source_id": i, "provider_id": 1 if i < 8 else 2}
        for i in range(15)
    ]

    async def fake_probe(kind, source_id, force=False, spacing=None):
        return {"source_id": source_id, "mismatch": False}

    monkeypatch.setattr(vod_probe.vod_db, "next_language_probe_targets", lambda limit, distinct: targets[:limit])
    monkeypatch.setattr(vod_probe, "probe_language_source", fake_probe)

    result = asyncio.run(vod_probe.run_language_trickle_cycle())

    assert result["targets"] == 15
    assert len(result["results"]) == 15


def test_language_trickle_capacity_refusal_records_two_hour_hold(db, monkeypatch):
    provider_id = db.upsert_provider("Full Pool", "http://capacity.invalid", "u", "p")
    config.save_language_trickle_settings(15, 180, None)

    async def no_slot(*args, **kwargs):
        raise vod_probe.ProbeRefused("No free probe connection", 60, "capacity")

    monkeypatch.setattr(vod_probe, "probe_language_source", no_slot)
    result = asyncio.run(vod_probe.language_trickle_once(
        {"kind": "movie", "source_id": 77, "provider_id": provider_id}
    ))

    assert result["status"] == "capacity_hold"
    assert result["retry_after"] == 7200
    hold = db.list_language_probe_capacity_holds()[0]
    assert hold["provider_id"] == provider_id
    assert hold["source_id"] == 77
    assert hold["attempt_count"] == 1
    assert hold["next_retry_at"] - hold["last_attempt_at"] == 7200


def test_language_cycle_stops_provider_queue_after_capacity_refusal(db, monkeypatch):
    config.save_language_trickle_settings(15, 180, None)
    config.save_language_trickle_enabled(True)
    first_provider = db.upsert_provider("Provider A", "http://a.invalid", "u", "p")
    second_provider = db.upsert_provider("Provider B", "http://b.invalid", "u", "p")
    targets = [
        {"kind": "movie", "source_id": 1, "provider_id": first_provider},
        {"kind": "movie", "source_id": 2, "provider_id": first_provider},
        {"kind": "movie", "source_id": 3, "provider_id": second_provider},
    ]
    monkeypatch.setattr(vod_probe.vod_db, "next_language_probe_targets", lambda *args: targets)
    attempted = []

    async def fake_once(target):
        attempted.append(target["source_id"])
        return {"status": "capacity_hold" if target["source_id"] == 1 else "checked"}

    monkeypatch.setattr(vod_probe, "language_trickle_once", fake_once)
    result = asyncio.run(vod_probe.run_language_trickle_cycle())

    assert result["targets"] == 3
    assert set(attempted) == {1, 3}


def test_language_trickle_manual_cycle_is_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.save_language_trickle_settings(15, 180, None)
    called = []
    monkeypatch.setattr(vod_probe.vod_db, "next_language_probe_targets", lambda *args: called.append(True))

    result = asyncio.run(vod_probe.run_language_trickle_cycle())

    assert result["targets"] == 0 and result["results"] == []
    assert called == []


def test_language_trickle_settings_allow_unlimited_daily_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.save_language_trickle_settings(15, 180, None)
    assert config.get_language_trickle_settings() == {
        "language_trickle_enabled": False,
        "language_trickle_probes_per_cycle": 15,
        "language_trickle_interval_seconds": 180,
        "language_trickle_daily_limit": None,
    }


def test_language_trickle_enabled_setting_can_be_toggled(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.save_language_trickle_settings(15, 180, None)
    config.save_language_trickle_enabled(False)
    assert config.get_language_trickle_settings()["language_trickle_enabled"] is False
    config.save_language_trickle_enabled(True)
    assert config.get_language_trickle_settings()["language_trickle_enabled"] is True


def test_provider_throttle_budget_and_gate_escalation(db):
    provider_id = db.upsert_provider("Throttle Provider", "http://throttle.invalid", "u", "p")
    state = db.language_probe_provider_state(provider_id, 50)
    assert state["status"] == "normal" and state["effective_budget"] == 50
    for _ in range(3):
        state = db.record_language_probe_throttle(provider_id, "429", "rate limit", 50)
    assert state["status"] == "normal" and state["effective_budget"] == 40

    for _ in range(9):
        state = db.record_language_probe_throttle(provider_id, "PUSHBACK", "slow down", 50)
    assert state["effective_budget"] == 10

    for _ in range(3):
        state = db.record_language_probe_throttle(provider_id, "429", "rate limit", 50)
    assert state["status"] == "gating" and state["tier"] == 1

    db.unblock_language_probe_provider(provider_id, 50)
    state = db.language_probe_provider_state(provider_id, 50)
    assert state["status"] == "normal" and state["effective_budget"] == 50


def test_capacity_hold_uses_two_hour_provider_retry_and_tracks_attempts(db):
    provider_id = db.upsert_provider("Capacity Provider", "http://capacity.invalid", "u", "p")
    movie_id = db.upsert_movie("Held Movie", 2026)
    db.add_movie_source(movie_id, provider_id, "held", "mp4")
    conn = db._connect()
    source_id = conn.execute("SELECT id FROM movie_sources WHERE movie_id=?", (movie_id,)).fetchone()["id"]
    conn.close()

    first = db.hold_language_probe_provider(provider_id, "movie", source_id,
                                            "No free connection", hold_seconds=7200, now=1000)
    second = db.hold_language_probe_provider(provider_id, "movie", source_id,
                                             "Still at capacity", hold_seconds=7200, now=2000)

    assert first["first_attempt_at"] == 1000
    assert first["next_retry_at"] == 8200
    assert second["first_attempt_at"] == 1000
    assert second["last_attempt_at"] == 2000
    assert second["next_retry_at"] == 9200
    assert second["attempt_count"] == 2
    hold = db.list_language_probe_capacity_holds(now=2000)[0]
    assert hold["title"] == "Held Movie" and hold["retry_due"] == 0
    db.clear_language_probe_capacity_hold(provider_id)
    restarted = db.hold_language_probe_provider(provider_id, "movie", source_id,
                                                "Capacity became full again", hold_seconds=7200, now=3000)
    assert restarted["first_attempt_at"] == 3000 and restarted["attempt_count"] == 1


def test_capacity_hold_suppresses_provider_until_retry_and_prioritizes_held_source(db):
    provider_id = db.upsert_provider("Capacity Queue", "http://capacity.invalid", "u", "p")
    category_id = db.upsert_category("Movies", "movie")
    source_ids = []
    for title in ("First Movie", "Second Movie"):
        movie_id = db.upsert_movie(title, 2026)
        db.place_movie_in_category(movie_id, category_id)
        db.add_movie_source(movie_id, provider_id, title, "mp4")
        conn = db._connect()
        source_ids.append(conn.execute("SELECT id FROM movie_sources WHERE movie_id=?", (movie_id,)).fetchone()["id"])
        conn.close()

    db.hold_language_probe_provider(provider_id, "movie", source_ids[1],
                                    "No free connection", hold_seconds=7200)
    assert db.next_language_probe_targets(10, False) == []

    conn = db._connect()
    conn.execute("UPDATE language_probe_capacity_provider_state SET next_retry_at=? WHERE provider_id=?",
                 (time.time() - 1, provider_id))
    conn.commit()
    conn.close()
    targets = db.next_language_probe_targets(10, False)
    assert targets[0]["source_id"] == source_ids[1]


def test_language_trickle_bounds_are_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.save_language_trickle_settings(2000, 0, None)
    settings = config.get_language_trickle_settings()
    assert settings["language_trickle_probes_per_cycle"] == 600
    assert settings["language_trickle_interval_seconds"] == 90


def test_language_audit_stats_keep_daily_probe_history(db):
    db.record_language_probe_run("movie", 10, 20, "observed", "EN", ["EN"], ["EN"], False, True)
    db.record_language_probe_run("episode", 11, 21, "observed", "EN", ["HI"], ["EN"], True, False)
    stats = db.language_audit_stats()
    assert stats["history"][0]["probes"] == 2
    assert stats["history"][0]["mismatches"] == 1
    assert stats["history"][0]["outside_wanted"] == 1
    assert stats["history"][0]["wanted_only"] == 1
    assert stats["history"][0]["not_needed"] == 1
    activity = db.language_probe_activity_today()
    assert activity["attempts"] == 2 and activity["completed"] == 2


def test_language_audit_provider_history_excludes_inactive_providers(db):
    active = db.upsert_provider("active-language-provider", "", "", "", provider_type="library")
    inactive = db.upsert_provider("inactive-language-provider", "", "", "", provider_type="library")
    db.set_provider_active(inactive, False)
    db.record_language_probe_run("movie", 21, active, "observed", "EN", ["EN"], ["EN"], False, True)
    db.record_language_probe_run("movie", 22, inactive, "observed", "EN", ["EN"], ["EN"], False, True)
    providers = {row["provider"] for row in db.language_audit_stats()["provider_history"]}
    assert "active-language-provider" in providers
    assert "inactive-language-provider" not in providers


def test_language_audit_stats_group_recent_error_codes_by_provider(db):
    pid = db.upsert_provider("error-code-provider", "", "", "", provider_type="library")
    db.record_language_probe_run("movie", 31, pid, "deferred", None, [], ["EN"], False, False,
                                "error-code-provider answered HTTP 404 for this source.")
    db.record_language_probe_run("movie", 32, pid, "deferred", None, [], ["EN"], False, False,
                                "error-code-provider asked us to slow down.")
    db.record_language_probe_run("movie", 33, pid, "deferred", None, [], ["EN"], False, False,
                                "error-code-provider has no free probe connection.")
    codes = {(row["provider"], row["code"]): row["count"] for row in db.language_audit_stats()["error_history"]}
    assert codes[("error-code-provider", "404")] == 1
    assert codes[("error-code-provider", "PUSHBACK")] == 1
    assert codes[("error-code-provider", "CAPACITY")] == 1


def test_language_mismatch_report_approval_updates_tag_and_archives_excluded_source(db, tmp_path):
    pid = db.upsert_provider("report-language", "", "", "", provider_type="library")
    mid = db.upsert_movie("Report Hindi Movie", 2026)
    path = tmp_path / "report.mkv"
    path.write_bytes(b"fixture")
    db.add_movie_source(mid, pid, "report", "mkv", local_file_path=str(path))
    conn = db._connect()
    sid = conn.execute("SELECT id FROM movie_sources WHERE movie_id=?", (mid,)).fetchone()["id"]
    conn.execute("UPDATE movie_sources SET language='EN' WHERE id=?", (sid,))
    conn.commit(); conn.close()
    db.set_source_language_probe("movie", sid, ["HI"], "observed", "local_embedded_audio_tags", "high")

    report = db.list_language_mismatches()
    row = next(r for r in report["results"] if r["source_id"] == sid)
    assert row["proposed_language"] == "HI" and row["proposed_archive"] is True

    result = db.decide_language_mismatch("movie", sid, "approved")
    assert result["action"] == "language_updated_and_archived"
    source = db.get_language_probe_source("movie", sid)
    assert source["language"] == "HI" and source["language_probe_decision"] == "approved"
    assert db.get_movie(mid)["review_excluded"] == 1
    assert not db.list_language_mismatches()["results"]


def test_unknown_language_is_separate_and_never_archive_approvable(db, tmp_path):
    pid = db.upsert_provider("unknown-language-provider", "", "", "", provider_type="library")
    mid = db.upsert_movie("Unknown Tagged Movie", 2026)
    path = tmp_path / "unknown.mkv"
    path.write_bytes(b"fixture")
    db.add_movie_source(mid, pid, "unknown", "mkv", local_file_path=str(path))
    conn = db._connect()
    sid = conn.execute("SELECT id FROM movie_sources WHERE movie_id=?", (mid,)).fetchone()["id"]
    conn.execute("UPDATE movie_sources SET language='EN' WHERE id=?", (sid,))
    conn.commit(); conn.close()
    db.set_source_language_probe("movie", sid, ["UNKNOWN"], "observed", "local_embedded_audio_tags", "low")

    assert not any(row["source_id"] == sid for row in db.list_language_mismatches()["results"])
    unknown = next(row for row in db.list_language_unknowns()["results"] if row["source_id"] == sid)
    assert unknown["proposed_archive"] is False
    with pytest.raises(ValueError, match="unknown or multiple"):
        db.decide_language_mismatch("movie", sid, "approved")

    result = db.approve_language_probe_source("movie", sid, "EN")
    source = db.get_language_probe_source("movie", sid)
    assert result["configured_language"] == "EN"
    assert source["language"] == "EN"
    assert source["language_probe_status"] == "manual_approved"
    assert source["language_probe_decision"] == "approved"
    assert source["language_probe_method"] == "manual_language_confirmed"
    assert source["language_probe_languages"] == '["EN"]'
    assert not any(row["source_id"] == sid for row in db.list_language_unknowns()["results"])
    assert not any(row["source_id"] == sid for row in db.next_language_probe_targets(100, distinct_providers=False))


def test_unknown_language_can_be_manually_set_to_another_enabled_language(db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.save_enabled_languages(["EN", "RU", "ES"])
    pid = db.upsert_provider("manual-language-provider", "", "", "", provider_type="library")
    mid = db.upsert_movie("Unknown Russian Movie", 2026)
    db.add_movie_source(mid, pid, "unknown-russian", "mkv")
    conn = db._connect()
    sid = conn.execute("SELECT id FROM movie_sources WHERE movie_id=?", (mid,)).fetchone()["id"]
    conn.execute("UPDATE movie_sources SET language='EN' WHERE id=?", (sid,))
    conn.commit(); conn.close()
    db.set_source_language_probe("movie", sid, [], "unknown", "embedded_audio_tags", "low")

    result = db.approve_language_probe_source("movie", sid, "RU")
    source = db.get_language_probe_source("movie", sid)

    assert result["configured_language"] == "RU"
    assert source["language"] == "RU"
    assert source["language_probe_decision"] == "approved"
    assert source["language_probe_languages"] == '["RU"]'
    assert db.list_language_unknowns()["enabled_languages"] == ["EN", "ES", "RU"]
    assert not any(row["source_id"] == sid for row in db.list_language_unknowns()["results"])


def test_unknown_language_confirmation_rejects_disabled_language(db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.save_enabled_languages(["EN"])
    pid = db.upsert_provider("invalid-language-provider", "", "", "", provider_type="library")
    mid = db.upsert_movie("Unknown Movie", 2026)
    db.add_movie_source(mid, pid, "unknown-invalid", "mkv")
    conn = db._connect()
    sid = conn.execute("SELECT id FROM movie_sources WHERE movie_id=?", (mid,)).fetchone()["id"]
    conn.commit(); conn.close()

    with pytest.raises(ValueError, match="must be enabled"):
        db.approve_language_probe_source("movie", sid, "RU")


def test_language_provider_error_does_not_pause_provider(monkeypatch):
    provider = {"id": 901, "name": "limited-provider", "provider_type": "xc"}
    source = {"provider_id": 901, "provider_stream_id": "x", "container_extension": "mp4"}
    async def fail_fetch(*args, **kwargs):
        raise vod_probe.ProbeRefused("HTTP 429", 900, "pushback")
    async def credentials(*args, **kwargs):
        return {"sub_account_id": 1}
    async def build(*args, **kwargs):
        return "http://provider.invalid/stream"
    monkeypatch.setattr(vod_probe, "_fetch_head", fail_fetch)
    monkeypatch.setattr(vod_probe.xc_server, "_select_upstream_credentials", credentials)
    monkeypatch.setattr(vod_probe.xc_server, "_build_upstream_url", build)
    monkeypatch.setattr(vod_probe.xc_server, "_release_capacity", lambda *args, **kwargs: None)

    with pytest.raises(vod_probe.ProbeRefused, match="HTTP 429"):
        asyncio.run(vod_probe._probe_remote_language("movie", source, provider, spacing=0))
    with pytest.raises(vod_probe.ProbeRefused, match="paused"):
        asyncio.run(vod_probe._probe_remote_language("movie", source, provider, spacing=0))


def test_probe_stores_size_and_resolution_and_is_cached(db, server):
    _, (sid,) = _sources(db, server)
    out = _probe(sid)
    assert out["resolution"] == "1080p" and out["codec"] == "h264" and out["file_size_bytes"] == TOTAL and out["checked"]
    assert len(hits) == 1
    again = _probe(sid)            # on file: the provider is not touched again
    assert again["resolution"] == "1080p" and len(hits) == 1
    src = vod_db.get_probe_source("movie", sid)
    assert src["probe_height"] == 1080 and src["file_size_bytes"] == TOTAL


def test_a_server_that_ignores_range_is_cut_off_after_the_cap(db, server):
    _, (sid,) = _sources(db, server, ("ignores-range",))
    seen = {}

    real = vod_probe._fetch_head

    async def spy(*a):
        data, total = await real(*a)
        seen["n"] = len(data)
        return data, total
    vod_probe._fetch_head = spy
    try:
        out = _probe(sid)
    finally:
        vod_probe._fetch_head = real
    assert seen["n"] <= vod_probe.PROBE_BYTES and out["file_size_bytes"] == TOTAL


def test_the_connection_slot_is_always_given_back(db, server):
    pid, (sid,) = _sources(db, server)
    _probe(sid)
    assert xc_server._active_downloads.get(pid, 0) == 0 and pid not in vod_probe._in_flight


def test_pushback_puts_the_provider_on_cooldown_for_everyone(db, server):
    pid, (a, b) = _sources(db, server, ("limited", "other"))
    err = _refused(a)
    assert "pushed back" in str(err) and err.retry_after >= 100
    assert vod_downloader._cooldown_until[pid] > 0
    assert "slow down" in str(_refused(b))                   # the next probe waits, and so would a download
    assert hits == ["limited"]


def test_a_connection_limit_page_backs_off(db, server):
    pid, (sid,) = _sources(db, server, ("busy",))
    assert "connection limit" in str(_refused(sid))
    assert vod_downloader._cooldown_until[pid] > 0


def test_probes_on_one_provider_are_spaced_out(db, server):
    _, (a, b) = _sources(db, server, ("a", "b"))
    _probe(a)
    err = _refused(b)
    assert "Easing off" in str(err) and err.retry_after > 0
    assert hits == ["a"]


def test_a_provider_cannot_be_hammered_in_a_burst(db, server):
    _, ids = _sources(db, server, tuple(f"s{i}" for i in range(vod_probe._WINDOW_MAX + 2)))
    for sid in ids[:vod_probe._WINDOW_MAX]:
        _probe(sid)
        vod_probe._last_end.clear()                          # skip the quiet gap; the ceiling must still hold
    err = _refused(ids[-1])
    assert "lot of checks" in str(err)
    assert len(hits) == vod_probe._WINDOW_MAX


def test_never_probes_while_a_download_holds_the_provider(db, server):
    pid, (sid,) = _sources(db, server)
    vod_downloader._provider_active[pid] = 1
    assert "busy with a download" in str(_refused(sid))
    assert hits == []


def test_never_probes_when_the_provider_has_no_free_connection(db, server):
    pid, (sid,) = _sources(db, server)
    db.set_provider_shared_limit(pid, 1)
    xc_server._active_vod_streams[pid] = 1                   # a viewer already holds the only connection
    try:
        err = _refused(sid)
    finally:
        xc_server._active_vod_streams.pop(pid, None)
    assert "playback" in str(err)
    assert hits == []                                        # the provider was never contacted


def test_one_probe_at_a_time_per_provider(db, server):
    pid, (sid,) = _sources(db, server)
    vod_probe._in_flight.add(pid)
    assert "already being checked" in str(_refused(sid))
    assert hits == []


def test_route_turns_a_refusal_into_a_429_with_retry_after(db, server):
    pid, (sid,) = _sources(db, server)
    vod_downloader._cooldown_until[pid] = __import__("time").time() + 90
    with pytest.raises(HTTPException) as e:
        asyncio.run(vod_routes.check_source_quality(vod_routes.SourceQualityRequest(kind="movie", source_id=sid)))
    assert e.value.status_code == 429 and int(e.value.headers["Retry-After"]) >= 80
    with pytest.raises(HTTPException) as e:
        asyncio.run(vod_routes.check_source_quality(vod_routes.SourceQualityRequest(kind="movie", source_id=99999)))
    assert e.value.status_code == 400


# ── background season / series checks ───────────────────────────────────────

def _series(db, base, stream_ids, season=1, pname="p1"):
    pid = db.upsert_provider(pname, base, "u", "secretpw")
    sid = db.upsert_series("Some Show", 2010)
    srcs = []
    for n, stream in enumerate(stream_ids, 1):
        eid = db.add_episode(sid, season, n, f"ep{n}")
        srcs.append(db.add_episode_source(eid, pid, stream, "mp4"))
    return pid, sid, srcs


@pytest.fixture()
def fast_batch(monkeypatch):
    monkeypatch.setattr(vod_probe, "_BATCH_SPACING", (0.05, 0.08))
    vod_probe._batches.clear()


async def _settle(b, states=("done", "paused", "cancelled"), timeout=20):
    end = asyncio.get_running_loop().time() + timeout
    while b["state"] not in states and asyncio.get_running_loop().time() < end:
        await asyncio.sleep(0.05)


def test_a_season_check_goes_one_episode_at_a_time_and_stores_each(db, server, fast_batch):
    pid, series_id, srcs = _series(db, server, ["e1", "e2", "e3"])

    async def go():
        view = await vod_probe.start_batch(series_id, 1)
        assert view["total"] == 3 and view["state"] == "running"
        await _settle(vod_probe._batches[view["id"]])
        return vod_probe._batches[view["id"]]
    b = asyncio.run(go())
    assert b["state"] == "done" and b["done"] == 3
    assert hits == ["e1", "e2", "e3"]                       # strictly in order, one connection at a time
    assert all(vod_db.get_probe_source("episode", s)["probe_height"] == 1080 for s in srcs)
    assert xc_server._active_downloads.get(pid, 0) == 0


def test_the_first_pushback_stops_the_whole_season_until_resumed(db, server, fast_batch):
    pid, series_id, srcs = _series(db, server, ["e1", "once", "e3", "e4"])

    async def go():
        view = await vod_probe.start_batch(series_id)
        b = vod_probe._batches[view["id"]]
        await _settle(b)
        assert b["state"] == "paused" and "pushed back" in b["message"]
        assert hits == ["e1", "once"]                       # e3 and e4 were never touched
        await asyncio.sleep(0.5)
        assert hits == ["e1", "once"]                       # and it does not retry on its own
        vod_downloader._cooldown_until.clear()              # the provider calmed down; a person resumes
        assert vod_probe.resume_batch(view["id"])
        await _settle(b, ("done",))
        return b
    b = asyncio.run(go())
    assert b["state"] == "done" and hits == ["e1", "once", "once", "e3", "e4"]   # the refused one is retried first


def test_a_season_check_can_be_cancelled(db, server, fast_batch, monkeypatch):
    monkeypatch.setattr(vod_probe, "_BATCH_SPACING", (0.5, 0.6))
    _, series_id, _ = _series(db, server, ["e1", "e2", "e3", "e4"])

    async def go():
        view = await vod_probe.start_batch(series_id)
        b = vod_probe._batches[view["id"]]
        while not hits:
            await asyncio.sleep(0.02)
        assert vod_probe.cancel_batch(view["id"])
        await asyncio.sleep(1.5)
        return b
    b = asyncio.run(go())
    assert b["state"] == "cancelled" and len(hits) < 4


def test_already_checked_episodes_are_skipped_and_one_series_runs_once(db, server, fast_batch):
    _, series_id, srcs = _series(db, server, ["e1", "e2"])
    db.set_source_probe("episode", srcs[0], 1000, 1280, 720, "h264")

    async def go():
        view = await vod_probe.start_batch(series_id)
        assert view["total"] == 1
        with pytest.raises(vod_probe.ProbeRefused, match="already being checked"):
            await vod_probe.start_batch(series_id)
        await _settle(vod_probe._batches[view["id"]])
        with pytest.raises(vod_probe.ProbeRefused, match="already been checked"):
            await vod_probe.start_batch(series_id)
    asyncio.run(go())
    assert hits == ["e2"]


def test_a_long_series_is_queued_in_capped_chunks(db, server, fast_batch, monkeypatch):
    monkeypatch.setattr(vod_probe, "_BATCH_MAX_EPISODES", 2)
    _, series_id, _ = _series(db, server, ["a", "b", "c", "d", "e"])

    async def go():
        view = await vod_probe.start_batch(series_id)
        vod_probe.cancel_batch(view["id"])
        return view
    view = asyncio.run(go())
    assert view["total"] == 2 and view["more_not_queued"] == 3


def test_a_batch_waits_for_a_download_instead_of_failing(db, server, fast_batch, monkeypatch):
    monkeypatch.setattr(vod_probe, "_BATCH_WAIT_CAP", 0.2)
    pid, series_id, _ = _series(db, server, ["e1"])
    vod_downloader._provider_active[pid] = 1               # a download holds the provider

    async def go():
        view = await vod_probe.start_batch(series_id)
        b = vod_probe._batches[view["id"]]
        await asyncio.sleep(0.8)
        assert hits == [] and b["state"] == "running" and b["message"].startswith("Waiting")
        vod_downloader._provider_active[pid] = 0
        vod_probe._last_end.clear()
        b["state"] = "running"
        await _settle(b, ("done",), 15)
        return b
    b = asyncio.run(go())
    assert b["state"] == "done" and hits == ["e1"]


def test_an_index_at_the_end_of_the_file_is_found_with_one_extra_small_request(db, server):
    _, (sid,) = _sources(db, server, ("moov-at-end",))
    out = _probe(sid)
    assert out["resolution"] == "1080p" and out["checked"] and not out["unreadable"]
    assert hits == ["moov-at-end", "moov-at-end"]            # the start, then the last few MB: nothing more


def test_a_file_with_no_readable_video_keeps_its_size_but_is_not_marked_checked(db, server):
    pid, (sid,) = _sources(db, server, ("no-video",))
    out = _probe(sid)
    assert out["unreadable"] and not out["checked"] and out["file_size_bytes"] == TOTAL and out["resolution"] is None
    assert vod_db.get_probe_source("movie", sid)["probed_at"] is None   # so it can be tried again later


def test_index_offset_follows_the_media_data_block():
    head = (32).to_bytes(4, "big") + b"ftypisom" + b"\0" * 20 + (8).to_bytes(4, "big") + b"free" + \
        (1000).to_bytes(4, "big") + b"mdat" + b"\0" * 100
    assert vod_probe._index_offset(head, 5000) == 40 + 1000
    assert vod_probe._index_offset(head, 900) is None                 # claims to end past the file: don't trust it
    assert vod_probe._index_offset(b"\0" * 64, 5000) is None          # not an MP4 layout

"""On-demand "Check quality": what it stores, and above all that it never leans on a provider.
ffprobe isn't needed here: the frame-size read is stubbed, the HTTP side runs against a real local server."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import HTTPException

import vod_db
import vod_downloader
import vod_probe
import vod_routes
import xc_server

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

"""Slow-transfer handling: the queue note (live speed + which server), and dropping a crawling transfer to
resume on a fresh connection. Runs against a local HTTP server whose first connection crawls."""

import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import config
import vod_db
import vod_downloader

from test_downloads import FILE_BYTES, _movie, _run, dl  # noqa: F401

HITS: dict = {}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        sid = self.path.rsplit("/", 1)[-1].split(".")[0]
        rng = self.headers.get("Range")
        HITS.setdefault(sid, []).append(rng)
        first = len(HITS[sid]) == 1
        data, start = FILE_BYTES, 0
        if rng and rng.startswith("bytes=") and sid != "norange":
            start = int(rng[6:].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        if sid != "norange":
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(data) - start))
        self.end_headers()
        crawl = first  # only the first connection to a path is slow
        for i in range(start, len(data), 16 * 1024):
            try:
                self.wfile.write(data[i:i + 16 * 1024])
                self.wfile.flush()
            except OSError:
                return
            if crawl or sid == "norange":
                time.sleep(0.016)  # about 1 MB/s


@pytest.fixture()
def slow_server():
    HITS.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def quick_windows(monkeypatch):
    monkeypatch.setattr(vod_downloader, "_SLOW_WINDOW", 1.0)
    monkeypatch.setattr(vod_downloader, "_SLOW_FLOOR_BPS", 2_000_000)
    monkeypatch.setattr(vod_downloader, "_SLOW_MIN_REMAINING", 100_000)
    monkeypatch.setattr(vod_downloader, "_SPEED_WINDOW", 1.0)


@pytest.fixture()
def notes(monkeypatch):
    seen = []
    real = vod_db.update_download_job

    def spy(job_id, *args, **kw):
        if kw.get("note"):
            seen.append(kw["note"])
        return real(job_id, *args, **kw)

    monkeypatch.setattr(vod_db, "update_download_job", spy)
    return seen


def test_a_crawling_transfer_resumes_on_a_fresh_connection(db, slow_server, dl, notes):
    mid, _ = _movie(db, slow_server, stream_id="crawl")
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "done"
    assert (dl / "Movies" / "Some Movie (2001) [tmdbid-777]" / "Some Movie (2001).mp4").read_bytes() == FILE_BYTES
    assert len(HITS["crawl"]) >= 2 and HITS["crawl"][0] is None
    assert HITS["crawl"][1] and HITS["crawl"][1].startswith("bytes=") and int(HITS["crawl"][1][6:-1]) > 0, "resumed, not restarted"
    assert any("reconnected 1×" in n for n in notes)


def test_the_note_names_the_server_without_its_host(db, slow_server, dl, notes):
    mid, _ = _movie(db, slow_server, stream_id="crawl")
    vod_downloader.enqueue(movie_ids=[mid])
    _run(1)
    shown = [n for n in notes if "server " in n]
    assert shown and all(re.search(r"server [0-9a-f]{5}\b", n) for n in shown)
    assert not any("127.0.0.1" in n for n in notes)


def test_nothing_is_dropped_when_the_provider_cannot_resume(db, slow_server, dl):
    mid, _ = _movie(db, slow_server, stream_id="norange")
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done"
    assert len(HITS["norange"]) == 1


def test_the_setting_can_turn_it_off(db, slow_server, dl):
    config.save_download_settings({"reconnect_slow": False})
    mid, _ = _movie(db, slow_server, stream_id="crawl")
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done"
    assert len(HITS["crawl"]) == 1


def test_a_speed_cap_is_never_mistaken_for_a_slow_provider(db, slow_server, dl):
    config.save_download_settings({"speed_limit_mbps": 8})
    mid, _ = _movie(db, slow_server, stream_id="crawl")
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done"
    assert len(HITS["crawl"]) == 1


def test_reconnecting_is_on_by_default_and_saved(db, tmp_path):
    assert config.get_download_settings()["reconnect_slow"] is True
    config.save_download_settings({"reconnect_slow": False})
    assert config.get_download_settings()["reconnect_slow"] is False


def test_server_ids_are_stable_and_short():
    assert vod_downloader._server_id("cdn.example.net") == vod_downloader._server_id("cdn.example.net")
    assert vod_downloader._server_id("a.example") != vod_downloader._server_id("b.example")
    assert re.fullmatch(r"[0-9a-f]{5}", vod_downloader._server_id("cdn.example.net"))


def test_window_rate_needs_enough_samples():
    assert vod_downloader._window_rate([], 8.0) is None
    assert vod_downloader._window_rate([(0.0, 0), (1.0, 5_000_000)], 8.0) is None  # about 3 seconds needed
    assert vod_downloader._window_rate([(0.0, 0), (4.0, 8_000_000)], 8.0) == pytest.approx(2_000_000)

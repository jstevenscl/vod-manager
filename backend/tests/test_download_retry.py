"""A provider that answers 5xx for a moment is retried on the same source (resuming) instead of failing the job."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import vod_downloader

from test_downloads import FILE_BYTES, _movie, _run, dl  # noqa: F401

HITS: dict = {}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        sid = self.path.rsplit("/", 1)[-1].split(".")[0]
        HITS[sid] = HITS.get(sid, 0) + 1
        if sid == "flaky" and HITS[sid] <= 2 or sid == "down":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(FILE_BYTES)))
        self.end_headers()
        self.wfile.write(FILE_BYTES)


@pytest.fixture()
def flaky_server(monkeypatch):
    HITS.clear()
    monkeypatch.setattr(vod_downloader, "_SERVER_ERROR_SECONDS", 0)
    monkeypatch.setattr(vod_downloader, "_RETRY_SECONDS", 0)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_a_brief_5xx_is_retried_and_the_download_finishes(db, flaky_server, dl):
    mid, _ = _movie(db, flaky_server, stream_id="flaky")
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done"
    assert HITS["flaky"] == 3


def test_a_provider_that_stays_down_fails_with_its_status(db, flaky_server, dl):
    mid, _ = _movie(db, flaky_server, stream_id="down")
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "failed" and "HTTP 503" in job["error"]
    assert HITS["down"] == vod_downloader._MAX_ATTEMPTS

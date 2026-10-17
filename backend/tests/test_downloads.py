"""Opt-in downloads: queueing, naming, the HTTP transfer (resume, failover, cancel,
HTML-instead-of-video, disk space, connection busy) and the settings guard rails.
The transfer tests run against a real local HTTP server that honours Range."""

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import HTTPException

import config
import vod_db
import vod_downloader
import vod_routes

FILE_BYTES = bytes(range(256)) * 8192  # 2 MiB, content is position-dependent so a bad resume shows up


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # /movie/u/p/<id>.mp4 or /series/u/p/<id>.mp4
        sid = self.path.rsplit("/", 1)[-1].split(".")[0]
        if sid == "limited":
            self.send_response(429)
            self.end_headers()
            return
        if sid == "gone":
            self.send_response(404)
            self.end_headers()
            return
        if sid == "html":
            body = b"<html>login expired</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        data = FILE_BYTES * (4 if sid == "slow" else 1)
        start = 0
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(data) - start))
        self.end_headers()
        step = 256 * 1024
        for i in range(start, len(data), step):
            try:
                self.wfile.write(data[i:i + step])
                self.wfile.flush()
            except OSError:
                return
            if sid == "slow":
                time.sleep(0.05)


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def dl(db, tmp_path):
    vod_downloader._cancel_ids.clear()
    vod_downloader._provider_active.clear()
    vod_downloader._cooldown_until.clear()
    vod_downloader._pushbacks.clear()
    out = tmp_path / "library"
    config.save_download_settings({"enabled": True, "output_dir": str(out), "min_free_gb": 0})
    return out


def _movie(db, base, stream_id="100", name="Some Movie", year=2001, tmdb="777", pname="p1"):
    pid = db.upsert_provider(pname, base, "u", "secretpw")
    mid = db.upsert_movie(name, year)
    if tmdb:
        conn = db._connect()
        conn.execute("UPDATE movies SET tmdb_id=? WHERE id=?", (tmdb, mid))
        conn.commit()
        conn.close()
    db.add_movie_source(mid, pid, stream_id, "mp4")
    return mid, pid


def _run(job_id):
    asyncio.run(vod_downloader.run_job(job_id))
    return vod_db.get_download_job(job_id)


# ── settings ────────────────────────────────────────────────────────────────

def test_downloads_are_off_by_default(db):
    s = config.get_download_settings()
    assert s["enabled"] is False and s["default_preset"] == "original" and s["max_concurrent"] == 1


def test_settings_validation(db, tmp_path):
    ok = {"output_dir": str(tmp_path / "x"), "default_preset": "original"}
    assert vod_downloader.validate_settings(ok) is None
    assert "absolute" in vod_downloader.validate_settings({"output_dir": "relative/dir"})
    assert vod_downloader.validate_settings({"output_dir": ""})
    assert vod_downloader.validate_settings({**ok, "default_preset": "bogus"})
    config.save_strm_export_settings({"output_dir": str(tmp_path / "x")})
    assert "different folder" in vod_downloader.validate_settings(ok)


def test_queue_route_refuses_while_disabled(db):
    with pytest.raises(HTTPException) as e:
        asyncio.run(vod_routes.queue_downloads(vod_routes.DownloadQueueRequest(movie_ids=[1])))
    assert e.value.status_code == 400 and "turned off" in e.value.detail


# ── naming / queueing ───────────────────────────────────────────────────────

def test_movie_and_episode_paths(db, server):
    mid, pid = _movie(db, server, name="Some Movie (2001)", year=2001)
    rel, stem, _ = vod_downloader.target_for({"kind": "movie", "item_id": mid})
    assert rel == "Some Movie (2001) [tmdbid-777]" and stem == "Some Movie (2001)"

    sid = db.upsert_series("Great Show", 2015)
    eid = db.add_episode(sid, 2, 5, "x")
    rel, stem, _ = vod_downloader.target_for({"kind": "episode", "item_id": eid})
    assert rel == "Great Show (2015)/Season 02" and stem == "Great Show - S02E05"


def test_enqueue_dedupes_and_counts(db, server, dl):
    mid, pid = _movie(db, server)
    no_src = db.upsert_movie("Orphan", 1999)
    r = vod_downloader.enqueue(movie_ids=[mid, no_src])
    assert r["queued"] == 1 and r["no_source"] == 1
    again = vod_downloader.enqueue(movie_ids=[mid])
    assert again["queued"] == 0 and again["already_queued"] == 1


def test_enqueue_series_by_season_and_exact_source_rules(db, server, dl):
    pid = db.upsert_provider("p1", server, "u", "pw")
    sid = db.upsert_series("Show", 2020)
    for season, num in ((1, 1), (1, 2), (2, 1)):
        db.add_episode_source(db.add_episode(sid, season, num, f"e{season}{num}"), pid, f"{season}{num}", "mp4")
    assert vod_downloader.enqueue(series_ids=[sid], season=1)["queued"] == 2
    assert vod_downloader.enqueue(series_ids=[sid])["queued"] == 1  # only season 2 is new

    mid, _ = _movie(db, server, pname="p2")
    other, _ = _movie(db, server, stream_id="101", name="Other", pname="p3")
    src_other = db._connect().execute("SELECT id FROM movie_sources WHERE movie_id=?", (other,)).fetchone()[0]
    with pytest.raises(ValueError):  # a source that belongs to a different movie
        vod_downloader.enqueue(movie_ids=[mid], source_id=src_other)
    with pytest.raises(ValueError):  # exact source for several titles makes no sense
        vod_downloader.enqueue(movie_ids=[mid, other], source_id=src_other)


def test_reencode_preset_needs_ffmpeg(db, server, dl, monkeypatch):
    mid, _ = _movie(db, server)
    monkeypatch.setattr(vod_downloader, "ffmpeg_available", lambda: False)
    with pytest.raises(ValueError, match="ffmpeg"):
        vod_downloader.enqueue(movie_ids=[mid], preset="h264_720")


def test_ffmpeg_command_shape():
    args = vod_downloader.build_ffmpeg_args("http://h/movie/u/p/1.mp4", __import__("pathlib").Path("/o/x.mkv.part"), "hevc_720", "UA", True)
    joined = " ".join(args)
    assert "libx265" in joined and "-crf 28" in joined and "min(720,ih)" in joined and "-reconnect 1" in joined
    assert args[args.index("-i") + 1].startswith("http://h/movie") and "-f matroska" in joined and args[-1].endswith("x.mkv.part")
    local = vod_downloader.build_ffmpeg_args("/media/a.avi", __import__("pathlib").Path("/o/y.mkv.part"), "h264_1080", None, False)
    assert "-reconnect" not in local and "libx264" in " ".join(local)


# ── the transfer ────────────────────────────────────────────────────────────

def test_download_writes_library_file(db, server, dl):
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "done" and job["progress"] == 1.0
    target = dl / "Movies" / "Some Movie (2001) [tmdbid-777]" / "Some Movie (2001).mp4"
    assert target.read_bytes() == FILE_BYTES
    assert job["final_path"].replace("\\", "/") == "Some Movie (2001) [tmdbid-777]/Some Movie (2001).mp4"
    assert not list(target.parent.glob("*.part"))


def test_second_run_finds_existing_file(db, server, dl):
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    _run(1)
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(2)
    assert job["status"] == "done" and job["note"] == "Already in the library"


def test_strm_stub_does_not_count_as_downloaded(db, server, dl):
    mid, _ = _movie(db, server)
    folder = dl / "Movies" / "Some Movie (2001) [tmdbid-777]"
    folder.mkdir(parents=True)
    (folder / "Some Movie (2001).strm").write_text("http://x")
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["note"] != "Already in the library"
    assert (folder / "Some Movie (2001).mp4").exists()


def test_partial_file_is_resumed(db, server, dl):
    mid, _ = _movie(db, server)
    folder = dl / "Movies" / "Some Movie (2001) [tmdbid-777]"
    work = dl / "Movies" / ".processing" / "job-1"
    work.mkdir(parents=True)
    (work / "Some Movie (2001).mp4.part").write_bytes(FILE_BYTES[:500_000])
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done"
    assert (folder / "Some Movie (2001).mp4").read_bytes() == FILE_BYTES


def test_episode_download_uses_series_path(db, server, dl):
    pid = db.upsert_provider("p1", server, "u", "pw")
    sid = db.upsert_series("Great Show", 2015)
    eid = db.add_episode(sid, 1, 3, "Third")
    db.add_episode_source(eid, pid, "55", "mp4")
    vod_downloader.enqueue(episode_ids=[eid])
    assert _run(1)["status"] == "done"
    assert (dl / "Shows" / "Great Show (2015)" / "Season 01" / "Great Show - S01E03.mp4").read_bytes() == FILE_BYTES


def test_fails_over_to_the_next_source_and_exact_source_does_not(db, server, dl):
    mid, p1 = _movie(db, server, stream_id="gone", pname="bad")
    p2 = db.upsert_provider("good", server, "u", "pw")
    db.add_movie_source(mid, p2, "200", "mp4")
    vod_downloader.enqueue(movie_ids=[mid], )
    job = _run(1)
    assert job["status"] == "done" and job["provider_name"] == "good"

    # pinning the bad source must NOT fall over to the good one
    mid2, _ = _movie(db, server, stream_id="gone", name="Pinned", tmdb=None, pname="bad2")
    p3 = db.upsert_provider("good2", server, "u", "pw")
    db.add_movie_source(mid2, p3, "201", "mp4")
    bad_src = db._connect().execute("SELECT id FROM movie_sources WHERE movie_id=? AND provider_stream_id='gone'", (mid2,)).fetchone()[0]
    vod_downloader.enqueue(movie_ids=[mid2], source_id=bad_src)
    assert _run(2)["status"] == "failed"


def test_prefer_provider_orders_sources(db, server, dl):
    mid, p1 = _movie(db, server, stream_id="100", pname="first")
    p2 = db.upsert_provider("second", server, "u", "pw")
    db.add_movie_source(mid, p2, "200", "mp4")
    vod_downloader.enqueue(movie_ids=[mid], prefer_provider_id=p2)
    assert _run(1)["provider_name"] == "second"


def test_failure_message_never_contains_the_login(db, server, dl):
    mid, _ = _movie(db, server, stream_id="gone")
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "failed" and "404" in job["error"]
    assert "secretpw" not in job["error"] and "/u/" not in job["error"]


def test_html_instead_of_video_is_rejected(db, server, dl):
    mid, _ = _movie(db, server, stream_id="html")
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "failed" and "web page" in job["error"]
    assert not list(dl.rglob("*.mp4"))


def test_free_space_limit_blocks_the_download(db, server, dl):
    config.save_download_settings({"min_free_gb": 10 ** 9})
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "failed" and "free space" in job["error"].lower()


def test_provider_busy_requeues_instead_of_failing(db, server, dl):
    mid, pid = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    vod_downloader._provider_active[pid] = 1
    job = _run(1)
    assert job["status"] == "queued" and "Waiting" in job["note"] and job["next_try_at"] > time.time()


def test_cancel_a_running_download_removes_the_partial(db, server, dl):
    mid, _ = _movie(db, server, stream_id="slow")
    vod_downloader.enqueue(movie_ids=[mid])

    async def scenario():
        task = asyncio.create_task(vod_downloader.run_job(1))
        for _ in range(100):
            await asyncio.sleep(0.1)
            job = vod_db.get_download_job(1)
            if job["status"] == "running" and list(dl.rglob("*.part")) and job["bytes_done"] > 0:
                break
        vod_downloader.cancel([1])
        await asyncio.wait_for(task, 20)

    asyncio.run(scenario())
    assert vod_db.get_download_job(1)["status"] == "cancelled"
    assert not list(dl.rglob("*.part")) and not list(dl.rglob("*.mp4"))


def test_retry_after_cancel_actually_runs(db, server, dl):
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    vod_downloader.cancel([1])
    assert vod_db.get_download_job(1)["status"] == "cancelled"
    assert vod_db.retry_download_job(1)
    assert _run(1)["status"] == "done"


def test_local_library_file_is_copied(db, dl, tmp_path):
    src = tmp_path / "source.mkv"
    src.write_bytes(FILE_BYTES)
    pid = db.upsert_provider("lib", "", "", "", provider_type="library")
    mid = db.upsert_movie("Local Film", 2010)
    db.add_movie_source(mid, pid, "k", "mkv", local_file_path=str(src))
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "done"
    assert (dl / "Movies" / "Local Film (2010)" / "Local Film (2010).mkv").read_bytes() == FILE_BYTES


def test_interrupted_downloads_go_back_in_the_queue(db, server, dl):
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    vod_db.update_download_job(1, status="running")
    assert vod_db.requeue_interrupted_downloads() == 1
    assert vod_db.get_download_job(1)["status"] == "queued"


def test_clear_only_removes_finished_rows(db, server, dl):
    mid, _ = _movie(db, server)
    other, _ = _movie(db, server, stream_id="101", name="Two", tmdb=None, pname="p9")
    vod_downloader.enqueue(movie_ids=[mid, other])
    _run(1)
    assert vod_db.clear_finished_downloads() == 1
    assert [j["id"] for j in vod_db.list_download_jobs()] == [2]

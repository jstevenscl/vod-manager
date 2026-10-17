"""Download pacing: per-provider overrides of the global defaults, the speed cap, backing off when a
provider pushes back, the rest between downloads, the time window, and the two-phase re-encode that
frees the provider connection before the CPU-heavy part."""

import asyncio
import time

import vod_db
import vod_downloader
import vod_routes

# fixtures and helpers shared with the main download tests
from test_downloads import FILE_BYTES, _movie, _run, dl, server  # noqa: F401


def test_provider_policy_uses_override_else_global(db):
    g = {"speed_limit_mbps": 40, "provider_concurrent": 2, "pause_seconds": 15}
    assert vod_downloader.provider_policy({}, g) == {"speed_mbps": 40, "max_concurrent": 2, "pause_seconds": 15}
    own = {"download_speed_limit_mbps": 0, "download_max_concurrent": 1, "download_pause_seconds": 60}
    assert vod_downloader.provider_policy(own, g) == {"speed_mbps": 0, "max_concurrent": 1, "pause_seconds": 60}  # 0 = explicitly unlimited


def test_provider_limits_route_and_null_means_global(db, server):
    pid = db.upsert_provider("p1", server, "u", "pw")
    asyncio.run(vod_routes.set_provider_download_limits(pid, vod_routes.ProviderDownloadLimits(speed_limit_mbps=8, max_concurrent=9, pause_seconds=5)))
    p = vod_db.get_provider(pid)
    assert (p["download_speed_limit_mbps"], p["download_max_concurrent"], p["download_pause_seconds"]) == (8, 4, 5)  # concurrency clamped
    asyncio.run(vod_routes.set_provider_download_limits(pid, vod_routes.ProviderDownloadLimits()))
    p = vod_db.get_provider(pid)
    assert p["download_speed_limit_mbps"] is None and p["download_pause_seconds"] is None


def test_speed_cap_slows_the_transfer(db, server, dl):
    mid, pid = _movie(db, server)
    db.set_provider_download_limits(pid, 16, None, None)  # 16 Mbit/s = 2 MB/s, the file is 2 MiB
    vod_downloader.enqueue(movie_ids=[mid])
    t = time.monotonic()
    assert _run(1)["status"] == "done"
    assert time.monotonic() - t >= 0.8, "a capped download must not run at full speed"


def test_uncapped_download_is_fast(db, server, dl):
    mid, pid = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    t = time.monotonic()
    assert _run(1)["status"] == "done"
    assert time.monotonic() - t < 3


def test_rate_limit_answer_backs_off_instead_of_failing(db, server, dl):
    mid, pid = _movie(db, server, stream_id="limited")
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "queued" and "429" in job["note"] and job["next_try_at"] > time.time() + 60
    assert vod_downloader._pushbacks[pid] == 1 and vod_downloader._cooldown_until[pid] > time.time()
    # while the provider is cooling down, nothing else starts on it
    other = db.upsert_movie("Other", 2002)
    db.add_movie_source(other, pid, "100", "mp4")
    vod_downloader.enqueue(movie_ids=[other])
    assert "cooling down" in _run(2)["note"]


def test_provider_that_keeps_refusing_is_given_up_on(db, server, dl):
    mid, pid = _movie(db, server, stream_id="limited")
    vod_downloader.enqueue(movie_ids=[mid])
    for _ in range(vod_downloader._PUSHBACK_GIVE_UP):
        vod_downloader._cooldown_until.clear()
        vod_db.update_download_job(1, status="queued", next_try_at=0)
        job = _run(1)
    assert job["status"] == "failed" and "keeps refusing" in job["error"]


def test_success_clears_the_pushback_count(db, server, dl):
    mid, pid = _movie(db, server)
    vod_downloader._pushbacks[pid] = 3
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done" and pid not in vod_downloader._pushbacks


def test_pause_between_downloads_on_one_provider(db, server, dl):
    mid, pid = _movie(db, server)
    db.set_provider_download_limits(pid, None, None, 45)
    other = db.upsert_movie("Other", 2002)
    db.add_movie_source(other, pid, "101", "mp4")
    vod_downloader.enqueue(movie_ids=[mid, other])
    assert _run(1)["status"] == "done"
    second = _run(2)
    assert second["status"] == "queued" and "cooling down" in second["note"]


def test_provider_concurrency_override(db, server, dl):
    mid, pid = _movie(db, server)
    vod_downloader._provider_active[pid] = 1
    vod_downloader.enqueue(movie_ids=[mid])
    assert "busy with another download" in _run(1)["note"]
    vod_db.update_download_job(1, status="queued", next_try_at=0)
    db.set_provider_download_limits(pid, None, 2, None)  # this provider tolerates two at once
    assert _run(1)["status"] == "done"


def test_time_window(db):
    def at(hour):
        return time.mktime((2026, 5, 4, hour, 30, 0, 0, 0, -1))

    off = {"window_enabled": False, "window_start": 1, "window_end": 7}
    assert vod_downloader.in_window(off, at(12))
    day = {"window_enabled": True, "window_start": 9, "window_end": 17}
    assert vod_downloader.in_window(day, at(9)) and not vod_downloader.in_window(day, at(17))
    night = {"window_enabled": True, "window_start": 22, "window_end": 6}  # wraps midnight
    assert vod_downloader.in_window(night, at(23)) and vod_downloader.in_window(night, at(3)) and not vod_downloader.in_window(night, at(12))


# ── re-encode: the connection is released before the encode; library files are never touched ──

def _fake_encoder(monkeypatch, seen):
    async def fake(src, part, preset, ua, duration, ctx, **kw):
        seen["src"] = src
        seen["connections_held"] = dict(vod_downloader.xc_server._active_downloads)
        part.write_bytes(b"x" * 4096)
        return 4096
    monkeypatch.setattr(vod_downloader, "_run_ffmpeg", fake)
    monkeypatch.setattr(vod_downloader, "ffmpeg_available", lambda: True)


def test_reencode_fetches_first_then_encodes_without_holding_the_connection(db, server, dl, monkeypatch):
    seen = {}
    _fake_encoder(monkeypatch, seen)
    mid, pid = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid], preset="h264_720")
    job = _run(1)
    folder = dl / "Movies" / "Some Movie (2001) [tmdbid-777]"
    assert job["status"] == "done" and (folder / "Some Movie (2001).mkv").read_bytes() == b"x" * 4096
    assert seen["src"].endswith(".mp4.src"), "the encode reads our own local copy, not the provider"
    assert not any(seen["connections_held"].values()), "the provider connection must be free while encoding"
    assert not list(folder.glob("*.src")) and not list(folder.glob("*.part")) and not list(folder.glob("*.mp4"))


def test_reencode_of_a_library_file_never_deletes_or_copies_it(db, dl, tmp_path, monkeypatch):
    seen = {}
    _fake_encoder(monkeypatch, seen)
    src = tmp_path / "keep.mkv"
    src.write_bytes(FILE_BYTES)
    pid = db.upsert_provider("lib", "", "", "", provider_type="library")
    mid = db.upsert_movie("Local Film", 2010)
    db.add_movie_source(mid, pid, "k", "mkv", local_file_path=str(src))
    vod_downloader.enqueue(movie_ids=[mid], preset="hevc_720")
    assert _run(1)["status"] == "done"
    assert seen["src"] == str(src) and src.read_bytes() == FILE_BYTES


def test_reencode_resumes_from_a_fetched_source_after_a_restart(db, server, dl, monkeypatch):
    seen = {}
    _fake_encoder(monkeypatch, seen)
    mid, pid = _movie(db, server, stream_id="gone")  # the provider would 404 if asked again
    work = dl / "Movies" / ".processing" / "job-1"
    work.mkdir(parents=True)
    (work / "Some Movie (2001).mp4.src").write_bytes(FILE_BYTES)
    vod_downloader.enqueue(movie_ids=[mid], preset="h264_1080")
    assert _run(1)["status"] == "done" and seen["src"].endswith(".mp4.src")


def test_failed_validation_rejects_the_file(db, server, dl, monkeypatch):
    async def bad(path):
        raise vod_downloader.DownloadError("The downloaded file isn't a playable video (it failed the ffprobe check).")
    monkeypatch.setattr(vod_downloader, "_validate_media", bad)
    mid, pid = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "failed" and "ffprobe" in job["error"]
    assert not list(dl.rglob("*.mp4"))

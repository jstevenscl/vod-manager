"""Unfinished downloads and encodes live in a processing folder; only a finished, verified file is moved
into the library, so nothing partial or corrupt is ever visible there."""

import asyncio

import config
import vod_db
import vod_downloader

from test_downloads import FILE_BYTES, _movie, _run, dl, server  # noqa: F401


def _library_files(root):
    return sorted(p for p in root.rglob("*") if p.is_file() and ".processing" not in p.parts)


def test_in_progress_files_never_appear_in_the_library(db, server, dl):
    mid, _ = _movie(db, server, stream_id="slow")          # 8 MiB trickle
    vod_downloader.enqueue(movie_ids=[mid])
    seen = {"work": [], "library": None}

    async def scenario():
        task = asyncio.create_task(vod_downloader.run_job(1))
        for _ in range(100):
            await asyncio.sleep(0.1)
            work = list((dl / "Movies" / ".processing").rglob("*.part"))
            if work and vod_db.get_download_job(1)["bytes_done"] > 0:
                seen["work"] = work
                seen["library"] = _library_files(dl)
                break
        await asyncio.wait_for(task, 30)

    asyncio.run(scenario())
    assert seen["work"], "the partial file is in the processing folder"
    assert seen["library"] == [], "the library folder has nothing in it while the download runs"
    final = dl / "Movies" / "Some Movie (2001) [tmdbid-777]" / "Some Movie (2001).mp4"
    assert vod_db.get_download_job(1)["status"] == "done" and final.read_bytes() == FILE_BYTES * 4
    assert _library_files(dl) == [final]
    assert not list((dl / "Movies" / ".processing").glob("job-*")), "the job's processing folder is cleaned up"


def test_an_explicit_processing_folder_is_used(db, server, dl, tmp_path):
    work = tmp_path / "scratch"
    config.save_download_settings({"work_dir": str(work)})
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done"
    assert not (dl / "Movies" / ".processing").exists()
    assert not list(work.glob("job-*")), "cleaned up after the move"
    assert (dl / "Movies" / "Some Movie (2001) [tmdbid-777]" / "Some Movie (2001).mp4").exists()


def test_moving_across_filesystems_goes_through_a_hidden_temporary_name(db, server, dl, monkeypatch):
    monkeypatch.setattr(vod_downloader, "_same_device", lambda a, b: False)
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    seen = []
    real_replace = vod_downloader.os.replace

    def spy(src, dst):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(vod_downloader.os, "replace", spy)
    assert _run(1)["status"] == "done"
    final = dl / "Movies" / "Some Movie (2001) [tmdbid-777]" / "Some Movie (2001).mp4"
    assert final.read_bytes() == FILE_BYTES
    hidden = [s for s, d in seen if d == str(final)]
    assert hidden and hidden[0].endswith(".Some Movie (2001).mp4.part"), "the copy was dotted-hidden until complete"
    assert _library_files(dl) == [final]


def test_a_corrupt_encode_never_reaches_the_library(db, server, dl, monkeypatch):
    async def fake_encode(src, part, preset, ua, duration, ctx, **kw):
        part.write_bytes(b"not really a video" * 100)
        return part.stat().st_size

    async def validate(path):
        if path.name.endswith(".mkv.part"):
            raise vod_downloader.DownloadError("The downloaded file isn't a playable video (it failed the ffprobe check).")

    monkeypatch.setattr(vod_downloader, "_run_ffmpeg", fake_encode)
    monkeypatch.setattr(vod_downloader, "_validate_media", validate)
    monkeypatch.setattr(vod_downloader, "ffmpeg_available", lambda: True)
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid], preset="h264_720")
    job = _run(1)
    assert job["status"] == "failed" and "ffprobe" in job["error"]
    assert _library_files(dl) == [], "a corrupt encode is never moved into the library"


def test_processing_folder_may_not_be_a_library_folder(db, tmp_path):
    s = {"output_dir": str(tmp_path / "dl"), "work_dir": str(tmp_path / "dl" / "Movies")}
    assert "processing folder must be different" in vod_downloader.validate_settings(s)
    assert vod_downloader.validate_settings({"output_dir": str(tmp_path / "dl"), "work_dir": str(tmp_path / "scratch")}) is None
    assert "absolute" in vod_downloader.validate_settings({"output_dir": str(tmp_path / "dl"), "work_dir": "scratch"})


def test_clearing_the_list_removes_leftover_processing_files(db, server, dl):
    mid, _ = _movie(db, server, stream_id="gone")           # fails, leaving its job folder behind for a retry
    vod_downloader.enqueue(movie_ids=[mid])
    work = dl / "Movies" / ".processing" / "job-1"
    work.mkdir(parents=True)
    (work / "Some Movie (2001).mp4.part").write_bytes(b"x" * 10)
    assert _run(1)["status"] == "failed"
    vod_downloader.purge_work([1])
    assert not work.exists()

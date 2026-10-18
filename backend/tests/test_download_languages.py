"""Audio-language selection and subtitle handling for downloads: the track planner, the ffmpeg command, trimming an
original without re-encoding, per-download overrides, and the retry without subtitles."""

import asyncio

import pytest

import config
import vod_db
import vod_downloader
import vod_routes
from vod_downloader import TrackPlan, parse_languages, plan_tracks

from test_downloads import FILE_BYTES, _movie, _run, dl, server  # noqa: F401


def S(index, kind, codec, lang=None, pic=False):
    return {"index": index, "codec_type": kind, "codec_name": codec, "language": lang, "attached_pic": pic}


MULTI = [S(0, "video", "h264"), S(1, "audio", "aac", "eng"), S(2, "audio", "ac3", "spa"), S(3, "audio", "aac", None),
         S(4, "subtitle", "subrip", "eng"), S(5, "subtitle", "subrip", "spa")]


# ── parsing and planning ────────────────────────────────────────────────────

def test_parse_languages_accepts_codes_and_names():
    assert parse_languages("eng, Japanese ; fr") == ["en", "ja", "fr"]
    assert parse_languages("EN,en,eng") == ["en"]
    for keep_all in ("", "all", "ALL", "*", "eng, all", None):
        assert parse_languages(keep_all) == []


def test_plan_filters_audio_and_subtitles_by_language():
    plan = plan_tracks(MULTI, ["en"], True)
    assert plan.video == 0 and plan.audio == [1, 3] and plan.subs == [(4, "subrip")] and plan.changed
    # the untagged track (3) is kept because it can't be ruled out


def test_plan_without_a_filter_keeps_everything_and_is_unchanged():
    plan = plan_tracks(MULTI, [], True)
    assert plan.audio == [1, 2, 3] and [i for i, _ in plan.subs] == [4, 5] and plan.changed is False


def test_subtitles_off_drops_them_even_without_a_filter():
    plan = plan_tracks(MULTI, [], False)
    assert plan.subs == [] and plan.changed is True


def test_no_match_falls_back_to_all_audio_so_the_file_is_never_silent():
    tracks = [S(0, "video", "h264"), S(1, "audio", "aac", "eng"), S(2, "audio", "aac", "spa")]
    assert plan_tracks(tracks, ["fr"], True).audio == [1, 2]


def test_cover_art_is_not_the_video_track():
    tracks = [S(0, "video", "mjpeg", pic=True), S(1, "video", "h264"), S(2, "audio", "aac", "eng")]
    assert plan_tracks(tracks, [], True).video == 1


# ── ffmpeg command ──────────────────────────────────────────────────────────

def test_reencode_command_maps_only_the_chosen_streams():
    plan = TrackPlan(video=0, audio=[1, 3], subs=[(4, "subrip"), (6, "mov_text")], changed=True)
    args = vod_downloader.build_ffmpeg_args("/in.mp4", __import__("pathlib").Path("/o.mkv.part"), "h264_720", None, False, plan)
    j = " ".join(args)
    assert "-map 0:0 -map 0:1 -map 0:3 -map 0:4 -map 0:6" in j
    assert "-c:v libx264" in j and "-c:a aac" in j
    assert "-c:s:0 copy" in j and "-c:s:1 srt" in j, "mov_text can't go into matroska as is, so it becomes srt"
    assert "-sn" not in args and "-disposition:a:0 default" in j


def test_trim_command_copies_streams_without_encoding():
    plan = TrackPlan(video=0, audio=[1], subs=[], changed=True)
    args = vod_downloader.build_ffmpeg_args("/in.mp4", __import__("pathlib").Path("/o.mkv.part"), "original", None, False, plan, copy=True)
    j = " ".join(args)
    assert "-c copy" in j and "libx264" not in j and "-vf" not in args and "-map 0:1" in j


def test_without_a_plan_the_old_behaviour_is_kept():
    args = vod_downloader.build_ffmpeg_args("/in.mp4", __import__("pathlib").Path("/o.mkv.part"), "h264_720", None, False)
    j = " ".join(args)
    assert "-map 0:v:0 -map 0:a? -sn -dn" in j


# ── end to end with a fake ffmpeg / ffprobe ─────────────────────────────────

@pytest.fixture()
def fakes(monkeypatch):
    calls = []

    async def probe(path):
        return MULTI

    async def run(src, part, preset, ua, duration, ctx, plan=None, copy=False, **kw):
        calls.append({"plan": plan, "copy": copy, "preset": preset, "src": str(src), **kw})
        if getattr(run, "fail_with_subs", False) and plan is not None and plan.subs:
            raise vod_downloader.DownloadError("Re-encode failed: Subtitle codec not supported")
        part.write_bytes(b"x" * 4096)
        return 4096

    monkeypatch.setattr(vod_downloader, "_probe_streams", probe)
    monkeypatch.setattr(vod_downloader, "_run_ffmpeg", run)
    monkeypatch.setattr(vod_downloader, "ffmpeg_available", lambda: True)
    run.calls = calls
    return run


def _mp4(dl):
    return dl / "Movies" / "Some Movie (2001) [tmdbid-777]" / "Some Movie (2001).mp4"


def _mkv(dl):
    return dl / "Movies" / "Some Movie (2001) [tmdbid-777]" / "Some Movie (2001).mkv"


def test_original_with_a_language_setting_is_trimmed_without_reencoding(db, server, dl, fakes):
    config.save_download_settings({"audio_languages": "eng"})
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done"
    (call,) = fakes.calls
    assert call["copy"] is True and call["plan"].audio == [1, 3] and call["src"].endswith(".mp4.src")
    assert _mkv(dl).exists() and not _mp4(dl).exists(), "a trimmed original is saved as mkv"


def test_original_with_no_language_setting_is_left_untouched(db, server, dl, fakes):
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done"
    assert fakes.calls == [] and _mp4(dl).read_bytes() == FILE_BYTES


def test_a_filter_that_removes_nothing_leaves_the_original_untouched(db, server, dl, fakes, monkeypatch):
    async def one_track(path):
        return [S(0, "video", "h264"), S(1, "audio", "aac", "eng")]
    monkeypatch.setattr(vod_downloader, "_probe_streams", one_track)
    config.save_download_settings({"audio_languages": "eng", "keep_subtitles": True})
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done"
    assert fakes.calls == [] and _mp4(dl).exists()


def test_a_per_download_language_overrides_the_setting(db, server, dl, fakes):
    config.save_download_settings({"audio_languages": "eng"})
    mid, _ = _movie(db, server)
    other = db.upsert_movie("Other", 2002)
    db.add_movie_source(other, 1, "101", "mp4")
    vod_downloader.enqueue(movie_ids=[mid], audio_languages="all")     # explicit "keep everything"
    assert _run(1)["status"] == "done" and fakes.calls == []
    assert vod_db.get_download_job(1)["audio_languages"] == "all"
    vod_downloader.enqueue(movie_ids=[other], audio_languages="spa")
    assert _run(2)["status"] == "done"
    assert fakes.calls[-1]["plan"].audio == [2, 3], "Spanish plus the untagged track"
    assert vod_db.get_download_job(2)["audio_languages"] == "spa"


def test_reencode_uses_the_language_plan(db, server, dl, fakes):
    config.save_download_settings({"audio_languages": "en", "keep_subtitles": False})
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid], preset="h264_720")
    assert _run(1)["status"] == "done"
    (call,) = fakes.calls
    assert call["copy"] is False and call["preset"] == "h264_720"
    assert call["plan"].audio == [1, 3] and call["plan"].subs == []


def test_an_unreadable_file_is_never_trimmed(db, server, dl, fakes, monkeypatch):
    async def nothing(path):
        return None
    monkeypatch.setattr(vod_downloader, "_probe_streams", nothing)
    config.save_download_settings({"audio_languages": "eng"})
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    assert _run(1)["status"] == "done" and fakes.calls == [] and _mp4(dl).exists()


def test_a_subtitle_failure_retries_without_subtitles(db, server, dl, fakes):
    fakes.fail_with_subs = True
    config.save_download_settings({"audio_languages": "eng", "keep_subtitles": True})
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "done"
    assert len(fakes.calls) == 2 and fakes.calls[0]["plan"].subs and fakes.calls[1]["plan"].subs == []
    assert _mkv(dl).exists()


def test_languages_need_ffmpeg_to_be_requested_per_download(db, server, dl, monkeypatch):
    monkeypatch.setattr(vod_downloader, "ffmpeg_available", lambda: False)
    mid, _ = _movie(db, server)
    with pytest.raises(ValueError, match="ffmpeg"):
        vod_downloader.enqueue(movie_ids=[mid], audio_languages="eng")
    assert vod_downloader.enqueue(movie_ids=[mid], audio_languages="all")["queued"] == 1   # "all" needs nothing


def test_settings_and_queue_routes_carry_the_language_options(db, server, dl, tmp_path, fakes):
    body = vod_routes.DownloadSettingsRequest(enabled=True, output_dir=str(tmp_path / "dl"), audio_languages="ENG, Jpn", keep_subtitles=False)
    out = asyncio.run(vod_routes.save_download_settings(body))
    assert out["settings"]["audio_languages"] == "eng, jpn" and out["settings"]["keep_subtitles"] is False
    mid, _ = _movie(db, server)
    r = asyncio.run(vod_routes.queue_downloads(vod_routes.DownloadQueueRequest(movie_ids=[mid], audio_languages="fra")))
    assert r["queued"] == 1 and vod_db.get_download_job(1)["audio_languages"] == "fra"

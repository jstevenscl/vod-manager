"""Hardware encoding options, the CPU fallback, the progress bar for files with no duration, and the page-error text."""

import asyncio
import os
from pathlib import Path

import pytest

import config
import vod_db
import vod_downloader
import vod_routes
from vod_downloader import build_ffmpeg_args, resolve_encoder

from test_downloads import FILE_BYTES, _movie, _run, dl, server  # noqa: F401


def args(preset="h264_720", **kw):
    return build_ffmpeg_args("/in.mkv", Path("/o.mkv.part"), preset, None, False, **kw)


# ── command lines ───────────────────────────────────────────────────────────

def test_software_is_the_default_and_threads_can_be_capped():
    j = " ".join(args())
    assert "-c:v libx264" in j and "-pix_fmt yuv420p" in j and "-threads" not in j
    assert "-threads 4" in " ".join(args(threads=4))


def test_vaapi_uses_the_render_device_and_the_gpu_encoder():
    a = args(engine="vaapi", device="/dev/dri/renderD129")
    j = " ".join(a)
    assert "-init_hw_device vaapi=hw:/dev/dri/renderD129" in j and "-hwaccel vaapi" in j
    assert "format=nv12,hwupload" in j and "-c:v h264_vaapi -qp 24" in j and "libx264" not in j
    assert a.index("-hwaccel") < a.index("-i"), "decoder options must come before the input"
    assert "hevc_vaapi" in " ".join(args("hevc_720", engine="vaapi", device="/dev/dri/renderD128"))


def test_qsv_goes_through_vaapi_device_init():
    j = " ".join(args(engine="qsv", device="/dev/dri/renderD128"))
    assert "vaapi=va:/dev/dri/renderD128" in j and "qsv=hw@va" in j and "-c:v h264_qsv -global_quality 24" in j


def test_nvenc_needs_no_device_path():
    j = " ".join(args(engine="nvenc"))
    assert "-hwaccel cuda" in j and "-c:v h264_nvenc" in j and "-cq 24" in j and "-vf" in j


def test_trimming_without_reencoding_never_uses_the_gpu():
    j = " ".join(args(copy=True, engine="vaapi", device="/dev/dri/renderD128"))
    assert "-c copy" in j and "vaapi" not in j


# ── choosing the engine ─────────────────────────────────────────────────────

@pytest.fixture()
def hw(monkeypatch):
    state = {"render_devices": [], "nvidia": False}
    monkeypatch.setattr(vod_downloader, "detect_hardware", lambda: state)
    monkeypatch.setattr(vod_downloader.os.path, "exists", lambda p: p in state["render_devices"])
    return state


def test_auto_prefers_a_render_device_then_nvidia_then_cpu(hw):
    assert resolve_encoder({"encoder": "auto"}) == ("software", None, None)
    hw["nvidia"] = True
    assert resolve_encoder({"encoder": "auto"}) == ("nvenc", None, None)
    hw["render_devices"] = ["/dev/dri/renderD128"]
    assert resolve_encoder({"encoder": "auto"}) == ("vaapi", "/dev/dri/renderD128", None)


def test_a_missing_gpu_degrades_to_the_cpu_with_a_reason(hw):
    eng, dev, why = resolve_encoder({"encoder": "vaapi"})
    assert eng == "software" and dev is None and "isn't available" in why
    eng, _, why = resolve_encoder({"encoder": "nvenc"})
    assert eng == "software" and "NVIDIA" in why


# ── fallback chain in a real job ────────────────────────────────────────────

def _fake_ffmpeg(monkeypatch, fail_engines=()):
    calls = []

    async def run(src, part, preset, ua, duration, ctx, plan=None, copy=False, engine="software", device=None, threads=0):
        calls.append({"engine": engine, "duration": duration, "note": ctx.note})
        if engine in fail_engines:
            raise vod_downloader.DownloadError("Re-encode failed: Cannot load libva")
        part.write_bytes(b"x" * 4096)
        return 4096

    async def noprobe(path):
        return None

    async def duration(path):
        return 3600.0

    monkeypatch.setattr(vod_downloader, "_run_ffmpeg", run)
    monkeypatch.setattr(vod_downloader, "_probe_streams", noprobe)
    monkeypatch.setattr(vod_downloader, "_probe_duration", duration)
    monkeypatch.setattr(vod_downloader, "ffmpeg_available", lambda: True)
    return calls


def test_a_failing_gpu_encoder_falls_back_to_the_cpu(db, server, dl, hw, monkeypatch):
    hw["render_devices"] = ["/dev/dri/renderD128"]
    calls = _fake_ffmpeg(monkeypatch, fail_engines=("vaapi",))
    config.save_download_settings({"encoder": "vaapi"})
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid], preset="h264_720")
    assert _run(1)["status"] == "done"
    assert [c["engine"] for c in calls] == ["vaapi", "software"]
    assert "using the CPU" in calls[1]["note"]


def test_a_working_gpu_encoder_is_used_and_named(db, server, dl, hw, monkeypatch):
    hw["render_devices"] = ["/dev/dri/renderD128"]
    calls = _fake_ffmpeg(monkeypatch)
    config.save_download_settings({"encoder": "auto"})
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid], preset="h264_720")
    assert _run(1)["status"] == "done"
    assert [c["engine"] for c in calls] == ["vaapi"] and "VAAPI" in calls[0]["note"]


def test_unknown_runtime_is_probed_from_the_file_so_the_bar_can_move(db, server, dl, monkeypatch):
    calls = _fake_ffmpeg(monkeypatch)
    mid, _ = _movie(db, server)
    vod_downloader.enqueue(movie_ids=[mid], preset="h264_720")
    assert _run(1)["status"] == "done"
    assert calls[0]["duration"] == 3600.0


def test_progress_reading_is_harmless_without_proc(tmp_path):
    assert vod_downloader._read_fraction(999999, str(tmp_path / "x.mkv"), 100) is None


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs Linux /proc")
def test_progress_follows_the_input_file_position(tmp_path):
    f = tmp_path / "in.bin"
    f.write_bytes(b"0" * 1000)
    with open(f, "rb") as fh:
        fh.read(250)
        assert vod_downloader._read_fraction(os.getpid(), str(f), 1000) == pytest.approx(0.25)


# ── page errors ─────────────────────────────────────────────────────────────

def test_a_page_error_says_what_the_provider_said(db, server, dl):
    mid, _ = _movie(db, server, stream_id="html")
    vod_downloader.enqueue(movie_ids=[mid])
    err = _run(1)["error"]
    assert "web page instead of a video file" in err and "HTTP 200" in err and "login expired" in err
    assert "secretpw" not in err and "<html>" not in err


def test_a_connection_limit_page_waits_instead_of_failing(db, server, dl):
    mid, _ = _movie(db, server, stream_id="busy")
    vod_downloader.enqueue(movie_ids=[mid])
    job = _run(1)
    assert job["status"] == "queued" and "Maximum connections" in (job["note"] or "")


# ── settings + test route ───────────────────────────────────────────────────

def test_settings_round_trip_and_validation(db, tmp_path):
    body = vod_routes.DownloadSettingsRequest(enabled=True, output_dir=str(tmp_path / "dl"), encoder="nvenc",
                                              hw_device="/dev/dri/renderD129", encode_threads=500)
    out = asyncio.run(vod_routes.save_download_settings(body))
    assert out["settings"]["encoder"] == "nvenc" and out["settings"]["encode_threads"] == 64
    assert "hardware" in out
    with pytest.raises(Exception, match="Unknown encoder"):
        asyncio.run(vod_routes.save_download_settings(vod_routes.DownloadSettingsRequest(encoder="quantum")))
    with pytest.raises(Exception, match="/dev/"):
        asyncio.run(vod_routes.save_download_settings(vod_routes.DownloadSettingsRequest(hw_device="renderD128")))


def test_the_test_button_reports_a_missing_gpu(db, hw, monkeypatch):
    monkeypatch.setattr(vod_downloader, "ffmpeg_available", lambda: True)
    r = asyncio.run(vod_routes.test_download_encoder(vod_routes.TestEncoderRequest(encoder="vaapi")))
    assert r["ok"] is False and "isn't available" in r["message"]

"""A download and a stream must never share a provider connection that only has room for one:
a download can't start while a stream holds it, and a stream can't start while a download does.
A provider with no declared limit is treated as having a single connection for this."""

import asyncio

import pytest

import config
import vod_db
import vod_downloader
import xc_server


@pytest.fixture(autouse=True)
def _clean_counters():
    for d in (xc_server._active_vod_streams, xc_server._active_downloads,
              xc_server._active_sub_account_streams, xc_server._active_sub_account_downloads):
        d.clear()
    vod_downloader._provider_active.clear()
    vod_downloader._cooldown_until.clear()
    vod_downloader._pushbacks.clear()
    yield
    for d in (xc_server._active_vod_streams, xc_server._active_downloads,
              xc_server._active_sub_account_streams, xc_server._active_sub_account_downloads):
        d.clear()


def _provider(db, name="p", max_streams=0):
    pid = db.upsert_provider(name, "http://x", "u", "p", max_streams=max_streams)
    return vod_db.get_provider(pid)


def _stream(p):
    return asyncio.run(xc_server._try_reserve_capacity(p))


def _download(p):
    return asyncio.run(xc_server._try_reserve_capacity(p, True))


def test_single_connection_is_exclusive_in_both_directions(db):
    p = _provider(db, max_streams=1)
    assert _download(p)
    assert not _stream(p), "a viewer must not start while a download holds the only connection"
    xc_server._release_capacity(p, None, is_download=True)

    assert _stream(p)
    assert not _download(p), "a download must not start while a viewer holds the only connection"
    xc_server._release_capacity(p, None)
    assert _download(p)


def test_unknown_limit_is_treated_as_one_connection(db):
    p = _provider(db, max_streams=0)
    assert _stream(p)
    assert not _download(p)
    xc_server._release_capacity(p)
    assert _download(p)
    assert not _stream(p)
    assert not _download(p), "two downloads would also need two connections"


def test_two_connections_allow_one_of_each_but_no_more(db):
    p = _provider(db, max_streams=2)
    assert _download(p)
    assert _stream(p)
    assert not _stream(p), "a second viewer would exceed the account's two connections"
    assert not _download(p)
    xc_server._release_capacity(p)
    xc_server._release_capacity(p, None, is_download=True)
    assert _download(p) and _stream(p)


def test_streams_without_a_download_are_not_newly_limited(db):
    p = _provider(db, max_streams=1)
    assert _stream(p) and _stream(p) and _stream(p)  # unchanged: stream-vs-stream isn't coordinated here


def test_live_tv_viewers_count_against_a_download(db, monkeypatch):
    p = _provider(db, max_streams=3)
    vod_db.set_provider_shared_limit(p["id"], 2)
    p = vod_db.get_provider(p["id"])
    monkeypatch.setattr(vod_db, "list_provider_live_accounts", lambda pid: [{"dispatcharr_connection_id": 1, "dispatcharr_account_id": 9, "dispatcharr_profile_id": None}])
    monkeypatch.setattr(vod_db, "get_dispatcharr_connection", lambda cid: {"id": 1, "url": "http://d", "token": "t", "label": "d"})

    async def one_live(*a, **k):
        return 1
    monkeypatch.setattr(xc_server, "_live_viewer_count", one_live)

    assert _download(p)            # 1 live viewer + this download = 2 of 2
    assert not _stream(p)          # nothing left for a stream
    xc_server._release_capacity(p, None, is_download=True)
    assert _stream(p)
    assert not _download(p)


def test_sub_accounts_are_gated_per_login(db):
    p = _provider(db, max_streams=1)
    db.create_provider_sub_account(p["id"], "second", "u2", "p2", max_streams=1)

    first = asyncio.run(xc_server._select_upstream_credentials(p, is_download=True))
    second = asyncio.run(xc_server._select_upstream_credentials(p, is_download=True))
    assert first["sub_account_id"] is None and second["sub_account_id"] is not None
    assert asyncio.run(xc_server._select_upstream_credentials(p)) is None, "both logins busy: no viewer can start"

    xc_server._release_capacity(p, second["sub_account_id"], is_download=True)
    viewer = asyncio.run(xc_server._select_upstream_credentials(p))
    assert viewer["sub_account_id"] == second["sub_account_id"]
    assert asyncio.run(xc_server._select_upstream_credentials(p, is_download=True)) is None


def test_download_job_waits_while_a_viewer_is_streaming(db, tmp_path):
    config.save_download_settings({"enabled": True, "output_dir": str(tmp_path / "lib"), "min_free_gb": 0})
    pid = db.upsert_provider("p1", "http://127.0.0.1:9", "u", "pw", max_streams=1)
    mid = db.upsert_movie("A Movie", 2000)
    db.add_movie_source(mid, pid, "1", "mp4")
    vod_downloader.enqueue(movie_ids=[mid])
    xc_server._active_vod_streams[pid] = 1  # someone is watching

    asyncio.run(vod_downloader.run_job(1))
    job = vod_db.get_download_job(1)
    assert job["status"] == "queued" and "in use by playback" in job["note"]
    assert not xc_server._active_downloads.get(pid), "the failed attempt must not leave a connection reserved"


def test_a_finished_download_frees_the_connection(db, tmp_path, monkeypatch):
    config.save_download_settings({"enabled": True, "output_dir": str(tmp_path / "lib"), "min_free_gb": 0})
    pid = db.upsert_provider("p1", "http://127.0.0.1:9", "u", "pw", max_streams=1)
    mid = db.upsert_movie("A Movie", 2000)
    db.add_movie_source(mid, pid, "1", "mp4")
    vod_downloader.enqueue(movie_ids=[mid])

    async def boom(*a, **k):
        raise vod_downloader.DownloadError("provider said no")
    monkeypatch.setattr(vod_downloader, "_download_http", boom)
    asyncio.run(vod_downloader.run_job(1))
    assert vod_db.get_download_job(1)["status"] == "failed"
    assert not xc_server._active_downloads.get(pid) and not vod_downloader._provider_active.get(pid)
    assert _stream(vod_db.get_provider(pid)), "a viewer can start again once the download is over"

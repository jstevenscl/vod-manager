"""A viewer always wins: when playback needs a connection a download is using, the download
pauses (keeping its partial file) and resumes by itself once the connection is free."""

import asyncio

import vod_db
import vod_downloader
import xc_server

from test_downloads import FILE_BYTES, _movie, dl, server  # noqa: F401


def _reset():
    for d in (xc_server._active_vod_streams, xc_server._active_downloads,
              xc_server._active_sub_account_streams, xc_server._active_sub_account_downloads):
        d.clear()
    vod_downloader._preempt_ids.clear()
    vod_downloader._job_provider.clear()


def test_a_viewer_pauses_a_running_download_which_then_resumes(db, server, dl):
    _reset()
    mid, pid = _movie(db, server, stream_id="slow")          # 8 MiB, trickles out
    db.set_provider_download_limits(pid, None, None, 0)
    vod_downloader.enqueue(movie_ids=[mid])
    provider = vod_db.get_provider(pid)
    # one connection only
    conn = vod_db._connect()
    conn.execute("UPDATE providers SET max_streams=1 WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    provider = vod_db.get_provider(pid)

    async def scenario():
        task = asyncio.create_task(vod_downloader.run_job(1))
        for _ in range(100):
            await asyncio.sleep(0.1)
            if vod_db.get_download_job(1)["bytes_done"] > 0:
                break
        assert vod_db.get_download_job(1)["status"] == "running"
        assert await xc_server._try_reserve_capacity(provider) is False, "the plain gate still refuses"
        viewer = await xc_server._select_credentials_preempting_downloads(provider)
        await asyncio.wait_for(task, 20)
        return viewer

    viewer = asyncio.run(scenario())
    assert viewer is not None, "the viewer got the connection"
    job = vod_db.get_download_job(1)
    assert job["status"] == "queued" and "Paused for a viewer" in job["note"]
    assert not xc_server._active_downloads.get(pid), "the download let go of the connection"
    part = list(dl.rglob("*.part"))
    assert part and part[0].stat().st_size > 0, "the partial file is kept so it can resume"

    # the viewer leaves; the download carries on from the partial file and the bytes match
    xc_server._release_capacity(provider, viewer["sub_account_id"])
    vod_db.update_download_job(1, next_try_at=0)
    asyncio.run(vod_downloader.run_job(1))
    job = vod_db.get_download_job(1)
    assert job["status"] == "done"
    final = dl / "Movies" / "Some Movie (2001) [tmdbid-777]" / "Some Movie (2001).mp4"
    assert final.read_bytes() == FILE_BYTES * 4


def test_no_download_means_no_waiting_and_no_pause(db, server, dl):
    _reset()
    mid, pid = _movie(db, server)
    conn = vod_db._connect()
    conn.execute("UPDATE providers SET max_streams=1 WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    provider = vod_db.get_provider(pid)
    assert asyncio.run(xc_server._select_credentials_preempting_downloads(provider)) is not None   # free: plain reserve
    # a second viewer is not gated against the first (unchanged); with no download involved nothing is paused
    asyncio.run(xc_server._select_credentials_preempting_downloads(provider))
    assert not vod_downloader._preempt_ids


def test_only_downloads_on_that_provider_are_paused(db, server, dl):
    _reset()
    vod_downloader._job_provider.update({1: 10, 2: 11})
    assert vod_downloader.preempt_provider_downloads({10}) is True
    assert vod_downloader._preempt_ids == {1}
    assert vod_downloader.preempt_provider_downloads({99}) is False

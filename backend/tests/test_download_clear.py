"""Removing single queue rows and clearing only failed/cancelled ones."""

import asyncio

import config
import vod_db
import vod_downloader
import vod_routes

from test_arr_handoff import _jobs, _movie


def _job(db, tmp_path, status, import_status=None):
    config.save_download_settings({"enabled": True, "output_dir": str(tmp_path / "library"), "min_free_gb": 0})
    vod_downloader.enqueue(movie_ids=[_movie(db)])
    jid = _jobs()[-1]["id"]
    vod_db.update_download_job(jid, status=status)
    if import_status:
        vod_db.update_import(jid, import_status=import_status, import_stage=4, import_note="x")
    return jid


def _clear(**kw):
    return asyncio.run(vod_routes.clear_downloads(vod_routes.DownloadClearRequest(**kw)))


def test_remove_one_row(db, tmp_path):
    a = _job(db, tmp_path, "failed")
    b = _job(db, tmp_path, "failed")
    assert _clear(job_ids=[a]) == {"cleared": 1, "kept": 0}
    assert vod_db.get_download_job(a) is None and vod_db.get_download_job(b) is not None


def test_remove_one_done_row_with_failed_import(db, tmp_path):
    a = _job(db, tmp_path, "done", "failed")
    assert _clear(job_ids=[a]) == {"cleared": 1, "kept": 0}
    assert vod_db.get_download_job(a) is None


def test_remove_never_touches_active_or_pending_rows(db, tmp_path):
    queued = _job(db, tmp_path, "queued")
    running = _job(db, tmp_path, "running")
    pending = _job(db, tmp_path, "done", "waiting")
    assert _clear(job_ids=[queued, running, pending]) == {"cleared": 0, "kept": 1}
    assert all(vod_db.get_download_job(j) for j in (queued, running, pending))


def test_clear_failed_keeps_done_rows(db, tmp_path):
    failed = _job(db, tmp_path, "failed")
    cancelled = _job(db, tmp_path, "cancelled")
    done = _job(db, tmp_path, "done")
    done_import_failed = _job(db, tmp_path, "done", "failed")
    assert _clear(failed_only=True) == {"cleared": 2, "kept": 0}
    assert vod_db.get_download_job(failed) is None and vod_db.get_download_job(cancelled) is None
    assert vod_db.get_download_job(done) and vod_db.get_download_job(done_import_failed)


def test_clear_without_body_still_clears_all_finished(db, tmp_path):
    _job(db, tmp_path, "failed")
    _job(db, tmp_path, "done")
    assert asyncio.run(vod_routes.clear_downloads()) == {"cleared": 2, "kept": 0}

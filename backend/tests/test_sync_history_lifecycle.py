"""Sync History rows must always reach a final state, every importer type must
write one, and Apply rules must count as a catalog refresh."""

import asyncio

import pytest

import apply_exclusions_job
import vod_importer


def _fake_xc_impl(changed):
    async def impl(_provider_id):
        return {"catalog_changed": changed, "changed_movie_ids": [], "changed_series_ids": [],
                "created_movie_ids": [], "created_series_ids": []}
    return impl


@pytest.mark.parametrize("changed", [False, True])
def test_deferred_enrichment_xc_import_still_finishes_its_run(db, monkeypatch, changed):
    # The scheduled refresher passes schedule_enrichment=False; the run used
    # to stay "running" forever because only the enrichment path closed it.
    pid = db.upsert_provider("Provider A", "http://a.invalid", "u", "p")
    monkeypatch.setattr(vod_importer, "_import_provider_catalog_impl", _fake_xc_impl(changed))

    result = asyncio.run(vod_importer.import_provider_catalog(pid, schedule_enrichment=False))

    run = db.get_catalog_sync_run(result["catalog_sync_run_id"])
    assert run["status"] == "ready"
    assert run["ready_at"]


def test_library_import_is_recorded_in_sync_history(db):
    pid = db.upsert_provider("Library B", "http://b.invalid", "u", "p", provider_type="plex")

    async def importer(_provider_id):
        return {"catalog_changed": False, "series_created": 0, "movies_created": 2,
                "changed_movie_ids": [], "changed_series_ids": []}

    result = asyncio.run(vod_importer.run_tracked_import(pid, importer))

    run = db.get_catalog_sync_run(result["catalog_sync_run_id"])
    assert run["provider_id"] == pid
    assert run["status"] == "ready"
    assert run["import_finished_at"]
    assert run["summary"]["movies_created"] == 2


def test_failed_library_import_is_recorded_as_failed(db):
    pid = db.upsert_provider("Library C", "http://c.invalid", "u", "p", provider_type="emby")

    async def importer(_provider_id):
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        asyncio.run(vod_importer.run_tracked_import(pid, importer))

    [run] = db.list_catalog_sync_runs()
    assert run["status"] == "failed"
    assert run["error"] == "TimeoutError"


def test_manual_non_xc_import_finish_marks_run_ready(db):
    pid = db.upsert_provider("Library D", "http://d.invalid", "u", "p", provider_type="plex")
    run_id = vod_importer.mark_import_running(pid, "Library D")

    vod_importer.mark_import_finished(pid, run_id=run_id, result={"catalog_changed": False})

    assert db.get_catalog_sync_run(run_id)["status"] == "ready"


def test_orphaned_running_runs_are_closed_on_startup(db):
    finished = db.create_catalog_sync_run(None, "Finished import")
    db.update_catalog_sync_run(finished, import_finished_at="1000.0")
    interrupted = db.create_catalog_sync_run(None, "Interrupted import")

    db.close_orphaned_catalog_sync_runs()

    assert db.get_catalog_sync_run(finished)["status"] == "ready"
    assert db.get_catalog_sync_run(finished)["ready_at"] == "1000.0"
    assert db.get_catalog_sync_run(interrupted)["status"] == "failed"


def test_apply_rules_stamps_catalog_refresh_time(db, monkeypatch):
    # Apply rules re-imports every provider; without the stamp the scheduled
    # refresher re-imported the same provider again on its next poll.
    pid = db.upsert_provider("Provider E", "http://e.invalid", "u", "p")

    async def fake_import(_provider_id, **_kwargs):
        return {"catalog_changed": False}

    async def no_resweep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(vod_importer, "import_provider_catalog", fake_import)
    monkeypatch.setattr(vod_importer, "resweep_smart_categories", no_resweep)
    apply_exclusions_job._jobs["t"] = {"status": "running", "total": 0, "completed": 0,
                                       "current_provider": None, "results": [], "error": None,
                                       "started_at": 0}
    try:
        asyncio.run(apply_exclusions_job._run_job("t"))
    finally:
        apply_exclusions_job._jobs.pop("t", None)

    assert db.get_provider(pid)["last_catalog_refresh_at"]

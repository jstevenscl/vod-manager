"""Every catalog importer must leave a final, durable Sync History summary."""

import asyncio
import sqlite3
import time

import pytest

import vod_importer


def test_tracked_adapter_import_is_recorded_and_closed(db):
    provider_id = db.upsert_provider("Library A", "http://a.invalid", "u", "p", provider_type="plex")

    async def importer(_provider_id):
        return {"movies_created": 2, "series_matched": 1, "catalog_changed": True}

    result = asyncio.run(vod_importer.run_tracked_import(provider_id, importer))

    report = db.get_catalog_sync_run(result["catalog_sync_run_id"])
    assert report["provider_id"] == provider_id
    assert report["provider_type"] == "plex"
    assert report["status"] == "ready"
    assert report["finished_at"]
    assert report["summary"] == {"movies_created": 2, "series_matched": 1, "catalog_changed": True}


def test_failed_tracked_adapter_import_is_closed(db):
    provider_id = db.upsert_provider("Library B", "http://b.invalid", "u", "p", provider_type="library")

    async def importer(_provider_id):
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        asyncio.run(vod_importer.run_tracked_import(provider_id, importer))

    [report] = db.list_catalog_sync_runs()
    assert report["status"] == "failed"
    assert report["error"] == "TimeoutError"


def test_restart_closes_orphaned_sync_history_run(db):
    run_id = db.create_catalog_sync_run(None, "Interrupted", "xc")

    assert db.close_orphaned_catalog_sync_runs() == 1
    report = db.get_catalog_sync_run(run_id)
    assert report["status"] == "failed"
    assert report["error"] == "interrupted by restart"


def test_xc_import_records_final_summary(db, monkeypatch):
    provider_id = db.upsert_provider("Provider A", "http://a.invalid", "u", "p")

    async def importer(_provider_id):
        return {"movies_created": 1, "catalog_changed": True}

    monkeypatch.setattr(vod_importer, "_import_provider_catalog_impl", importer)
    monkeypatch.setattr(vod_importer, "schedule_known_series_identity_reconciliation", lambda: True)

    result = asyncio.run(vod_importer.import_provider_catalog(provider_id))

    report = db.get_catalog_sync_run(result["catalog_sync_run_id"])
    assert report["status"] == "ready"
    assert report["provider_type"] == "xc"
    assert report["summary"] == {"movies_created": 1, "catalog_changed": True}


def test_missing_provider_keeps_report_without_foreign_key_error(db):
    run_id = db.create_catalog_sync_run(999999, "Removed provider", "plex")
    report = db.get_catalog_sync_run(run_id)
    assert report["provider_id"] is None
    assert report["provider_name"] == "Removed provider"


@pytest.mark.parametrize("operation", ["create", "finish", "restart"])
def test_failed_report_write_releases_database_lock(db, monkeypatch, operation):
    run_id = db.create_catalog_sync_run(None, "Report", "xc")

    def fail_commit(_conn):
        raise RuntimeError("report commit failed")

    monkeypatch.setattr(db, "_commit_with_retry", fail_commit)
    with pytest.raises(RuntimeError, match="report commit failed"):
        if operation == "create":
            db.create_catalog_sync_run(None, "Another report", "xc")
        elif operation == "finish":
            db.finish_catalog_sync_run(run_id)
        else:
            db.close_orphaned_catalog_sync_runs()

    conn = sqlite3.connect(db.DB_PATH, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    finally:
        conn.close()


def test_report_failure_does_not_replace_import_result(db, monkeypatch):
    provider_id = db.upsert_provider("Library C", "http://c.invalid", "u", "p", provider_type="plex")

    def fail_report(*_args, **_kwargs):
        raise RuntimeError("report unavailable")

    async def importer(_provider_id):
        return {"catalog_changed": False}

    monkeypatch.setattr(db, "create_catalog_sync_run", fail_report)
    assert asyncio.run(vod_importer.run_tracked_import(provider_id, importer)) == {"catalog_changed": False}


def test_adapter_report_write_does_not_block_event_loop(db, monkeypatch):
    provider_id = db.upsert_provider("Library D", "http://d.invalid", "u", "p", provider_type="plex")
    original = db.create_catalog_sync_run

    def slow_report(*args, **kwargs):
        time.sleep(0.1)
        return original(*args, **kwargs)

    async def importer(_provider_id):
        return {"catalog_changed": False}

    monkeypatch.setattr(db, "create_catalog_sync_run", slow_report)

    async def run():
        start = time.monotonic()
        async def ticker():
            await asyncio.sleep(0.02)
            return time.monotonic() - start
        return await asyncio.gather(vod_importer.run_tracked_import(provider_id, importer), ticker())

    result, tick = asyncio.run(run())
    assert result["catalog_sync_run_id"]
    assert tick < 0.08

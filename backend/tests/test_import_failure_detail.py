"""Import failures keep a redacted error message, and Apply rules defers
enrichment (upstream PR #31 review)."""

import asyncio

import pytest

import apply_exclusions_job
import vod_importer

_LEAKY = "Server error '513' for url 'http://h.invalid/player_api.php?username=bob&password=hunter2'"


def test_failed_tracked_import_keeps_redacted_message(db):
    pid = db.upsert_provider("Library F", "http://f.invalid", "u", "p", provider_type="emby")

    async def importer(_provider_id):
        raise RuntimeError(_LEAKY)

    with pytest.raises(RuntimeError):
        asyncio.run(vod_importer.run_tracked_import(pid, importer))

    [run] = db.list_catalog_sync_runs()
    assert "513" in run["error"]
    assert "hunter2" not in run["error"]


def test_failed_xc_import_keeps_redacted_message(db, monkeypatch):
    pid = db.upsert_provider("Provider G", "http://g.invalid", "u", "p")

    async def impl(_provider_id):
        raise RuntimeError(_LEAKY)

    monkeypatch.setattr(vod_importer, "_import_provider_catalog_impl", impl)

    with pytest.raises(RuntimeError):
        asyncio.run(vod_importer.import_provider_catalog(pid, schedule_enrichment=False))

    [run] = db.list_catalog_sync_runs()
    assert "513" in run["error"]
    assert "hunter2" not in run["error"]
    assert "513" in vod_importer._IMPORT_PROGRESS["error"]
    assert "hunter2" not in vod_importer._IMPORT_PROGRESS["error"]


def test_apply_rules_defers_enrichment_for_xc(db, monkeypatch):
    db.upsert_provider("Provider H", "http://h.invalid", "u", "p")
    calls = []

    async def fake_import(_provider_id, **kwargs):
        calls.append(kwargs)
        return {"catalog_changed": False}

    async def no_resweep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(vod_importer, "import_provider_catalog", fake_import)
    monkeypatch.setattr(vod_importer, "resweep_smart_categories", no_resweep)
    apply_exclusions_job._jobs["t2"] = {"status": "running", "total": 0, "completed": 0,
                                        "current_provider": None, "results": [], "error": None,
                                        "started_at": 0}
    try:
        asyncio.run(apply_exclusions_job._run_job("t2"))
    finally:
        apply_exclusions_job._jobs.pop("t2", None)

    assert calls == [{"schedule_enrichment": False}]

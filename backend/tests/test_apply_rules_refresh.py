"""Apply rules re-imports every active provider, so it must count as a
catalog refresh and must route each provider type to its own importer."""

import asyncio

import apply_exclusions_job
import library_importer
import vod_importer


def _run_apply_rules():
    apply_exclusions_job._jobs["t"] = {"status": "running", "total": 0, "completed": 0,
                                       "current_provider": None, "results": [], "error": None,
                                       "started_at": 0}
    try:
        asyncio.run(apply_exclusions_job._run_job("t"))
        return apply_exclusions_job._jobs["t"]
    finally:
        apply_exclusions_job._jobs.pop("t", None)


async def _no_resweep(*_args, **_kwargs):
    return None


def test_apply_rules_stamps_catalog_refresh_time(db, monkeypatch):
    pid = db.upsert_provider("Provider A", "http://a.invalid", "u", "p")

    async def fake_import(_provider_id, **_kwargs):
        return {"catalog_changed": False}

    monkeypatch.setattr(vod_importer, "import_provider_catalog", fake_import)
    monkeypatch.setattr(vod_importer, "resweep_smart_categories", _no_resweep)

    _run_apply_rules()

    assert db.get_provider(pid)["last_catalog_refresh_at"]


def test_failed_provider_is_not_stamped(db, monkeypatch):
    pid = db.upsert_provider("Provider B", "http://b.invalid", "u", "p")

    async def failing_import(_provider_id, **_kwargs):
        raise TimeoutError()

    monkeypatch.setattr(vod_importer, "import_provider_catalog", failing_import)
    monkeypatch.setattr(vod_importer, "resweep_smart_categories", _no_resweep)

    _run_apply_rules()

    assert not db.get_provider(pid)["last_catalog_refresh_at"]


def test_apply_rules_uses_library_importer_for_library_providers(db, monkeypatch):
    pid = db.upsert_provider("Library C", "", "", "", provider_type="library")
    called = []

    async def fake_library(provider_id):
        called.append(provider_id)
        return {"catalog_changed": False}

    async def xc_must_not_run(*_args, **_kwargs):
        raise AssertionError("library provider sent to the XC importer")

    monkeypatch.setattr(library_importer, "import_library", fake_library)
    monkeypatch.setattr(vod_importer, "import_provider_catalog", xc_must_not_run)
    monkeypatch.setattr(vod_importer, "resweep_smart_categories", _no_resweep)

    job = _run_apply_rules()

    assert called == [pid]
    assert "error" not in job["results"][0]

"""Apply-rules job: provider errors shown in the UI must not carry the
provider login, and progress must not count past the provider total."""

import asyncio

import apply_exclusions_job


def _run(monkeypatch, job_id, import_fn, resweep=None):
    provider = {"id": 1, "name": "Provider One", "is_active": True, "provider_type": "xc"}
    monkeypatch.setattr(apply_exclusions_job.vod_db, "list_providers", lambda: [provider])
    monkeypatch.setattr(apply_exclusions_job.vod_importer, "import_provider_catalog", import_fn)
    if resweep is not None:
        monkeypatch.setattr(apply_exclusions_job.vod_importer, "resweep_smart_categories", resweep)
    apply_exclusions_job._jobs[job_id] = {"results": [], "completed": 0, "total": 0, "current_provider": None}
    asyncio.run(apply_exclusions_job._run_job(job_id))
    return apply_exclusions_job._jobs[job_id]


def test_provider_error_hides_credentials(monkeypatch):
    async def failing_import(provider_id):
        raise RuntimeError(
            "Server error '513 <none>' for url "
            "'http://example.invalid/player_api.php?username=realuser&password=realpass&action=get_vod_categories'"
        )

    async def resweep():
        return None

    error = _run(monkeypatch, "redaction-test", failing_import, resweep)["results"][0]["error"]
    assert "realuser" not in error and "realpass" not in error
    assert "513" in error


def test_progress_never_exceeds_total_while_finalizing(monkeypatch):
    seen = {}

    async def ok_import(provider_id):
        return {}

    async def resweep():
        job = apply_exclusions_job._jobs["finalizing-test"]
        seen.update(completed=job["completed"], total=job["total"],
                    current=job["current_provider"], phase=job.get("phase"))

    _run(monkeypatch, "finalizing-test", ok_import, resweep)
    assert seen == {"completed": 1, "total": 1, "current": None, "phase": "finalizing"}

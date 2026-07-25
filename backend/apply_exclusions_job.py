"""
Background job wrapper for retroactively applying the current import
exclusion rules (language + per-provider category) across every active
provider's existing catalog -- a real re-import per provider, which for a
large catalog (hundreds of thousands of items) can take minutes. Runs
detached so the frontend can poll for progress ("provider 2 of 5, currently
syncing Mega-OTT") instead of holding one blocking HTTP request open with
no way to tell an admin it's still working rather than hung.

Job state lives in memory only, not the DB -- ephemeral, scoped to one run,
nothing worth persisting across a server restart.
"""

import asyncio
import logging
import time
import uuid

import dispatcharr_dvr_importer
import emby_vod_importer
import plex_importer
import vod_db
import vod_importer

logger = logging.getLogger(__name__)

_MAX_TRACKED_JOBS = 5

_jobs: dict[str, dict] = {}


async def _run_job(job_id: str) -> None:
    job = _jobs[job_id]
    try:
        providers = [p for p in await asyncio.to_thread(vod_db.list_providers) if p["is_active"]]
        job["total"] = len(providers)
        for p in providers:
            job["current_provider"] = p["name"]
            try:
                if p.get("provider_type") == "plex":
                    result = await plex_importer.import_plex_library(p["id"])
                elif p.get("provider_type") in ("emby", "jellyfin"):
                    result = await emby_vod_importer.import_emby_library(p["id"])
                elif p.get("provider_type") == "dispatcharr_dvr":
                    # DVR recordings have no language/category exclusion rules
                    # to retroactively apply yet -- this just re-runs the same
                    # idempotent import, and exists so a DVR provider doesn't
                    # fall into the XC branch below and error out.
                    result = await dispatcharr_dvr_importer.import_dvr_recordings(p["id"])
                else:
                    result = await vod_importer.import_provider_catalog(p["id"])
                job["results"].append({"provider": p["name"], **result})
            except Exception as exc:
                logger.error("[apply_exclusions_job] provider=%s failed: %s", p["name"], exc)
                job["results"].append({"provider": p["name"], "error": str(exc)})
            job["completed"] += 1
        job["current_provider"] = None
        job["status"] = "done"
        logger.info("[apply_exclusions_job] job=%s done: %d provider(s)", job_id, job["completed"])
    except Exception as exc:
        logger.warning("[apply_exclusions_job] job=%s failed: %s", job_id, exc)
        job["status"] = "error"
        job["error"] = str(exc)


def start_job() -> str:
    if len(_jobs) >= _MAX_TRACKED_JOBS:
        oldest_id = min(_jobs, key=lambda jid: _jobs[jid]["started_at"])
        del _jobs[oldest_id]
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "status": "running", "total": 0, "completed": 0, "current_provider": None,
        "results": [], "error": None, "started_at": time.time(),
    }
    asyncio.create_task(_run_job(job_id))
    return job_id


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)

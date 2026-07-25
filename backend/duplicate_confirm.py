"""
Background TMDB-confirmed-match check for Duplicate Finder.

A "confirmed" duplicate group is one where every candidate shares the exact
same tmdb_id (proof they're the same real title) AND one candidate's name
matches TMDB's own title exactly -- that candidate is the confident merge
target, no manual pick needed (see vod_routes.py's /duplicates/merge-confirmed/).

Checking this against TMDB means one real GET per distinct tmdb_id -- no
bulk-lookup-by-ids endpoint exists. A real catalog scan can surface
thousands of candidate ids (this app's own test catalog: ~4,700 for movies
alone), which is far too slow and rate-limit-risky to do inline in one
blocking HTTP request -- a multi-minute request is a request nothing (browser,
proxy, FastAPI worker) is built to hold open. Runs instead as a detached
asyncio task in throttled batches, with progress the frontend polls for.

Job state lives in memory only, not the DB -- ephemeral, scoped to one scan
session, nothing worth persisting across a server restart.
"""

import asyncio
import logging
import time
import uuid

import tmdb_sync
import vod_db

logger = logging.getLogger(__name__)

_BATCH_SIZE = 30
_BATCH_DELAY_SECONDS = 1.0
_MAX_TRACKED_JOBS = 5

_jobs: dict[str, dict] = {}


def _find_keeper(items: list[dict], tmdb_title: str | None) -> dict | None:
    if not tmdb_title:
        return None
    title_norm = tmdb_title.strip().lower()
    for item in items:
        if item["name"].strip().lower() == title_norm:
            return item
    return None


async def _run_job(job_id: str, content_type: str) -> None:
    job = _jobs[job_id]
    try:
        groups = await asyncio.to_thread(vod_db.find_duplicate_groups, content_type)
        # "Pure" groups only -- every candidate shares one non-null tmdb_id.
        # A group with even one tmdb_id-less candidate has no way to confirm
        # that candidate belongs, so it stays in manual review regardless of
        # what the rest of the group's ids say.
        pure_groups = [
            g for g in groups
            if len({i["tmdb_id"] for i in g["items"]}) == 1 and g["items"][0]["tmdb_id"]
        ]
        distinct_ids = sorted({g["items"][0]["tmdb_id"] for g in pure_groups})
        job["total"] = len(distinct_ids)

        details: dict[str, dict] = {}
        for i in range(0, len(distinct_ids), _BATCH_SIZE):
            if job["cancelled"]:
                job["status"] = "cancelled"
                return
            batch = distinct_ids[i:i + _BATCH_SIZE]
            batch_details = await tmdb_sync.get_tmdb_details_for_ids(batch, content_type)
            details.update(batch_details)
            job["checked"] = len(details)
            if i + _BATCH_SIZE < len(distinct_ids):
                await asyncio.sleep(_BATCH_DELAY_SECONDS)

        confirmed = []
        for g in pure_groups:
            tmdb_id = g["items"][0]["tmdb_id"]
            keeper = _find_keeper(g["items"], details.get(tmdb_id, {}).get("title"))
            if not keeper:
                continue
            confirmed.append({
                "keep_id": keeper["id"],
                "merge_ids": [i["id"] for i in g["items"] if i["id"] != keeper["id"]],
                "matched_title": keeper["name"],
                "tmdb_id": tmdb_id,
            })
        job["confirmed"] = confirmed
        job["status"] = "done"
        logger.info("[duplicate_confirm] job=%s content_type=%s checked=%d confirmed=%d",
                     job_id, content_type, len(distinct_ids), len(confirmed))
    except Exception as exc:
        logger.warning("[duplicate_confirm] job=%s failed: %s", job_id, exc)
        job["status"] = "error"
        job["error"] = str(exc)


def start_job(content_type: str) -> str:
    if len(_jobs) >= _MAX_TRACKED_JOBS:
        oldest_id = min(_jobs, key=lambda jid: _jobs[jid]["started_at"])
        del _jobs[oldest_id]
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "content_type": content_type, "status": "running",
        "checked": 0, "total": 0, "confirmed": [], "error": None,
        "cancelled": False, "started_at": time.time(),
    }
    asyncio.create_task(_run_job(job_id, content_type))
    return job_id


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def cancel_job(job_id: str) -> None:
    job = _jobs.get(job_id)
    if job:
        job["cancelled"] = True

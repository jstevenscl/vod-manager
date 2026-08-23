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
        # find_duplicate_groups already splits apart any group with 2+
        # DIFFERING tmdb_ids (see vod_db._split_by_tmdb_conflict) before this
        # ever runs -- a conflicting id is treated as proof of non-duplicate,
        # not ambiguity. So every group reaching here has at most ONE
        # distinct non-null tmdb_id among its candidates. That leaves two
        # real cases: every candidate carries it ("pure" -- existing
        # confirmed-match tier below), or only some do while the rest have
        # no id at all ("partial" -- GH issue #2's second pass, see below).
        id_groups = [g for g in groups if len({i["tmdb_id"] for i in g["items"] if i["tmdb_id"]}) == 1]
        pure_groups = [g for g in id_groups if all(i["tmdb_id"] for i in g["items"])]
        partial_groups = [g for g in id_groups if not all(i["tmdb_id"] for i in g["items"])]
        distinct_ids = sorted({i["tmdb_id"] for g in id_groups for i in g["items"] if i["tmdb_id"]})
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
            # Real bug found live 2026-07-31 (user report: 1710 items stuck
            # in review, "many with two candidates both matching TMDB" but
            # zero confirmed matches): this used to require an EXACT
            # candidate-name-to-TMDB-title match to confirm the group at
            # all, not just to pick which candidate to keep -- so a group
            # where every candidate agreed on tmdb_id (already, by itself,
            # real proof they're duplicates -- see this module's docstring)
            # was silently dropped whenever BOTH candidates' own names
            # differed from TMDB's exact title string, which real provider
            # naming (year suffixes, punctuation, "Director's Cut", etc.)
            # makes the common case, not the exception. The shared tmdb_id
            # is the actual confirmation; the exact-title match was only
            # ever meant to help pick a keeper. Now: still prefer the exact
            # match when one exists (keeps the "airtight" pick), but fall
            # back to items[0] -- find_duplicate_groups already sorts each
            # group by (-source_count, -category_count), i.e. the most-
            # sourced/most-placed candidate -- rather than dropping the
            # group outright.
            exact_match = _find_keeper(g["items"], details.get(tmdb_id, {}).get("title"))
            keeper = exact_match or g["items"][0]
            confirmed.append({
                "keep_id": keeper["id"],
                "merge_ids": [i["id"] for i in g["items"] if i["id"] != keeper["id"]],
                "matched_title": keeper["name"],
                "tmdb_id": tmdb_id,
                "exact_title_match": exact_match is not None,
            })
        job["confirmed"] = confirmed

        # Second pass (GH issue #2): a group where only SOME candidates carry
        # the group's one tmdb_id isn't as airtight as "pure" (no sibling
        # corroborates that id), but it's still real evidence -- ONLY trust
        # it when the id-holder's own year also matches TMDB's canonical year
        # for that id (self-consistent), same bar the frontend's "unconfirmed
        # -- TMDB confirms only this candidate" badge already uses. A
        # self-INCONSISTENT id-holder (year mismatch) never qualifies here --
        # that's the strongest negative signal short of an outright conflict,
        # not a green light.
        second_pass = []
        for g in partial_groups:
            tmdb_id = next(i["tmdb_id"] for i in g["items"] if i["tmdb_id"])
            detail = details.get(tmdb_id, {})
            true_year = detail.get("year")
            id_holders = [i for i in g["items"] if i["tmdb_id"]]
            self_consistent_holders = [i for i in id_holders if true_year is not None and i["year"] == true_year]
            if not self_consistent_holders:
                continue
            exact_match = _find_keeper(self_consistent_holders, detail.get("title"))
            keeper = exact_match or self_consistent_holders[0]
            second_pass.append({
                "keep_id": keeper["id"],
                "merge_ids": [i["id"] for i in g["items"] if i["id"] != keeper["id"]],
                "matched_title": keeper["name"],
                "tmdb_id": tmdb_id,
                "exact_title_match": exact_match is not None,
            })
        job["second_pass"] = second_pass

        job["status"] = "done"
        logger.info("[duplicate_confirm] job=%s content_type=%s checked=%d confirmed=%d second_pass=%d",
                     job_id, content_type, len(distinct_ids), len(confirmed), len(second_pass))
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
        "checked": 0, "total": 0, "confirmed": [], "second_pass": [], "error": None,
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

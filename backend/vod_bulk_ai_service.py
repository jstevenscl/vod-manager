"""
Bulk AI-assisted resolution for Needs Year Review, Missing Artwork, and
Duplicate Finder -- the existing one-item-at-a-time "Search TMDB -> AI
suggest -> Use this" flow (ai_assist.py + the /needs-review/ and
/missing-artwork/ resolve routes) does real work but doesn't scale to a
catalog with hundreds of items stuck in any of these three queues. This
reuses those exact same building blocks (tmdb_sync.search_title,
ai_assist.suggest_year_review_match) in a background job over a caller-
picked set of items/groups, auto-applying only when the AI reports HIGH
confidence -- medium/low/no-match items are left untouched for manual
review, never guessed into the pool. Same "never auto-resolves without a
real signal" contract the single-item flow already has, just batched.

Three independent job kinds, one shared progress-tracking shape (_new_job/
get_bulk_ai_job) -- same in-memory job-dict + polled-GET pattern as
duplicate_confirm.py, just generalized to an arbitrary job_id instead of
being keyed by one content_type, since a batch of caller-picked ids has no
single natural key.
"""

import asyncio
import logging
import time
import uuid

import ai_assist
import tmdb_sync
import vod_db

logger = logging.getLogger(__name__)

_HIGH_CONFIDENCE = "high"
_MAX_TRACKED_JOBS = 5

_jobs: dict[str, dict] = {}


def _new_job(total: int) -> str:
    if len(_jobs) >= _MAX_TRACKED_JOBS:
        oldest_id = min(_jobs, key=lambda jid: _jobs[jid]["started_at"])
        del _jobs[oldest_id]
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "running": True, "total": total, "done": 0,
        "resolved": 0, "skipped": 0, "errors": 0,
        "results": [], "error": None,
        "started_at": time.time(), "finished_at": None,
    }
    return job_id


def get_bulk_ai_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def _finish_job(job_id: str) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job["running"] = False
        job["finished_at"] = time.time()


def _record(job_id: str, result: dict) -> None:
    job = _jobs[job_id]
    job["results"].append(result)
    job["done"] += 1
    job[{"resolved": "resolved", "skipped": "skipped", "error": "errors"}.get(result["status"], "skipped")] += 1


# ---------------------------------------------------------------------------
# Needs Year Review
# ---------------------------------------------------------------------------

async def _resolve_one_needs_review(content_type: str, item_id: int) -> dict:
    item = vod_db.get_movie(item_id) if content_type == "movie" else vod_db.get_series(item_id)
    if not item:
        return {"id": item_id, "status": "error", "detail": "not found"}
    if not item.get("needs_year_review"):
        return {"id": item_id, "name": item["name"], "status": "skipped", "detail": "no longer needs review"}

    try:
        candidates = await tmdb_sync.search_title(item["name"], content_type)
    except Exception as exc:
        return {"id": item_id, "name": item["name"], "status": "error", "detail": f"TMDB search failed: {exc}"}
    if not candidates:
        return {"id": item_id, "name": item["name"], "status": "skipped", "detail": "no TMDB results"}

    try:
        suggestion = await ai_assist.suggest_year_review_match(item["name"], item.get("provider_category_name"), content_type, candidates)
    except Exception as exc:
        return {"id": item_id, "name": item["name"], "status": "error", "detail": f"AI suggestion failed: {exc}"}

    idx = suggestion.get("best_match_index")
    if idx is None or suggestion.get("confidence") != _HIGH_CONFIDENCE or not (0 <= idx < len(candidates)):
        return {
            "id": item_id, "name": item["name"], "status": "skipped",
            "detail": suggestion.get("reasoning") or "no confident match",
        }

    pick = candidates[idx]
    try:
        result = vod_db.resolve_year_review(content_type, item_id, pick.get("year"), pick.get("tmdb_id"))
    except ValueError as exc:
        return {"id": item_id, "name": item["name"], "status": "error", "detail": str(exc)}
    if result.get("merged_into"):
        return {"id": item_id, "name": item["name"], "status": "resolved", "detail": f"merged into #{result['merged_into']}"}
    return {"id": item_id, "name": item["name"], "status": "resolved", "detail": f"set year {pick.get('year')} (tmdb_id={pick.get('tmdb_id')})"}


async def _run_needs_review_job(job_id: str, content_type: str, ids: list[int]) -> None:
    try:
        for item_id in ids:
            try:
                result = await _resolve_one_needs_review(content_type, item_id)
            except Exception as exc:
                result = {"id": item_id, "status": "error", "detail": str(exc)}
            _record(job_id, result)
    except Exception as exc:
        _jobs[job_id]["error"] = str(exc)
        logger.exception("[vod_bulk_ai] needs-review job %s failed: %s", job_id, exc)
    finally:
        _finish_job(job_id)


async def start_needs_review_bulk_resolve(content_type: str, ids: list[int]) -> str:
    job_id = _new_job(len(ids))
    asyncio.create_task(_run_needs_review_job(job_id, content_type, ids))
    return job_id


# ---------------------------------------------------------------------------
# Missing Artwork
# ---------------------------------------------------------------------------

async def _resolve_one_missing_artwork(content_type: str, item_id: int) -> dict:
    item = vod_db.get_movie(item_id) if content_type == "movie" else vod_db.get_series(item_id)
    if not item:
        return {"id": item_id, "status": "error", "detail": "not found"}
    if item.get("poster_url"):
        return {"id": item_id, "name": item["name"], "status": "skipped", "detail": "already has a poster"}

    try:
        candidates = await tmdb_sync.search_title(item["name"], content_type)
    except Exception as exc:
        return {"id": item_id, "name": item["name"], "status": "error", "detail": f"TMDB search failed: {exc}"}
    if not candidates:
        return {"id": item_id, "name": item["name"], "status": "skipped", "detail": "no TMDB results"}

    try:
        suggestion = await ai_assist.suggest_year_review_match(item["name"], item.get("provider_category_name"), content_type, candidates)
    except Exception as exc:
        return {"id": item_id, "name": item["name"], "status": "error", "detail": f"AI suggestion failed: {exc}"}

    idx = suggestion.get("best_match_index")
    if idx is None or suggestion.get("confidence") != _HIGH_CONFIDENCE or not (0 <= idx < len(candidates)):
        return {
            "id": item_id, "name": item["name"], "status": "skipped",
            "detail": suggestion.get("reasoning") or "no confident match",
        }

    pick = candidates[idx]
    if not pick.get("poster_url"):
        return {"id": item_id, "name": item["name"], "status": "skipped", "detail": "AI's pick has no TMDB poster either"}
    try:
        result = vod_db.resolve_missing_artwork(content_type, item_id, pick["poster_url"], pick.get("tmdb_id"), pick.get("name"), pick.get("year"))
    except ValueError as exc:
        return {"id": item_id, "name": item["name"], "status": "error", "detail": str(exc)}
    if result.get("merged_into"):
        return {"id": item_id, "name": item["name"], "status": "resolved", "detail": f"merged into #{result['merged_into']}"}
    return {"id": item_id, "name": item["name"], "status": "resolved", "detail": "poster applied"}


async def _run_missing_artwork_job(job_id: str, content_type: str, ids: list[int]) -> None:
    try:
        for item_id in ids:
            try:
                result = await _resolve_one_missing_artwork(content_type, item_id)
            except Exception as exc:
                result = {"id": item_id, "status": "error", "detail": str(exc)}
            _record(job_id, result)
    except Exception as exc:
        _jobs[job_id]["error"] = str(exc)
        logger.exception("[vod_bulk_ai] missing-artwork job %s failed: %s", job_id, exc)
    finally:
        _finish_job(job_id)


async def start_missing_artwork_bulk_resolve(content_type: str, ids: list[int]) -> str:
    job_id = _new_job(len(ids))
    asyncio.create_task(_run_missing_artwork_job(job_id, content_type, ids))
    return job_id


# ---------------------------------------------------------------------------
# Duplicate Finder
# ---------------------------------------------------------------------------

async def _resolve_one_duplicate_group(content_type: str, keep_id: int, merge_ids: list[int]) -> dict:
    getter = vod_db.get_movie if content_type == "movie" else vod_db.get_series
    all_ids = [keep_id] + merge_ids
    items = [getter(i) for i in all_ids]
    if any(item is None for item in items):
        return {"keep_id": keep_id, "merge_ids": merge_ids, "status": "skipped", "detail": "group no longer intact"}

    tmdb_ids = {i["tmdb_id"] for i in items if i.get("tmdb_id")}
    if len(tmdb_ids) == 1:
        # All members already agree on one confirmed tmdb_id -- a stronger
        # signal than an AI guess, no LLM call needed.
        verdict_detail = "tmdb_id already agrees across the group"
    elif len(tmdb_ids) > 1:
        # find_duplicate_groups already splits conflicting tmdb_ids into
        # separate groups (_split_by_tmdb_conflict) -- reaching this means
        # the group changed underneath this job (a concurrent edit). Refuse
        # rather than merge across a real, confirmed conflict.
        return {"keep_id": keep_id, "merge_ids": merge_ids, "status": "skipped", "detail": "tmdb_id conflict -- group changed since it was scanned"}
    else:
        candidate_rows = [
            {"name": i["name"], "year": i.get("year"), "genre": i.get("genre"), "description": i.get("description")}
            for i in items
        ]
        try:
            verdict = await ai_assist.verify_duplicate_group(content_type, candidate_rows)
        except Exception as exc:
            return {"keep_id": keep_id, "merge_ids": merge_ids, "status": "error", "detail": f"AI verification failed: {exc}"}
        if not verdict.get("same_title") or verdict.get("confidence") != _HIGH_CONFIDENCE:
            return {
                "keep_id": keep_id, "merge_ids": merge_ids, "status": "skipped",
                "detail": verdict.get("reasoning") or "AI was not confident these are the same title",
            }
        verdict_detail = verdict.get("reasoning") or "AI confirmed same title"

    merge_fn = vod_db.merge_movie if content_type == "movie" else vod_db.merge_series
    for mid in merge_ids:
        merge_fn(mid, keep_id)
    return {"keep_id": keep_id, "merge_ids": merge_ids, "status": "resolved", "detail": verdict_detail}


async def _run_duplicates_job(job_id: str, content_type: str, groups: list[dict]) -> None:
    try:
        for group in groups:
            keep_id = group["keep_id"]
            merge_ids = group["merge_ids"]
            try:
                result = await _resolve_one_duplicate_group(content_type, keep_id, merge_ids)
            except Exception as exc:
                result = {"keep_id": keep_id, "merge_ids": merge_ids, "status": "error", "detail": str(exc)}
            _record(job_id, result)
    except Exception as exc:
        _jobs[job_id]["error"] = str(exc)
        logger.exception("[vod_bulk_ai] duplicates job %s failed: %s", job_id, exc)
    finally:
        _finish_job(job_id)


async def start_duplicates_bulk_resolve(content_type: str, groups: list[dict]) -> str:
    job_id = _new_job(len(groups))
    asyncio.create_task(_run_duplicates_job(job_id, content_type, groups))
    return job_id

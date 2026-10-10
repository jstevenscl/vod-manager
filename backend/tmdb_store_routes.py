"""API for the local TMDB store page: status/stats, lookup, settings, manual runs, clear."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import tmdb_fill
import tmdb_store
from config import get_tmdb_api_key, get_tmdb_store_settings, save_tmdb_store_settings
from routes import require_auth

router = APIRouter(prefix="/api/tmdb-store", tags=["tmdb-store"])
_tasks: set[asyncio.Task] = set()  # keep manual-run tasks referenced until done
_GUARDS = [Depends(require_auth)]


class StoreSettings(BaseModel):
    enabled: bool | None = None
    burst_size: int | None = None
    bursts_per_day: int | None = None
    prefill_top: int | None = None
    daily_budget: int | None = None
    concurrency: int | None = None


@router.get("/status", dependencies=_GUARDS)
async def get_status():
    settings = get_tmdb_store_settings()
    has_key = bool(get_tmdb_api_key())
    stats = await asyncio.to_thread(tmdb_store.stats)
    next_burst = await asyncio.to_thread(tmdb_store.get_meta, "next_burst_at")
    return {
        "has_api_key": has_key,
        "active": has_key and settings["enabled"],
        "settings": settings,
        "stats": stats,
        "requests_today": await asyncio.to_thread(tmdb_store.requests_today),
        "next_burst_at": float(next_burst) if next_burst else None,
        "db_size_bytes": await asyncio.to_thread(tmdb_store.db_size_bytes),
        "job": tmdb_fill.status,
    }


@router.get("/search", dependencies=_GUARDS)
async def search(q: str, type: str = "movie", year: int | None = None):
    if type not in tmdb_store.MEDIA_TYPES:
        raise HTTPException(400, detail="type must be movie or tv")
    try:
        return await tmdb_fill.lookup(q, type, year)
    except Exception as exc:
        raise HTTPException(502, detail=f"TMDB lookup failed: {tmdb_fill.tmdb_sync._redact(exc)}")


@router.put("/settings", dependencies=_GUARDS)
async def put_settings(body: StoreSettings):
    return save_tmdb_store_settings(body.model_dump(exclude_none=True))


@router.post("/run/{job}", dependencies=_GUARDS)
async def run(job: str):
    if job not in ("export", "changes", "burst"):
        raise HTTPException(400, detail="job must be export, changes or burst")
    if not get_tmdb_api_key():
        raise HTTPException(400, detail="Save a TMDB API key first")
    if not get_tmdb_store_settings()["enabled"]:
        raise HTTPException(400, detail="The local TMDB library is turned off")
    if tmdb_fill.status["running"]:
        raise HTTPException(409, detail="A TMDB store job is already running")
    task = asyncio.create_task(tmdb_fill.run_job(job))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"started": job}


@router.post("/clear", dependencies=_GUARDS)
async def clear():
    """Delete all stored data to free disk space. Only while the library is
    off, so nothing refills it straight away."""
    if get_tmdb_store_settings()["enabled"]:
        raise HTTPException(400, detail="Turn the local library off first")
    if tmdb_fill.status["running"]:
        raise HTTPException(409, detail="A TMDB store job is still running -- try again when it finishes")
    await asyncio.to_thread(tmdb_store.clear)
    return {"cleared": True}

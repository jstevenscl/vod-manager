"""One-time, user-started backfill of missing episode air dates.

Walks every series that has a TMDB id and at least one episode without an air date
and calls the existing vod_importer.fill_series_air_dates for each (local TMDB library
first, TMDB only for what is still missing). It never contacts a provider. State and
progress live in config.json so a restart can pick the run up where it stopped.
"""

import asyncio
import logging
import time

import config
import vod_db
import vod_importer

logger = logging.getLogger(__name__)

# KNM: gentle pace between series so a big catalog does not hammer TMDB; tmdb_sync._tmdb_get
# already adds the shared concurrency cap and 429/503 backoff. 2026-11-19
_PAUSE_SECONDS = 0.2
_SAVE_EVERY = 25

_COUNTERS = ("total", "processed", "filled", "skipped_no_tmdb_id", "errors")
_task: asyncio.Task | None = None


def _load() -> dict:
    raw = config.get_air_date_backfill()
    state = {k: int(raw.get(k) or 0) for k in _COUNTERS}
    state["state"] = raw.get("state") if raw.get("state") in ("running", "done") else "pending"
    state["started_at"] = raw.get("started_at")
    state["finished_at"] = raw.get("finished_at")
    return state


def _save(state: dict) -> None:
    keep = {k: state.get(k) for k in (*_COUNTERS, "state", "started_at", "finished_at")}
    config.save_air_date_backfill(keep)


def _alive() -> bool:
    return _task is not None and not _task.done()


def get_status() -> dict:
    state = _load()
    state["running"] = _alive()
    return state


async def run() -> None:
    state = _load()
    try:
        ids = await asyncio.to_thread(vod_db.list_series_ids_missing_air_dates)
        state["skipped_no_tmdb_id"] = await asyncio.to_thread(vod_db.count_series_missing_air_dates_without_tmdb_id)
        # Resume: series already finished have no missing dates and drop out of `ids`.
        state["total"] = state["processed"] + len(ids)
        _save(state)
        for n, series_id in enumerate(ids, 1):
            try:
                before = await asyncio.to_thread(vod_db.count_episodes_missing_air_date, series_id)
                await vod_importer.fill_series_air_dates(series_id)
                after = await asyncio.to_thread(vod_db.count_episodes_missing_air_date, series_id)
                state["filled"] += max(0, before - after)
            except Exception as exc:  # a bad series is logged and skipped, never aborts the run
                state["errors"] += 1
                logger.warning("[air_date_backfill] series_id=%s skipped: %s", series_id, exc)
            state["processed"] += 1
            if n % _SAVE_EVERY == 0:
                _save(state)
            if _PAUSE_SECONDS:
                await asyncio.sleep(_PAUSE_SECONDS)
        state["state"] = "done"
        state["finished_at"] = time.time()
        _save(state)
        logger.info("[air_date_backfill] done: %s", {k: state[k] for k in _COUNTERS})
    except asyncio.CancelledError:
        _save(state)  # stays "running" on disk; resumed at next startup
        raise
    except Exception as exc:
        # Leave the state "running" so Start-up resume retries; surface the cause in the log.
        logger.error("[air_date_backfill] stopped early: %s", exc)
        _save(state)


def _launch(state: dict) -> None:
    global _task
    state["state"] = "running"
    state["started_at"] = state["started_at"] or time.time()
    state["finished_at"] = None
    _save(state)
    _task = asyncio.get_running_loop().create_task(run())


def start() -> dict:
    """Idempotent: ignored when already running or done. Must be called inside the event loop."""
    state = _load()
    if state["state"] == "done" or _alive():
        return get_status()
    _launch(state)
    return get_status()


def resume_if_interrupted() -> None:
    """At app startup: a run still marked running died with the previous process; continue it."""
    state = _load()
    if state["state"] == "running" and not _alive():
        _launch(state)

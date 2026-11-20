"""One-time, user-started backfill of missing episode air dates.

Walks every series that has a TMDB id and at least one episode without an air date
and calls the existing vod_importer.fill_series_air_dates for each (local TMDB library
first, TMDB only for what is still missing). It never contacts a provider. State and
progress live in config.json so a restart can pick the run up where it stopped.
"""

import asyncio
import contextlib
import logging
import time

import config
import tmdb_sync
import vod_db
import vod_importer

logger = logging.getLogger(__name__)

# KNM: one-time catch-up of a large catalog, so it runs much faster than the always-on
# features, yet well under TMDB's ~50 requests/s per IP: aim for 20/s and never exceed 25/s
# in total across all workers. Only real TMDB requests are paced; series answered from the
# local TMDB library cost no waiting. 2026-11-19
_TARGET_REQUESTS_PER_SECOND = 20.0
# KNM: hard ceiling on the request rate, whatever the target or recovery logic says. 2026-11-19
_HARD_CAP_REQUESTS_PER_SECOND = 25.0
# KNM: series processed in parallel. Each worker has one request in flight at a time, and
# _MAX_IN_FLIGHT bounds the backfill's share of tmdb_sync's global cap of 40 concurrent
# requests, leaving the rest free for card opens and the trickle. 2026-11-19
_WORKERS = 12
_MAX_IN_FLIGHT = 16
# KNM: after TMDB answers 429/503 the backfill halves its own rate (the shared 5s backoff
# also stalls the rest of the app), then climbs back 25% every _RECOVER_AFTER seconds. 2026-11-19
_MIN_REQUESTS_PER_SECOND = 2.0
_SLOWDOWN_COOLDOWN = 5.0
_RECOVER_AFTER = 10.0
_SAVE_EVERY = 25

_clock = time.monotonic
_sleep = asyncio.sleep


class _RateLimiter:
    """Spaces requests at least 1/rate apart across every caller. No await sits between
    reading and updating _next, so concurrent callers cannot race. The rate can drop
    after a 429/503 and recovers step by step, never above min(target, hard cap)."""

    def __init__(self, target: float, cap: float, clock=None, sleep=None):
        self._ceiling = min(float(target), float(cap))
        self.rate = self._ceiling
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._last_slowdown = float("-inf")
        self._last_change = float("-inf")

    def _now(self) -> float:
        return (self._clock or _clock)()

    async def acquire(self) -> None:
        now = self._now()
        if self.rate < self._ceiling and now - self._last_change >= _RECOVER_AFTER:
            self.rate = min(self._ceiling, self.rate * 1.25)
            self._last_change = now
        start = max(now, self._next)
        self._next = start + 1.0 / self.rate
        wait = start - now
        if wait > 0:
            await (self._sleep or _sleep)(wait)

    def slow_down(self) -> None:
        now = self._now()
        if now - self._last_slowdown < _SLOWDOWN_COOLDOWN:
            return  # one burst of 429s counts once
        self._last_slowdown = self._last_change = now
        self.rate = max(min(_MIN_REQUESTS_PER_SECOND, self._ceiling), self.rate / 2.0)


class _Pacer:
    """Installed for the backfill's tasks via tmdb_sync._tmdb_pacing."""

    def __init__(self, limiter: _RateLimiter):
        self.limiter = limiter
        self._in_flight = asyncio.Semaphore(_MAX_IN_FLIGHT)

    @contextlib.asynccontextmanager
    async def slot(self):
        async with self._in_flight:
            await self.limiter.acquire()
            yield

    def throttled(self) -> None:
        self.limiter.slow_down()


# GH #82: "stills_filled" counts episode images added alongside the air dates ("filled").
_COUNTERS = ("total", "processed", "filled", "stills_filled", "skipped_no_tmdb_id", "errors")
_task: asyncio.Task | None = None


def _load() -> dict:
    raw = config.get_air_date_backfill()
    # GH #82: a run that finished before episode images were part of this job (no "stills_filled"
    # counter on record) only did the dates, so it is offered again as a fresh, pending run.
    if raw.get("state") == "done" and "stills_filled" not in raw:
        raw = {}
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


async def _process(series_id: int, state: dict, tmdb_locks: dict) -> None:
    try:
        series = await asyncio.to_thread(vod_db.get_series, series_id)
        tmdb_id = str((series or {}).get("tmdb_id") or "").strip()
        # Series sharing a TMDB id take turns: the second finds the first one's data in the
        # local library instead of asking TMDB again.
        async with tmdb_locks.setdefault(tmdb_id, asyncio.Lock()):
            before = await asyncio.to_thread(vod_db.count_episodes_missing_air_date, series_id)
            stills_before = await asyncio.to_thread(vod_db.count_episodes_missing_still, series_id)
            await vod_importer.fill_series_air_dates(series_id)
            after = await asyncio.to_thread(vod_db.count_episodes_missing_air_date, series_id)
            stills_after = await asyncio.to_thread(vod_db.count_episodes_missing_still, series_id)
        state["filled"] += max(0, before - after)
        state["stills_filled"] += max(0, stills_before - stills_after)
    except Exception as exc:  # a bad series is logged and skipped, never aborts the run
        state["errors"] += 1
        logger.warning("[air_date_backfill] series_id=%s skipped: %s", series_id, exc)
    # No await below: counters change only in this synchronous stretch, so workers cannot race.
    state["processed"] += 1
    if state["processed"] % _SAVE_EVERY == 0:
        _save(state)


async def _worker(queue: asyncio.Queue, state: dict, tmdb_locks: dict) -> None:
    while True:
        try:
            series_id = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        await _process(series_id, state, tmdb_locks)


async def run() -> None:
    state = _load()
    workers: list[asyncio.Task] = []
    try:
        ids = await asyncio.to_thread(vod_db.list_series_ids_missing_air_dates)
        state["skipped_no_tmdb_id"] = await asyncio.to_thread(vod_db.count_series_missing_air_dates_without_tmdb_id)
        # Resume: series already finished have no missing dates and drop out of `ids`.
        state["total"] = state["processed"] + len(ids)
        _save(state)
        queue: asyncio.Queue = asyncio.Queue()
        for series_id in ids:
            queue.put_nowait(series_id)
        tmdb_locks: dict = {}
        # The pacer is set before the workers start, so each worker task inherits it; no other
        # part of the app sees it.
        token = tmdb_sync._tmdb_pacing.set(_Pacer(_RateLimiter(_TARGET_REQUESTS_PER_SECOND, _HARD_CAP_REQUESTS_PER_SECOND)))
        try:
            workers = [asyncio.create_task(_worker(queue, state, tmdb_locks)) for _ in range(_WORKERS)]
        finally:
            tmdb_sync._tmdb_pacing.reset(token)
        await asyncio.gather(*workers)
        state["state"] = "done"
        state["finished_at"] = time.time()
        _save(state)
        logger.info("[air_date_backfill] done: %s", {k: state[k] for k in _COUNTERS})
    except asyncio.CancelledError:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        _save(state)  # stays "running" on disk; resumed at next startup
        raise
    except Exception as exc:
        # Leave the state "running" so Start-up resume retries; surface the cause in the log.
        logger.error("[air_date_backfill] stopped early: %s", exc)
        for w in workers:
            w.cancel()
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

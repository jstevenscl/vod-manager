"""Automatic TMDB episode titles.

Keeps episode names filled in as a provider adds episodes, without the user
re-running "Use TMDB episode titles". Three modes (config `auto_episode_titles`):

  off      -- nothing runs on its own
  applied  -- only series the user has already applied TMDB titles to (default)
  all      -- every series that has a confirmed TMDB id

It is always "fill" mode: only blank/placeholder names are replaced and a name
the user set or locked is never touched. Names come from the local TMDB library
first; TMDB is only asked for what that leaves unfilled. A series is only sent to TMDB when it
actually has an unlocked placeholder-named episode, and is then not looked at
again for a day, so an idle library costs no TMDB traffic."""

import asyncio
import logging
import time

import tmdb_sync
import vod_db
from config import get_auto_episode_titles_mode

logger = logging.getLogger(__name__)

RECHECK_SECONDS = 24 * 3600
# 100/hour left a 3k-series backlog ~32h behind; ~1 TMDB call per season
MAX_SERIES_PER_RUN = 250

_status: dict = {"last_run_at": None, "running": False, "last_result": None}
_run_lock = asyncio.Lock()


def get_status() -> dict:
    return dict(_status)


async def _fill(series_id: int, episodes: list[dict]) -> int:
    names = {(e["season_number"], e["episode_number"]): e["name"] for e in episodes if e.get("name")}
    if not names:
        return 0
    applied = await asyncio.to_thread(vod_db.apply_tmdb_episode_titles, series_id, names, "fill", False)
    return applied.get("renamed") or 0


async def run_once(mode: str | None = None, limit: int = MAX_SERIES_PER_RUN) -> dict:
    """One pass over up to `limit` due series. A second concurrent caller gets a
    RuntimeError instead of doubling up on TMDB."""
    mode = mode or get_auto_episode_titles_mode()
    result = {"mode": mode, "series_checked": 0, "series_changed": 0, "episodes_renamed": 0, "from_local": 0, "failed": 0}
    if mode not in ("applied", "all"):
        return result
    if _run_lock.locked():
        raise RuntimeError("an automatic episode-title pass is already running")
    async with _run_lock:
        _status["running"] = True
        try:
            due = await asyncio.to_thread(
                vod_db.list_series_due_for_auto_episode_titles, mode == "applied", time.time() - RECHECK_SECONDS, limit)
            for series in due:
                result["series_checked"] += 1
                try:
                    # Local TMDB library first; TMDB only for what it left as placeholders.
                    tmdb_id = str(series["tmdb_id"])
                    renamed = await _fill(series["id"], await tmdb_sync.get_series_episode_list_local(tmdb_id))
                    if renamed:
                        result["from_local"] += 1
                    if await asyncio.to_thread(vod_db.series_has_placeholder_episodes, series["id"]):
                        renamed += await _fill(series["id"], await tmdb_sync.get_series_episode_list_cached(tmdb_id))
                    if renamed:
                        result["series_changed"] += 1
                        result["episodes_renamed"] += renamed
                except Exception as exc:  # a stale id or a TMDB hiccup on one series must not stop the pass
                    result["failed"] += 1
                    logger.info("[auto_episode_titles] %s: %s", series.get("name"), tmdb_sync._redact(exc))
                finally:
                    await asyncio.to_thread(vod_db.mark_series_episode_titles_checked, series["id"], time.time())
            return result
        finally:
            _status.update({"running": False, "last_run_at": time.time(), "last_result": result})

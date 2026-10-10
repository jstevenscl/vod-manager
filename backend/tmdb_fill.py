"""
Keeps the local TMDB store (tmdb_store) filled and fresh in the background.

Active only once a TMDB API key is saved (and the store isn't disabled in its
settings). Each cycle, as due:
  - import TMDB's daily ID export (keyless file download, once a day) -- the
    work list, ranked by TMDB popularity;
  - refresh: mark stored titles TMDB reports as changed (/movie/changes,
    /tv/changes) so they're refetched;
  - a small burst: refetch changed titles first, then fetch the most popular
    not-yet-stored titles, a few times a day, under a daily request budget.

Also the store's lookup entry point: local name/ID search first, live TMDB
search (results stored) only on a local miss.
"""

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import httpx

import tmdb_automatch
import tmdb_store
import tmdb_sync
from config import DATA_DIR, get_tmdb_api_key, get_tmdb_store_settings

logger = logging.getLogger(__name__)

_EXPORT_URL = "https://files.tmdb.org/p/exports/{kind}_ids_{date}.json.gz"
_EXPORT_KINDS = {"movie": "movie", "tv": "tv_series"}
_DAY = 86400
_POLL_SECONDS = 300
_CHANGES_MAX_DAYS = 14  # TMDB's /changes window limit

_lock = asyncio.Lock()
status: dict = {"running": False, "phase": None, "done": 0, "total": 0, "last_result": None, "last_error": None}


def _set(**kw) -> None:
    status.update(kw)


async def import_exports() -> dict:
    """Download today's (or the most recent available) daily ID export per
    type and replace export_ids with it."""
    out = {}
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        for media_type, kind in _EXPORT_KINDS.items():
            _set(phase=f"downloading {media_type} id export")
            out[media_type] = None
            for days_back in range(3):
                day = datetime.now(timezone.utc) - timedelta(days=days_back)
                url = _EXPORT_URL.format(kind=kind, date=day.strftime("%m_%d_%Y"))
                path = DATA_DIR / f"tmdb_export_{media_type}.json.gz"
                try:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            continue
                        with open(path, "wb") as fh:
                            async for chunk in resp.aiter_bytes(1 << 16):
                                fh.write(chunk)
                    _set(phase=f"importing {media_type} id export")
                    count = await asyncio.to_thread(tmdb_store.import_export_file, media_type, path)
                    await asyncio.to_thread(tmdb_store.set_meta, f"export_date:{media_type}", day.strftime("%Y-%m-%d"))
                    out[media_type] = count
                    logger.info("[tmdb_fill] imported %d %s ids from %s export", count, media_type, day.date())
                    break
                except Exception as exc:
                    logger.warning("[tmdb_fill] %s export %s failed: %s", media_type, day.date(), exc)
                finally:
                    path.unlink(missing_ok=True)
    await asyncio.to_thread(tmdb_store.set_meta, "exports_imported_at", time.time())
    return out


async def refresh_changes() -> dict:
    """Flag stored titles TMDB changed since the last check for refetch."""
    api_key = get_tmdb_api_key()
    if not api_key:
        return {}
    now = datetime.now(timezone.utc)
    out = {}
    for media_type in tmdb_store.MEDIA_TYPES:
        _set(phase=f"checking {media_type} changes")
        last = float(await asyncio.to_thread(tmdb_store.get_meta, f"changes_checked:{media_type}", "0") or 0)
        start = now - timedelta(days=1) if not last else max(
            datetime.fromtimestamp(last, timezone.utc), now - timedelta(days=_CHANGES_MAX_DAYS)
        )
        ids: list[int] = []
        page, pages = 1, 1
        while page <= pages:
            async with tmdb_sync._tmdb_semaphore:
                r = await tmdb_sync._tmdb_get(
                    f"{tmdb_sync._API_BASE}/{media_type}/changes",
                    params={
                        "api_key": api_key, "page": page,
                        "start_date": start.strftime("%Y-%m-%d"), "end_date": now.strftime("%Y-%m-%d"),
                    },
                )
            r.raise_for_status()
            data = r.json()
            ids += [int(item["id"]) for item in data.get("results", []) if item.get("id") is not None]
            pages = min(int(data.get("total_pages") or 1), 500)
            page += 1
        await asyncio.to_thread(tmdb_store.add_requests, page - 1)
        marked = await asyncio.to_thread(tmdb_store.mark_changed, media_type, ids)
        await asyncio.to_thread(tmdb_store.set_meta, f"changes_checked:{media_type}", now.timestamp())
        out[media_type] = {"changed_on_tmdb": len(ids), "stored_marked": max(marked, 0)}
    return out


async def run_burst(size: int | None = None) -> dict:
    """Refetch changed titles first, then the most popular unfilled ones."""
    settings = get_tmdb_store_settings()
    size = settings["burst_size"] if size is None else size
    budget_left = max(0, settings["daily_budget"] - await asyncio.to_thread(tmdb_store.requests_today))
    size = min(size, budget_left)
    if size <= 0:
        return {"fetched": 0, "failed": 0, "gone": 0, "skipped": "daily budget reached"}
    per_type = math.ceil(size / len(tmdb_store.MEDIA_TYPES))
    work: list[tuple[str, int]] = []
    for media_type in tmdb_store.MEDIA_TYPES:
        stale = await asyncio.to_thread(tmdb_store.list_stale_ids, media_type, per_type)
        fresh = await asyncio.to_thread(
            tmdb_store.list_fill_candidates, media_type, per_type - len(stale), settings["prefill_top"]
        ) if len(stale) < per_type else []
        work += [(media_type, i) for i in stale + fresh]
    work = work[:size]
    counts = {"fetched": 0, "failed": 0, "gone": 0}
    _set(phase="filling", done=0, total=len(work))
    sem = asyncio.Semaphore(max(1, settings["concurrency"]))

    async def one(media_type: str, tmdb_id: int) -> None:
        async with sem:
            try:
                payload = await tmdb_sync.fetch_title_payload(media_type, tmdb_id, use_store=False)
                counts["fetched" if payload is not None else "failed"] += 1
            except tmdb_sync.TmdbNotFoundError:
                counts["gone"] += 1
            except Exception as exc:
                counts["failed"] += 1
                logger.warning("[tmdb_fill] %s %s failed: %s", media_type, tmdb_id, exc)
            status["done"] += 1

    await asyncio.gather(*[one(t, i) for t, i in work])
    logger.info("[tmdb_fill] burst: %s", counts)
    return counts


async def _run(phase: str, coro) -> dict:
    async with _lock:
        _set(running=True, phase=phase, done=0, total=0, last_error=None)
        try:
            result = await coro
            _set(last_result={"job": phase, "at": time.time(), **(result or {})})
            return result
        except Exception as exc:
            _set(last_error=f"{phase}: {tmdb_sync._redact(exc)}")
            logger.warning("[tmdb_fill] %s failed: %s", phase, tmdb_sync._redact(exc))
            return {"error": str(status["last_error"])}
        finally:
            _set(running=False, phase=None)


async def run_job(job: str) -> dict:
    jobs = {"export": import_exports, "changes": refresh_changes, "burst": run_burst,
            "match": tmdb_automatch.run_auto_match}
    return await _run(job, jobs[job]())


async def run_due_jobs() -> bool:
    """One scheduler tick. Returns False when the store is inactive."""
    settings = get_tmdb_store_settings()
    if not get_tmdb_api_key() or not settings["enabled"]:
        return False
    now = time.time()
    meta = lambda key: float(tmdb_store.get_meta(key, "0") or 0)  # noqa: E731
    if now - await asyncio.to_thread(meta, "exports_imported_at") >= _DAY:
        await _run("export", import_exports())
    if now - await asyncio.to_thread(meta, "changes_run_at") >= _DAY:
        await _run("changes", refresh_changes())
        await asyncio.to_thread(tmdb_store.set_meta, "changes_run_at", now)
    if now >= await asyncio.to_thread(meta, "next_burst_at"):
        await _run("burst", run_burst())
        # Then assign TMDB IDs to catalog titles that have none.
        if settings["auto_match_batch"]:
            await _run("match", tmdb_automatch.run_auto_match())
        interval = _DAY / max(1, settings["bursts_per_day"])
        await asyncio.to_thread(tmdb_store.set_meta, "next_burst_at", time.time() + interval)
    return True


async def fill_loop() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            await run_due_jobs()
        except Exception as exc:
            logger.warning("[tmdb_fill] scheduler tick failed: %s", exc)
        await asyncio.sleep(_POLL_SECONDS)


def _present(row: dict) -> dict:
    row = dict(row)
    path = row.pop("poster_path", None)
    row["poster_url"] = f"https://image.tmdb.org/t/p/w185{path}" if path else None
    return row


def _row_from_tmdb(media_type: str, item: dict) -> dict:
    """A TMDB detail payload or /search item in the stored-row shape _present takes."""
    is_movie = media_type == "movie"
    date = item.get("release_date" if is_movie else "first_air_date") or None
    cast = [c.get("name") for c in (item.get("credits") or {}).get("cast", [])[:5] if c.get("name")]
    return {
        "media_type": media_type,
        "tmdb_id": int(item["id"]),
        "title": item.get("title" if is_movie else "name"),
        "original_title": item.get("original_title" if is_movie else "original_name"),
        "year": tmdb_store._year(date),
        "release_date": date,
        "poster_path": item.get("poster_path") or None,
        "overview": item.get("overview") or None,
        "vote_average": item.get("vote_average") or None,
        "popularity": item.get("popularity"),
        "content_rating": tmdb_store._us_rating(media_type, item),
        "top_cast": ", ".join(cast) or None,
        "genres": ", ".join(g["name"] for g in item.get("genres") or [] if g.get("name")) or None,
    }


async def _live_lookup(query: str, media_type: str, year: int | None) -> dict:
    """Library off: straight to TMDB, nothing stored."""
    if query.isdigit():
        try:
            data = await tmdb_sync.fetch_title_payload(media_type, query)
        except tmdb_sync.TmdbNotFoundError:
            data = None
        return {"source": "tmdb", "results": [_present(_row_from_tmdb(media_type, data))] if data else []}
    api_key = get_tmdb_api_key()
    if not api_key or not query:
        return {"source": "tmdb", "results": []}
    params = {"api_key": api_key, "query": query}
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    async with tmdb_sync._tmdb_semaphore:
        r = await tmdb_sync._tmdb_get(f"{tmdb_sync._API_BASE}/search/{media_type}", params=params)
    r.raise_for_status()
    items = [i for i in r.json().get("results", [])[:10] if i.get("id") is not None]
    return {"source": "tmdb", "results": [_present(_row_from_tmdb(media_type, i)) for i in items]}


async def lookup(query: str, media_type: str, year: int | None = None, limit: int = 10) -> dict:
    """Local first; on a miss, live TMDB search whose top results get stored.
    With the library off, always live and nothing is stored."""
    query = (query or "").strip()
    if not tmdb_sync.store_enabled():
        return await _live_lookup(query, media_type, year)
    if query.isdigit():
        local = await asyncio.to_thread(tmdb_store.get_title, media_type, int(query))
        if local:
            return {"source": "local", "results": [_present(local)]}
        try:
            await tmdb_sync.fetch_title_payload(media_type, query)
        except tmdb_sync.TmdbNotFoundError:
            return {"source": "tmdb", "results": []}
        row = await asyncio.to_thread(tmdb_store.get_title, media_type, int(query))
        return {"source": "tmdb", "results": [_present(row)] if row else []}

    hits = await asyncio.to_thread(tmdb_store.search, media_type, query, year, limit)
    if hits:
        return {"source": "local", "results": [_present(h) for h in hits]}
    api_key = get_tmdb_api_key()
    if not api_key or not query:
        return {"source": "local", "results": []}
    params = {"api_key": api_key, "query": query}
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    async with tmdb_sync._tmdb_semaphore:
        r = await tmdb_sync._tmdb_get(f"{tmdb_sync._API_BASE}/search/{media_type}", params=params)
    r.raise_for_status()
    await asyncio.to_thread(tmdb_store.add_requests, 1)
    ids = [int(item["id"]) for item in r.json().get("results", [])[:5] if item.get("id") is not None]

    async def store_one(tmdb_id: int) -> None:
        try:
            await tmdb_sync.fetch_title_payload(media_type, tmdb_id)
        except Exception:
            pass

    await asyncio.gather(*[store_one(i) for i in ids])
    rows = [await asyncio.to_thread(tmdb_store.get_title, media_type, i) for i in ids]
    return {"source": "tmdb", "results": [_present(r) for r in rows if r]}

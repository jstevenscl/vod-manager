"""Language auto-clean: hide an unwanted-language feed without a person clicking.

KNM: 2026-11-05 - default OFF. A feed is hidden only when three independent
signals agree: the audio probe found tracks and none is a wanted language, TMDB's
original_language is not wanted, and no TMDB spoken language is wanted. It only
hides a feed while another wanted feed keeps the title playable (never archives
a title), never touches a source someone already decided, logs every change so
it can be reverted, and runs small daytime batches that pause while an import,
sync or enrichment is running, then wait a cooldown before resuming.
"""
import asyncio
import json
import logging
import time
import zlib

import config
import tmdb_store
import vod_db

logger = logging.getLogger(__name__)

_last_busy_at: float = time.time()  # a restart counts as activity: wait one cooldown first
_state: dict = {"last_run_at": None, "last_status": None, "last_applied": 0, "busy_reason": None}


def _now() -> float:
    return time.time()


def _busy_reason() -> str | None:
    import tmdb_fill
    import vod_importer
    # KNM: 2026-11-06 - getattr so builds without a given job tracker still work.
    def running(name, key="running"):
        return bool((getattr(vod_importer, name, None) or {}).get(key))
    if running("_IMPORT_PROGRESS") or running("_IMPORT_PROGRESS", "queued"):
        return "import"
    # The job trackers below are the ones this codebase actually has; the getattr names
    # further down exist only in some builds, so check these directly too.
    import_lock = getattr(vod_importer, "_XC_IMPORT_LOCK", None)
    if import_lock is not None and import_lock.locked():
        return "import"
    if getattr(vod_importer, "_EPISODE_TRICKLE_RUNNING", False):
        return "episode_preloading"
    if (getattr(vod_importer, "_CATALOG_WORKFLOW_PROGRESS", None) or {}).get("state") in ("queued", "running"):
        return "catalog_workflow"
    if running("_ENRICH_PROGRESS") or running("_TMDB_ENRICH_PROGRESS"):
        return "enrichment"
    task = getattr(vod_importer, "_POST_IMPORT_ENRICH_TASK", None)
    if task is not None and not task.done():
        return "enrichment"
    if running("_EPISODE_DISCOVERY_PROGRESS"):
        return "episode_discovery"
    if (getattr(tmdb_fill, "status", None) or {}).get("running"):
        return "tmdb_sync"
    return None


def _tmdb_languages(media_type: str, tmdb_id) -> tuple[str | None, set[str]] | None:
    """(original_language, spoken languages) from the local TMDB store, any age."""
    if not tmdb_id:
        return None
    try:
        with tmdb_store._conn() as conn:
            row = conn.execute("SELECT raw FROM titles WHERE media_type=? AND tmdb_id=?",
                               (media_type, int(tmdb_id))).fetchone()
    except Exception:
        return None
    if row is None or row["raw"] is None:
        return None
    try:
        data = json.loads(zlib.decompress(row["raw"]))
    except (ValueError, zlib.error):
        return None
    original = vod_db.canonical_language(data.get("original_language"))
    spoken = {vod_db.canonical_language(s.get("iso_639_1")) for s in data.get("spoken_languages") or [] if s.get("iso_639_1")}
    if not original and not spoken:
        return None
    return original, spoken


_MOVIE_SQL = """
    SELECT 'movie' AS kind, src.id AS source_id, src.language, src.language_probe_languages,
           m.id AS parent_id, m.tmdb_id, m.name AS title,
           EXISTS (SELECT 1 FROM movie_sources o WHERE o.movie_id=src.movie_id AND o.id!=src.id
                   AND (COALESCE(o.language,'EN') IN ({ph}) OR o.language='MULTI')) AS has_sibling
    FROM movie_sources src JOIN movies m ON m.id=src.movie_id
    WHERE src.language_probe_status='observed' AND src.language_probe_decision IS NULL
      AND src.language_probe_languages IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM language_autoclean_log l
                      WHERE l.kind='movie' AND l.source_id=src.id AND l.reverted_at IS NOT NULL)
    ORDER BY src.id
"""
_EPISODE_SQL = """
    SELECT 'episode' AS kind, src.id AS source_id, src.language, src.language_probe_languages,
           s.id AS parent_id, s.tmdb_id, s.name AS title,
           (EXISTS (SELECT 1 FROM episode_sources o WHERE o.episode_id=src.episode_id AND o.id!=src.id
                    AND (COALESCE(o.language,'EN') IN ({ph}) OR o.language='MULTI'))
            AND EXISTS (SELECT 1 FROM series_sources ss WHERE ss.series_id=s.id AND ss.provider_id!=src.provider_id
                        AND (COALESCE(ss.language,'EN') IN ({ph}) OR ss.language='MULTI'))) AS has_sibling
    FROM episode_sources src JOIN episodes e ON e.id=src.episode_id JOIN series s ON s.id=e.series_id
    WHERE src.language_probe_status='observed' AND src.language_probe_decision IS NULL
      AND src.language_probe_languages IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM language_autoclean_log l
                      WHERE l.kind='episode' AND l.source_id=src.id AND l.reverted_at IS NOT NULL)
    ORDER BY src.id
"""


def _scan(limit: int, count_all: bool) -> dict:
    enabled = list(vod_db.get_enabled_languages())
    wanted = {vod_db.canonical_language(e) for e in enabled} | {"MULTI"}
    ph = ",".join("?" for _ in enabled)
    skipped = {k: 0 for k in ("probe_has_wanted", "only_source", "no_tmdb_data",
                              "tmdb_original_language", "tmdb_spoken_language")}
    candidates: list[dict] = []
    conn = vod_db._connect()
    try:
        for sql, media_type, params in ((_MOVIE_SQL, "movie", enabled), (_EPISODE_SQL, "tv", enabled * 2)):
            for row in conn.execute(sql.format(ph=ph), params):
                if not count_all and len(candidates) >= limit:
                    break
                try:
                    observed = [o for o in json.loads(row["language_probe_languages"] or "[]") if o and o != "UNKNOWN"]
                except (TypeError, ValueError):
                    observed = []
                if not observed:
                    continue  # unknown/untagged stays with Unknown Language Review
                if {vod_db.canonical_language(o) for o in observed} & wanted:
                    skipped["probe_has_wanted"] += 1
                    continue
                if not row["has_sibling"]:
                    skipped["only_source"] += 1
                    continue
                tmdb = _tmdb_languages(media_type, row["tmdb_id"])
                if tmdb is None:
                    skipped["no_tmdb_data"] += 1
                    continue
                original, spoken = tmdb
                if original in wanted:
                    skipped["tmdb_original_language"] += 1
                    continue
                if spoken & wanted:
                    skipped["tmdb_spoken_language"] += 1
                    continue
                if len(candidates) < limit:
                    candidates.append({"kind": row["kind"], "source_id": row["source_id"], "title": row["title"],
                                       "current_language": row["language"], "observed_languages": sorted(observed),
                                       "tmdb_original_language": original, "tmdb_spoken_languages": sorted(spoken)})
    finally:
        conn.close()
    return {"enabled_languages": enabled, "candidates": candidates, "skipped": skipped}


def preview(limit: int = 200) -> dict:
    """Dry run: what the next batches would hide, and why the rest were left alone."""
    return _scan(limit, count_all=True)


def apply_batch(limit: int) -> dict:
    found = _scan(limit, count_all=False)["candidates"]
    applied = 0
    for c in found:
        table = "movie_sources" if c["kind"] == "movie" else "episode_sources"
        conn = vod_db._connect()
        try:
            row = conn.execute(f"SELECT language, language_probe_decision FROM {table} WHERE id=?", (c["source_id"],)).fetchone()
        finally:
            conn.close()
        if not row or row["language_probe_decision"] is not None:
            continue
        try:
            result = vod_db.decide_language_mismatch(c["kind"], c["source_id"], "approved")
        except ValueError as exc:
            logger.info("[language-autoclean] skipped %s %s: %s", c["kind"], c["source_id"], exc)
            continue
        vod_db.log_language_autoclean(c["kind"], c["source_id"], row["language"], result.get("language"),
                                      c["observed_languages"], c["tmdb_original_language"])
        applied += 1
    return {"applied": applied, "considered": len(found)}


def list_log(limit: int = 200) -> list[dict]:
    return vod_db.list_language_autoclean_log(limit)


def revert(log_id: int) -> bool:
    return vod_db.revert_language_autoclean(log_id)


async def run_cycle() -> dict:
    global _last_busy_at
    settings = config.get_language_autoclean_settings()
    if not settings["language_autoclean_enabled"]:
        status = {"status": "disabled"}
    elif (reason := _busy_reason()):
        _last_busy_at = _now()
        status = {"status": "paused_busy", "reason": reason}
    elif _now() - _last_busy_at < settings["language_autoclean_cooldown_seconds"]:
        status = {"status": "cooldown"}
    else:
        result = await asyncio.to_thread(apply_batch, settings["language_autoclean_batch_size"])
        status = {"status": "applied" if result["applied"] else "idle", **result}
    _state.update(last_run_at=_now(), last_status=status["status"], busy_reason=status.get("reason"),
                  last_applied=status.get("applied", 0))
    return status


def get_status() -> dict:
    return dict(_state)


async def autoclean_loop() -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await run_cycle()
        except Exception:
            logger.exception("[language-autoclean] cycle failed")
        await asyncio.sleep(config.get_language_autoclean_settings()["language_autoclean_interval_seconds"])

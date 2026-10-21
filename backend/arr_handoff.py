"""Library handoff: gets finished downloads into Radarr/Sonarr (and so into Plex).

1. When a download is queued, the title is looked up in Radarr/Sonarr and added if
   missing (never searching). Jobs go import_status add -> ready | needs_manual.
2. When the file appears in the "finished" folder (after an encoder such as Tdarr),
   the watcher asks Radarr/Sonarr to import it and confirms the import.
Off until Radarr/Sonarr are configured (config.handoff_enabled)."""

import asyncio
import logging
import time
from pathlib import Path

import config
import library_presence
import notices
import vod_db
from arr_client import ArrClient, ArrError

logger = logging.getLogger(__name__)

_APP = {"movie": "radarr", "episode": "sonarr"}
MANUAL_NOTE = "Add this title to Radarr/Sonarr manually"


def client_for(kind: str) -> ArrClient | None:
    """Radarr client for movie jobs, Sonarr for episode jobs; None when not configured."""
    app = _APP[kind]
    url, key = config.get_handoff_settings()[f"{app}_url"], config.get_handoff_api_key(app)
    return ArrClient(app, url, key) if url and key else None


def _title_for(job: dict) -> tuple[tuple, int | None, str, int | None]:
    """(group key, tmdb id, title, year) of the movie or series the job belongs to."""
    if job["kind"] == "movie":
        row = vod_db.get_movie(job["item_id"])
        key = ("movie", job["item_id"])
    else:
        episode = vod_db.get_episode(job["item_id"])
        row = vod_db.get_series(episode["series_id"]) if episode else None
        key = ("series", episode["series_id"] if episode else None)
    if not row:
        return key, None, "", None
    tmdb = row.get("tmdb_id")
    return key, int(tmdb) if tmdb and str(tmdb).isdigit() else None, row["name"], row.get("year")


def _resolve_group(client, kind: str, tmdb: int | None, title: str, year: int | None) -> tuple[str, int | None, str | None]:
    """(import_status, arr_id, note) for one movie/series."""
    try:
        item = client.lookup(tmdb, title, year)
        if item is None:
            return "needs_manual", None, MANUAL_NOTE
        if not item.get("id"):
            s = config.get_handoff_settings()
            app = _APP[kind]
            monitor = s["new_movie_monitor"] if kind == "movie" else s["new_show_monitor"]
            item = client.add(item, s[f"{app}_root"], s[f"{app}_profile_id"], monitor)
            logger.info("[handoff] added %r to %s (monitor=%s, no search)", title, app, monitor)
        return "ready", int(item["id"]), None
    except ArrError as exc:
        return "add", None, str(exc)                # stays 'add': retried on the next tick


def _resolve_pending_sync() -> None:
    groups: dict[tuple, list[dict]] = {}
    meta: dict[tuple, tuple] = {}
    for job in vod_db.list_import_jobs(("add",)):
        key, tmdb, title, year = _title_for(job)
        groups.setdefault(key, []).append(job)
        meta[key] = (job["kind"], tmdb, title, year)
    for key, jobs in groups.items():
        kind, tmdb, title, year = meta[key]
        client = client_for(kind)
        if client is None:
            continue
        status, arr_id, note = _resolve_group(client, kind, tmdb, title, year) if title else \
            ("needs_manual", None, MANUAL_NOTE)
        for job in jobs:
            vod_db.update_import(job["id"], import_status=status, arr_id=arr_id, import_note=note)


async def resolve_pending() -> None:
    """Looks up / adds the titles of every 'add' job, one call per movie or series."""
    await asyncio.to_thread(_resolve_pending_sync)


# ── import watcher ──────────────────────────────────────────────────────────

VIDEO_EXTS = {"mkv", "mp4", "m4v", "avi", "ts", "mov", "webm"}
STABLE_SECS = 60                # the finished file must stop growing this long before importing
COMMAND_TIMEOUT_SECS = 15 * 60
CAUSE_IN_DOWNLOADS = "The encoder hasn't picked it up or hasn't finished. Check its queue or nodes."
CAUSE_NOWHERE = "The encoder may have failed or moved it. Check its error list."
_PREFIX = {"movie": "movies", "episode": "shows"}


def _download_root(kind: str) -> Path:
    import vod_downloader  # lazy: vod_downloader imports config/vod_db at load, avoid a cycle
    return vod_downloader.roots_for(config.get_download_settings())[kind]


def _finished_root(kind: str, settings: dict) -> Path:
    """Where finished files appear as vod-manager sees it; blank = the download folder (no encoder)."""
    custom = settings[f"{_PREFIX[kind]}_finished_dir"]
    return Path(custom) if custom else _download_root(kind)


def finished_path_for(job: dict, settings: dict) -> Path | None:
    """The job's file in the finished folder: same relative folder and stem, any video
    extension (an encoder may turn .mp4 into .mkv)."""
    final = Path(job["final_path"])
    folder = _finished_root(job["kind"], settings) / final.parent
    if not folder.is_dir():
        return None
    for entry in sorted(folder.iterdir()):
        if (entry.is_file() and entry.name.startswith(final.stem + ".")
                and entry.suffix.lstrip(".").lower() in VIDEO_EXTS):
            return entry
    return None


def to_arr_path(local: Path, settings: dict, kind: str) -> str:
    """The same path as Radarr/Sonarr sees it (their container may mount the share elsewhere)."""
    arr_dir = settings[f"{_PREFIX[kind]}_arr_dir"]
    if not arr_dir:
        return str(local)
    return arr_dir.rstrip("/") + "/" + local.relative_to(_finished_root(kind, settings)).as_posix()


def _import_target(found: Path, settings: dict, kind: str) -> str:
    # Radarr imports a movie folder; Sonarr gets the single episode file
    return to_arr_path(found.parent if kind == "movie" else found, settings, kind)


def likely_cause(job: dict, settings: dict) -> str:
    return CAUSE_IN_DOWNLOADS if (_download_root(job["kind"]) / job["final_path"]).exists() else CAUSE_NOWHERE


def _episode_numbers(job: dict) -> tuple[int | None, int | None]:
    if job["kind"] != "episode":
        return None, None
    ep = vod_db.get_episode(job["item_id"]) or {}
    return ep.get("season_number"), ep.get("episode_number")


def _fail(job: dict, status: str, note: str, **extra) -> None:
    vod_db.update_import(job["id"], import_status=status, import_note=note, **extra)
    notices.add("import", "error", f"Import failed: {job['title']}", note, job_id=job["id"], email=True)


def _stall_check(job: dict, settings: dict, now: float) -> None:
    hours = (now - (job["import_since"] or now)) / 3600
    stages = settings["stall_hours"]
    if hours >= settings["fail_hours"]:
        _fail(job, "failed", likely_cause(job, settings), import_stage=len(stages) + 1)
        return
    reached = sum(1 for limit in stages if hours >= limit)
    if reached > (job["import_stage"] or 0):
        # one notice per stage reached; a long gap between ticks sends only the latest
        notices.add("import", "warn", f"Still waiting for the encoder: {job['title']}",
                    likely_cause(job, settings), job_id=job["id"], email=True)
        vod_db.update_import(job["id"], import_stage=reached)


def _tick_waiting(job: dict, client, settings: dict, now: float) -> None:
    found = finished_path_for(job, settings)
    if found is None:
        _stall_check(job, settings, now)
        return
    if not job["arr_id"]:
        # also covers a download that finished before the queue-time add step ran
        _, tmdb, title, year = _title_for(job)
        _, arr_id, note = _resolve_group(client, job["kind"], tmdb, title, year)
        if not arr_id:
            vod_db.update_import(job["id"], import_note=note)
            return
        vod_db.update_import(job["id"], arr_id=arr_id, import_note=None)
    size = found.stat().st_size
    if size != job["import_seen_size"] or job["import_seen_at"] is None:
        vod_db.update_import(job["id"], import_seen_size=size, import_seen_at=now)
        return
    if now - job["import_seen_at"] < STABLE_SECS:
        return
    cmd_id = client.import_path(_import_target(found, settings, job["kind"]), job["arr_id"], *_episode_numbers(job))
    vod_db.update_import(job["id"], import_status="importing", import_cmd_id=cmd_id, import_cmd_at=now,
                         import_note=None)


def _tick_importing(job: dict, client, settings: dict, now: float) -> None:
    status = (client.command(job["import_cmd_id"]) or {}).get("status")
    if status == "completed" and client.has_file(job["arr_id"], *_episode_numbers(job)):
        vod_db.update_import(job["id"], import_status="imported", import_note=None)
        logger.info("[handoff] job=%s imported into %s", job["id"], _APP[job["kind"]])
        try:  # show the In Library badge now, before the next media-server scan
            library_presence.mark_imported(job)
        except Exception:
            logger.exception("[handoff] job=%s in-library mark failed", job["id"])
    elif status in ("completed", "failed", "aborted", "cancelled"):
        found = finished_path_for(job, settings)
        reason = client.rejection_for(_import_target(found, settings, job["kind"])) if found else None
        _fail(job, "failed", reason or "Radarr/Sonarr didn't import the file")
    elif now - (job["import_cmd_at"] or now) > COMMAND_TIMEOUT_SECS:
        _fail(job, "timed_out", "Radarr/Sonarr is still working on the import after 15 minutes")


def _tick_sync(now: float) -> None:
    _resolve_pending_sync()
    settings = config.get_handoff_settings()
    clients: dict = {}
    for job in vod_db.list_import_jobs(("waiting", "importing")):
        kind = job["kind"]
        if kind not in clients:
            clients[kind] = client_for(kind)
        if clients[kind] is None:
            continue
        step = _tick_waiting if job["import_status"] == "waiting" else _tick_importing
        try:
            step(job, clients[kind], settings, now)
        except ArrError as exc:
            vod_db.update_import(job["id"], import_note=str(exc))     # status kept; retried next tick
        except OSError as exc:
            logger.warning("[handoff] job=%s: %s", job["id"], exc)


async def tick(now: float | None = None) -> None:
    if not (config.handoff_enabled("movie") or config.handoff_enabled("episode")):
        return
    await asyncio.to_thread(_tick_sync, time.time() if now is None else now)


async def handoff_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await tick()
        except Exception:
            logger.exception("[handoff] tick failed")


async def retry_import(job_id: int) -> None:
    """Starts the job's import over: back to waiting, stall clock and stages reset."""
    await asyncio.to_thread(vod_db.update_import, job_id, import_status="waiting", import_stage=0,
                            import_note=None, import_cmd_id=None, import_cmd_at=None, import_seen_size=None,
                            import_seen_at=None, import_since=time.time())

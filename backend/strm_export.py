"""Export the curated VOD library as .strm files for Emby / Jellyfin.

Each .strm is a one-line text file holding a stream URL. Emby/Jellyfin can't
speak the Xtream Codes API for movies/shows, but they scan folders -- so the
exporter writes a Movies/ and Shows/ tree into a folder the media server has
mounted, and every file points back at this app's own /movie/... and /series/...
stream endpoints (the same ones XC clients use), so playback still goes through
VOD Manager's source failover.

What is exported follows the same rules as the XC catalog: only items placed in
an active category, language-gated sources, blocked streams excluded, and --
when a dedicated XC client with a category allowlist is chosen -- only that
client's categories. Layout follows Emby/Jellyfin naming:

    Movies/Title (Year) [tmdbid-N]/Title (Year).strm
    Shows/Show (Year) [tmdbid-N]/Season NN/Show - SNNENN.strm
"""

import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import config
import vod_db

logger = logging.getLogger("vod_manager")

MARKER_FILE = ".vod-manager-strm-export"
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_NAME = 120

_status_lock = threading.Lock()
_run_lock = threading.Lock()
_status: dict = {
    "running": False, "last_run_at": None, "last_error": None, "last_result": None,
}


def get_status() -> dict:
    with _status_lock:
        return dict(_status)


def _set_status(**fields) -> None:
    with _status_lock:
        _status.update(fields)


def safe_name(name: str) -> str:
    """A single path component: illegal/control characters removed, whitespace
    collapsed, trailing dots/spaces stripped (Windows shares reject them) and
    length capped. Never empty."""
    cleaned = _ILLEGAL.sub("", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")
    cleaned = cleaned[:_MAX_NAME].rstrip(". ")
    return cleaned or "Untitled"


def _title_year(name: str, year: int | None) -> str:
    base = safe_name(name)
    return f"{base} ({year})" if year else base


def _stream_url(base_url: str, kind: str, username: str, password: str, stream_id: int, ext: str | None) -> str:
    ext = re.sub(r"[^A-Za-z0-9]", "", ext or "") or "mp4"
    return f"{base_url}/{kind}/{quote(username, safe='')}/{quote(password, safe='')}/{stream_id}.{ext}"


def validate_settings(settings: dict) -> str | None:
    """Returns a human-readable problem, or None when the settings can run."""
    out = (settings.get("output_dir") or "").strip()
    if not out:
        return "Choose an output folder."
    path = Path(out)
    if not path.is_absolute():
        return "The output folder must be an absolute path."
    if path.parent == path:
        return "The output folder cannot be a filesystem root."
    base = (settings.get("base_url") or "").strip()
    if not re.match(r"^https?://[^/\s]+", base):
        return "Enter the base URL Emby/Jellyfin uses to reach VOD Manager, e.g. http://192.168.1.10:8282"
    if not settings.get("xc_client_id"):
        return "Choose the XC user whose credentials go into the .strm URLs."
    if not (settings.get("include_movies") or settings.get("include_series")):
        return "Turn on Movies, TV Shows or both."
    return None


def _check_output_dir(path: Path) -> None:
    """Refuse to write into (or, in mirror mode, delete from) a folder this
    exporter didn't create: it must be empty/new or carry our marker file."""
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"{path} is not a folder.")
        marker = path / MARKER_FILE
        if not marker.exists() and any(path.iterdir()):
            raise ValueError(
                f"{path} is not empty and wasn't created by VOD Manager. Pick an empty folder "
                f"(or one this exporter already uses) so nothing else in it can be touched."
            )


def build_plan(settings: dict) -> dict:
    """Pure read: the {relative_path: url} map the export would write, plus
    counts. No filesystem access, so it also powers the dry-run preview."""
    client = vod_db.get_xc_client(int(settings["xc_client_id"]))
    if not client:
        raise ValueError("The chosen XC user no longer exists.")
    if not client.get("enabled", 1):
        raise ValueError("The chosen XC user is disabled; its credentials would not work for playback.")
    base_url = settings["base_url"].strip().rstrip("/")
    user, pw = client["username"], client["password"]
    raw_allow = client.get("category_allowlist")
    allowed = {int(x) for x in raw_allow.split(",") if x.strip().isdigit()} if raw_allow else None
    include_unmatched = bool(settings.get("include_unmatched"))

    files: dict[str, str] = {}
    counts = {"movies": 0, "episodes": 0, "series": 0, "skipped_no_tmdb": 0}

    if settings.get("include_movies"):
        seen: set[int] = set()
        for row in vod_db.get_movie_export_rows():
            if allowed is not None and row["category_id"] not in allowed:
                continue
            if row["movie_id"] in seen:
                continue  # one file per movie even when it sits in several categories
            seen.add(row["movie_id"])
            title = _title_year(row["name"], row.get("year"))
            if row.get("tmdb_id"):
                folder = f"{title} [tmdbid-{row['tmdb_id']}]"
                rel = f"Movies/{folder}/{title}.strm"
            elif include_unmatched:
                rel = f"Movies/Unmatched/{title} [id-{row['movie_id']}]/{title}.strm"
            else:
                counts["skipped_no_tmdb"] += 1
                continue
            files[rel] = _stream_url(base_url, "movie", user, pw, row["export_stream_id"], row.get("container_extension"))
            counts["movies"] += 1

    if settings.get("include_series"):
        seen_series: set[int] = set()
        for row in vod_db.get_series_export_rows():
            if allowed is not None and row["category_id"] not in allowed:
                continue
            if row["series_id"] in seen_series:
                continue
            seen_series.add(row["series_id"])
            title = _title_year(row["name"], row.get("year"))
            if row.get("tmdb_id"):
                show_dir = f"Shows/{title} [tmdbid-{row['tmdb_id']}]"
            elif include_unmatched:
                show_dir = f"Shows/Unmatched/{title} [id-{row['series_id']}]"
            else:
                counts["skipped_no_tmdb"] += 1
                continue
            show_name = safe_name(row["name"])
            wrote_any = False
            for ep in vod_db.get_episode_export_rows_for_series(row["series_id"]):
                if ep.get("provider_id") is None:
                    continue  # no playable source in an enabled language: it would 404
                season, number = int(ep["season_number"]), int(ep["episode_number"])
                rel = f"{show_dir}/Season {season:02d}/{show_name} - S{season:02d}E{number:02d}.strm"
                files[rel] = _stream_url(base_url, "series", user, pw, ep["export_episode_id"], ep.get("container_extension"))
                counts["episodes"] += 1
                wrote_any = True
            if wrote_any:
                counts["series"] += 1

    return {"files": files, "counts": counts}


def _scan_existing(root: Path) -> set[str]:
    """Relative POSIX paths of every .strm under Movies/ and Shows/ -- the only
    files mirror mode is ever allowed to delete."""
    found: set[str] = set()
    for top in ("Movies", "Shows"):
        base = root / top
        if not base.is_dir():
            continue
        for dirpath, _dirs, filenames in os.walk(base):
            for fn in filenames:
                if fn.lower().endswith(".strm"):
                    found.add(Path(dirpath, fn).relative_to(root).as_posix())
    return found


def run_export(settings: dict, dry_run: bool = False) -> dict:
    """Writes (or, for dry_run, only counts) the .strm tree. Files whose content
    is unchanged are left untouched so media-server rescans stay cheap. In mirror
    mode, .strm files no longer in the plan are removed, then empty folders."""
    problem = validate_settings(settings)
    if problem:
        raise ValueError(problem)
    root = Path(settings["output_dir"].strip())
    _check_output_dir(root)
    plan = build_plan(settings)
    files: dict[str, str] = plan["files"]
    existing = _scan_existing(root) if root.exists() else set()
    result = dict(plan["counts"])
    result.update({"dry_run": dry_run, "written": 0, "updated": 0, "unchanged": 0, "removed": 0, "total": len(files)})

    for rel, url in files.items():
        target = root / rel
        if rel in existing:
            try:
                same = target.read_text(encoding="utf-8").strip() == url
            except OSError:
                same = False
            if same:
                result["unchanged"] += 1
                continue
            result["updated"] += 1
        else:
            result["written"] += 1
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(url + "\n", encoding="utf-8")
            os.replace(tmp, target)

    if settings.get("mirror"):
        stale = sorted(existing - set(files))
        result["removed"] = len(stale)
        if not dry_run:
            for rel in stale:
                try:
                    (root / rel).unlink()
                except OSError as exc:
                    logger.warning("[strm_export] couldn't remove %s: %s", rel, exc)
            for top in ("Movies", "Shows"):
                base = root / top
                if base.is_dir():
                    for dirpath, _dirs, _files in os.walk(base, topdown=False):
                        if Path(dirpath) != base and not any(Path(dirpath).iterdir()):
                            try:
                                Path(dirpath).rmdir()
                            except OSError:
                                pass

    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        (root / MARKER_FILE).write_text("Managed by VOD Manager's .strm export. Safe to point Emby/Jellyfin at the Movies and Shows folders.\n", encoding="utf-8")
    return result


def run_tracked_export(settings: dict | None = None, dry_run: bool = False) -> dict:
    """run_export wrapped with the status bookkeeping and a single-run lock, for
    the route and the scheduler. Raises ValueError for a settings problem,
    RuntimeError if another export is already running."""
    settings = settings if settings is not None else config.get_strm_export_settings()
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("An export is already running.")
    try:
        if not dry_run:
            _set_status(running=True, last_error=None)
        result = run_export(settings, dry_run=dry_run)
        if not dry_run:
            _set_status(running=False, last_run_at=time.time(), last_result=result)
        return result
    except Exception as exc:
        if not dry_run:
            _set_status(running=False, last_run_at=time.time(), last_error=str(exc))
        raise
    finally:
        _run_lock.release()

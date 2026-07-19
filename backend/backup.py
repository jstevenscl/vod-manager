"""
Per-component backup/restore/reset for this app's persisted state. Each
component (a config file or the database) can be downloaded, restored from a
previously-downloaded file, or reset to a fresh empty state independently of
the others -- e.g. a corrupt database can be wiped without touching saved
credentials, or a config can be rolled back without discarding the imported
catalog.

Restore and reset always move the current file aside to a timestamped backup
under DATA_DIR/backups/ rather than deleting it outright, so a mistake here
is itself recoverable. SQLite components are downloaded via a live backup
snapshot (sqlite3's own .backup() API) rather than copying the raw file, so
an in-progress write elsewhere in the app can never produce a torn/corrupt
download.
"""

import json
import logging
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse

from config import DATA_DIR
from routes import require_auth, require_configured
import vod_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backup", tags=["backup"])
_GUARDS = [Depends(require_configured), Depends(require_auth)]

_BACKUP_DIR = DATA_DIR / "backups"


class _Component:
    def __init__(self, id: str, filename: str, label: str, kind: str, reinit: Optional[Callable[[], None]] = None):
        self.id = id
        self.filename = filename
        self.label = label
        self.kind = kind  # "json" | "sqlite"
        self.reinit = reinit

    @property
    def path(self) -> Path:
        return DATA_DIR / self.filename


_COMPONENTS = [
    _Component("config", "config.json", "Configuration (Dispatcharr connection, TMDB key, login credentials)", "json"),
    _Component("sessions", "sessions.json", "Active login sessions", "json"),
    _Component("database", "vod_db.sqlite", "VOD database (providers, movies, series, categories)", "sqlite", reinit=vod_db.init_db),
]

_BY_ID = {c.id: c for c in _COMPONENTS}


def _get_component(component_id: str) -> _Component:
    c = _BY_ID.get(component_id)
    if not c:
        raise HTTPException(404, detail=f"unknown component '{component_id}'")
    return c


def _stash_current(component: "_Component") -> None:
    """Moves the current file (and, for sqlite, its -wal/-shm sidecars) aside
    into DATA_DIR/backups/ with a timestamp, rather than deleting it -- so
    restore/reset is itself reversible from the filesystem if something goes
    wrong."""
    if not component.path.exists():
        return
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        src = component.path.with_name(component.path.name + suffix) if suffix else component.path
        if src.exists():
            dest = _BACKUP_DIR / f"{component.filename}{suffix}.pre-{stamp}.bak"
            shutil.move(str(src), str(dest))
            logger.info("[backup] stashed %s -> %s", src, dest)


@router.get("/components/", dependencies=_GUARDS)
async def list_components():
    out = []
    for c in _COMPONENTS:
        exists = c.path.exists()
        out.append({
            "id": c.id,
            "label": c.label,
            "kind": c.kind,
            "exists": exists,
            "size_bytes": c.path.stat().st_size if exists else 0,
            "modified_at": c.path.stat().st_mtime if exists else None,
        })
    return out


@router.get("/download/{component_id}/", dependencies=_GUARDS)
async def download_component(component_id: str):
    component = _get_component(component_id)
    if not component.path.exists():
        raise HTTPException(404, detail=f"{component.filename} does not exist yet")

    if component.kind == "sqlite":
        # VACUUM INTO produces a complete, self-contained, consistent copy in
        # one atomic statement -- safe even while the app is mid-write to the
        # real file (WAL mode means a raw file copy can otherwise miss data
        # still sitting in the -wal sidecar). Target path must not already
        # exist, hence the delete-then-recreate dance with tempfile.
        tmp_path = tempfile.NamedTemporaryFile(prefix="backup-", suffix=".sqlite", delete=False).name
        Path(tmp_path).unlink()
        conn = sqlite3.connect(str(component.path))
        conn.execute("VACUUM INTO ?", (tmp_path,))
        conn.close()
        return FileResponse(tmp_path, filename=component.filename, media_type="application/octet-stream")

    return FileResponse(component.path, filename=component.filename, media_type="application/json")


@router.post("/restore/{component_id}/", dependencies=_GUARDS)
async def restore_component(component_id: str, file: UploadFile):
    component = _get_component(component_id)
    contents = await file.read()

    if component.kind == "json":
        try:
            json.loads(contents)
        except Exception as exc:
            raise HTTPException(400, detail=f"not valid JSON: {exc}")
    else:
        # Validate it's actually a usable SQLite file before touching
        # anything real -- write to a temp path and open it read-only.
        tmp = tempfile.NamedTemporaryFile(prefix="restore-", suffix=".sqlite", delete=False)
        tmp.write(contents)
        tmp.close()
        try:
            conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
            conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
        except Exception as exc:
            Path(tmp.name).unlink(missing_ok=True)
            raise HTTPException(400, detail=f"not a valid SQLite database: {exc}")

    _stash_current(component)
    if component.kind == "sqlite":
        shutil.move(tmp.name, str(component.path))
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        component.path.write_bytes(contents)

    logger.info("[backup] restored %s from uploaded file (%d bytes)", component.filename, len(contents))
    return {"ok": True}


@router.post("/reset/{component_id}/", dependencies=_GUARDS)
async def reset_component(component_id: str):
    component = _get_component(component_id)
    _stash_current(component)
    if component.reinit:
        component.reinit()
    logger.info("[backup] reset %s to fresh state", component.filename)
    return {"ok": True}

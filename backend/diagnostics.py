"""
PII-safe diagnostic log export -- lets a user download the app's own log
history (persisted to DATA_DIR/logs/ by main.py's rotating file handler) with
credentials, IPs, and provider hostnames scrubbed, so it can be shared for
troubleshooting (e.g. a support request) without exposing anything sensitive.

Per-request credential redaction already happens at log-write time (see
main.py's logging filters and xc_server._redact_upstream_url) -- this module
re-runs that same redaction defensively (belt-and-suspenders against a future
log call that forgets to redact) and adds two passes that only make sense at
export time, since they need to ask vod_db/config what's actually configured
right now: real IP addresses, and each configured provider's hostname.
"""

import logging
import re
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from config import LOG_FILE, get_config as get_dispatcharr_config
from routes import require_auth
import vod_db
from xc_server import _redact_upstream_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])
_GUARDS = [Depends(require_auth)]

_IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


def _known_hostnames() -> dict[str, str]:
    """Maps each configured provider/Dispatcharr hostname to a stable,
    non-identifying placeholder. Rebuilt fresh on every export since
    providers can be added or removed between downloads."""
    hosts: dict[str, str] = {}
    for idx, p in enumerate(vod_db.list_providers(), start=1):
        host = urlparse(p["base_url"]).hostname
        if host:
            hosts.setdefault(host, f"provider-{idx}-host")
    dispatcharr_url, _ = get_dispatcharr_config()
    if dispatcharr_url:
        host = urlparse(dispatcharr_url).hostname
        if host:
            hosts.setdefault(host, "dispatcharr-host")
    return hosts


def _redact_diagnostic_text(text: str) -> str:
    text = _redact_upstream_url(text)
    for host, placeholder in _known_hostnames().items():
        text = text.replace(host, placeholder)
    return _IPV4_RE.sub("***.***.***.***", text)


def _log_files() -> list:
    """Oldest first, so the exported file reads chronologically. Rotation
    leaves the active file unsuffixed (most recent) and each rotated-out file
    numbered .1 (most recent rotated-out) through .LOG_BACKUP_COUNT (oldest)."""
    if not LOG_FILE.exists():
        return []
    rotated = [p for p in LOG_FILE.parent.glob(f"{LOG_FILE.name}.*") if p.suffix.lstrip(".").isdigit()]
    rotated.sort(key=lambda p: int(p.suffix.lstrip(".")), reverse=True)
    return rotated + [LOG_FILE]


@router.get("/logs/", dependencies=_GUARDS)
async def download_diagnostic_logs():
    files = _log_files()
    if not files:
        raw = "No logs recorded yet."
    else:
        raw = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in files)
    redacted = _redact_diagnostic_text(raw)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    logger.info("[diagnostics] exported %d log file(s) as redacted diagnostic bundle", len(files))
    return PlainTextResponse(
        redacted,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="vod-manager-diagnostics-{stamp}.log"'},
    )

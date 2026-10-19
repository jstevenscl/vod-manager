"""Persistent notices: things the user must see and acknowledge (e.g. a download the
encoder never finished). Shown under the header bell until acknowledged; optionally
also emailed through the SMTP settings."""

import logging

import config
import notifications
import vod_db

logger = logging.getLogger(__name__)


def add(kind: str, severity: str, title: str, body: str | None = None, job_id: int | None = None,
        email: bool = False) -> int:
    nid = vod_db.add_notice(kind, severity, title, body, job_id)
    if email:
        try:
            notifications.send_email(config.get_smtp_settings().get("admin_recipients") or [],
                                     "[VOD & DVR Manager] " + title, body or title)
        except Exception as exc:  # a notice must never fail because email did
            logger.warning("[notices] email for notice %s failed: %s", nid, exc)
    return nid


def list_open() -> list[dict]:
    return vod_db.list_open_notices()


def count_open() -> int:
    return len(vod_db.list_open_notices())


def acknowledge(notice_id: int) -> bool:
    return vod_db.acknowledge_notice(notice_id)

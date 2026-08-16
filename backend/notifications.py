"""Outbound email notifications -- built 2026-07-28 for DVR disk-quota
threshold warnings (the first real trigger), but written as a generic
send_email so a future notification type doesn't need a second mechanism.

SMTP settings live in config.py's normal settings store (config.
get_smtp_settings/save_smtp_settings) -- the password is encrypted at rest
via secrets_util, same as every other real credential in this app. No SMTP
provider is assumed; any real relay (a personal email account's app
password, a transactional-email service, a local relay) works as long as
it speaks plain SMTP+STARTTLS on the configured host/port.
"""

import logging
import smtplib
from email.mime.text import MIMEText

import config

logger = logging.getLogger(__name__)


def send_email(to_addresses: list[str], subject: str, body: str) -> bool:
    """Best-effort -- a notification failure must never break whatever
    triggered it (e.g. a DVR import pass mid-way through real work), so
    this always returns a bool rather than raising. Silently returns False
    if SMTP isn't configured yet or there's nowhere to send to -- not an
    error, just nothing to do."""
    settings = config.get_smtp_settings()
    if not settings.get("host") or not to_addresses:
        return False
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.get("from_address") or settings.get("username") or "vod-manager@localhost"
    msg["To"] = ", ".join(to_addresses)
    try:
        with smtplib.SMTP(settings["host"], settings.get("port") or 587, timeout=10) as server:
            if settings.get("use_tls", True):
                server.starttls()
            if settings.get("username"):
                server.login(settings["username"], settings.get("password") or "")
            server.sendmail(msg["From"], to_addresses, msg.as_string())
        return True
    except Exception as exc:
        logger.warning("[notifications] failed to send email to %s: %s", to_addresses, exc)
        return False


def notify_quota_threshold(
    admin_recipients: list[str], user_email: str | None, username: str,
    provider_name: str, pct: int, usage_bytes: int, quota_bytes: int,
) -> None:
    """Called from dispatcharr_dvr_importer's quota-warning pass -- see
    vod_db.quota_warnings_sent's own table comment for how re-sends are
    deduped/reset. Goes to both the admin's standing recipient list AND
    this specific person's own email, if they've set one (see the 'Both'
    call from the user, 2026-07-28) -- either list can be empty."""
    to = [r for r in admin_recipients if r]
    if user_email:
        to.append(user_email)
    if not to:
        return
    subject = f"[VOD & DVR Manager] {username} reached {pct}% of their DVR quota"
    body = (
        f"{username}'s DVR recordings on {provider_name} are now using "
        f"{usage_bytes / 1e9:.1f} GB of their {quota_bytes / 1e9:.1f} GB quota ({pct}%).\n\n"
        "This is an automated notice from VOD & DVR Manager."
    )
    send_email(to, subject, body)

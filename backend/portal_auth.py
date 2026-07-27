"""Session store + auth dependency for the end-user DVR portal
(backend/portal_routes.py). Deliberately independent of backend/auth.py's
admin session store -- separate file, separate token namespace, separate
header name (X-Portal-Session-Token vs. the admin's X-Session-Token) -- so a
portal token can never be confused with or accidentally accepted on an admin
route, or vice versa.

Login here is two steps: password (create_pending_session) then a TOTP code
(promote_to_full_session) -- MFA is mandatory for every portal account (see
portal_routes.py), so require_portal_auth only ever accepts a session with
mfa_verified=True. A pending session can't reach any /api/portal/* data
route on its own.
"""

import json
import secrets
import time
from typing import Optional

from fastapi import Header, HTTPException

import vod_db
from config import DATA_DIR

_SESSIONS_FILE = DATA_DIR / "portal_sessions.json"
_SESSIONS: dict[str, dict] = {}  # token -> {"portal_account_id": int, "expires": float, "mfa_verified": bool}

SESSION_TTL = 86400 * 7  # full (post-MFA) session, same lifetime as the admin login
PENDING_SESSION_TTL = 300  # 5 minutes to complete the MFA step after a password check


def _load() -> None:
    if not _SESSIONS_FILE.exists():
        return
    try:
        now = time.time()
        data = json.loads(_SESSIONS_FILE.read_text())
        _SESSIONS.update({
            k: v for k, v in data.items()
            if isinstance(v, dict) and v.get("expires", 0) > now
        })
    except Exception:
        pass


def _save() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SESSIONS_FILE.write_text(json.dumps(_SESSIONS))


def create_pending_session(portal_account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {
        "portal_account_id": portal_account_id,
        "expires": time.time() + PENDING_SESSION_TTL,
        "mfa_verified": False,
    }
    _save()
    return token


def get_pending_account_id(pending_token: str) -> Optional[int]:
    entry = _SESSIONS.get(pending_token)
    if entry is None or entry.get("mfa_verified"):
        return None
    if time.time() > entry["expires"]:
        del _SESSIONS[pending_token]
        _save()
        return None
    return entry["portal_account_id"]


def promote_to_full_session(pending_token: str) -> Optional[str]:
    """Consumes a verified pending token and issues a fresh full session
    token -- the pending token is discarded rather than upgraded in place,
    so a leaked/logged pending token can't be replayed after MFA succeeds."""
    account_id = get_pending_account_id(pending_token)
    if account_id is None:
        return None
    del _SESSIONS[pending_token]
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {
        "portal_account_id": account_id,
        "expires": time.time() + SESSION_TTL,
        "mfa_verified": True,
    }
    _save()
    return token


def verify_full_session(token: str) -> Optional[int]:
    """Returns the portal_account_id if token is a live, MFA-verified
    session, else None."""
    entry = _SESSIONS.get(token)
    if entry is None or not entry.get("mfa_verified"):
        return None
    if time.time() > entry["expires"]:
        del _SESSIONS[token]
        _save()
        return None
    return entry["portal_account_id"]


def revoke_session(token: str) -> None:
    _SESSIONS.pop(token, None)
    _save()


async def require_portal_auth(
    x_portal_session_token: Optional[str] = Header(None, alias="X-Portal-Session-Token"),
) -> dict:
    """FastAPI dependency for every /api/portal/* data route -- resolves the
    caller's own portal_accounts row server-side from the session token.
    Route handlers must read provider_id/dispatcharr_user_id from the
    returned dict, never from a client-supplied param, so a portal user can
    never request another person's data by changing an id in the request."""
    if not x_portal_session_token:
        raise HTTPException(401, detail="unauthorized")
    account_id = verify_full_session(x_portal_session_token)
    if account_id is None:
        raise HTTPException(401, detail="unauthorized")
    account = vod_db.get_portal_account(account_id)
    if account is None:
        raise HTTPException(401, detail="unauthorized")
    return account


def resolve_portal_account_from_token(token: Optional[str]) -> Optional[dict]:
    """Same resolution as require_portal_auth, but callable with a bare
    token string rather than as a header-reading FastAPI dependency -- for
    portal_routes.portal_library_stream, whose URL is handed directly to a
    <video> element/HLS player. Those can't attach a custom
    X-Portal-Session-Token header (no XHR/fetch involved once the browser's
    own media engine takes over the request), so that one route accepts the
    same session token via a `?token=` query param instead -- the same
    reason xc_server.py's own stream routes embed credentials in the URL
    path rather than a header."""
    if not token:
        return None
    account_id = verify_full_session(token)
    if account_id is None:
        return None
    return vod_db.get_portal_account(account_id)


_load()

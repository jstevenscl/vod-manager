import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from auth import create_session, revoke_session, verify_session
from config import has_credentials, set_credentials, verify_credentials

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["vod-manager-core"])

if not has_credentials():
    logger.warning(
        "[routes] No admin login is configured yet -- every API route is unauthenticated until "
        "one is set (Settings, or the first-run screen). If this instance is reachable from "
        "outside a trusted network, set a login now."
    )


# ── Guards ────────────────────────────────────────────────────────────────────

async def require_auth(x_session_token: Optional[str] = Header(None, alias="X-Session-Token")):
    if not has_credentials():
        return  # no credentials configured — auth not enforced yet
    if not x_session_token or not verify_session(x_session_token):
        raise HTTPException(401, detail="unauthorized")


# Brute-force protection for the admin login -- same shape as xc_server.py's
# XC-client lockout (per-IP, in-memory, resets on restart) but a separate,
# simpler tracker since this guards a single fixed account rather than an
# arbitrary set of clients. Without this, /auth/login/ had no rate limit at
# all: unlimited password guesses over the network.
_LOGIN_MAX_ATTEMPTS = 8
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_LOCKOUT_SECONDS = 900
_LOGIN_SWEEP_INTERVAL_SECONDS = 600  # bound memory growth under sustained attack from many distinct IPs

_login_failed_attempts: dict[str, tuple[int, float]] = {}
_login_locked_until: dict[str, float] = {}
_login_last_sweep_at = 0.0


def _login_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _sweep_expired_login_entries() -> None:
    # Entries for an IP that never reaches the lockout threshold otherwise
    # sit in _login_failed_attempts forever -- every distinct IP that ever
    # fails a login once (internet scanners/bots included) leaks memory
    # indefinitely without this, same fix xc_server.py's lockout already has.
    global _login_last_sweep_at
    now = time.monotonic()
    if now - _login_last_sweep_at < _LOGIN_SWEEP_INTERVAL_SECONDS:
        return
    _login_last_sweep_at = now
    for ip, (_, window_started) in list(_login_failed_attempts.items()):
        if now - window_started > _LOGIN_WINDOW_SECONDS:
            _login_failed_attempts.pop(ip, None)
    for ip, expires in list(_login_locked_until.items()):
        if now >= expires:
            _login_locked_until.pop(ip, None)


def _login_locked_out(ip: str) -> bool:
    _sweep_expired_login_entries()
    expires = _login_locked_until.get(ip)
    if expires is None:
        return False
    if time.monotonic() >= expires:
        del _login_locked_until[ip]
        return False
    return True


def _record_login_failure(ip: str) -> None:
    now = time.monotonic()
    count, window_started = _login_failed_attempts.get(ip, (0, now))
    if now - window_started > _LOGIN_WINDOW_SECONDS:
        count, window_started = 0, now
    count += 1
    if count >= _LOGIN_MAX_ATTEMPTS:
        _login_locked_until[ip] = now + _LOGIN_LOCKOUT_SECONDS
        _login_failed_attempts.pop(ip, None)
        logger.warning("[routes] %s locked out of admin login for %ds after %d failed attempts",
                        ip, _LOGIN_LOCKOUT_SECONDS, count)
    else:
        _login_failed_attempts[ip] = (count, window_started)


# ── Request models ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class CredentialsRequest(BaseModel):
    username: str
    password: str


# ── Auth endpoints (no auth required) ────────────────────────────────────────

@router.post("/auth/login/")
async def login(body: LoginRequest, request: Request):
    ip = _login_client_ip(request)
    if _login_locked_out(ip):
        raise HTTPException(429, detail="Too many failed login attempts. Try again later.")
    if not verify_credentials(body.username, body.password):
        _record_login_failure(ip)
        raise HTTPException(401, detail="Invalid username or password")
    _login_failed_attempts.pop(ip, None)
    return {"token": create_session()}


@router.get("/auth/verify/")
async def auth_verify(x_session_token: Optional[str] = Header(None, alias="X-Session-Token")):
    if not has_credentials():
        return {"valid": True, "no_credentials": True}
    return {"valid": bool(x_session_token and verify_session(x_session_token))}


@router.post("/auth/logout/")
async def logout(x_session_token: Optional[str] = Header(None, alias="X-Session-Token")):
    if x_session_token:
        revoke_session(x_session_token)
    return {"ok": True}


# ── Settings endpoints ────────────────────────────────────────────────────────

@router.get("/settings/")
async def get_settings():
    return {"has_credentials": has_credentials()}


@router.post("/settings/credentials/")
async def set_credentials_endpoint(
    body: CredentialsRequest,
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    if has_credentials():
        # Allow if env var recovery mode is active, otherwise require session
        env_override = bool(
            os.environ.get("VODMANAGER_ADMIN_USER") and
            os.environ.get("VODMANAGER_ADMIN_PASSWORD")
        )
        if not env_override and not (x_session_token and verify_session(x_session_token)):
            raise HTTPException(401, detail="unauthorized")
    if not body.username.strip():
        raise HTTPException(400, detail="Username is required.")
    if len(body.password) < 6:
        raise HTTPException(400, detail="Password must be at least 6 characters.")
    set_credentials(body.username.strip(), body.password)
    return {"ok": True}


# ── Version endpoint ──────────────────────────────────────────────────────────

@router.get("/version/")
async def get_version():
    return {"version": "0.1.00"}

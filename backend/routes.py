import logging
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from auth import create_session, revoke_session, verify_session
from config import (
    config_from_env, get_config, has_credentials, is_configured,
    save_config, set_credentials, verify_credentials,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["vod-manager-core"])


# ── Guards ────────────────────────────────────────────────────────────────────

async def require_configured():
    if not is_configured():
        raise HTTPException(503, detail="not_configured")


async def require_auth(x_session_token: Optional[str] = Header(None, alias="X-Session-Token")):
    if not has_credentials():
        return  # no credentials configured — auth not enforced yet
    if not x_session_token or not verify_session(x_session_token):
        raise HTTPException(401, detail="unauthorized")


# ── Request models ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class CredentialsRequest(BaseModel):
    username: str
    password: str


class SettingsRequest(BaseModel):
    dispatcharr_url:   str
    dispatcharr_token: str


# ── Auth endpoints (no auth required) ────────────────────────────────────────

@router.post("/auth/login/")
async def login(body: LoginRequest):
    if not verify_credentials(body.username, body.password):
        raise HTTPException(401, detail="Invalid username or password")
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
    url, token = get_config()
    return {
        "configured":      bool(url and token),
        "dispatcharr_url": url,
        "has_token":       bool(token),
        "from_env":        config_from_env(),
        "has_credentials": has_credentials(),
    }


@router.post("/settings/")
async def save_settings(body: SettingsRequest):
    if config_from_env():
        raise HTTPException(400, detail="Configuration is managed via environment variables and cannot be changed here.")
    if not body.dispatcharr_url.strip() or not body.dispatcharr_token.strip():
        raise HTTPException(400, detail="Both URL and token are required.")
    save_config(body.dispatcharr_url.strip(), body.dispatcharr_token.strip())
    return {"ok": True}


@router.post("/settings/test/")
async def test_connection(body: SettingsRequest):
    url = body.dispatcharr_url.rstrip("/").strip()
    if not url or not body.dispatcharr_token.strip():
        return {"ok": False, "message": "URL and token are required."}
    try:
        # /channels/summary/ (not the paginated /channels/ list) -- the paginated
        # endpoint has to run pagination-metadata bookkeeping (effectively a COUNT
        # across the whole channels table) even with page_size=1, which measured
        # ~10x slower than summary on a real instance (3.0s vs 0.29s with ~5,000
        # channels) and could plausibly exceed this timeout on a larger library or
        # a busy Dispatcharr instance. summary is a cheap existence/auth check.
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{url}/api/channels/channels/summary/",
                headers={"X-API-Key": body.dispatcharr_token.strip()},
            )
            if resp.status_code == 200:
                return {"ok": True, "message": "Connected successfully"}
            elif resp.status_code in (401, 403):
                return {"ok": False, "message": "Invalid API token"}
            else:
                return {"ok": False, "message": f"Unexpected response: HTTP {resp.status_code}"}
    except httpx.ConnectError:
        return {"ok": False, "message": "Could not connect — check the URL"}
    except httpx.TimeoutException:
        return {"ok": False, "message": "Connection timed out"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@router.post("/settings/disconnect/")
async def disconnect(x_session_token: Optional[str] = Header(None, alias="X-Session-Token")):
    if has_credentials() and not (x_session_token and verify_session(x_session_token)):
        raise HTTPException(401, detail="unauthorized")
    save_config("", "")
    return {"ok": True}


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
    return {"version": "0.1.0"}

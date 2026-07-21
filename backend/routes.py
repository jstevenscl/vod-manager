import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from auth import create_session, revoke_session, verify_session
from config import has_credentials, set_credentials, verify_credentials

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["vod-manager-core"])


# ── Guards ────────────────────────────────────────────────────────────────────

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

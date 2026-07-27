"""End-user self-service DVR portal API -- everything under /api/portal/.

Deliberately separate from vod_routes.py's admin API: a different auth
system entirely (portal_auth.py, not routes.py's admin session), and every
data route below derives its caller's identity ONLY from the resolved
portal_accounts row (require_portal_auth), never from a client-supplied
provider_id/dispatcharr_user_id -- so a portal user can never see or touch
another person's data by editing a request.

Where the underlying business logic already exists in vod_routes.py (DVR
connection resolution, stream-conflict prediction, EPG datetime parsing)
this imports and reuses it directly rather than duplicating it -- see the
`from vod_routes import ...` below.
"""

import logging
import time
from typing import Optional

import pyotp
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

import dispatcharr_dvr_client
import portal_auth
import vod_db
from portal_auth import require_portal_auth
from secrets_util import decrypt_value, verify_password
from vod_routes import _parse_epg_datetime, _predict_stream_conflict, _require_dvr_connection
from xc_server import _proxy_vod_stream

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/portal", tags=["dvr-portal"])


# ── Brute-force protection for the portal login ─────────────────────────────
# Same shape/constants as routes.py's admin-login lockout -- a second,
# independent tracker (own dict, own IPs), not shared state, so failed
# attempts against one login can't lock out the other.

_LOGIN_MAX_ATTEMPTS = 8
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_LOCKOUT_SECONDS = 900
_LOGIN_SWEEP_INTERVAL_SECONDS = 600

_login_failed_attempts: dict[str, tuple[int, float]] = {}
_login_locked_until: dict[str, float] = {}
_login_last_sweep_at = 0.0


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _sweep_expired_login_entries() -> None:
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
        logger.warning("[portal_routes] %s locked out of portal login for %ds after %d failed attempts",
                        ip, _LOGIN_LOCKOUT_SECONDS, count)
    else:
        _login_failed_attempts[ip] = (count, window_started)


# ── Request models ────────────────────────────────────────────────────────────

class PortalLoginRequest(BaseModel):
    username: str
    password: str


class PortalPendingTokenRequest(BaseModel):
    pending_token: str


class PortalMfaCodeRequest(BaseModel):
    pending_token: str
    code: str


class PortalRecordingRuleRequest(BaseModel):
    label: str
    title: str
    tvg_id: Optional[str] = None
    title_mode: str = "exact"
    mode: str = "all"
    channel_id: int


# ── TOTP verification with anti-replay ──────────────────────────────────────

def _verify_totp_code(account: dict, code: str) -> bool:
    """RFC 6238 verification with a +/-1 time-step drift allowance, plus
    anti-replay: a code is only accepted for the highest not-yet-used
    counter it matches, and that counter is then persisted (vod_db.
    set_portal_account_totp_counter) so the exact same code -- or any
    earlier one -- is rejected if submitted again within its own still-
    otherwise-valid window. Without this, a shoulder-surfed/intercepted
    code would be usable twice, not just once, for up to ~30s."""
    secret = decrypt_value(account["totp_secret"])
    totp = pyotp.TOTP(secret)
    now_counter = int(time.time() // totp.interval)
    last_counter = account.get("totp_last_counter")
    for counter in (now_counter + 1, now_counter, now_counter - 1):
        if last_counter is not None and counter <= last_counter:
            continue
        if totp.generate_otp(counter) == code:
            vod_db.set_portal_account_totp_counter(account["id"], counter)
            return True
    return False


# ── Auth endpoints (no guard) ────────────────────────────────────────────────

@router.post("/auth/login/")
async def portal_login(body: PortalLoginRequest, request: Request):
    ip = _client_ip(request)
    if _login_locked_out(ip):
        raise HTTPException(429, detail="Too many failed login attempts. Try again later.")
    account = vod_db.get_portal_account_by_username(body.username)
    if not account or not verify_password(body.password, account["password_salt"], account["password_hash"]):
        _record_login_failure(ip)
        raise HTTPException(401, detail="Invalid username or password")
    _login_failed_attempts.pop(ip, None)
    pending_token = portal_auth.create_pending_session(account["id"])
    return {"pending_token": pending_token, "enrollment_required": not bool(account["totp_enabled"])}


@router.post("/auth/enroll-mfa/")
async def portal_enroll_mfa(body: PortalPendingTokenRequest):
    """Generates a fresh TOTP secret for this pending login and stores it
    (encrypted at rest) unconfirmed -- confirm-mfa below is what actually
    flips totp_enabled once the person proves they've added it to an
    authenticator app. Safe to call again before confirming (e.g. the user
    re-scans): each call just overwrites the still-unconfirmed secret."""
    account_id = portal_auth.get_pending_account_id(body.pending_token)
    if account_id is None:
        raise HTTPException(401, detail="login session expired, please log in again")
    account = vod_db.get_portal_account(account_id)
    if account is None:
        raise HTTPException(401, detail="unauthorized")
    if account["totp_enabled"]:
        raise HTTPException(400, detail="MFA is already enrolled for this account")
    secret = pyotp.random_base32()
    vod_db.set_portal_account_totp(account_id, secret, totp_enabled=False)
    otpauth_uri = pyotp.totp.TOTP(secret).provisioning_uri(name=account["username"], issuer_name="VOD Manager Portal")
    return {"secret": secret, "otpauth_uri": otpauth_uri}


@router.post("/auth/confirm-mfa/")
async def portal_confirm_mfa(body: PortalMfaCodeRequest):
    """First-time enrollment confirmation -- validates the code against the
    just-enrolled (still-unconfirmed) secret, then flips totp_enabled and
    promotes the pending login to a real session in one step."""
    account_id = portal_auth.get_pending_account_id(body.pending_token)
    if account_id is None:
        raise HTTPException(401, detail="login session expired, please log in again")
    account = vod_db.get_portal_account(account_id)
    if account is None or not account.get("totp_secret"):
        raise HTTPException(400, detail="no pending MFA enrollment for this account")
    if not _verify_totp_code(account, body.code):
        raise HTTPException(401, detail="Invalid code")
    vod_db.enable_confirmed_portal_totp(account_id)
    token = portal_auth.promote_to_full_session(body.pending_token)
    if not token:
        raise HTTPException(401, detail="login session expired, please log in again")
    return {"token": token}


@router.post("/auth/verify-mfa/")
async def portal_verify_mfa(body: PortalMfaCodeRequest):
    """Returning-user login step: pending token (password already checked)
    + a code from their already-enrolled authenticator app."""
    account_id = portal_auth.get_pending_account_id(body.pending_token)
    if account_id is None:
        raise HTTPException(401, detail="login session expired, please log in again")
    account = vod_db.get_portal_account(account_id)
    if account is None or not account["totp_enabled"] or not account.get("totp_secret"):
        raise HTTPException(400, detail="MFA is not enrolled for this account")
    if not _verify_totp_code(account, body.code):
        raise HTTPException(401, detail="Invalid code")
    token = portal_auth.promote_to_full_session(body.pending_token)
    if not token:
        raise HTTPException(401, detail="login session expired, please log in again")
    return {"token": token}


@router.get("/auth/verify/")
async def portal_auth_verify(x_portal_session_token: Optional[str] = Header(None, alias="X-Portal-Session-Token")):
    return {"valid": bool(x_portal_session_token and portal_auth.verify_full_session(x_portal_session_token))}


@router.post("/auth/logout/")
async def portal_logout(x_portal_session_token: Optional[str] = Header(None, alias="X-Portal-Session-Token")):
    if x_portal_session_token:
        portal_auth.revoke_session(x_portal_session_token)
    return {"ok": True}


# ── Data endpoints (all guarded, all scoped to the caller's own identity) ───

@router.get("/me/")
async def portal_me(account: dict = Depends(require_portal_auth)):
    provider = vod_db.get_provider(account["provider_id"])
    dispatcharr_username = None
    try:
        _, connection = _require_dvr_connection(account["provider_id"])
        users = await dispatcharr_dvr_client.list_users(connection)
        user = next((u for u in users if u.get("id") == account["dispatcharr_user_id"]), None)
        dispatcharr_username = user["username"] if user else None
    except Exception as exc:
        logger.warning("[portal_routes] portal_me: couldn't resolve Dispatcharr username: %s", exc)
    return {
        "username": account["username"],
        "dispatcharr_username": dispatcharr_username,
        "provider_name": provider["name"] if provider else None,
    }


@router.get("/recording-rules/")
async def portal_list_recording_rules(account: dict = Depends(require_portal_auth)):
    rules = vod_db.list_recording_profiles(account["provider_id"])
    return [r for r in rules if r.get("dispatcharr_user_id") == account["dispatcharr_user_id"]]


@router.post("/recording-rules/")
async def portal_create_recording_rule(body: PortalRecordingRuleRequest, account: dict = Depends(require_portal_auth)):
    """Same underlying scheduling call vod_routes.create_recording_profile
    uses, but dispatcharr_user_id/provider_id are always forced to the
    caller's own identity, and no category fields are exposed here -- a
    portal user's recordings fall back to the provider's own default DVR
    categories rather than picking a specific one."""
    provider_id = account["provider_id"]
    _, connection = _require_dvr_connection(provider_id)
    visible = await dispatcharr_dvr_client.visible_channel_ids(connection, account["dispatcharr_user_id"])
    if visible is not None and body.channel_id not in visible:
        raise HTTPException(403, detail="That channel isn't in your Dispatcharr lineup.")
    conflict = await _predict_stream_conflict(
        provider_id, connection, account["dispatcharr_user_id"],
        {"title": body.title, "channel_id": body.channel_id},
    )
    if conflict:
        raise HTTPException(409, detail=conflict)
    scheduled_by = {
        "dispatcharr_user_id": account["dispatcharr_user_id"],
        "dispatcharr_username": account["username"],
        "profile_label": body.label,
    }
    try:
        schedule_result = await dispatcharr_dvr_client.schedule_channel_recordings(
            connection, body.channel_id, body.title, body.mode, scheduled_by=scheduled_by,
        )
    except Exception as exc:
        raise HTTPException(502, detail=f"Dispatcharr rejected the recording: {exc}")
    profile_id = vod_db.create_recording_profile(
        provider_id, body.label, body.title, body.tvg_id, body.title_mode,
        None, "contains", body.mode, body.channel_id,
        None, None, account["dispatcharr_user_id"], None,
    )
    profile = vod_db.get_recording_profile(profile_id)
    profile["scheduled_now"] = schedule_result.get("scheduled", 0)
    profile["total_matches"] = schedule_result.get("total_matches", 0)
    return profile


@router.delete("/recording-rules/{profile_id}/")
async def portal_delete_recording_rule(profile_id: int, account: dict = Depends(require_portal_auth)):
    profile = vod_db.get_recording_profile(profile_id)
    if not profile:
        raise HTTPException(404, detail="recording rule not found")
    if profile.get("dispatcharr_user_id") != account["dispatcharr_user_id"]:
        raise HTTPException(403, detail="not your recording rule")
    if profile.get("channel_id"):
        try:
            _, connection = _require_dvr_connection(profile["provider_id"])
            upcoming = await dispatcharr_dvr_client.list_scheduled_recordings(connection)
            import datetime as _dt
            now = _dt.datetime.now(_dt.timezone.utc)
            target_title = (profile["title"] or "").strip().lower()
            for r in upcoming:
                if r.get("channel") != profile["channel_id"]:
                    continue
                program = (r.get("custom_properties") or {}).get("program") or {}
                if (program.get("title") or "").strip().lower() != target_title:
                    continue
                start = _parse_epg_datetime(r.get("start_time"))
                if start and start <= now:
                    continue
                try:
                    await dispatcharr_dvr_client.delete_recording(connection, r["id"])
                except Exception as exc:
                    logger.warning("[portal_routes] delete_recording_rule(%s): failed to remove recording %s: %s",
                                    profile_id, r.get("id"), exc)
        except Exception as exc:
            logger.warning("[portal_routes] delete_recording_rule(%s): failed to clean up Dispatcharr recordings: %s",
                            profile_id, exc)
    vod_db.delete_recording_profile(profile_id)
    return {"ok": True}


@router.get("/epg-search/")
async def portal_epg_search(title: str, account: dict = Depends(require_portal_auth)):
    if not title.strip():
        raise HTTPException(400, detail="title is required")
    _, connection = _require_dvr_connection(account["provider_id"])
    try:
        programs = await dispatcharr_dvr_client.search_epg_programs(connection, title)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))
    visible = await dispatcharr_dvr_client.visible_channel_ids(connection, account["dispatcharr_user_id"])
    if visible is None:
        return programs  # couldn't resolve their lineup -- fail open rather than hide everything
    filtered = []
    for program in programs:
        channels = [c for c in program.get("channels", []) if c.get("id") in visible]
        if channels:
            filtered.append({**program, "channels": channels})
    return filtered


@router.get("/upcoming/")
async def portal_upcoming(account: dict = Depends(require_portal_auth)):
    _, connection = _require_dvr_connection(account["provider_id"])
    try:
        upcoming = await dispatcharr_dvr_client.list_scheduled_recordings(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))
    return [
        r for r in upcoming
        if (r.get("custom_properties") or {}).get("scheduled_by", {}).get("dispatcharr_user_id")
        == account["dispatcharr_user_id"]
    ]


@router.get("/usage/")
async def portal_usage(account: dict = Depends(require_portal_auth)):
    provider_id, dispatcharr_user_id = account["provider_id"], account["dispatcharr_user_id"]
    limit_row = vod_db.get_dvr_user_limit(provider_id, dispatcharr_user_id)
    usage = vod_db.dvr_user_disk_usage_bytes(provider_id, dispatcharr_user_id)
    stream_limit = None
    try:
        _, connection = _require_dvr_connection(provider_id)
        users = await dispatcharr_dvr_client.list_users(connection)
        user = next((u for u in users if u.get("id") == dispatcharr_user_id), None)
        stream_limit = user.get("stream_limit") if user else None
    except Exception as exc:
        logger.warning("[portal_routes] portal_usage: couldn't resolve stream_limit: %s", exc)
    return {
        "actual_bytes": usage["actual_bytes"],
        "virtual_bytes": usage["virtual_bytes"],
        "total_bytes": usage["total_bytes"],
        "disk_quota_bytes": limit_row["disk_quota_bytes"] if limit_row else None,
        "stream_reserve": limit_row["stream_reserve"] if limit_row else 0,
        "stream_limit": stream_limit,
    }


@router.get("/library/")
async def portal_library(account: dict = Depends(require_portal_auth)):
    dispatcharr_user_id = account["dispatcharr_user_id"]
    return {
        "movies": vod_db.list_portal_library_movies(dispatcharr_user_id),
        "episodes": vod_db.list_portal_library_episodes(dispatcharr_user_id),
    }


@router.get("/library/{kind}/{item_id}/stream/")
async def portal_library_stream(kind: str, item_id: int, request: Request, token: str):
    """Authenticated playback proxy -- deliberately does NOT reuse the
    admin UI's internal-preview-credential URL builders (xcCredentialsQuery),
    since that would hand the portal's browser session a set of standing XC
    credentials rather than a one-off, ownership-checked request. Reuses
    xc_server._proxy_vod_stream's own dispatcharr_dvr local-file branch for
    the actual file-serving/range-request logic.

    Takes the session token as a `?token=` query param, NOT the usual
    X-Portal-Session-Token header -- this URL is handed directly to a
    <video> element, which can't attach custom headers. See
    portal_auth.resolve_portal_account_from_token's docstring."""
    account = portal_auth.resolve_portal_account_from_token(token)
    if account is None:
        raise HTTPException(401, detail="unauthorized")
    dispatcharr_user_id = account["dispatcharr_user_id"]
    if kind == "movie":
        if not vod_db.movie_owned_by_portal_user(item_id, dispatcharr_user_id):
            raise HTTPException(404, detail="not found")
        movie = vod_db.get_movie(item_id)
        sources = vod_db.list_movie_sources_for_streaming(item_id)
        title = f"{movie['name']} ({movie['year']})" if movie and movie.get("year") else (movie or {}).get("name", "?")
    elif kind == "episode":
        if not vod_db.episode_owned_by_portal_user(item_id, dispatcharr_user_id):
            raise HTTPException(404, detail="not found")
        episode = vod_db.get_episode(item_id)
        sources = vod_db.list_episode_sources_for_streaming(item_id)
        title = (episode or {}).get("name", "?")
    else:
        raise HTTPException(400, detail="kind must be 'movie' or 'episode'")
    # xc_server._proxy_vod_stream's own "kind" convention is "movie"/"series"
    # (used for its log-line labels only) -- map our "episode" to "series".
    proxy_kind = "series" if kind == "episode" else "movie"
    return await _proxy_vod_stream(proxy_kind, account["username"], sources, request, title=title)

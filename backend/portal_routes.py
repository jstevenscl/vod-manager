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
import tmdb_sync
import vod_db
from portal_auth import require_portal_auth
from secrets_util import decrypt_value, verify_password
from vod_routes import _parse_epg_datetime, _predict_stream_conflict, _require_dvr_connection
from xc_server import _proxy_vod_stream, fetch_proxied_image

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


class PortalScheduleSingleRequest(BaseModel):
    channel_id: int
    program_id: int
    title: str
    sub_title: Optional[str] = None
    tvg_id: Optional[str] = None
    start_time: str
    end_time: str
    season: Optional[int] = None
    episode: Optional[int] = None
    onscreen_episode: Optional[str] = None


class ConvertToSeriesRequest(BaseModel):
    mode: str = "new"
    label: Optional[str] = None


class ConvertToSingleRequest(BaseModel):
    keep: str = "next"  # "next" (cancel every other upcoming episode) or "all" (leave them all scheduled)


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
async def portal_verify_mfa(body: PortalMfaCodeRequest, request: Request):
    """Returning-user login step: pending token (password already checked)
    + a code from their already-enrolled authenticator app. Reuses the same
    per-IP lockout as the password step above -- a correct password alone
    doesn't bypass it: an attacker who already has valid credentials could
    otherwise mint fresh 5-minute pending tokens (portal_login never counts
    as a "failure") and hammer 6-digit TOTP guesses with no throttle at
    all. A wrong code here counts as a failed attempt exactly like a wrong
    password does."""
    ip = _client_ip(request)
    if _login_locked_out(ip):
        raise HTTPException(429, detail="Too many failed login attempts. Try again later.")
    account_id = portal_auth.get_pending_account_id(body.pending_token)
    if account_id is None:
        raise HTTPException(401, detail="login session expired, please log in again")
    account = vod_db.get_portal_account(account_id)
    if account is None or not account["totp_enabled"] or not account.get("totp_secret"):
        raise HTTPException(400, detail="MFA is not enrolled for this account")
    if not _verify_totp_code(account, body.code):
        _record_login_failure(ip)
        raise HTTPException(401, detail="Invalid code")
    _login_failed_attempts.pop(ip, None)
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

@router.get("/channels/")
async def portal_list_channels(account: dict = Depends(require_portal_auth)):
    """id -> {name, channel_number} for labeling a channel_id already stored
    on the caller's own recording rules/upcoming recordings -- those only
    ever store the bare id (see dispatcharr_dvr_client.list_channels'
    docstring), so without this the portal has nothing but a meaningless
    number to show. Not scoped to the caller's own visible lineup (unlike
    epg-search/upcoming) since this is pure display labeling of rules/
    recordings the other endpoints already scoped -- a channel name isn't
    sensitive."""
    _, connection = _require_dvr_connection(account["provider_id"])
    try:
        channels = await dispatcharr_dvr_client.list_channels(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))
    return {c["id"]: {"name": c.get("name"), "channel_number": c.get("channel_number")} for c in channels}


@router.get("/guide/")
async def portal_guide(account: dict = Depends(require_portal_auth)):
    """Real browsable guide -- every visible channel with its next few
    airings, not just search-by-title results. Powers the Portal's Guide
    tab (added 2026-07-28: search-only was the whole flow before this,
    real user feedback that a guide/scrollable-browse mode was still
    missing). Grid data has no channel info of its own (just tvg_id), so
    it's cross-referenced against list_channels here.

    Confirmed live (2026-07-28): a real Channel's own tvg_id field does not
    always match its actual EPG program data's tvg_id -- e.g. channel
    30625's tvg_id is "FOXKWKT.us" but its real programs carry
    "KWKT-DT(FOX)(KWKTDT).us". Every local-affiliate channel checked hit
    this, which is why the guide only ever showed higher-numbered
    cable/streaming channels and never the local ones. Same underlying
    class of tvg_id-string ambiguity create_recording's docstring already
    documents for Dispatcharr's own Series Rules -- channel_id-scoped
    lookups sidestep it, tvg_id-string lookups don't. Fix: only trust grid
    data for a channel when it actually produced a match; every other
    VISIBLE channel (never all ~2400 -- that's not worth the request count)
    falls back to a real channel_id-scoped search, run concurrently."""
    import asyncio
    import datetime as _dt
    _, connection = _require_dvr_connection(account["provider_id"])
    try:
        programs = await dispatcharr_dvr_client.get_epg_grid(connection)
        channels = await dispatcharr_dvr_client.list_channels(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))
    visible = await dispatcharr_dvr_client.visible_channel_ids(connection, account["dispatcharr_user_id"])
    now = _dt.datetime.now(_dt.timezone.utc)

    by_tvg: dict[str, list[dict]] = {}
    for p in programs:
        tvg = p.get("tvg_id")
        if tvg:
            by_tvg.setdefault(tvg, []).append(p)

    visible_channels = [ch for ch in channels if visible is None or ch.get("id") in visible]

    grid_hits: dict[int, list[dict]] = {}
    needs_fallback: list[dict] = []
    for ch in visible_channels:
        progs = by_tvg.get(ch.get("tvg_id"), [])
        if progs:
            grid_hits[ch["id"]] = progs
        else:
            needs_fallback.append(ch)
    # visible=None means visible_channel_ids couldn't resolve a real lineup
    # at all (fail-open, see its own docstring) -- visible_channels is then
    # every channel Dispatcharr has (~2400), and exhaustively fallback-
    # searching all of them one by one is never reasonable regardless of
    # why resolution failed. Capped, not skipped entirely, so a real (if
    # incomplete) guide still loads instead of an empty one.
    needs_fallback = needs_fallback[:150]

    async def _fallback(ch: dict) -> tuple[int, list[dict]]:
        try:
            found = await dispatcharr_dvr_client.search_epg_programs(connection, "", channel_id=ch["id"], limit=24, include_in_progress=True)
        except Exception as exc:
            logger.warning("[portal_routes] guide fallback search failed for channel=%s: %s", ch["id"], exc)
            found = []
        return ch["id"], found

    if needs_fallback:
        fallback_results = await asyncio.gather(*(_fallback(ch) for ch in needs_fallback))
        for channel_id, found in fallback_results:
            if found:
                grid_hits[channel_id] = found

    result = []
    for ch in visible_channels:
        progs = sorted(grid_hits.get(ch["id"], []), key=lambda p: p.get("start_time") or "")
        upcoming = [p for p in progs if (_parse_epg_datetime(p.get("end_time")) or now) > now][:24]
        if not upcoming:
            continue
        result.append({
            "channel": {"id": ch.get("id"), "name": ch.get("name"), "channel_number": ch.get("channel_number")},
            "programs": [_normalize_guide_program(p) for p in upcoming],
        })
    result.sort(key=lambda r: r["channel"]["channel_number"] if r["channel"]["channel_number"] is not None else 1e9)
    return result


def _normalize_guide_program(p: dict) -> dict:
    """Season/episode/onscreen_episode genuinely exist in the underlying EPG
    data (confirmed live 2026-07-28 -- Dispatcharr's own guide UI showed
    "S63E222" for a recording whose season/episode came through as null
    everywhere in this pipeline), but live in two different shapes depending
    on which source supplied this program: /api/epg/grid/ puts them at the
    top level, /api/epg/programs/search/ (used for every fallback-covered
    channel) nests them under custom_properties. The Scheduler and
    /schedule-single/ only ever saw the plain top-level fields before this,
    so a recording made from a fallback-covered channel (which is most
    local-affiliate channels, see get_epg_grid's docstring) always lost its
    real episode identity -- directly why General Hospital imported as a
    movie instead of an episode despite the EPG genuinely having "S63E222"
    for that exact airing."""
    props = p.get("custom_properties") or {}
    return {
        **p,
        "season": p.get("season") if p.get("season") is not None else props.get("season"),
        "episode": p.get("episode") if p.get("episode") is not None else props.get("episode"),
        "onscreen_episode": p.get("onscreen_episode") if p.get("onscreen_episode") is not None else props.get("onscreen_episode"),
    }


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
        "email": account.get("email"),
    }


class PortalUpdateEmailRequest(BaseModel):
    email: str | None = None


@router.put("/me/email/")
async def portal_update_email(body: PortalUpdateEmailRequest, account: dict = Depends(require_portal_auth)):
    """Self-service -- lets someone add/change/clear the email their own
    DVR quota warnings go to (see notifications.notify_quota_threshold),
    without needing an admin to do it for them. The admin's own Portal
    Access edit still calls vod_db.set_portal_account_email directly for
    the same field -- this is just the other caller. Real requirement from
    the user, 2026-07-28 ('Both' -- admin always warned, the person
    themselves also does if they've set an email)."""
    email = (body.email or "").strip() or None
    vod_db.set_portal_account_email(account["id"], email)
    return {"email": email}


def _enforce_hard_fail_quota(provider_id: int, dispatcharr_user_id: int) -> None:
    """Raises 409 if this person is currently at/over their DVR disk quota
    AND their dvr_user_limits.quota_policy is 'hard_fail' (the other real
    policy, 'delete_oldest', instead auto-evicts old recordings in
    dispatcharr_dvr_importer -- nothing to block here for that case).
    Called from both portal_schedule_single and portal_create_recording_rule
    -- real requirement from the user, 2026-07-28: quota policy is chosen
    once at creation (see vod_db.create_dvr_user_limit) and actually
    enforced, not just displayed. A person with no dvr_user_limits row at
    all has no quota configured, so nothing to check."""
    limit_row = vod_db.get_dvr_user_limit(provider_id, dispatcharr_user_id)
    if not limit_row or limit_row.get("disk_quota_bytes") is None:
        return
    if limit_row.get("quota_policy") != "hard_fail":
        return
    usage = vod_db.dvr_user_disk_usage_bytes(provider_id, dispatcharr_user_id)
    if usage["total_bytes"] >= limit_row["disk_quota_bytes"]:
        raise HTTPException(
            409,
            detail=f"You're at your DVR storage quota ({usage['total_bytes'] / 1e9:.1f} GB of "
                   f"{limit_row['disk_quota_bytes'] / 1e9:.1f} GB) -- remove something from your "
                   "library before scheduling more.",
        )


def _enforce_dvr_category_assigned(provider_id: int, dispatcharr_user_id: int, kind: str) -> None:
    """Raises 403 if this person has no dvr_user_limits.default_{kind}_
    category_id of their own. Real requirement from the user, 2026-07-29:
    DVR categories are per-person, deliberately assigned by the admin when
    setting the person up (vod_db.update_dvr_user_limit) -- never a silent
    shared fallback for a portal user's own scheduling. Before this, a
    person with nothing assigned could still schedule a recording that
    later imports with no category to land in -- dispatcharr_dvr_importer's
    resolution chain used to fall through further, all the way to the
    provider-level dvr_movie_category_id/dvr_series_category_id, which is
    exactly the shared "default" the user said they don't want relied on
    for a portal user's own recordings. That provider-level fallback still
    exists for admin-created recording rules (vod_routes.
    create_recording_profile), which is a separate, admin-controlled path
    -- this only gates the portal's own self-service scheduling."""
    limit_row = vod_db.get_dvr_user_limit(provider_id, dispatcharr_user_id)
    category_id = (limit_row or {}).get(f"default_{kind}_category_id")
    if not category_id:
        noun = "movie" if kind == "movie" else "series"
        raise HTTPException(
            403,
            detail=f"You don't have a {noun} category assigned for DVR yet -- ask your admin to set one up "
                   f"before you can schedule a {noun} recording.",
        )


@router.post("/schedule-single/")
async def portal_schedule_single(body: PortalScheduleSingleRequest, account: dict = Depends(require_portal_auth)):
    """One specific airing, no recurring rule -- the 'Record this episode'
    choice in the Scheduler, as opposed to 'Record the series'
    (portal_create_recording_rule below, which creates a dvr_recording_profiles
    row that keeps discovering new episodes). Calls
    dispatcharr_dvr_client.create_recording directly, same as the
    channel_id-scoped path that function's own docstring explains is the fix
    for Dispatcharr's own Series Rules tvg_id-collision bug -- appropriate
    here too since this always has a real numeric channel_id already, never
    an ambiguous tvg_id to resolve.

    is_already_scheduled is the exact helper create_recording's own docstring
    says exists for callers exactly like this one -- schedule_channel_
    recordings has this dedup built into its own loop, but this path calls
    create_recording directly and was missing it entirely, confirmed live
    2026-07-28: the same airing could be scheduled 3+ times in a row with no
    conflict error at all.

    season/episode/onscreen_episode are threaded through here (from the
    Scheduler grid, via _normalize_guide_program) rather than left to
    default None -- without them, create_recording's own custom_properties
    snapshot ends up with no real episode identity, and the DVR importer
    falls back all the way to "must be a movie" (see _resolve_season_episode)
    even when the EPG genuinely has it, confirmed live 2026-07-28 with a
    real General Hospital recording that Dispatcharr's own guide showed as
    "S63E222" but imported into the pool as an untitled movie.

    An identity match against an EXISTING recording is no longer a flat
    409: if that recording is already imported (a movie_sources/
    episode_sources row exists for it), the calling person is attached as
    an additional owner of that same file via add_movie/episode_source_owner
    -- real requirement from the user, 2026-07-28: "Bill records the same
    thing Emby already has" should share the one file, not error out, and
    each of them later removing it from their own Library must not affect
    the other (see remove_movie/episode_library_owner). Only a genuine
    real-world time-slot overlap (no identity match at all -- see
    find_existing_recording's own docstring) still means "you can't record
    this, the channel's already busy" and stays a 409.

    A match that ISN'T imported yet (still recording, or completed but not
    yet swept by the importer) used to also 409 -- closed 2026-07-28 via
    add_pending_recording_claim: rather than PATCHing Dispatcharr's own
    Recording object (untested against a live instance, real risk of
    malforming someone's actual in-progress recording), this just records
    the claim locally, keyed by the same (provider, channel, identity)
    triple find_existing_recording used to find the match. dispatcharr_dvr_
    importer consumes it the moment that exact recording actually imports,
    attaching this person then instead."""
    provider_id = account["provider_id"]
    _, connection = _require_dvr_connection(provider_id)
    visible = await dispatcharr_dvr_client.visible_channel_ids(connection, account["dispatcharr_user_id"])
    if visible is not None and body.channel_id not in visible:
        raise HTTPException(403, detail="That channel isn't in your Dispatcharr lineup.")
    _enforce_hard_fail_quota(provider_id, account["dispatcharr_user_id"])
    # season presence mirrors the DVR importer's own movie-vs-episode
    # heuristic (see dispatcharr_dvr_importer's "must be a movie" fallback
    # when there's no real episode identity) -- same signal, checked here at
    # schedule time instead of after the fact at import time.
    _enforce_dvr_category_assigned(provider_id, account["dispatcharr_user_id"], "series" if body.season is not None else "movie")
    identity_props = {"season": body.season, "episode": body.episode, "onscreen_episode": body.onscreen_episode}
    match_program = {
        "title": body.title, "sub_title": body.sub_title, "start_time": body.start_time, "end_time": body.end_time,
        "custom_properties": identity_props,
    }
    existing_recording = await dispatcharr_dvr_client.find_existing_recording(connection, body.channel_id, match_program)
    if existing_recording is not None:
        existing_stream_id = str(existing_recording["id"])
        attached = vod_db.attach_portal_user_to_existing_recording(provider_id, existing_stream_id, account["dispatcharr_user_id"])
        if attached:
            return {"attached_existing": True, "recording": existing_recording}
        identity_key = dispatcharr_dvr_client.episode_identity_key(match_program)
        vod_db.add_pending_recording_claim(provider_id, body.channel_id, identity_key, account["dispatcharr_user_id"])
        return {"attached_existing": "pending", "recording": existing_recording}
    already = await dispatcharr_dvr_client.is_already_scheduled(connection, body.channel_id, match_program)
    if already:
        raise HTTPException(409, detail="That time slot on this channel is already claimed by another recording.")
    conflict = await _predict_stream_conflict(
        provider_id, connection, account["dispatcharr_user_id"],
        {"title": body.title, "channel_id": body.channel_id},
    )
    if conflict:
        raise HTTPException(409, detail=conflict)
    scheduled_by = {
        "dispatcharr_user_id": account["dispatcharr_user_id"],
        "dispatcharr_username": account["username"],
        "profile_label": body.title,
    }
    program = {
        "tvg_id": body.tvg_id, "title": body.title, "sub_title": body.sub_title,
        "start_time": body.start_time, "end_time": body.end_time,
        "custom_properties": identity_props,
    }
    try:
        recording = await dispatcharr_dvr_client.create_recording(connection, body.channel_id, program, scheduled_by=scheduled_by)
    except Exception as exc:
        raise HTTPException(502, detail=f"Dispatcharr rejected the recording: {exc}")
    return recording


@router.get("/recording-rules/")
async def portal_list_recording_rules(account: dict = Depends(require_portal_auth)):
    rules = vod_db.list_recording_profiles(account["provider_id"])
    return [r for r in rules if r.get("dispatcharr_user_id") == account["dispatcharr_user_id"]]


@router.get("/my-recordings/")
async def portal_my_recordings(account: dict = Depends(require_portal_auth)):
    """Series (dvr_recording_profiles rows) plus true singles (one-off
    Recordings with no backing profile) in one call, so My Recordings can
    show both kinds and offer the right convert action for each. 'Single'
    isn't a stored flag anywhere -- it's derived here as "one of my own
    scheduled recordings whose (title, channel) doesn't match any of my own
    profiles" -- deliberately, so converting either direction (delete/create
    a profile) reclassifies a recording automatically on the next fetch,
    with no separate field to keep in sync."""
    provider_id = account["provider_id"]
    _, connection = _require_dvr_connection(provider_id)
    profiles = [p for p in vod_db.list_recording_profiles(provider_id) if p.get("dispatcharr_user_id") == account["dispatcharr_user_id"]]
    profile_keys = {(p["title"].strip().lower(), p["channel_id"]) for p in profiles if p.get("title") and p.get("channel_id")}
    try:
        recordings = await dispatcharr_dvr_client.list_scheduled_recordings(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))
    singles = []
    for r in recordings:
        scheduled_by = (r.get("custom_properties") or {}).get("scheduled_by") or {}
        if scheduled_by.get("dispatcharr_user_id") != account["dispatcharr_user_id"]:
            continue
        program = (r.get("custom_properties") or {}).get("program") or {}
        title = program.get("title")
        if not title:
            continue
        key = (title.strip().lower(), r.get("channel"))
        if key in profile_keys:
            continue
        singles.append({
            "id": r["id"], "title": title, "sub_title": program.get("sub_title"),
            "channel_id": r.get("channel"), "start_time": r.get("start_time"), "end_time": r.get("end_time"),
        })
    return {"series": profiles, "singles": singles}


@router.post("/singles/{recording_id}/convert-to-series/")
async def portal_convert_single_to_series(recording_id: int, body: ConvertToSeriesRequest, account: dict = Depends(require_portal_auth)):
    """The already-scheduled recording itself is never touched -- this just
    adds a dvr_recording_profiles row for its (title, channel) so future
    episodes get discovered too. schedule_channel_recordings' own dedup
    (see its docstring) means its first scan pass correctly recognizes the
    existing recording rather than double-booking it."""
    provider_id = account["provider_id"]
    _, connection = _require_dvr_connection(provider_id)
    try:
        recordings = await dispatcharr_dvr_client.list_scheduled_recordings(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))
    recording = next((r for r in recordings if r["id"] == recording_id), None)
    if not recording:
        raise HTTPException(404, detail="recording not found")
    scheduled_by = (recording.get("custom_properties") or {}).get("scheduled_by") or {}
    if scheduled_by.get("dispatcharr_user_id") != account["dispatcharr_user_id"]:
        raise HTTPException(403, detail="not your recording")
    program = (recording.get("custom_properties") or {}).get("program") or {}
    title = program.get("title")
    channel_id = recording.get("channel")
    if not title or not channel_id:
        raise HTTPException(400, detail="This recording is missing title/channel info -- can't convert it.")
    new_scheduled_by = {
        "dispatcharr_user_id": account["dispatcharr_user_id"],
        "dispatcharr_username": account["username"],
        "profile_label": body.label or title,
    }
    try:
        schedule_result = await dispatcharr_dvr_client.schedule_channel_recordings(
            connection, channel_id, title, body.mode, scheduled_by=new_scheduled_by,
        )
    except Exception as exc:
        raise HTTPException(502, detail=f"Dispatcharr rejected the recording: {exc}")
    profile_id = vod_db.create_recording_profile(
        provider_id, body.label or title, title, program.get("tvg_id"), "contains",
        None, "contains", body.mode, channel_id,
        None, None, account["dispatcharr_user_id"], None,
    )
    profile = vod_db.get_recording_profile(profile_id)
    profile["scheduled_now"] = schedule_result.get("scheduled", 0)
    return profile


@router.post("/recording-rules/{profile_id}/convert-to-single/")
async def portal_convert_series_to_single(profile_id: int, body: ConvertToSingleRequest, account: dict = Depends(require_portal_auth)):
    """Deletes the profile (stops finding new episodes) -- keep='next'
    additionally cancels every OTHER already-scheduled upcoming episode,
    leaving just the earliest one; keep='all' leaves them all scheduled.
    Same upcoming-recordings matching pattern portal_delete_recording_rule
    already uses for its own cleanup."""
    profile = vod_db.get_recording_profile(profile_id)
    if not profile:
        raise HTTPException(404, detail="recording rule not found")
    if profile.get("dispatcharr_user_id") != account["dispatcharr_user_id"]:
        raise HTTPException(403, detail="not your recording rule")
    if profile.get("channel_id") and body.keep == "next":
        try:
            _, connection = _require_dvr_connection(profile["provider_id"])
            upcoming = await dispatcharr_dvr_client.list_scheduled_recordings(connection)
            import datetime as _dt
            now = _dt.datetime.now(_dt.timezone.utc)
            target_title = (profile["title"] or "").strip().lower()
            matches = []
            for r in upcoming:
                if r.get("channel") != profile["channel_id"]:
                    continue
                program = (r.get("custom_properties") or {}).get("program") or {}
                if (program.get("title") or "").strip().lower() != target_title:
                    continue
                start = _parse_epg_datetime(r.get("start_time"))
                if start and start <= now:
                    continue
                matches.append((start, r))
            matches.sort(key=lambda m: m[0] or now)
            for _, r in matches[1:]:
                try:
                    await dispatcharr_dvr_client.delete_recording(connection, r["id"])
                except Exception as exc:
                    logger.warning("[portal_routes] convert_series_to_single(%s): failed to cancel recording %s: %s",
                                    profile_id, r.get("id"), exc)
        except Exception as exc:
            logger.warning("[portal_routes] convert_series_to_single(%s): failed to trim upcoming recordings: %s",
                            profile_id, exc)
    vod_db.delete_recording_profile(profile_id)
    return {"ok": True}


@router.post("/recording-rules/")
async def portal_create_recording_rule(body: PortalRecordingRuleRequest, account: dict = Depends(require_portal_auth)):
    """Same underlying scheduling call vod_routes.create_recording_profile
    uses, but dispatcharr_user_id/provider_id are always forced to the
    caller's own identity, and no category fields are exposed here -- a
    portal user always records into their own personally-assigned DVR
    category (dvr_user_limits.default_movie/series_category_id, set by the
    admin), never a category they pick themselves and never a shared
    provider-level fallback -- see _enforce_dvr_category_assigned above."""
    provider_id = account["provider_id"]
    _, connection = _require_dvr_connection(provider_id)
    visible = await dispatcharr_dvr_client.visible_channel_ids(connection, account["dispatcharr_user_id"])
    if visible is not None and body.channel_id not in visible:
        raise HTTPException(403, detail="That channel isn't in your Dispatcharr lineup.")
    _enforce_hard_fail_quota(provider_id, account["dispatcharr_user_id"])
    # A recording rule discovers episodes over time, and each one resolves
    # to movie- or series-type on its own at import time (an unnumbered
    # talk-show airing imports as a movie even under a channel-scoped rule)
    # -- there's no single kind to check here the way schedule-single can
    # check season presence. Requiring at least one of the two assigned
    # categories is the practical floor: someone with neither has nowhere
    # for ANY of this rule's recordings to land.
    limit_row = vod_db.get_dvr_user_limit(provider_id, account["dispatcharr_user_id"])
    if not (limit_row or {}).get("default_movie_category_id") and not (limit_row or {}).get("default_series_category_id"):
        raise HTTPException(403, detail="You don't have a DVR category assigned yet -- ask your admin to set one up before you can schedule recordings.")
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


@router.delete("/upcoming/{recording_id}/")
async def portal_cancel_upcoming(recording_id: int, account: dict = Depends(require_portal_auth)):
    """Cancels one specific upcoming recording -- was genuinely missing
    (Upcoming only ever showed a list, no way to act on a row). Ownership
    check mirrors portal_upcoming's own filter exactly, so a person can only
    ever cancel a recording that already belongs to them; deleting one that
    a series rule created doesn't touch the rule itself, just this one
    airing -- the rule's own periodic rescan simply finds it again unless
    the underlying EPG listing goes away too."""
    _, connection = _require_dvr_connection(account["provider_id"])
    try:
        upcoming = await dispatcharr_dvr_client.list_scheduled_recordings(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))
    recording = next((r for r in upcoming if r["id"] == recording_id), None)
    if not recording:
        raise HTTPException(404, detail="recording not found")
    scheduled_by = (recording.get("custom_properties") or {}).get("scheduled_by") or {}
    if scheduled_by.get("dispatcharr_user_id") != account["dispatcharr_user_id"]:
        raise HTTPException(403, detail="not your recording")
    try:
        await dispatcharr_dvr_client.delete_recording(connection, recording_id)
    except Exception as exc:
        raise HTTPException(502, detail=f"Dispatcharr rejected the cancellation: {exc}")
    return {"ok": True}


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
    provider_id, dispatcharr_user_id = account["provider_id"], account["dispatcharr_user_id"]
    return {
        "movies": vod_db.list_portal_library_movies(provider_id, dispatcharr_user_id),
        "episodes": vod_db.list_portal_library_episodes(provider_id, dispatcharr_user_id),
    }


@router.delete("/library/movies/{movie_id}/")
async def portal_remove_library_movie(movie_id: int, account: dict = Depends(require_portal_auth)):
    """Removes this movie from the CALLING person's own Library only --
    reference-counted (see vod_db.remove_movie_library_owner's docstring):
    if someone else also has this same recording in their Library (Bill
    recorded the same thing Emby already had, see
    attach_portal_user_to_existing_recording), it stays fully intact for
    them, file included. The file on disk is only actually deleted once
    nobody has it left. Real requirement from the user, 2026-07-28."""
    provider_id, dispatcharr_user_id = account["provider_id"], account["dispatcharr_user_id"]
    if not vod_db.movie_owned_by_portal_user(movie_id, dispatcharr_user_id):
        raise HTTPException(404, detail="That's not in your library.")
    result = vod_db.remove_movie_library_owner(movie_id, provider_id, dispatcharr_user_id)
    return result


@router.delete("/library/episodes/{episode_id}/")
async def portal_remove_library_episode(episode_id: int, account: dict = Depends(require_portal_auth)):
    """Episode counterpart to portal_remove_library_movie."""
    provider_id, dispatcharr_user_id = account["provider_id"], account["dispatcharr_user_id"]
    if not vod_db.episode_owned_by_portal_user(episode_id, dispatcharr_user_id):
        raise HTTPException(404, detail="That's not in your library.")
    result = vod_db.remove_episode_library_owner(episode_id, provider_id, dispatcharr_user_id)
    return result


@router.get("/library/shows/{series_id}/episodes/")
async def portal_show_canonical_episodes(series_id: int, account: dict = Depends(require_portal_auth)):
    """Every episode TMDB knows this show ever had, each flagged against
    what THIS person actually has -- recorded (a real file), upcoming (a
    Dispatcharr recording scheduled but not finished yet), or missing
    (nothing at all). Same idea as vod_routes' admin-only gap-view route,
    but scoped to one portal person's own ownership instead of the whole
    pool, and merged with /upcoming/ too. Real requirement from the user,
    2026-07-29: the Library's season selector should show a long-running
    show's real history, not just whatever few episodes happen to already
    be recorded -- confirmed live that General Hospital alone has 63
    seasons/~10.8k episodes on TMDB, so this always goes through
    tmdb_sync's cache (get_series_episode_list_cached), never a live TMDB
    call per page view.

    canonical=False (not a 404) when the series has no tmdb_id yet -- the
    Library should keep showing whatever's actually recorded/upcoming even
    for a not-yet-enriched show, just without the full-history view."""
    series = vod_db.get_series(series_id)
    if not series:
        raise HTTPException(404, detail="series not found")
    if not series.get("tmdb_id"):
        return {"canonical": False, "episodes": []}
    try:
        canonical = await tmdb_sync.get_series_episode_list_cached(series["tmdb_id"])
    except Exception as exc:
        raise HTTPException(502, detail=f"TMDB lookup failed: {exc}")

    dispatcharr_user_id = account["dispatcharr_user_id"]
    recorded = {
        (e["season_number"], e["episode_number"]): e
        for e in vod_db.list_portal_library_episodes(account["provider_id"], dispatcharr_user_id)
        if e["series_id"] == series_id
    }
    upcoming: dict[tuple, dict] = {}
    try:
        _, connection = _require_dvr_connection(account["provider_id"])
        for r in await dispatcharr_dvr_client.list_scheduled_recordings(connection):
            cp = r.get("custom_properties") or {}
            if (cp.get("scheduled_by") or {}).get("dispatcharr_user_id") != dispatcharr_user_id:
                continue
            title = ((cp.get("program") or {}).get("title") or "").strip().lower()
            if title != series["name"].strip().lower():
                continue
            key = (cp.get("season"), cp.get("episode"))
            if key[0] is not None and key[1] is not None:
                upcoming[key] = r
    except Exception as exc:
        logger.warning("[portal_routes] portal_show_canonical_episodes: couldn't check upcoming: %s", exc)

    episodes = []
    for ep in canonical:
        key = (ep["season_number"], ep["episode_number"])
        rec = recorded.get(key)
        up = upcoming.get(key)
        episodes.append({
            **ep,
            "status": "recorded" if rec else ("upcoming" if up else "missing"),
            "library_episode_id": rec["id"] if rec else None,
            "file_size_bytes": rec["file_size_bytes"] if rec else None,
            "recording_id": up["id"] if up else None,
            "start_time": up["start_time"] if up else None,
        })
    return {"canonical": True, "episodes": episodes}


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


@router.get("/image-proxy")
async def portal_image_proxy(url: str, token: str):
    """Portal counterpart to vod_routes.image_proxy -- same reasoning (see
    xc_server.fetch_proxied_image's docstring), using the portal's own
    token system since portal users aren't admins. Takes the session token
    as a `?token=` query param rather than the usual X-Portal-Session-Token
    header for the same reason as portal_library_stream above -- a plain
    <img src> can't attach a custom header."""
    if portal_auth.resolve_portal_account_from_token(token) is None:
        raise HTTPException(401, detail="unauthorized")
    return await fetch_proxied_image(url)

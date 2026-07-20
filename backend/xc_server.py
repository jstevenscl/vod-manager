"""
Minimal Xtream-Codes-compatible VOD server.

Purpose: let one or more Dispatcharr instances each be configured with an
"XC" M3U account pointing at VOD Manager, so their refresh-vod flow pulls VOD
categories/movies from our own curated pool (vod_db) instead of a real
external provider.

Auth: each downstream instance gets its own auto-generated, high-entropy
username/password pair (a "client" -- see vod_db.create_xc_client and the
xc_clients table), stored locally rather than fetched from any one
Dispatcharr's own account. This supports multiple independent Dispatcharr
instances pulling from the same pool, each individually identifiable and
revocable, without assuming any of their source IPs are stable (VPN-fronted
or CGNAT'd instances routinely aren't). See _authenticate.
"""

import asyncio
import ipaddress
import logging
import os
import re
import secrets
import shutil
import tempfile
import time

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

import config
from dispatcharr_client import DispatcharrClient
import emby_vod_client
import plex_client
import vod_db

logger = logging.getLogger("vod_manager.xc_server")

router = APIRouter(tags=["xc-vod"])

vod_db.init_db()

# Some real XC providers (e.g. ProviderD) silently drop the connection -- no
# HTTP response at all -- for requests without a browser-like User-Agent,
# httpx's default included. Same fix as vod_importer.py's XCProviderClient,
# needed here too since this is a separate connection (the actual stream
# relay, not the catalog import).
_UPSTREAM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}


# Brute-force protection for the XC login. The XC protocol itself has no
# concept of this (username/password in a URL, checked per-request, full
# stop) -- if this server is ever reachable from outside a trusted LAN/VPN,
# that's an open, unthrottled login form. Per-client-IP lockout after
# repeated failures closes that gap without needing anything outside the
# app itself, and without assuming the caller's IP is stable or unique to
# them (it isn't, for VPN-fronted or CGNAT'd instances) -- this is abuse
# throttling, not identity. In-memory/best-effort like _active_sessions
# below -- a restart clears it, which is an acceptable reset for this
# purpose. Thresholds are configurable (Settings → Security) since the
# right value depends on real-world exposure; _lockout_settings() caches
# the config-file read briefly rather than hitting disk on every request.
_SWEEP_INTERVAL_SECONDS = 600   # bound memory growth under sustained attack from many distinct IPs
_LOCKOUT_SETTINGS_CACHE_TTL = 30

_failed_attempts: dict[str, tuple[int, float]] = {}  # ip -> (count, window_started_at)
_locked_until: dict[str, float] = {}                  # ip -> monotonic time lockout expires
_last_sweep_at = 0.0
_lockout_settings_cache: dict | None = None
_lockout_settings_cache_at = 0.0


def _lockout_settings() -> dict:
    global _lockout_settings_cache, _lockout_settings_cache_at
    now = time.monotonic()
    if _lockout_settings_cache is None or (now - _lockout_settings_cache_at) >= _LOCKOUT_SETTINGS_CACHE_TTL:
        _lockout_settings_cache = config.get_lockout_settings()
        _lockout_settings_cache_at = now
    return _lockout_settings_cache


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _sweep_expired_auth_entries() -> None:
    global _last_sweep_at
    now = time.monotonic()
    if now - _last_sweep_at < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep_at = now
    window_seconds = _lockout_settings()["lockout_window_seconds"]
    for ip, (_, window_started) in list(_failed_attempts.items()):
        if now - window_started > window_seconds:
            _failed_attempts.pop(ip, None)
    for ip, expires in list(_locked_until.items()):
        if now >= expires:
            _locked_until.pop(ip, None)


def _is_locked_out(client_ip: str) -> bool:
    expires = _locked_until.get(client_ip)
    if expires is None:
        return False
    if time.monotonic() >= expires:
        del _locked_until[client_ip]
        return False
    return True


def _record_auth_failure(client_ip: str) -> None:
    settings = _lockout_settings()
    max_attempts = settings["lockout_max_attempts"]
    window_seconds = settings["lockout_window_seconds"]
    lockout_seconds = settings["lockout_duration_seconds"]

    now = time.monotonic()
    count, window_started = _failed_attempts.get(client_ip, (0, now))
    if now - window_started > window_seconds:
        count, window_started = 0, now
    count += 1
    if count >= max_attempts:
        _locked_until[client_ip] = now + lockout_seconds
        _failed_attempts.pop(client_ip, None)
        logger.warning("[xc_server] %s locked out for %ds after %d failed auth attempts in %ds",
                        client_ip, lockout_seconds, count, window_seconds)
    else:
        _failed_attempts[client_ip] = (count, window_started)


def _record_auth_success(client_ip: str) -> None:
    _failed_attempts.pop(client_ip, None)


def _ip_allowed(client_ip: str, allowlist: str) -> bool:
    """allowlist is a comma-separated list of IPs and/or CIDRs, e.g.
    '203.0.113.4,198.51.100.0/24'. Invalid entries are skipped (logged), not
    fatal -- a typo in one entry shouldn't lock out every entry."""
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowlist.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            logger.warning("[xc_server] skipping invalid allowlist entry %r", entry)
    return False


def _allowed_category_ids(client: dict) -> set[int] | None:
    """None means unrestricted (every existing client, unless explicitly
    given an allowlist) -- callers must treat None as 'skip the check', not
    as an empty allowlist, or every client would suddenly see nothing."""
    raw = client.get("category_allowlist")
    if not raw:
        return None
    ids = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if entry.isdigit():
            ids.add(int(entry))
    return ids


def _movie_allowed(client: dict, movie_id: int) -> bool:
    allowed = _allowed_category_ids(client)
    if allowed is None:
        return True
    return bool(allowed.intersection(vod_db.get_movie_category_ids(movie_id)))


def _series_allowed(client: dict, series_id: int) -> bool:
    allowed = _allowed_category_ids(client)
    if allowed is None:
        return True
    return bool(allowed.intersection(vod_db.get_series_category_ids(series_id)))


def _find_matching_client_sync(username: str, password: str) -> dict | None:
    """Checks the given credentials against every enabled client without
    short-circuiting on the first match -- so response time doesn't leak
    which position in the list (if any) matched. Plain sync function run via
    asyncio.to_thread (see _authenticate) -- vod_db.list_enabled_xc_clients
    opens its own SQLite connection, and this runs on every single request
    across every route, so it shouldn't block the event loop either."""
    matched = None
    for client in vod_db.list_enabled_xc_clients():
        user_ok = secrets.compare_digest(username.encode(), client["username"].encode())
        pass_ok = secrets.compare_digest(password.encode(), client["password"].encode())
        if user_ok and pass_ok:
            matched = client
    return matched


async def _authenticate(username: str, password: str, request: Request) -> dict | None:
    """Returns the matched client dict on success, None otherwise -- truthy/
    falsy either way, so existing `if not await _authenticate(...)` call
    sites keep working unchanged."""
    _sweep_expired_auth_entries()
    client_ip = _client_ip(request)
    if _is_locked_out(client_ip):
        return None

    matched = await asyncio.to_thread(_find_matching_client_sync, username, password)
    if matched is None:
        _record_auth_failure(client_ip)
        return None

    if matched["ip_allowlist"] and not _ip_allowed(client_ip, matched["ip_allowlist"]):
        logger.warning("[xc_server] client '%s' presented valid credentials from disallowed IP %s",
                        matched["label"], client_ip)
        _record_auth_failure(client_ip)
        return None

    _record_auth_success(client_ip)
    await asyncio.to_thread(vod_db.record_xc_client_seen, matched["id"], client_ip)
    return matched


def _log_hit(request: Request) -> None:
    params = dict(request.query_params)
    if "password" in params:
        params["password"] = "***"
    logger.info("[xc_server] %s %s", request.url.path, params)


def _handle_player_api_action(action: str, params, authenticated: dict) -> dict | list:
    """All the actual catalog work for player_api.php, as a plain synchronous
    function run off the event loop via asyncio.to_thread (see the route
    below) -- every vod_db.* call here opens its own SQLite connection and
    some (get_series_info especially, before it was fixed to use a bulk
    query) do many of them per request. Run inline inside the async route
    handler, that's real synchronous blocking time on FastAPI's single
    event-loop thread -- confirmed live: a real Dispatcharr full-catalog
    sync froze the whole server, including totally unrelated requests like
    a login, until the sync's burst of get_series_info calls finished."""
    allowed_category_ids = _allowed_category_ids(authenticated)

    if action == "get_vod_categories":
        return [
            {"category_id": str(c["id"]), "category_name": c["name"], "parent_id": 0}
            for c in vod_db.list_categories(content_type="movie")
            if allowed_category_ids is None or c["id"] in allowed_category_ids
        ]

    if action == "get_vod_streams":
        rows = vod_db.get_movie_export_rows()
        if allowed_category_ids is not None:
            rows = [r for r in rows if r["category_id"] in allowed_category_ids]
        return [{
            "num": i + 1,
            "name": row["name"] + row["name_suffix"],
            "stream_type": "movie",
            "stream_id": row["export_stream_id"],
            "stream_icon": row.get("poster_url") or "",
            "rating": "0",
            "rating_5based": 0,
            "year": row["year"],
            "added": str(int(time.time())),
            "category_id": str(row["category_id"]),
            "container_extension": row["container_extension"] or "mp4",
            "custom_sid": "",
            "direct_source": "",
        } for i, row in enumerate(rows)]

    if action == "get_vod_info":
        vod_id = params.get("vod_id")
        row = vod_db.get_movie_export_row_by_stream_id(int(vod_id)) if vod_id else None
        if row and allowed_category_ids is not None and row["category_id"] not in allowed_category_ids:
            row = None
        if not row:
            return {"info": {}, "movie_data": {}}
        return {
            "info": {
                "name": row["name"] + row["name_suffix"],
                "o_name": row["name"],
                "cover_big": row.get("poster_url") or "",
                "genre": row["genre"] or "",
                "plot": row["description"] or "",
                "cast": row.get("cast_list") or "",
                "director": row.get("director") or "",
                "release_date": "",
                "year": row["year"],
                "rating": "0",
                "duration_secs": row["duration_secs"] or 0,
            },
            "movie_data": {
                "stream_id": row["export_stream_id"],
                "name": row["name"] + row["name_suffix"],
                "added": str(int(time.time())),
                "category_id": str(row["category_id"]),
                "container_extension": row["container_extension"] or "mp4",
            },
        }

    if action == "get_series_categories":
        return [
            {"category_id": str(c["id"]), "category_name": c["name"], "parent_id": 0}
            for c in vod_db.list_categories(content_type="series")
            if allowed_category_ids is None or c["id"] in allowed_category_ids
        ]

    if action == "get_series":
        rows = vod_db.get_series_export_rows()
        if allowed_category_ids is not None:
            rows = [r for r in rows if r["category_id"] in allowed_category_ids]
        return [{
            "num": i + 1,
            "name": row["name"] + row["name_suffix"],
            "series_id": row["export_series_id"],
            "cover": row.get("poster_url") or "",
            "plot": row["description"] or "",
            "cast": row.get("cast_list") or "",
            "director": row.get("director") or "",
            "genre": row["genre"] or "",
            "releaseDate": "",
            "rating": "0",
            "rating_5based": 0,
            "last_modified": str(int(time.time())),
            "category_id": str(row["category_id"]),
            "backdrop_path": [],
            "youtube_trailer": "",
            "episode_run_time": "",
            "year": row["year"],
        } for i, row in enumerate(rows)]

    if action == "get_series_info":
        series_id_param = params.get("series_id")
        row = vod_db.get_series_export_row_by_export_id(int(series_id_param)) if series_id_param else None
        if row and allowed_category_ids is not None and row["category_id"] not in allowed_category_ids:
            row = None
        if not row:
            return {"seasons": [], "info": {}, "episodes": {}}

        episodes_by_season: dict[str, list] = {}
        for ep in vod_db.get_episode_export_rows_for_series(row["series_id"]):
            season_key = str(ep["season_number"])
            episodes_by_season.setdefault(season_key, []).append({
                "id": str(ep["export_episode_id"]),
                "episode_num": ep["episode_number"],
                "title": ep["name"],
                "container_extension": (ep["container_extension"] or "mp4"),
                "info": {
                    "plot": ep["description"] or "",
                    "duration_secs": ep["duration_secs"] or 0,
                },
            })

        return {
            "seasons": [{"season_number": int(s)} for s in sorted(episodes_by_season, key=int)],
            "info": {
                "name": row["name"] + row["name_suffix"],
                "cover": row.get("poster_url") or "",
                "plot": row["description"] or "",
                "genre": row["genre"] or "",
                "releaseDate": "",
                "cast": row.get("cast_list") or "",
                "director": row.get("director") or "",
                "rating": "0",
                "year": row["year"],
            },
            "episodes": episodes_by_season,
        }

    logger.warning("[xc_server] unhandled action=%s params=%s", action, dict(params))
    return []


@router.get("/player_api.php")
async def player_api(request: Request):
    _log_hit(request)
    action   = request.query_params.get("action")
    username = request.query_params.get("username", "")
    password = request.query_params.get("password", "")
    authenticated = await _authenticate(username, password, request)
    now = int(time.time())

    if not action:
        # XC protocol overloads this same no-action call as the login
        # handshake — real XC servers respond 200 with auth:0 on bad
        # credentials rather than an HTTP error, so clients can surface a
        # clean "invalid login" instead of a connection failure.
        return {
            "user_info": {
                "username": username,
                "password": password,
                "message": "" if authenticated else "Invalid credentials",
                "auth": 1 if authenticated else 0,
                "status": "Active" if authenticated else "Disabled",
                "exp_date": str(now + 365 * 24 * 3600),
                "is_trial": "0",
                "active_cons": "0",
                "created_at": str(now),
                "max_connections": "1",
                "allowed_output_formats": ["m3u8", "ts"],
            },
            "server_info": {
                "url": request.url.hostname,
                "port": str(request.url.port or 80),
                "https_port": "443",
                "server_protocol": request.url.scheme,
                "rtmp_port": "25462",
                "timezone": "UTC",
                "timestamp_now": now,
                "time_now": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        }

    if not authenticated:
        logger.warning("[xc_server] rejected action=%s for username=%s (bad credentials)", action, username)
        return Response(status_code=401, content="Unauthorized")

    return await asyncio.to_thread(_handle_player_api_action, action, request.query_params, authenticated)


_active_vod_streams: dict[int, int] = {}  # provider_id -> count of VOD streams we're currently relaying
_live_viewer_cache: dict[int, tuple[int, float]] = {}  # dispatcharr_account_id -> (viewer_count, monotonic_at)
_LIVE_VIEWER_CACHE_TTL = 5.0  # seconds — short, since range-seek requests can fire many proxy calls in quick succession

# conn_id -> session info, for the "Activity" panel in VOD Manager and for
# driving Plex timeline heartbeats. Best-effort/in-memory only — a restart
# clears it, same as _active_vod_streams above.
_active_sessions: dict[str, dict] = {}
_PLEX_HEARTBEAT_INTERVAL_SECONDS = 10.0


def get_active_sessions() -> list[dict]:
    return list(_active_sessions.values())


# Sessions marked for forced termination -- checked by the relay loop itself
# (see _proxy_vod_stream's relay()) on every chunk, same choke point as the
# request.is_disconnected() check. Needed because disconnect detection isn't
# fully reliable on its own (a client closing its tab doesn't always tear
# down the TCP connection promptly, especially through a proxy/tunnel), so a
# session can outlive the player that opened it -- confirmed live: a closed
# preview kept relaying real bytes from the upstream provider afterward.
_kill_requested: set[str] = set()


def kill_session(conn_id: str) -> bool:
    """Returns False if conn_id isn't currently an active session (nothing
    to kill) -- true even if it was already marked, so a caller can't
    accidentally kill a *different*, later session that happens to reuse
    the id (conn_ids are time-stamped, so that's only a theoretical risk,
    but worth being precise about). HLS sessions (see below) have no single
    long-lived request whose chunk loop can check _kill_requested -- ffmpeg
    writes segments to disk independently of whether anyone's fetching them
    -- so those are torn down directly instead of just flagged."""
    session = _active_sessions.get(conn_id)
    if session is None:
        return False
    hls_id = session.get("_hls_id")
    if hls_id:
        asyncio.ensure_future(_stop_hls_session(hls_id))
    else:
        _kill_requested.add(conn_id)
    return True


async def _live_viewer_count(connection: dict, dispatcharr_account_id: int) -> int:
    cache_key = (connection["id"], dispatcharr_account_id)
    now = time.monotonic()
    cached = _live_viewer_cache.get(cache_key)
    if cached and (now - cached[1]) < _LIVE_VIEWER_CACHE_TTL:
        return cached[0]
    try:
        account = await DispatcharrClient(connection["url"], connection["token"]).get(f"/api/m3u/accounts/{dispatcharr_account_id}/")
        count = sum(p.get("current_viewers", 0) for p in account.get("profiles", []))
    except Exception as exc:
        logger.warning("[xc_server] failed to fetch live viewer count from connection=%s account=%s: %s",
                        connection["label"], dispatcharr_account_id, exc)
        return _live_viewer_cache.get(cache_key, (0, 0))[0]
    _live_viewer_cache[cache_key] = (count, now)
    return count


async def _has_capacity(provider: dict) -> bool:
    """True if opening one more VOD stream against this provider wouldn't
    exceed its real, total connection cap — which is shared across every
    live-TV account (on any Dispatcharr instance) plus VOD Manager's own
    usage that draws from the same real upstream. A single real provider's
    connection pool is one thing regardless of how many different
    Dispatcharr instances happen to have their own native live-TV account
    against it (see vod_db.provider_live_accounts) -- one real subscription
    limit, summed across all of them. Only coordinates when the provider has
    both shared_connection_limit and at least one linked live account;
    otherwise always returns True (no coordination attempted)."""
    limit = provider.get("shared_connection_limit")
    if not limit:
        return True
    live_accounts = await asyncio.to_thread(vod_db.list_provider_live_accounts, provider["id"])
    if not live_accounts:
        return True

    live_count = 0
    for link in live_accounts:
        connection = await asyncio.to_thread(vod_db.get_dispatcharr_connection, link["dispatcharr_connection_id"])
        if not connection:
            continue
        live_count += await _live_viewer_count(connection, link["dispatcharr_account_id"])

    our_count = _active_vod_streams.get(provider["id"], 0)
    return (live_count + our_count) < limit


_PLEX_PROBE_GRACE_SECONDS = 3.0  # short connections under this are treated as
# a player briefly probing several items (e.g. scrolling an episode list),
# not real playback — never reported to Plex at all, so Activity doesn't
# flash a session into existence and immediately mark it stopped/errored.


async def _plex_heartbeat_loop(
    provider: dict, rating_key: str, conn_id: str, total_bytes: int, duration_secs: int | None,
) -> None:
    """Sends Plex a 'playing' timeline update every ~10s for as long as this
    session is open, so it shows up in Now Playing / Activity. Position is
    approximated from how far into the file we've relayed so far (see the
    range_start_byte/bytes_sent tracking in _proxy_vod_stream) — not frame-
    accurate, but close enough for a progress bar."""
    client = plex_client.PlexClient(provider)
    duration_ms = (duration_secs or 0) * 1000
    try:
        await asyncio.sleep(_PLEX_PROBE_GRACE_SECONDS)
        while True:
            session = _active_sessions.get(conn_id)
            if not session:
                return
            played_bytes = session["range_start_byte"] + session["bytes_sent"]
            time_ms = int((played_bytes / total_bytes) * duration_ms) if total_bytes and duration_ms else 0
            await client.report_timeline(rating_key, "playing", time_ms, duration_ms)
            session["plex_reported"] = True
            await asyncio.sleep(_PLEX_HEARTBEAT_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass


async def _emby_heartbeat_loop(
    provider: dict, item_id: str, conn_id: str, total_bytes: int, duration_secs: int | None,
) -> None:
    """Emby counterpart to _plex_heartbeat_loop — same grace-period/position-
    approximation/probe-flapping-avoidance shape, adapted to Emby's three-call
    Sessions/Playing → Playing/Progress → Playing/Stopped shape instead of
    Plex's single timeline endpoint."""
    client = emby_vod_client.EmbyVodClient(provider)
    duration_ticks = (duration_secs or 0) * 10_000_000
    play_session_id = conn_id
    reported_playing = False
    try:
        await asyncio.sleep(_PLEX_PROBE_GRACE_SECONDS)
        while True:
            session = _active_sessions.get(conn_id)
            if not session:
                return
            played_bytes = session["range_start_byte"] + session["bytes_sent"]
            position_ticks = int((played_bytes / total_bytes) * duration_ticks) if total_bytes and duration_ticks else 0
            if not reported_playing:
                await client.report_playing(item_id, item_id, play_session_id, position_ticks)
                reported_playing = True
            else:
                await client.report_progress(item_id, item_id, play_session_id, position_ticks)
            session["emby_reported"] = True
            await asyncio.sleep(_PLEX_HEARTBEAT_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass


def _build_upstream_url(kind: str, provider: dict, source: dict) -> str:
    if provider.get("provider_type") == "plex":
        # provider_stream_id holds the Plex Part key (e.g.
        # "/library/parts/12345/file.mkv") captured at import time —
        # direct-play file serving, not XC's movie/user/pass/id.ext shape.
        return f"{provider['base_url'].rstrip('/')}{source['provider_stream_id']}?X-Plex-Token={provider['password']}"
    if provider.get("provider_type") in ("emby", "jellyfin"):
        # provider_stream_id holds the Emby/Jellyfin item Id captured at
        # import time; Static=true skips server-side transcoding, so this
        # is a true direct-play passthrough of the source file. No
        # MediaSourceId param — Emby's own MediaSources[].Id is a distinct
        # string (e.g. "mediasource_<item id>"), not the item id itself, and
        # passing the wrong value 400s ("Value cannot be null (mediaSource)")
        # rather than being ignored — omitting it lets Emby auto-resolve the
        # item's (typically singular) source instead.
        ext = source["container_extension"] or "mp4"
        item_id = source["provider_stream_id"]
        return (f"{provider['base_url'].rstrip('/')}/emby/Videos/{item_id}/stream.{ext}"
                f"?Static=true&api_key={provider['password']}")
    ext = source["container_extension"] or "mp4"
    return f"{provider['base_url'].rstrip('/')}/{kind}/{provider['username']}/{provider['password']}/{source['provider_stream_id']}.{ext}"


async def _transcode_vod_stream(kind: str, source: dict, request: Request, start_secs: int = 0, title: str = "?") -> Response:
    """Re-encodes a source to browser-compatible H.264/AAC on the fly and
    streams the result — for files a stock <video> element can't play
    natively (AVI containers, DTS/AC-3 audio, XviD/DivX video, etc.). Only
    matters for the in-app test player; external players (VLC etc.) already
    handle these files fine via the direct Copy URL. Real re-encoding, not
    the '-c copy' remux the live-TV endpoint uses in routes.py — that only
    repackages the container, which doesn't help when the actual codec
    inside is what the browser can't decode.

    No seek support once playing: this is a single forward-only ffmpeg pipe,
    not HLS segments, so scrubbing mid-stream won't work (see
    vod_manager-1qk for the real fix, a proper HLS segmenter -- a
    meaningfully bigger feature than this). start_secs is a cheaper partial
    fix for a real workflow need: jumping straight past an intro to verify
    an ambiguous title without waiting through it every time. It's an
    input-side ffmpeg -ss (seeks via the container's own index before
    decoding starts, not a decode-then-discard seek), so a new stream has
    to be requested for each jump rather than dragging a scrubber -- still
    far cheaper than watching from zero every time."""
    provider = vod_db.get_provider(source["provider_id"])
    if not provider:
        return Response(status_code=404, content="not found")
    upstream_url = _build_upstream_url(kind, provider, source)
    conn_id = f"transcode-{time.time():.3f}"
    logger.info("[xc_server] %s transcode OPEN id=%s start=%ds upstream=%s", kind, conn_id, start_secs, upstream_url)

    _active_sessions[conn_id] = {
        "conn_id": conn_id, "kind": kind, "title": f"{title} (transcoded)", "provider_name": provider["name"],
        "provider_type": provider.get("provider_type", "xc"), "started_at": time.time(),
        "bytes_sent": 0, "total_bytes": 0, "duration_secs": None,
        "range_start_byte": 0, "plex_reported": False, "emby_reported": False,
    }

    async def generate():
        args = ["ffmpeg", "-loglevel", "error"]
        if start_secs > 0:
            args += ["-ss", str(start_secs)]
        args += [
            "-fflags", "+discardcorrupt+genpts",
            "-i", upstream_url,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def log_stderr():
            async for line in proc.stderr:
                logger.warning("[ffmpeg-vod] id=%s: %s", conn_id, line.decode(errors="replace").rstrip())
        asyncio.ensure_future(log_stderr())

        bytes_sent = 0
        t_start = time.monotonic()
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                if conn_id in _kill_requested:
                    break
                if await request.is_disconnected():
                    break
                bytes_sent += len(chunk)
                if conn_id in _active_sessions:
                    _active_sessions[conn_id]["bytes_sent"] = bytes_sent
                yield chunk
        finally:
            _kill_requested.discard(conn_id)
            _active_sessions.pop(conn_id, None)
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            logger.info("[xc_server] %s transcode CLOSE id=%s duration=%.2fs bytes=%d",
                        kind, conn_id, time.monotonic() - t_start, bytes_sent)

    return StreamingResponse(
        generate(), media_type="video/mp4",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── HLS segmented transcoding (real seek support) ───────────────────────────
# _transcode_vod_stream above pipes a single forward-only fMP4 stream to the
# browser -- no seek support, since the player never gets a real duration or
# segment index (see vod_manager-1qk). This gives the in-app test player a
# real HLS source instead: ffmpeg writes a growing playlist + .ts segments to
# a per-session temp directory, and the player (via hls.js) fetches them like
# any other HLS stream, with genuine backward seek across everything encoded
# so far (forward seek past the live edge is naturally blocked, same as any
# in-progress live/event HLS playlist -- there's nothing to seek to yet).
# Only worth this complexity for the in-app player -- external players (VLC
# etc.) already seek fine against the direct Copy URL, and real end viewers
# use external players via Dispatcharr, never this transcode path at all.

_HLS_SEGMENT_SECONDS = 4
_HLS_IDLE_TIMEOUT_SECONDS = 45  # no playlist/segment request in this long -> assume the player closed, tear down
_HLS_SWEEP_INTERVAL_SECONDS = 15
_HLS_PLAYLIST_READY_TIMEOUT_SECONDS = 20.0  # how long to wait for ffmpeg to produce a first segment before giving up

_hls_sessions: dict[str, dict] = {}  # hls_id -> {tmpdir, proc, conn_id, last_request_at}
_last_hls_sweep_at = 0.0


def _read_text_file(path: str) -> str:
    with open(path, "r") as f:
        return f.read()


async def _start_hls_session(kind: str, source: dict, request: Request, username: str, password: str,
                              start_secs: int, title: str) -> str | None:
    provider = vod_db.get_provider(source["provider_id"])
    if not provider:
        return None
    upstream_url = _build_upstream_url(kind, provider, source)

    hls_id = secrets.token_urlsafe(16)
    tmpdir = tempfile.mkdtemp(prefix=f"vodhls-{hls_id[:8]}-")
    conn_id = f"hls-{hls_id}"
    base_url = f"{request.url.scheme}://{request.url.netloc}/hls-segment/{username}/{password}/{hls_id}/"

    args = ["ffmpeg", "-loglevel", "error"]
    if start_secs > 0:
        args += ["-ss", str(start_secs)]
    args += [
        "-fflags", "+discardcorrupt+genpts",
        "-i", upstream_url,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-f", "hls",
        "-hls_time", str(_HLS_SEGMENT_SECONDS),
        "-hls_list_size", "0",
        "-hls_playlist_type", "event",
        "-hls_flags", "independent_segments+temp_file",
        "-hls_segment_filename", os.path.join(tmpdir, "seg%05d.ts"),
        "-hls_base_url", base_url,
        os.path.join(tmpdir, "index.m3u8"),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    async def log_stderr():
        async for line in proc.stderr:
            logger.warning("[ffmpeg-hls] id=%s: %s", hls_id, line.decode(errors="replace").rstrip())
    asyncio.ensure_future(log_stderr())

    _hls_sessions[hls_id] = {
        "tmpdir": tmpdir, "proc": proc, "conn_id": conn_id, "last_request_at": time.monotonic(),
    }
    _active_sessions[conn_id] = {
        "conn_id": conn_id, "kind": kind, "title": f"{title} (HLS)", "provider_name": provider["name"],
        "provider_type": provider.get("provider_type", "xc"), "started_at": time.time(),
        "bytes_sent": 0, "total_bytes": 0, "duration_secs": None,
        "range_start_byte": 0, "plex_reported": False, "emby_reported": False,
        "_hls_id": hls_id,
    }
    logger.info("[xc_server] %s HLS OPEN id=%s start=%ds upstream=%s", kind, hls_id, start_secs, upstream_url)
    return hls_id


async def _stop_hls_session(hls_id: str) -> None:
    session = _hls_sessions.pop(hls_id, None)
    if not session:
        return
    _active_sessions.pop(session["conn_id"], None)
    proc = session["proc"]
    if proc.returncode is None:
        proc.kill()
        await proc.wait()
    await asyncio.to_thread(shutil.rmtree, session["tmpdir"], True)
    logger.info("[xc_server] HLS CLOSE id=%s", hls_id)


async def _sweep_stale_hls_sessions() -> None:
    global _last_hls_sweep_at
    now = time.monotonic()
    if now - _last_hls_sweep_at < _HLS_SWEEP_INTERVAL_SECONDS:
        return
    _last_hls_sweep_at = now
    stale = [hid for hid, s in _hls_sessions.items() if now - s["last_request_at"] > _HLS_IDLE_TIMEOUT_SECONDS]
    for hid in stale:
        logger.info("[xc_server] HLS id=%s idle timeout (no request in %ds), tearing down", hid, _HLS_IDLE_TIMEOUT_SECONDS)
        await _stop_hls_session(hid)


async def hls_sweep_loop() -> None:
    """Standalone background task (registered in main.py's lifespan) --
    _sweep_stale_hls_sessions above only runs opportunistically from inside
    the playlist/segment routes, which is fine while there's other HLS
    traffic but does nothing at all for the common real case: someone
    previews a movie, closes the tab, and never opens the HLS player again
    all session. Without this, that leaks a running ffmpeg process and its
    temp directory until the container restarts."""
    while True:
        await asyncio.sleep(_HLS_SWEEP_INTERVAL_SECONDS)
        try:
            global _last_hls_sweep_at
            _last_hls_sweep_at = time.monotonic()
            now = time.monotonic()
            stale = [hid for hid, s in _hls_sessions.items() if now - s["last_request_at"] > _HLS_IDLE_TIMEOUT_SECONDS]
            for hid in stale:
                logger.info("[xc_server] HLS id=%s idle timeout (no request in %ds), tearing down", hid, _HLS_IDLE_TIMEOUT_SECONDS)
                await _stop_hls_session(hid)
        except Exception as exc:
            logger.warning("[xc_server] hls_sweep_loop cycle failed: %s", exc)


async def _serve_hls_playlist(kind: str, source: dict, request: Request, username: str, password: str,
                               start_secs: int, title: str) -> Response:
    await _sweep_stale_hls_sessions()
    hls_id = await _start_hls_session(kind, source, request, username, password, start_secs, title)
    if hls_id is None:
        return Response(status_code=404, content="not found")
    session = _hls_sessions[hls_id]
    playlist_path = os.path.join(session["tmpdir"], "index.m3u8")

    # HLS players expect the playlist to already list a fetchable segment on
    # first load -- wait briefly for ffmpeg to actually produce one rather
    # than handing back an empty/nonexistent playlist immediately.
    deadline = time.monotonic() + _HLS_PLAYLIST_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if os.path.isfile(playlist_path):
            content = await asyncio.to_thread(_read_text_file, playlist_path)
            if "#EXTINF" in content:
                return PlainTextResponse(content, media_type="application/vnd.apple.mpegurl")
        if session["proc"].returncode is not None:
            break  # ffmpeg exited before producing a usable segment
        await asyncio.sleep(0.3)

    logger.warning("[xc_server] HLS id=%s playlist not ready within %.0fs, aborting", hls_id, _HLS_PLAYLIST_READY_TIMEOUT_SECONDS)
    await _stop_hls_session(hls_id)
    return Response(status_code=504, content="transcode did not start in time")


@router.get("/preview/movie-source-hls/{username}/{password}/{source_id_ext}/index.m3u8")
async def preview_movie_source_hls_playlist(username: str, password: str, source_id_ext: str, request: Request, start: int = 0):
    _log_hit(request)
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    source_id = int(source_id_ext.split(".")[0])
    source = vod_db.get_movie_source_for_streaming(source_id)
    if not source or not _movie_allowed(client, source["movie_id"]):
        return Response(status_code=404, content="not found")
    title = f"{source['movie_name']} ({source['movie_year']})" if source.get("movie_year") else source["movie_name"]
    return await _serve_hls_playlist("movie", source, request, username, password, start_secs=max(0, start), title=title)


@router.get("/preview/series-source-hls/{username}/{password}/{source_id_ext}/index.m3u8")
async def preview_episode_source_hls_playlist(username: str, password: str, source_id_ext: str, request: Request, start: int = 0):
    _log_hit(request)
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    source_id = int(source_id_ext.split(".")[0])
    source = vod_db.get_episode_source_for_streaming(source_id)
    if not source or not _series_allowed(client, source["series_id"]):
        return Response(status_code=404, content="not found")
    title = f"{source['series_name']} S{source['season_number']}E{source['episode_number']} — {source['episode_name']}"
    return await _serve_hls_playlist("series", source, request, username, password, start_secs=max(0, start), title=title)


@router.get("/hls-segment/{username}/{password}/{hls_id}/{segment_name}")
async def hls_segment(username: str, password: str, hls_id: str, segment_name: str, request: Request):
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    await _sweep_stale_hls_sessions()
    session = _hls_sessions.get(hls_id)
    if not session:
        return Response(status_code=404, content="not found")
    if not re.fullmatch(r"seg\d+\.ts", segment_name):
        return Response(status_code=400, content="bad segment name")
    session["last_request_at"] = time.monotonic()
    path = os.path.join(session["tmpdir"], segment_name)
    if not os.path.isfile(path):
        return Response(status_code=404, content="not found")
    return FileResponse(path, media_type="video/mp2t")


async def _proxy_vod_stream(
    kind: str, username: str, sources: list[dict], request: Request,
    title: str = "?", duration_secs: int | None = None,
) -> Response:
    """Tries each source (provider carrying this movie/episode) in order,
    most-recently-imported first, falling over to the next one if a provider
    is unreachable, returns an error status, or is at its shared connection
    capacity — real cross-provider failover, not just a single best-guess
    source. See vod_db.list_movie_sources_for_streaming."""
    conn_id = f"{username}-{time.time():.3f}"

    if not sources:
        logger.warning("[xc_server] %s stream 404 id=%s (no active source)", kind, conn_id)
        return Response(status_code=404, content="not found")

    forward_headers = {}
    if "range" in request.headers:
        forward_headers["range"] = request.headers["range"]
    logger.info("[xc_server] %s stream request id=%s range=%s", kind, conn_id, forward_headers.get("range", "(none)"))

    last_error = None
    for idx, source in enumerate(sources):
        provider = vod_db.get_provider(source["provider_id"])
        if not provider:
            continue

        if not await _has_capacity(provider):
            last_error = "at shared connection capacity"
            logger.warning("[xc_server] %s stream source %d/%d (%s) at shared capacity id=%s, trying next",
                            kind, idx + 1, len(sources), provider["name"], conn_id)
            continue

        upstream_url = _build_upstream_url(kind, provider, source)

        # follow_redirects=True: real providers commonly 302 movie/series
        # requests off to a CDN edge host rather than serving the file
        # directly — without this, we'd relay that dead-end redirect
        # straight to the client instead of the actual video.
        custom_ua = provider.get("custom_user_agent")
        client = httpx.AsyncClient(
            timeout=30.0, follow_redirects=True,
            headers={"User-Agent": custom_ua} if custom_ua else _UPSTREAM_HEADERS,
        )
        t_connect_start = time.monotonic()
        try:
            upstream_req = client.build_request("GET", upstream_url, headers=forward_headers)
            upstream_resp = await client.send(upstream_req, stream=True)
        except Exception as exc:
            await client.aclose()
            last_error = str(exc)
            logger.warning("[xc_server] %s stream source %d/%d (%s) connect FAILED id=%s after %.1fs: %s: %s",
                            kind, idx + 1, len(sources), provider["name"], conn_id,
                            time.monotonic() - t_connect_start, type(exc).__name__, exc)
            continue

        if upstream_resp.status_code >= 400:
            last_error = f"HTTP {upstream_resp.status_code}"
            logger.warning("[xc_server] %s stream source %d/%d (%s) returned %s id=%s, trying next",
                            kind, idx + 1, len(sources), provider["name"], last_error, conn_id)
            await upstream_resp.aclose()
            await client.aclose()
            continue

        logger.info(
            "[xc_server] %s stream OPEN id=%s -> provider=%s (source %d/%d) status=%s connect=%.2fs upstream=%s",
            kind, conn_id, provider["name"], idx + 1, len(sources),
            upstream_resp.status_code, time.monotonic() - t_connect_start, upstream_url,
        )
        _active_vod_streams[provider["id"]] = _active_vod_streams.get(provider["id"], 0) + 1

        # Approximate playback position from the requested Range's start byte
        # — we're relaying raw bytes, not a real player, so this is the only
        # signal available for "where in the file is this." Good enough for
        # Plex's timeline heartbeat and the Activity panel's progress display.
        range_start_byte = 0
        if "range" in forward_headers:
            try:
                range_start_byte = int(forward_headers["range"].split("=")[1].split("-")[0])
            except (IndexError, ValueError):
                pass
        total_bytes = int(upstream_resp.headers.get("content-length") or 0)
        if "content-range" in upstream_resp.headers:
            try:
                total_bytes = int(upstream_resp.headers["content-range"].rsplit("/", 1)[-1])
            except ValueError:
                pass

        _active_sessions[conn_id] = {
            "conn_id": conn_id, "kind": kind, "title": title, "provider_name": provider["name"],
            "provider_type": provider.get("provider_type", "xc"), "started_at": time.time(),
            "bytes_sent": 0, "total_bytes": total_bytes, "duration_secs": duration_secs,
            "range_start_byte": range_start_byte, "plex_reported": False, "emby_reported": False,
        }

        heartbeat_task = None
        plex_rating_key = source.get("plex_rating_key")
        emby_item_id = source.get("provider_stream_id")
        if provider.get("provider_type") == "plex" and plex_rating_key:
            heartbeat_task = asyncio.create_task(
                _plex_heartbeat_loop(provider, plex_rating_key, conn_id, total_bytes, duration_secs)
            )
        elif provider.get("provider_type") in ("emby", "jellyfin") and emby_item_id:
            heartbeat_task = asyncio.create_task(
                _emby_heartbeat_loop(provider, emby_item_id, conn_id, total_bytes, duration_secs)
            )

        async def relay():
            t_start = time.monotonic()
            bytes_sent = 0
            outcome = "ok"
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    if conn_id in _kill_requested:
                        outcome = "killed"
                        break
                    if await request.is_disconnected():
                        outcome = "client disconnected"
                        break
                    bytes_sent += len(chunk)
                    if conn_id in _active_sessions:
                        _active_sessions[conn_id]["bytes_sent"] = bytes_sent
                    yield chunk
            except Exception as exc:
                outcome = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                _kill_requested.discard(conn_id)
                await upstream_resp.aclose()
                await client.aclose()
                _active_vod_streams[provider["id"]] = max(0, _active_vod_streams.get(provider["id"], 1) - 1)
                closed_session = _active_sessions.pop(conn_id, None)
                if heartbeat_task:
                    heartbeat_task.cancel()
                    # Only tell Plex it "stopped" if we ever actually told it
                    # "playing" — a connection that never made it past the
                    # probe grace period was never registered in the first
                    # place, so there's nothing to close out.
                    if plex_rating_key and closed_session and closed_session.get("plex_reported"):
                        final_ms = int(((range_start_byte + bytes_sent) / total_bytes) * (duration_secs or 0) * 1000) if total_bytes else 0
                        asyncio.create_task(plex_client.PlexClient(provider).report_timeline(
                            plex_rating_key, "stopped", final_ms, (duration_secs or 0) * 1000,
                        ))
                    if emby_item_id and closed_session and closed_session.get("emby_reported"):
                        final_ticks = int(((range_start_byte + bytes_sent) / total_bytes) * (duration_secs or 0) * 10_000_000) if total_bytes else 0
                        asyncio.create_task(emby_vod_client.EmbyVodClient(provider).report_stopped(
                            emby_item_id, emby_item_id, conn_id, final_ticks,
                        ))
                logger.info(
                    "[xc_server] %s stream CLOSE id=%s outcome=%s duration=%.2fs bytes=%d (%.1f KB/s)",
                    kind, conn_id, outcome, time.monotonic() - t_start, bytes_sent,
                    (bytes_sent / 1024) / max(time.monotonic() - t_start, 0.001),
                )

        passthrough_headers = {}
        for h in ("content-range", "content-length"):
            if h in upstream_resp.headers:
                passthrough_headers[h] = upstream_resp.headers[h]
        # Some real providers send a malformed Accept-Ranges value (e.g. a
        # byte range instead of the literal token "bytes") — never pass that
        # through verbatim; set it ourselves from what actually happened.
        if upstream_resp.status_code == 206 or "content-range" in passthrough_headers:
            passthrough_headers["accept-ranges"] = "bytes"

        return StreamingResponse(
            relay(),
            status_code=upstream_resp.status_code,
            media_type=upstream_resp.headers.get("content-type", "video/mp4"),
            headers=passthrough_headers,
        )

    logger.warning("[xc_server] %s stream id=%s exhausted %d source(s), last error: %s",
                    kind, conn_id, len(sources), last_error)
    return Response(status_code=502, content="all sources failed")


@router.get("/movie/{username}/{password}/{stream_id_ext}")
async def movie_stream(username: str, password: str, stream_id_ext: str, request: Request):
    _log_hit(request)
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    export_stream_id = int(stream_id_ext.split(".")[0])
    row = vod_db.get_movie_export_row_by_stream_id(export_stream_id)
    if not row:
        return Response(status_code=404, content="not found")
    allowed = _allowed_category_ids(client)
    if allowed is not None and row["category_id"] not in allowed:
        return Response(status_code=404, content="not found")
    sources = vod_db.list_movie_sources_for_streaming(row["movie_id"])
    title = f"{row['name']} ({row['year']})" if row.get("year") else row["name"]
    return await _proxy_vod_stream("movie", username, sources, request, title=title, duration_secs=row.get("duration_secs"))


@router.get("/series/{username}/{password}/{episode_id_ext}")
async def series_stream(username: str, password: str, episode_id_ext: str, request: Request):
    _log_hit(request)
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    export_episode_id = int(episode_id_ext.split(".")[0])
    row = vod_db.get_episode_export_row_by_export_id(export_episode_id)
    if not row:
        return Response(status_code=404, content="not found")
    if not _series_allowed(client, row["series_id"]):
        return Response(status_code=404, content="not found")
    sources = vod_db.list_episode_sources_for_streaming(row["episode_id"])
    series = vod_db.get_series(row["series_id"])
    series_name = series["name"] if series else "?"
    title = f"{series_name} S{row['season_number']}E{row['episode_number']} — {row['name']}"
    return await _proxy_vod_stream("series", username, sources, request, title=title, duration_secs=row.get("duration_secs"))


# ── Preview streaming ────────────────────────────────────────────────────────
# Play/copy-URL for a movie or episode directly, without requiring it to be
# placed in a category first. The real /movie and /series routes above are
# keyed by export_stream_id, which only exists once something is placed —
# that's a real requirement for the public catalog (Dispatcharr needs a
# distinct id per placement to avoid collapsing same-(name,year) entries),
# but it means an unplaced item has no way to preview at all. These routes
# key directly off movie_id/episode_id instead, same XC-credential-in-URL
# auth as above so a copied link still works pasted into VLC or any player,
# not just in-app.

@router.get("/preview/movie/{username}/{password}/{movie_id_ext}")
async def preview_movie_stream(username: str, password: str, movie_id_ext: str, request: Request):
    _log_hit(request)
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    movie_id = int(movie_id_ext.split(".")[0])
    movie = vod_db.get_movie(movie_id)
    if not movie or not _movie_allowed(client, movie_id):
        return Response(status_code=404, content="not found")
    sources = vod_db.list_movie_sources_for_streaming(movie_id)
    title = f"{movie['name']} ({movie['year']})" if movie.get("year") else movie["name"]
    return await _proxy_vod_stream("movie", username, sources, request, title=title, duration_secs=movie.get("duration_secs"))


@router.get("/preview/series/{username}/{password}/{episode_id_ext}")
async def preview_episode_stream(username: str, password: str, episode_id_ext: str, request: Request):
    _log_hit(request)
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    episode_id = int(episode_id_ext.split(".")[0])
    episode = vod_db.get_episode(episode_id)
    if episode and not _series_allowed(client, episode["series_id"]):
        return Response(status_code=404, content="not found")
    sources = vod_db.list_episode_sources_for_streaming(episode_id)
    if episode:
        series = vod_db.get_series(episode["series_id"])
        series_name = series["name"] if series else "?"
        title = f"{series_name} S{episode['season_number']}E{episode['episode_number']} — {episode['name']}"
    else:
        title = "?"
    return await _proxy_vod_stream("series", username, sources, request, title=title, duration_secs=episode.get("duration_secs") if episode else None)


# Per-source preview — forces exactly one specific provider's copy rather
# than the normal priority-order failover across every provider that has
# this movie/episode. That's the point of these: they belong on each row in
# the Sources list (testing a *specific* provider's file), not next to a
# category placement, which is just a label and plays identically regardless
# of which category you look at it from.

@router.get("/preview/movie-source/{username}/{password}/{source_id_ext}")
async def preview_movie_source_stream(username: str, password: str, source_id_ext: str, request: Request):
    _log_hit(request)
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    source_id = int(source_id_ext.split(".")[0])
    source = vod_db.get_movie_source_for_streaming(source_id)
    if not source or not _movie_allowed(client, source["movie_id"]):
        return Response(status_code=404, content="not found")
    title = f"{source['movie_name']} ({source['movie_year']})" if source.get("movie_year") else source["movie_name"]
    return await _proxy_vod_stream("movie", username, [source], request, title=title, duration_secs=source.get("duration_secs"))


@router.get("/preview/series-source/{username}/{password}/{source_id_ext}")
async def preview_episode_source_stream(username: str, password: str, source_id_ext: str, request: Request):
    _log_hit(request)
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    source_id = int(source_id_ext.split(".")[0])
    source = vod_db.get_episode_source_for_streaming(source_id)
    if not source or not _series_allowed(client, source["series_id"]):
        return Response(status_code=404, content="not found")
    title = f"{source['series_name']} S{source['season_number']}E{source['episode_number']} — {source['episode_name']}"
    return await _proxy_vod_stream("series", username, [source], request, title=title, duration_secs=source.get("duration_secs"))


# Transcoded variants — same auth/lookup, but re-encode to browser-compatible
# H.264/AAC via ffmpeg instead of relaying the raw file. Use when the direct
# preview above fails with a codec error.

@router.get("/preview/movie-source-transcoded/{username}/{password}/{source_id_ext}")
async def preview_movie_source_transcoded(username: str, password: str, source_id_ext: str, request: Request, start: int = 0):
    _log_hit(request)
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    source_id = int(source_id_ext.split(".")[0])
    source = vod_db.get_movie_source_for_streaming(source_id)
    if not source or not _movie_allowed(client, source["movie_id"]):
        return Response(status_code=404, content="not found")
    title = f"{source['movie_name']} ({source['movie_year']})" if source.get("movie_year") else source["movie_name"]
    return await _transcode_vod_stream("movie", source, request, start_secs=max(0, start), title=title)


@router.get("/preview/series-source-transcoded/{username}/{password}/{source_id_ext}")
async def preview_episode_source_transcoded(username: str, password: str, source_id_ext: str, request: Request, start: int = 0):
    _log_hit(request)
    client = await _authenticate(username, password, request)
    if not client:
        return Response(status_code=401, content="Unauthorized")
    source_id = int(source_id_ext.split(".")[0])
    source = vod_db.get_episode_source_for_streaming(source_id)
    if not source or not _series_allowed(client, source["series_id"]):
        return Response(status_code=404, content="not found")
    title = f"{source['series_name']} S{source['season_number']}E{source['episode_number']} — {source['episode_name']}"
    return await _transcode_vod_stream("series", source, request, start_secs=max(0, start), title=title)

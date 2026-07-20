import hashlib
import os
import json
import secrets
from pathlib import Path

DATA_DIR    = Path(os.environ.get("DATA_DIR", "/app/data"))
CONFIG_FILE = DATA_DIR / "config.json"
APP_PORT    = int(os.environ.get("APP_PORT", "8282"))


def _read_raw() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def _write_raw(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


# ── Dispatcharr connection ───────────────────────────────────────────────────

def get_config() -> tuple[str, str]:
    url   = os.environ.get("DISPATCHARR_URL", "").rstrip("/")
    token = os.environ.get("DISPATCHARR_TOKEN", "")
    if url and token:
        return url, token
    data = _read_raw()
    return data.get("dispatcharr_url", "").rstrip("/"), data.get("dispatcharr_token", "")


def save_config(url: str, token: str) -> None:
    data = _read_raw()
    data.update({"dispatcharr_url": url.rstrip("/"), "dispatcharr_token": token})
    _write_raw(data)


def config_from_env() -> bool:
    return bool(os.environ.get("DISPATCHARR_URL") and os.environ.get("DISPATCHARR_TOKEN"))


def is_configured() -> bool:
    url, token = get_config()
    return bool(url and token)


# ── VOD manager ──────────────────────────────────────────────────────────────
# The Dispatcharr M3U account (account_type=XC) that points back at our own
# xc_server — each provider we manage gets synced there as one M3U profile,
# so Dispatcharr's own per-profile max_streams enforcement stays in lockstep
# with what we know about each real provider's connection limit.

def get_vod_xc_account_id() -> int | None:
    data = _read_raw()
    return data.get("vod_xc_account_id")


def save_vod_xc_account_id(account_id: int) -> None:
    data = _read_raw()
    data["vod_xc_account_id"] = int(account_id)
    _write_raw(data)


def get_last_enrichment_run() -> float | None:
    """When bulk_enrich_all last actually ran — persisted so a container
    restart doesn't reset the schedule and fire a full pass again 45s later
    (every restart used to do exactly that, which is real background write
    load competing with anything else the app is doing at that moment)."""
    data = _read_raw()
    return data.get("last_enrichment_run")


def save_last_enrichment_run(timestamp: float) -> None:
    data = _read_raw()
    data["last_enrichment_run"] = timestamp
    _write_raw(data)


def get_tmdb_api_key() -> str | None:
    data = _read_raw()
    return data.get("tmdb_api_key") or None


def save_tmdb_api_key(api_key: str) -> None:
    data = _read_raw()
    data["tmdb_api_key"] = api_key
    _write_raw(data)


# ── XC login lockout ─────────────────────────────────────────────────────────
# Defaults match xc_server.py's original hardcoded constants. Configurable
# since the right threshold depends on real-world exposure (a shared-NAT
# household behind one connected instance's IP could plausibly hit the
# default threshold with typo'd credentials; an internet-facing deployment
# might want it tighter, not looser).

_LOCKOUT_DEFAULTS = {
    "lockout_max_attempts": 10,
    "lockout_window_seconds": 300,
    "lockout_duration_seconds": 900,
}


def get_lockout_settings() -> dict:
    data = _read_raw()
    return {k: data.get(k, v) for k, v in _LOCKOUT_DEFAULTS.items()}


def save_lockout_settings(max_attempts: int, window_seconds: int, duration_seconds: int) -> None:
    data = _read_raw()
    data.update({
        "lockout_max_attempts":     max(1, int(max_attempts)),
        "lockout_window_seconds":   max(1, int(window_seconds)),
        "lockout_duration_seconds": max(1, int(duration_seconds)),
    })
    _write_raw(data)


# ── Background refresh scheduling ───────────────────────────────────────────
# Catalog refresh interval is per provider_type, not a single global value --
# a Plex/Emby library scan can take 18+ minutes and real disk I/O, while an
# XC provider's catalog pull is cheap; forcing them onto the same interval
# means either XC providers go stale waiting on Plex's cadence, or Plex/Emby
# get rescanned far more often than needed. Enrichment TTL and the TMDB Lists
# auto-sync interval are each a single global value -- lower-stakes, no
# similar per-source cost asymmetry. TMDB auto-sync defaults to disabled
# (None) since it's new background API traffic that didn't run before at all;
# opt-in rather than silently started for existing deployments.

_REFRESH_DEFAULTS = {
    "catalog_refresh_seconds_xc":       6 * 3600,
    "catalog_refresh_seconds_plex":     6 * 3600,
    "catalog_refresh_seconds_emby":     6 * 3600,
    "catalog_refresh_seconds_jellyfin": 6 * 3600,
    "enrichment_ttl_seconds":           24 * 3600,
    "tmdb_sync_interval_seconds":       None,
}


def get_refresh_settings() -> dict:
    data = _read_raw()
    return {k: data.get(k, v) for k, v in _REFRESH_DEFAULTS.items()}


def save_refresh_settings(
    catalog_refresh_seconds_xc: int,
    catalog_refresh_seconds_plex: int,
    catalog_refresh_seconds_emby: int,
    catalog_refresh_seconds_jellyfin: int,
    enrichment_ttl_seconds: int,
    tmdb_sync_interval_seconds: int | None,
) -> None:
    data = _read_raw()
    data.update({
        "catalog_refresh_seconds_xc":       max(60, int(catalog_refresh_seconds_xc)),
        "catalog_refresh_seconds_plex":     max(60, int(catalog_refresh_seconds_plex)),
        "catalog_refresh_seconds_emby":     max(60, int(catalog_refresh_seconds_emby)),
        "catalog_refresh_seconds_jellyfin": max(60, int(catalog_refresh_seconds_jellyfin)),
        "enrichment_ttl_seconds":           max(60, int(enrichment_ttl_seconds)),
        "tmdb_sync_interval_seconds":       max(60, int(tmdb_sync_interval_seconds)) if tmdb_sync_interval_seconds else None,
    })
    _write_raw(data)


# ── Auth ──────────────────────────────────────────────────────────────────────

def has_credentials() -> bool:
    if os.environ.get("VODMANAGER_ADMIN_USER") and os.environ.get("VODMANAGER_ADMIN_PASSWORD"):
        return True
    data = _read_raw()
    return bool(data.get("auth_username") and data.get("auth_hash"))


def verify_credentials(username: str, password: str) -> bool:
    env_user = os.environ.get("VODMANAGER_ADMIN_USER", "")
    env_pass = os.environ.get("VODMANAGER_ADMIN_PASSWORD", "")
    if env_user and env_pass:
        return (
            secrets.compare_digest(username.encode(), env_user.encode()) and
            secrets.compare_digest(password.encode(), env_pass.encode())
        )
    data        = _read_raw()
    stored_user = data.get("auth_username", "")
    stored_salt = data.get("auth_salt", "")
    stored_hash = data.get("auth_hash", "")
    if not (stored_user and stored_salt and stored_hash):
        return False
    candidate = hashlib.sha256((stored_salt + password).encode()).hexdigest()
    return (
        secrets.compare_digest(username.encode(), stored_user.encode()) and
        secrets.compare_digest(candidate.encode(), stored_hash.encode())
    )


def set_credentials(username: str, password: str) -> None:
    salt   = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    data   = _read_raw()
    data.update({"auth_username": username, "auth_salt": salt, "auth_hash": hashed})
    _write_raw(data)

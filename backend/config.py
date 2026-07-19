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

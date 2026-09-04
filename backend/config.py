import hashlib
import os
import json
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

DATA_DIR    = Path(os.environ.get("DATA_DIR", "/app/data"))
CONFIG_FILE = DATA_DIR / "config.json"
APP_PORT    = int(os.environ.get("APP_PORT", "8282"))

# Single source of truth for the semantic version -- this already drifted out
# of sync once before (main.py's FastAPI(version=...) vs. routes.py's /version/
# endpoint each having their own independent hardcoded literal), so both now
# import this instead of repeating the string.
APP_VERSION = "0.2.11"

# Persisted log file for main.py's rotating file handler -- the app previously
# only logged to stdout, so a container restart (or just not having docker
# logs handy) lost all history. Also the source diagnostics.py reads from for
# the "download diagnostic logs" export.
LOG_DIR         = DATA_DIR / "logs"
LOG_FILE        = LOG_DIR / "vod_manager.log"
LOG_BACKUP_COUNT = 5


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


# ── Encryption key ───────────────────────────────────────────────────────────
# Lives inside config.json rather than its own file specifically so it rides
# along with config's existing backup/restore/reset lifecycle -- restoring a
# config backup onto a fresh instance needs to bring the same key with it, or
# every already-encrypted provider password / Dispatcharr token in the
# restored database becomes permanently undecryptable. A config reset wiping
# the key too is a foreseeable consequence of the same action already
# destroying the Dispatcharr connection / TMDB key / admin login, not a new
# separate footgun.

def get_or_create_encryption_key() -> bytes:
    data = _read_raw()
    key = data.get("encryption_key")
    if key:
        return key.encode()
    new_key = Fernet.generate_key()
    data["encryption_key"] = new_key.decode()
    _write_raw(data)
    return new_key


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


def get_anthropic_api_key() -> str | None:
    data = _read_raw()
    return data.get("anthropic_api_key") or None


def save_anthropic_api_key(api_key: str) -> None:
    data = _read_raw()
    data["anthropic_api_key"] = api_key
    _write_raw(data)


# ── SMTP / notifications ─────────────────────────────────────────────────────
# The one real credential in this settings block (the SMTP password) is
# encrypted at rest via secrets_util, same as every other real credential in
# this app (provider passwords, Dispatcharr tokens, XC client secrets) --
# imported lazily inside these two functions rather than at module level,
# since secrets_util itself imports config (for get_or_create_encryption_key)
# and a top-level import here would be circular.

def get_smtp_settings() -> dict:
    data = _read_raw()
    smtp = data.get("smtp") or {}
    import secrets_util
    return {
        "host": smtp.get("host") or None,
        "port": smtp.get("port") or 587,
        "username": smtp.get("username") or None,
        "password": secrets_util.decrypt_value(smtp.get("password")),
        "from_address": smtp.get("from_address") or None,
        "use_tls": smtp.get("use_tls", True),
        "admin_recipients": smtp.get("admin_recipients") or [],
    }


def save_smtp_settings(
    host: str | None, port: int, username: str | None, password: str | None,
    from_address: str | None, use_tls: bool, admin_recipients: list[str],
) -> None:
    import secrets_util
    data = _read_raw()
    existing = data.get("smtp") or {}
    data["smtp"] = {
        "host": host,
        "port": port,
        "username": username,
        # A blank password on save means "leave the existing one alone" --
        # the settings form never round-trips the real decrypted password
        # back to the browser, so an empty submit must not wipe it.
        "password": secrets_util.encrypt_value(password) if password else existing.get("password"),
        "from_address": from_address,
        "use_tls": use_tls,
        "admin_recipients": admin_recipients,
    }
    _write_raw(data)


# ── Stream source priority mode ──────────────────────────────────────────────
# User-definable (Curation & Maintenance), per the user's own words 2026-07-29:
# "quality>provider, provider>quality, or just provider, or just quality."
# Default MUST stay "provider" -- this changes live stream selection for
# every source-ordering query in vod_db.py, not something to default-flip.
STREAM_PRIORITY_MODES = ("provider", "quality", "quality_then_provider", "provider_then_quality")


def get_stream_priority_mode() -> str:
    data = _read_raw()
    mode = data.get("stream_priority_mode")
    return mode if mode in STREAM_PRIORITY_MODES else "provider"


def save_stream_priority_mode(mode: str) -> None:
    if mode not in STREAM_PRIORITY_MODES:
        raise ValueError(f"invalid stream_priority_mode: {mode!r}")
    data = _read_raw()
    data["stream_priority_mode"] = mode
    _write_raw(data)


# ── XC title display format (GH issue #1) ────────────────────────────────────
# Pool identity (movies.name/year, dedup matching, Title & Metadata Rules) is
# untouched by this -- it only controls what's appended to the "name" field
# XC clients (Dispatcharr, TiviMate, etc.) actually see in get_vod_streams/
# get_vod_info, so it composes with "Apply TMDB Titles" (which already lets a
# user adopt TMDB's own title into the pool) instead of requiring a second,
# separate title-source toggle.

def get_xc_title_include_year() -> bool:
    return bool(_read_raw().get("xc_title_include_year", False))


def save_xc_title_include_year(enabled: bool) -> None:
    data = _read_raw()
    data["xc_title_include_year"] = bool(enabled)
    _write_raw(data)


# ── Duplicate Finder: quality-prefix matching ────────────────────────────────
# User request 2026-09-04 (vod_manager-cct): a provider's "4K: Title" and
# plain "Title" import as two separate pool entries (exact name+year matching
# only) -- Stream Priority's "quality" mode (above) already picks the best
# SOURCE once they're on one movie/series, but nothing groups the split rows
# together in the first place. Opt-in, default OFF, deliberately -- per the
# user's own explicit requirement: recognizing a leading "4K:"/"UHD:"/"FHD:"
# as the same title changes what Duplicate Finder surfaces for every existing
# install the moment it's on, which is a bigger blast radius than a typical
# bugfix default. Only affects what Duplicate Finder GROUPS as candidates for
# review -- merging still goes through the existing manual/bulk-confirm merge
# flow, nothing auto-merges just from this being enabled.

def get_duplicate_finder_quality_prefix_matching() -> bool:
    return bool(_read_raw().get("duplicate_finder_quality_prefix_matching", False))


def save_duplicate_finder_quality_prefix_matching(enabled: bool) -> None:
    data = _read_raw()
    data["duplicate_finder_quality_prefix_matching"] = bool(enabled)
    _write_raw(data)


# ── AI provider selection ────────────────────────────────────────────────────
# ai_assist.py can talk to any of these three -- a user might already have a
# key for one and not another, or want to compare quality/cost, so the key
# for each is stored independently of which one is actually "active".

AI_PROVIDERS = ("anthropic", "openai", "gemini")

_AI_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-5-mini",
    "gemini": "gemini-2.5-flash",
}


def get_ai_provider() -> str:
    data = _read_raw()
    provider = data.get("ai_provider")
    return provider if provider in AI_PROVIDERS else "anthropic"


def get_ai_model() -> str:
    data = _read_raw()
    return data.get("ai_model") or _AI_DEFAULT_MODELS[get_ai_provider()]


def save_ai_provider(provider: str, model: str | None = None) -> None:
    if provider not in AI_PROVIDERS:
        raise ValueError(f"unknown AI provider '{provider}'")
    data = _read_raw()
    data["ai_provider"] = provider
    data["ai_model"] = model.strip() if model and model.strip() else _AI_DEFAULT_MODELS[provider]
    _write_raw(data)


def get_import_language_exclusion() -> dict:
    """Global (not per-provider) since the same admin almost always wants the
    same languages excluded everywhere -- unlike categories, which genuinely
    differ provider to provider. See vod_importer._should_auto_archive."""
    data = _read_raw()
    return {
        "exclude_prefixes": data.get("import_exclude_language_prefixes") or [],
        "exclude_non_latin": bool(data.get("import_exclude_non_latin")),
    }


def save_import_language_exclusion(exclude_prefixes: list[str], exclude_non_latin: bool) -> None:
    data = _read_raw()
    data["import_exclude_language_prefixes"] = [p.strip().upper() for p in exclude_prefixes if p.strip()]
    data["import_exclude_non_latin"] = bool(exclude_non_latin)
    _write_raw(data)


def get_default_categories_prompt_dismissed() -> bool:
    """Whether the admin has already answered the one-time "include 18+ in
    the built-in All Movies/All TV Shows categories?" prompt (see
    vod_db._seed_default_categories) -- distinct from whether they changed
    the default, since leaving it on the safe default is a valid answer
    too and shouldn't keep re-prompting."""
    return bool(_read_raw().get("default_categories_prompt_dismissed"))


def set_default_categories_prompt_dismissed() -> None:
    data = _read_raw()
    data["default_categories_prompt_dismissed"] = True
    _write_raw(data)


def get_hide_dvr_tab() -> bool:
    """A deployment-wide display preference, not a per-browser one (unlike
    the active-tab/theme choices App.tsx keeps in localStorage) -- whether
    DVR is used at all is a decision about this VOD Manager instance, not
    about who's currently looking at it. Defaults to False (shown) so
    existing installs see no change until an admin opts in."""
    return bool(_read_raw().get("hide_dvr_tab"))


def set_hide_dvr_tab(hidden: bool) -> None:
    data = _read_raw()
    data["hide_dvr_tab"] = hidden
    _write_raw(data)


def get_openai_api_key() -> str | None:
    data = _read_raw()
    return data.get("openai_api_key") or None


def save_openai_api_key(api_key: str) -> None:
    data = _read_raw()
    data["openai_api_key"] = api_key
    _write_raw(data)


def get_gemini_api_key() -> str | None:
    data = _read_raw()
    return data.get("gemini_api_key") or None


def save_gemini_api_key(api_key: str) -> None:
    data = _read_raw()
    data["gemini_api_key"] = api_key
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
# PBKDF2-HMAC-SHA256 with a real iteration count -- a plain single-round
# SHA-256 (this file's original scheme) is fast enough that a leaked
# salt+hash pair is crackable offline at billions of guesses/sec on a GPU;
# PBKDF2's iteration count is specifically what makes that expensive.
# 260k iterations matches OWASP's 2023 minimum recommendation for this
# algorithm. auth_scheme distinguishes old accounts from new ones so
# existing logins keep working -- see verify_credentials's legacy branch.

_PBKDF2_ITERATIONS = 260_000


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS).hex()


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
    if not secrets.compare_digest(username.encode(), stored_user.encode()):
        return False

    if data.get("auth_scheme") == "pbkdf2":
        candidate = _hash_password(password, stored_salt)
        return secrets.compare_digest(candidate.encode(), stored_hash.encode())

    # Legacy single-round-SHA-256 account (predates auth_scheme). Verify
    # against the old scheme, then transparently upgrade to PBKDF2 on this
    # successful login -- migrates existing accounts off the weaker hash
    # without forcing a password reset.
    legacy_candidate = hashlib.sha256((stored_salt + password).encode()).hexdigest()
    ok = secrets.compare_digest(legacy_candidate.encode(), stored_hash.encode())
    if ok:
        set_credentials(stored_user, password)
    return ok


def set_credentials(username: str, password: str) -> None:
    salt   = secrets.token_hex(16)
    hashed = _hash_password(password, salt)
    data   = _read_raw()
    data.update({"auth_username": username, "auth_salt": salt, "auth_hash": hashed, "auth_scheme": "pbkdf2"})
    _write_raw(data)

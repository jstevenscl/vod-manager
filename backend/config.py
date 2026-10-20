import hashlib
import os
import json
import secrets
import threading
from pathlib import Path

try:
    import fcntl  # POSIX only -- production always runs in the Linux container;
                  # on Windows (local dev/tests) this is None and the cross-process
                  # lock below is skipped, same as the rest of the app's dev story.
except ImportError:
    fcntl = None

from cryptography.fernet import Fernet

DATA_DIR    = Path(os.environ.get("DATA_DIR", "/app/data"))
CONFIG_FILE = DATA_DIR / "config.json"
APP_PORT    = int(os.environ.get("APP_PORT", "8282"))

# Single source of truth for the semantic version -- this already drifted out
# of sync once before (main.py's FastAPI(version=...) vs. routes.py's /version/
# endpoint each having their own independent hardcoded literal), so both now
# import this instead of repeating the string.
APP_VERSION = "0.3.05"

# Persisted log file for main.py's rotating file handler -- the app previously
# only logged to stdout, so a container restart (or just not having docker
# logs handy) lost all history. Also the source diagnostics.py reads from for
# the "download diagnostic logs" export.
LOG_DIR         = DATA_DIR / "logs"
LOG_FILE        = LOG_DIR / "vod_manager.log"
LOG_BACKUP_COUNT = 5


# In-memory cache of config.json -- every one of this file's ~40 getters
# (including require_auth's has_credentials() check, run synchronously on
# the event loop for nearly every API request via _GUARDS) used to call
# _read_raw() straight through to a blocking disk read, every single call,
# uncached. Invisible on a fast local disk, but the moment the underlying
# volume is under heavy I/O pressure from something else on the same mount
# (a large SQLite write -- e.g. a slow provider import, or a backfill/scan
# touching the whole catalog), those blocking reads, being on the event
# loop and not wrapped in asyncio.to_thread, stall every concurrent
# request in the whole app, not just the slow one. Caching removes the
# disk read from the hot path entirely for every call after the first.
#
# Reads staying cached (possibly stale) is fine -- config values rarely
# need to be instantaneously fresh across processes. WRITES are a
# different story: see _update_raw below.
_raw_cache: dict | None = None


def _read_raw() -> dict:
    global _raw_cache
    if _raw_cache is None:
        if CONFIG_FILE.exists():
            try:
                _raw_cache = json.loads(CONFIG_FILE.read_text())
            except Exception:
                _raw_cache = {}
        else:
            _raw_cache = {}
    return _raw_cache


def _write_raw(data: dict) -> None:
    global _raw_cache
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    _raw_cache = data


# Guards every config.json WRITE (not reads -- see _raw_cache's own
# docstring) against two different processes stepping on each other.
# _CONFIG_WRITE_LOCK (in-process) and an flock on a sibling lock file
# (cross-process, POSIX only) together close both halves of the race --
# flock alone doesn't serialize two threads in the SAME process (POSIX
# advisory locks are per-process), and the in-process lock obviously can't
# reach a separate process.
_CONFIG_WRITE_LOCK = threading.Lock()


def _config_write_window_hook() -> None:
    """No-op in production. Sits at the exact point a racing writer has
    already read a (possibly stale) config dict but before it has acquired
    the lock below -- tests monkeypatch this to force genuine interleaving
    right at that window, rather than relying on OS scheduling luck to
    (rarely, flakily) hit a race this narrow."""
    pass


def _update_raw(mutate) -> dict:
    """The only correct way to WRITE any part of config.json. Every
    `data = _read_raw(); data[...] = x; _write_raw(data)` call site used to
    read from (and write back) this PROCESS's own possibly-stale in-memory
    cache -- if a DIFFERENT process had written config.json since this
    process's cache was last populated, that write blindly overwrites the
    whole file with a dict that never saw the other process's change,
    silently erasing it. Not a narrow one-time race: it can happen on ANY
    write, at ANY time, any time two processes touch config.json around
    the same time (a one-off `docker exec`/`docker run --rm` script against
    the same data volume while the app is up is an explicitly supported
    pattern -- see _raw_cache's own docstring).

    Found live 2026-09-23 testing playback after merging three PRs: a
    `docker exec` script created an XC client (encrypting its password
    under a freshly-generated key K1, written to config.json). Moments
    later the app's own background enrichment scheduler called
    save_last_enrichment_run from ITS process, whose cache had been
    populated as {} (no key) before the script ran -- that write blindly
    overwrote config.json with its stale dict, erasing K1. The client's
    password was left permanently encrypted under a key nothing could
    reconstruct; the very next credential encrypted anywhere in the app
    silently generated ANOTHER new key, masking the loss with no error at
    all until playback failed with an inexplicable 401.

    mutate(data) is called with a dict freshly read from disk under the
    lock (bypassing every process's stale cache, including this one's) and
    should mutate it in place; the result is then written back and
    returned. This subsumes get_or_create_encryption_key's own former
    bespoke lock+fresh-read, which is now just one caller of this same
    general mechanism."""
    _config_write_window_hook()
    with _CONFIG_WRITE_LOCK:
        lock_path = CONFIG_FILE.parent / ".config.lock"
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "a+")
        try:
            if fcntl:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            global _raw_cache
            _raw_cache = None  # force a genuine disk read, not this process's stale cache
            data = _read_raw()
            mutate(data)
            _write_raw(data)
            return data
        finally:
            if fcntl:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()


# ── Dispatcharr connection ───────────────────────────────────────────────────

def get_config() -> tuple[str, str]:
    url   = os.environ.get("DISPATCHARR_URL", "").rstrip("/")
    token = os.environ.get("DISPATCHARR_TOKEN", "")
    if url and token:
        return url, token
    data = _read_raw()
    return data.get("dispatcharr_url", "").rstrip("/"), data.get("dispatcharr_token", "")


def save_config(url: str, token: str) -> None:
    _update_raw(lambda data: data.update({"dispatcharr_url": url.rstrip("/"), "dispatcharr_token": token}))


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
    """Generating this key is a one-time, NOT-safely-repeatable event: unlike
    every other config.json field, once ANYTHING has been encrypted under a
    key, silently generating a different one doesn't just get overwritten
    cleanly -- decrypt_value's InvalidToken fallback returns that old
    ciphertext unchanged with no error, so the credential just quietly stops
    working (surfaces later as an inexplicable "wrong password", not an
    error pointing at the real cause). See _update_raw's own docstring for
    the two distinct races this guards against (creating the key at all,
    and a later unrelated write silently erasing an already-created one)."""
    data = _read_raw()
    key = data.get("encryption_key")
    if key:
        return key.encode()

    result = {}
    def _mutate(d: dict) -> None:
        if not d.get("encryption_key"):
            d["encryption_key"] = Fernet.generate_key().decode()
        result["key"] = d["encryption_key"]
    _update_raw(_mutate)
    return result["key"].encode()


# ── VOD manager ──────────────────────────────────────────────────────────────
# The Dispatcharr M3U account (account_type=XC) that points back at our own
# xc_server — each provider we manage gets synced there as one M3U profile,
# so Dispatcharr's own per-profile max_streams enforcement stays in lockstep
# with what we know about each real provider's connection limit.

def get_vod_xc_account_id() -> int | None:
    data = _read_raw()
    return data.get("vod_xc_account_id")


def save_vod_xc_account_id(account_id: int) -> None:
    _update_raw(lambda data: data.__setitem__("vod_xc_account_id", int(account_id)))


def get_last_enrichment_run() -> float | None:
    """When bulk_enrich_all last actually ran — persisted so a container
    restart doesn't reset the schedule and fire a full pass again 45s later
    (every restart used to do exactly that, which is real background write
    load competing with anything else the app is doing at that moment)."""
    data = _read_raw()
    return data.get("last_enrichment_run")


def save_last_enrichment_run(timestamp: float) -> None:
    _update_raw(lambda data: data.__setitem__("last_enrichment_run", timestamp))


def get_tmdb_api_key() -> str | None:
    data = _read_raw()
    return data.get("tmdb_api_key") or None


def save_tmdb_api_key(api_key: str) -> None:
    _update_raw(lambda data: data.__setitem__("tmdb_api_key", api_key))


def get_mdblist_api_key() -> str | None:
    data = _read_raw()
    return data.get("mdblist_api_key") or None


def save_mdblist_api_key(api_key: str) -> None:
    _update_raw(lambda data: data.__setitem__("mdblist_api_key", api_key))


def get_anthropic_api_key() -> str | None:
    data = _read_raw()
    return data.get("anthropic_api_key") or None


def save_anthropic_api_key(api_key: str) -> None:
    _update_raw(lambda data: data.__setitem__("anthropic_api_key", api_key))


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
    def _mutate(data: dict) -> None:
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
    _update_raw(_mutate)


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
    _update_raw(lambda data: data.__setitem__("stream_priority_mode", mode))


# ── XC title display format (GH issue #1) ────────────────────────────────────
# Pool identity (movies.name/year, dedup matching, Title & Metadata Rules) is
# untouched by this -- it only controls what's appended to the "name" field
# XC clients (Dispatcharr, TiviMate, etc.) actually see in get_vod_streams/
# get_vod_info, so it composes with "Apply TMDB Titles" (which already lets a
# user adopt TMDB's own title into the pool) instead of requiring a second,
# separate title-source toggle.

# ── .strm export for Emby / Jellyfin (see strm_export.py) ────────────────────

_STRM_EXPORT_DEFAULTS = {
    "output_dir": "", "base_url": "", "xc_client_id": None,
    "include_movies": True, "include_series": True, "include_unmatched": False,
    "mirror": True, "interval_hours": 0,
}


def get_strm_export_settings() -> dict:
    saved = _read_raw().get("strm_export") or {}
    out = {**_STRM_EXPORT_DEFAULTS, **{k: saved[k] for k in _STRM_EXPORT_DEFAULTS if k in saved}}
    out["interval_hours"] = max(0, int(out["interval_hours"] or 0))
    return out


def save_strm_export_settings(settings: dict) -> None:
    clean = {k: settings[k] for k in _STRM_EXPORT_DEFAULTS if k in settings}
    _update_raw(lambda data: data.__setitem__("strm_export", {**(data.get("strm_export") or {}), **clean}))


# ── Opt-in file downloads into a library folder (see vod_downloader.py) ──────
# OFF by default: saving a provider's files to disk is a different act from
# linking to them, so it only exists once the user turns it on.

DOWNLOAD_PRESETS = ("original", "h264_1080", "h264_720", "h264_480", "hevc_1080", "hevc_720")

_DOWNLOADS_DEFAULTS = {
    "enabled": False, "output_dir": "", "default_preset": "original",
    # optional separate roots: movies_dir / shows_dir replace output_dir/Movies and output_dir/Shows
    "movies_dir": "", "shows_dir": "",
    # optional processing folder: downloads and encodes happen here and only the finished, verified file is
    # moved into the library. Blank = a hidden .processing folder inside each library folder.
    "work_dir": "",
    # which audio languages to keep ("" = all tracks as they are) and whether to keep subtitles (in those languages)
    "audio_languages": "", "keep_subtitles": True,
    "tmdb_id_in_folder": True,  # KNM: 2026-10-18 optional [tmdbid-N] in download folder names
    "max_concurrent": 1, "min_free_gb": 5,
    # re-encode engine: software (CPU), auto, vaapi (Intel/AMD GPU), qsv (Intel) or nvenc (NVIDIA); hw_device is the
    # render device for vaapi/qsv ("" = the first /dev/dri/renderD*); encode_threads caps the CPU threads (0 = ffmpeg's choice)
    "encoder": "software", "hw_device": "", "encode_threads": 0,
    # pacing defaults; every provider can override these (Providers -> Downloads)
    "speed_limit_mbps": 0,      # 0 = unlimited
    "provider_concurrent": 1,   # downloads at once from ONE provider
    "pause_seconds": 0,         # rest between two downloads from the same provider
    "reconnect_slow": True,     # drop and resume a transfer that crawls, on a fresh connection
    # optional: only START downloads inside this local-time window (hours 0-23)
    "window_enabled": False, "window_start": 1, "window_end": 7,
}


def get_download_settings() -> dict:
    saved = _read_raw().get("downloads") or {}
    out = {**_DOWNLOADS_DEFAULTS, **{k: saved[k] for k in _DOWNLOADS_DEFAULTS if k in saved}}
    out["enabled"] = bool(out["enabled"])
    for key in ("output_dir", "movies_dir", "shows_dir", "work_dir"):
        out[key] = (out[key] or "").strip()
    out["audio_languages"] = (out["audio_languages"] or "").strip().lower()
    out["keep_subtitles"] = bool(out["keep_subtitles"])
    out["tmdb_id_in_folder"] = bool(out["tmdb_id_in_folder"])
    out["reconnect_slow"] = bool(out["reconnect_slow"])
    out["max_concurrent"] = min(4, max(1, int(out["max_concurrent"] or 1)))
    out["encoder"] = out["encoder"] if out["encoder"] in ("software", "auto", "vaapi", "qsv", "nvenc") else "software"
    out["hw_device"] = (out["hw_device"] or "").strip()
    out["encode_threads"] = min(64, max(0, int(out["encode_threads"] or 0)))
    out["min_free_gb"] = max(0, int(out["min_free_gb"] or 0))
    out["speed_limit_mbps"] = max(0, int(out["speed_limit_mbps"] or 0))
    out["provider_concurrent"] = min(4, max(1, int(out["provider_concurrent"] or 1)))
    out["pause_seconds"] = max(0, int(out["pause_seconds"] or 0))
    out["window_enabled"] = bool(out["window_enabled"])
    out["window_start"] = min(23, max(0, int(out["window_start"] or 0)))
    out["window_end"] = min(23, max(0, int(out["window_end"] or 0)))
    if out["default_preset"] not in DOWNLOAD_PRESETS:
        out["default_preset"] = "original"
    return out


def save_download_settings(settings: dict) -> None:
    clean = {k: settings[k] for k in _DOWNLOADS_DEFAULTS if k in settings}
    if "default_preset" in clean and clean["default_preset"] not in DOWNLOAD_PRESETS:
        raise ValueError("default_preset must be one of: " + ", ".join(DOWNLOAD_PRESETS))
    _update_raw(lambda data: data.__setitem__("downloads", {**(data.get("downloads") or {}), **clean}))


# library handoff -- finished downloads are imported by Radarr/Sonarr
# (see arr_handoff.py). Off until a URL + API key is saved for that app.
_HANDOFF_DEFAULTS = {
    "radarr_url": "", "sonarr_url": "",
    # where the finished file appears as vod-manager sees it (blank = the download folder),
    # and the same folder as Radarr/Sonarr sees it (blank = same path)
    "movies_finished_dir": "", "shows_finished_dir": "",
    "movies_arr_dir": "", "shows_arr_dir": "",
    # only used when vod-manager adds a missing title
    "radarr_root": "", "radarr_profile_id": 0, "sonarr_root": "", "sonarr_profile_id": 0,
    "new_show_monitor": "future", "new_movie_monitor": "unmonitored",
    "stall_hours": [4, 8, 16], "fail_hours": 24,
}
_HANDOFF_MONITORS = {"new_show_monitor": ("future", "none"), "new_movie_monitor": ("unmonitored", "monitored")}


def get_handoff_settings() -> dict:
    saved = _read_raw().get("handoff") or {}
    out = {**_HANDOFF_DEFAULTS, **{k: saved[k] for k in _HANDOFF_DEFAULTS if k in saved}}
    for key in ("radarr_url", "sonarr_url", "movies_finished_dir", "shows_finished_dir",
                "movies_arr_dir", "shows_arr_dir", "radarr_root", "sonarr_root"):
        out[key] = (out[key] or "").strip()
    for key in ("radarr_url", "sonarr_url"):
        out[key] = out[key].rstrip("/")
    for key, allowed in _HANDOFF_MONITORS.items():
        if out[key] not in allowed:
            out[key] = _HANDOFF_DEFAULTS[key]
    out["radarr_profile_id"] = int(out["radarr_profile_id"] or 0)
    out["sonarr_profile_id"] = int(out["sonarr_profile_id"] or 0)
    try:
        hours = [float(h) for h in out["stall_hours"]]
        fail = float(out["fail_hours"])
        if not (hours and all(a < b for a, b in zip([0] + hours, hours + [fail]))):
            raise ValueError
        out["stall_hours"] = [int(h) if h == int(h) else h for h in hours]
        out["fail_hours"] = int(fail) if fail == int(fail) else fail
    except (TypeError, ValueError):
        out["stall_hours"], out["fail_hours"] = list(_HANDOFF_DEFAULTS["stall_hours"]), _HANDOFF_DEFAULTS["fail_hours"]
    out["radarr_has_key"] = bool(saved.get("radarr_api_key"))
    out["sonarr_has_key"] = bool(saved.get("sonarr_api_key"))
    return out


def get_handoff_api_key(app: str) -> str | None:
    import secrets_util
    value = (_read_raw().get("handoff") or {}).get(f"{app}_api_key")
    return secrets_util.decrypt_value(value) if value else None


def handoff_enabled(kind: str) -> bool:
    """kind is a download kind: 'movie' (Radarr) or 'episode' (Sonarr)."""
    app = "radarr" if kind == "movie" else "sonarr"
    return bool(get_handoff_settings()[f"{app}_url"] and get_handoff_api_key(app))


def save_handoff_settings(settings: dict) -> None:
    """A blank API key keeps the saved one (it's never sent to the browser); '-' clears it."""
    import secrets_util
    clean = {k: settings[k] for k in _HANDOFF_DEFAULTS if k in settings}
    # Encrypt before taking the config lock: encrypt_value may itself write the key file via _update_raw.
    keys = {}
    for app in ("radarr", "sonarr"):
        key = (settings.get(f"{app}_api_key") or "").strip()
        keys[app] = None if key == "-" else secrets_util.encrypt_value(key) if key else ""

    def _mutate(data: dict) -> None:
        current = {**(data.get("handoff") or {}), **clean}
        for app, enc in keys.items():
            if enc is None:
                current.pop(f"{app}_api_key", None)
            elif enc:
                current[f"{app}_api_key"] = enc
        data["handoff"] = current
    _update_raw(_mutate)


# In Library badge -- the user's own media servers (see library_presence.py).
# A server is either a link to an existing Plex/Jellyfin/Emby provider (only the provider id
# is stored; its URL and token are read from the provider when used) or entered by hand.
LIBRARY_SERVER_TYPES = ("plex", "jellyfin", "emby")
_LIBRARY_SCAN_HOURS_DEFAULT = 6


def _library_refs(libraries) -> list[dict]:
    return [{"key": str(lib["key"]), "title": lib.get("title") or "", "kind": lib.get("kind") or ""}
            for lib in libraries or []]


def get_library_presence_settings() -> dict:
    """Provider-linked servers come back as {id, source, provider_id, libraries};
    library_presence.configured_servers() fills in the provider's details."""
    saved = _read_raw().get("library_presence") or {}
    servers = []
    for s in saved.get("servers") or []:
        if s.get("source") == "provider":
            servers.append({"id": s["id"], "source": "provider", "provider_id": int(s["provider_id"]),
                            "libraries": _library_refs(s.get("libraries"))})
        else:                                   # entries saved without a source were typed in
            servers.append({
                "id": s["id"], "source": "manual", "type": s["type"], "name": s.get("name") or s["type"].title(),
                "url": s.get("url") or "", "has_key": bool(s.get("api_key")),
                "libraries": _library_refs(s.get("libraries")),
            })
    try:
        hours = max(1, int(saved.get("scan_hours") or _LIBRARY_SCAN_HOURS_DEFAULT))
    except (TypeError, ValueError):
        hours = _LIBRARY_SCAN_HOURS_DEFAULT
    return {"servers": servers, "scan_hours": hours}


def get_library_server_key(server_id: str) -> str | None:
    """A manual server's key (a provider-linked server uses the provider's)."""
    import secrets_util
    for s in (_read_raw().get("library_presence") or {}).get("servers") or []:
        if s["id"] == server_id and s.get("api_key"):
            return secrets_util.decrypt_value(s["api_key"])
    return None


def save_library_presence_settings(settings: dict) -> None:
    """Replaces the server list. A manual server without an id is new; a blank key
    keeps the saved one for that id (keys are never sent to the browser); '-' clears
    it. A provider-linked server is keyed provider:<id>, so its scanned rows stay
    with it if it's unticked and ticked again."""
    import secrets_util
    import uuid
    servers, linked = [], set()
    for s in settings.get("servers") or []:
        if s.get("source") == "provider":
            pid = int(s["provider_id"])
            if pid not in linked:
                linked.add(pid)
                servers.append({"id": f"provider:{pid}", "source": "provider", "provider_id": pid,
                                "libraries": _library_refs(s.get("libraries")), "_key": None})
            continue
        stype = (s.get("type") or "").strip().lower()
        if stype not in LIBRARY_SERVER_TYPES:
            raise ValueError("Server type must be one of: " + ", ".join(LIBRARY_SERVER_TYPES))
        key = (s.get("api_key") or "").strip()
        servers.append({
            "id": s.get("id") or uuid.uuid4().hex[:8], "source": "manual", "type": stype,
            "name": (s.get("name") or "").strip() or stype.title(),
            "url": (s.get("url") or "").strip().rstrip("/"),
            "libraries": _library_refs(s.get("libraries")),
            # Encrypt before taking the config lock: encrypt_value may itself write the key file via _update_raw.
            "_key": None if key == "-" else secrets_util.encrypt_value(key) if key else "",
        })
    try:
        raw = settings.get("scan_hours")
        hours = max(1, int(_LIBRARY_SCAN_HOURS_DEFAULT if raw in (None, "") else raw))
    except (TypeError, ValueError):
        hours = _LIBRARY_SCAN_HOURS_DEFAULT

    def _mutate(data: dict) -> None:
        old = {s["id"]: s for s in (data.get("library_presence") or {}).get("servers") or []}
        out = []
        for s in servers:
            enc = s.pop("_key")
            if enc:
                s["api_key"] = enc
            elif enc == "" and old.get(s["id"], {}).get("api_key"):
                s["api_key"] = old[s["id"]]["api_key"]
            out.append(s)
        data["library_presence"] = {**(data.get("library_presence") or {}), "servers": out, "scan_hours": hours}
    _update_raw(_mutate)


_AUTO_EPISODE_TITLE_MODES = ("off", "applied", "all")


def get_auto_episode_titles_mode() -> str:
    mode = _read_raw().get("auto_episode_titles")
    return mode if mode in _AUTO_EPISODE_TITLE_MODES else "applied"


def save_auto_episode_titles_mode(mode: str) -> None:
    if mode not in _AUTO_EPISODE_TITLE_MODES:
        raise ValueError("mode must be one of: " + ", ".join(_AUTO_EPISODE_TITLE_MODES))
    _update_raw(lambda data: data.__setitem__("auto_episode_titles", mode))


def get_xc_title_include_year() -> bool:
    return bool(_read_raw().get("xc_title_include_year", False))


def save_xc_title_include_year(enabled: bool) -> None:
    _update_raw(lambda data: data.__setitem__("xc_title_include_year", bool(enabled)))


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
    _update_raw(lambda data: data.__setitem__("duplicate_finder_quality_prefix_matching", bool(enabled)))


def get_duplicate_finder_auto_merge_tmdb() -> bool:
    return bool(_read_raw().get("duplicate_finder_auto_merge_tmdb", True))


def save_duplicate_finder_auto_merge_tmdb(enabled: bool) -> None:
    _update_raw(lambda data: data.__setitem__("duplicate_finder_auto_merge_tmdb", bool(enabled)))


# ── AI provider selection ────────────────────────────────────────────────────
# ai_assist.py can talk to any of these three -- a user might already have a
# key for one and not another, or want to compare quality/cost, so the key
# for each is stored independently of which one is actually "active".

AI_PROVIDERS = ("anthropic", "openai", "gemini")

_AI_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-5-mini",
    # "-latest" alias: Google retires specific models quickly (2.5 Flash/Pro now
    # answer 404 "no longer available to new users" on newer accounts), the alias
    # always points at a current one.
    "gemini": "gemini-flash-latest",
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
    def _mutate(data: dict) -> None:
        data["ai_provider"] = provider
        data["ai_model"] = model.strip() if model and model.strip() else _AI_DEFAULT_MODELS[provider]
    _update_raw(_mutate)


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
    def _mutate(data: dict) -> None:
        data["import_exclude_language_prefixes"] = [p.strip().upper() for p in exclude_prefixes if p.strip()]
        data["import_exclude_non_latin"] = bool(exclude_non_latin)
    _update_raw(_mutate)


def get_import_country_exclusion() -> list[str]:
    """Global (not per-provider) sibling to get_import_language_exclusion
    above, same reasoning -- keyed on a title's trailing "(<country code>)"
    tag (vod_db._country_suffix_code) instead of its leading language
    prefix. A separate provider convention from the language prefix (e.g.
    "Married at First Sight (NZ)" vs "EN| Married at First Sight"), so this
    is its own setting rather than folded into the language one. See
    vod_importer._should_auto_archive."""
    data = _read_raw()
    return [c.strip().upper() for c in (data.get("import_exclude_country_codes") or []) if c.strip()]


def save_import_country_exclusion(exclude_country_codes: list[str]) -> None:
    _update_raw(lambda data: data.__setitem__(
        "import_exclude_country_codes", [c.strip().upper() for c in exclude_country_codes if c.strip()]))


def get_enabled_languages() -> list[str]:
    """Which already-imported sources are eligible for playback/export/
    failover (vod_db._enabled_languages_clause), independent of
    get_import_language_exclusion above -- that one gates what gets
    imported in the first place and only catches titles whose raw_name
    carries a recognizable prefix, so it can't retroactively hide
    foreign-tagged rows already sitting in the pool or ones tagged only by
    category name. This is the backstop: a live query-time filter over
    the `language` column already computed on every source row. Defaults
    to English + Spanish, matching the original hardcoded behavior."""
    data = _read_raw()
    codes = data.get("enabled_playback_languages")
    if not codes:
        return ["EN", "ES"]
    return [c.strip().upper() for c in codes if c.strip()]


def save_enabled_languages(codes: list[str]) -> None:
    _update_raw(lambda data: data.__setitem__(
        "enabled_playback_languages", [c.strip().upper() for c in codes if c.strip()]))


def get_default_categories_prompt_dismissed() -> bool:
    """Whether the admin has already answered the one-time "include 18+ in
    the built-in All Movies/All TV Shows categories?" prompt (see
    vod_db._seed_default_categories) -- distinct from whether they changed
    the default, since leaving it on the safe default is a valid answer
    too and shouldn't keep re-prompting."""
    return bool(_read_raw().get("default_categories_prompt_dismissed"))


def set_default_categories_prompt_dismissed() -> None:
    _update_raw(lambda data: data.__setitem__("default_categories_prompt_dismissed", True))


def get_hide_dvr_tab() -> bool:
    """A deployment-wide display preference, not a per-browser one (unlike
    the active-tab/theme choices App.tsx keeps in localStorage) -- whether
    DVR is used at all is a decision about this VOD Manager instance, not
    about who's currently looking at it. Defaults to False (shown) so
    existing installs see no change until an admin opts in."""
    return bool(_read_raw().get("hide_dvr_tab"))


def set_hide_dvr_tab(hidden: bool) -> None:
    _update_raw(lambda data: data.__setitem__("hide_dvr_tab", hidden))


def get_openai_api_key() -> str | None:
    data = _read_raw()
    return data.get("openai_api_key") or None


def save_openai_api_key(api_key: str) -> None:
    _update_raw(lambda data: data.__setitem__("openai_api_key", api_key))


def get_gemini_api_key() -> str | None:
    data = _read_raw()
    return data.get("gemini_api_key") or None


def save_gemini_api_key(api_key: str) -> None:
    _update_raw(lambda data: data.__setitem__("gemini_api_key", api_key))


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
    _update_raw(lambda data: data.update({
        "lockout_max_attempts":     max(1, int(max_attempts)),
        "lockout_window_seconds":   max(1, int(window_seconds)),
        "lockout_duration_seconds": max(1, int(duration_seconds)),
    }))


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
    _update_raw(lambda data: data.update({
        "catalog_refresh_seconds_xc":       max(60, int(catalog_refresh_seconds_xc)),
        "catalog_refresh_seconds_plex":     max(60, int(catalog_refresh_seconds_plex)),
        "catalog_refresh_seconds_emby":     max(60, int(catalog_refresh_seconds_emby)),
        "catalog_refresh_seconds_jellyfin": max(60, int(catalog_refresh_seconds_jellyfin)),
        "enrichment_ttl_seconds":           max(60, int(enrichment_ttl_seconds)),
        "tmdb_sync_interval_seconds":       max(60, int(tmdb_sync_interval_seconds)) if tmdb_sync_interval_seconds else None,
    }))


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
    _update_raw(lambda data: data.update(
        {"auth_username": username, "auth_salt": salt, "auth_hash": hashed, "auth_scheme": "pbkdf2"}))


# Local TMDB library (tmdb_store/tmdb_fill) -- opt-in: background fill only
# runs once a TMDB API key is saved AND the library is turned on.
_TMDB_STORE_DEFAULTS = {
    "enabled": False,
    "burst_size": 2000,
    "bursts_per_day": 4,
    "daily_budget": 20000,
    "concurrency": 4,
    "auto_match_batch": 2000,  # no-ID titles auto-matched per burst; 0 = off
    "popular_keep": 25000,  # most popular TMDB titles kept per type; 0 = catalog only
}


def get_tmdb_store_settings() -> dict:
    stored = _read_raw().get("tmdb_store") or {}
    return {k: stored.get(k, v) for k, v in _TMDB_STORE_DEFAULTS.items()}


def save_tmdb_store_settings(updates: dict) -> dict:
    def mutate(data: dict) -> None:
        current = {k: v for k, v in (data.get("tmdb_store") or {}).items() if k in _TMDB_STORE_DEFAULTS}
        for key, value in updates.items():
            if key not in _TMDB_STORE_DEFAULTS or value is None:
                continue
            current[key] = bool(value) if key == "enabled" else max(0, int(value))
        data["tmdb_store"] = current

    _update_raw(mutate)
    return get_tmdb_store_settings()

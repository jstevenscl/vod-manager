import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import verify_session
from config import (
    get_ai_model,
    get_ai_provider,
    get_anthropic_api_key,
    get_default_categories_prompt_dismissed,
    get_gemini_api_key,
    get_hide_dvr_tab,
    get_import_language_exclusion,
    get_lockout_settings,
    get_openai_api_key,
    get_refresh_settings,
    get_smtp_settings,
    get_stream_priority_mode,
    get_tmdb_api_key,
    has_credentials,
    save_ai_provider,
    save_anthropic_api_key,
    save_gemini_api_key,
    save_import_language_exclusion,
    save_lockout_settings,
    save_openai_api_key,
    save_refresh_settings,
    save_smtp_settings,
    save_stream_priority_mode,
    save_tmdb_api_key,
    set_default_categories_prompt_dismissed,
    set_hide_dvr_tab,
)
from routes import require_auth
from secrets_util import hash_password, looks_like_fernet_token
import ai_assist
import apply_exclusions_job
import dispatcharr_dvr_client
import dispatcharr_dvr_importer
import duplicate_confirm
import emby_vod_importer
import plex_importer
import portal_auth
import tmdb_sync
import vod_db
import vod_importer
import vod_sync
from xc_server import fetch_proxied_image, get_active_sessions, kill_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/vod", tags=["vod-manager"])

_GUARDS = [Depends(require_auth)]

vod_db.init_db()


# ── Request models ──────────────────────────────────────────────────────────

class TmdbApiKeyRequest(BaseModel):
    api_key: str


class SmtpSettingsRequest(BaseModel):
    host: Optional[str] = None
    port: int = 587
    username: Optional[str] = None
    password: Optional[str] = None
    from_address: Optional[str] = None
    use_tls: bool = True
    admin_recipients: list[str] = []


class AiProviderRequest(BaseModel):
    provider: str
    model: Optional[str] = None


class AiApiKeyRequest(BaseModel):
    provider: str
    api_key: str


class DefaultCategoriesAdultRequest(BaseModel):
    include_adult: bool


class HideDvrTabRequest(BaseModel):
    hidden: bool


class ImportLanguageExclusionRequest(BaseModel):
    exclude_prefixes: list[str] = []
    exclude_non_latin: bool = False


class ProviderImportExcludeCategoriesRequest(BaseModel):
    category_names: list[str] = []
    exclude_uncategorized: bool = False


class SuggestCategoryRuleRequest(BaseModel):
    description: str
    content_type: str


class AiEvaluateCategoryRequest(BaseModel):
    description: str
    prefilter_rule_json: Optional[str] = None
    limit: int = 300


class LockoutSettingsRequest(BaseModel):
    lockout_max_attempts: int
    lockout_window_seconds: int
    lockout_duration_seconds: int


class RefreshSettingsRequest(BaseModel):
    catalog_refresh_seconds_xc: int
    catalog_refresh_seconds_plex: int
    catalog_refresh_seconds_emby: int
    catalog_refresh_seconds_jellyfin: int
    enrichment_ttl_seconds: int
    tmdb_sync_interval_seconds: Optional[int] = None


class XcClientRequest(BaseModel):
    label: str
    ip_allowlist: Optional[str] = None


class XcClientUpdateRequest(BaseModel):
    label: Optional[str] = None
    enabled: Optional[bool] = None
    ip_allowlist: Optional[str] = None
    clear_ip_allowlist: bool = False
    category_allowlist: Optional[str] = None
    clear_category_allowlist: bool = False


class MetadataRuleRequest(BaseModel):
    content_type: str  # 'movie', 'series', or 'both'
    field: str
    pattern: str
    replacement: str = ""
    sort_order: int = 0
    is_regex: bool = False  # literal-text matching by default -- see vod_db's is_regex migration comment


class MetadataRulePreviewRequest(BaseModel):
    content_type: str
    field: str
    pattern: str
    replacement: str = ""
    is_regex: bool = False


class ProviderRequest(BaseModel):
    name: str
    base_url: str
    username: str
    password: str
    max_streams: int = 0
    priority: int = 0
    provider_type: str = "xc"


class EnableDvrRequest(BaseModel):
    dvr_local_path: Optional[str] = None
    dvr_movie_category_id: Optional[int] = None
    dvr_series_category_id: Optional[int] = None
    dvr_remote_recordings_root: Optional[str] = None
    priority: int = 0
    dvr_delete_after_copy: bool = False


class RecordingProfileRequest(BaseModel):
    provider_id: int
    label: str
    title: str
    tvg_id: Optional[str] = None
    title_mode: str = "exact"
    description: Optional[str] = None
    description_mode: str = "contains"
    mode: str = "all"
    channel_id: Optional[int] = None
    target_movie_category_id: Optional[int] = None
    target_series_category_id: Optional[int] = None
    dispatcharr_user_id: Optional[int] = None
    backfill_mode: Optional[str] = None


class RecordingProfilePreviewRequest(BaseModel):
    provider_id: int
    title: str
    channel_id: Optional[int] = None
    tvg_id: Optional[str] = None
    title_mode: str = "exact"
    description: Optional[str] = None
    description_mode: str = "contains"


class MissingEpisodeResolveRequest(BaseModel):
    provider_id: int
    season_number: int
    episode_number: int
    episode_name: Optional[str] = None


class MissingEpisodeScheduleRequest(BaseModel):
    provider_id: int
    channel_id: int
    program: dict


class DvrUserLimitRequest(BaseModel):
    provider_id: int
    dispatcharr_user_id: int
    dispatcharr_username: str
    stream_reserve: int = 0
    disk_quota_bytes: Optional[int] = None
    retention_max_age_days: Optional[int] = None
    retention_max_episodes_per_show: Optional[int] = None
    default_movie_category_id: Optional[int] = None
    default_series_category_id: Optional[int] = None
    quota_policy: str = "hard_fail"


class DvrUserLimitUpdateRequest(BaseModel):
    stream_reserve: int = 0
    disk_quota_bytes: Optional[int] = None
    retention_max_age_days: Optional[int] = None
    retention_max_episodes_per_show: Optional[int] = None
    default_movie_category_id: Optional[int] = None
    default_series_category_id: Optional[int] = None
    quota_policy: Optional[str] = None


class ApplyRetentionRequest(BaseModel):
    movies: list[dict] = []
    episodes: list[dict] = []


class PortalAccountRequest(BaseModel):
    provider_id: int
    dispatcharr_user_id: int
    username: str
    password: str
    email: Optional[str] = None


class PortalAccountPasswordRequest(BaseModel):
    password: str


class PortalAccountEmailRequest(BaseModel):
    email: Optional[str] = None


class DispatcharrConnectionRequest(BaseModel):
    label: str
    url: str
    token: str


class ConnectDispatcharrInstanceRequest(BaseModel):
    label: str
    url: str
    token: str
    vod_manager_public_url: str


class DispatcharrConnectionUpdateRequest(BaseModel):
    label: Optional[str] = None
    url: Optional[str] = None
    token: Optional[str] = None
    vod_relay_account_id: Optional[int] = None
    clear_vod_relay_account_id: bool = False


class ProviderLiveAccountRequest(BaseModel):
    dispatcharr_connection_id: int
    dispatcharr_account_id: int
    dispatcharr_profile_id: Optional[int] = None


class DiscoveredProfileKey(BaseModel):
    dispatcharr_account_id: int
    dispatcharr_profile_id: int


class ImportDiscoveredProfilesRequest(BaseModel):
    profiles: list[DiscoveredProfileKey]


class CategoryRequest(BaseModel):
    name: str
    content_type: str  # 'movie' or 'series'
    is_smart: bool = False
    sort_order: int = 0
    rule_json: Optional[str] = None
    sync_source: Optional[str] = None


class ResolveYearReviewRequest(BaseModel):
    year: int
    tmdb_id: Optional[str] = None


class ResolveMissingArtworkRequest(BaseModel):
    poster_url: str
    tmdb_id: Optional[str] = None
    name: Optional[str] = None
    year: Optional[int] = None


class MergeDuplicateGroupRequest(BaseModel):
    content_type: str
    keep_id: int
    merge_ids: list[int]


class IgnoreDuplicateGroupRequest(BaseModel):
    content_type: str
    item_ids: list[int]


class MergeDuplicateGroupPair(BaseModel):
    keep_id: int
    merge_ids: list[int]


class RenameRequest(BaseModel):
    name: str
    year: Optional[int] = None


class BulkMissingArtworkPosterRequest(BaseModel):
    content_type: str
    poster_url: str
    ids: Optional[list[int]] = None
    search: Optional[str] = None
    excluded: bool = False
    script: Optional[str] = None
    prefixes: Optional[str] = None


class BulkMissingArtworkExcludeRequest(BaseModel):
    content_type: str
    set_excluded: bool
    ids: Optional[list[int]] = None
    search: Optional[str] = None
    excluded: bool = False
    script: Optional[str] = None
    prefixes: Optional[str] = None
    keep_codes: Optional[str] = None
    dry_run: bool = False


class BulkLibraryExcludeRequest(BaseModel):
    content_type: str
    set_excluded: bool
    ids: Optional[list[int]] = None
    search: Optional[str] = None
    excluded: Optional[bool] = None
    script: Optional[str] = None
    prefixes: Optional[str] = None
    keep_codes: Optional[str] = None
    dry_run: bool = False


class MovieRequest(BaseModel):
    name: str
    year: Optional[int] = None
    tmdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    genre: Optional[str] = None
    description: Optional[str] = None
    duration_secs: Optional[int] = None
    poster_url: Optional[str] = None


class MovieSourceRequest(BaseModel):
    provider_id: int
    provider_stream_id: str
    container_extension: str = "mp4"


class MoveMovieSourceRequest(BaseModel):
    target_movie_id: int


class MoveEpisodeSourceRequest(BaseModel):
    target_series_id: int
    season_number: int
    episode_number: int
    name: str


class PlacementRequest(BaseModel):
    category_id: int


class BulkPlaceRequest(BaseModel):
    category_id: int
    ids: Optional[list[int]] = None
    search: Optional[str] = None
    source_category_id: Optional[int] = None
    source_provider_id: Optional[int] = None


class BulkArchiveRequest(BaseModel):
    content_type: str  # 'movie' or 'series'
    ids: list[int]
    archived: bool


class SeriesRequest(BaseModel):
    name: str
    year: Optional[int] = None
    tmdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    genre: Optional[str] = None
    description: Optional[str] = None
    poster_url: Optional[str] = None


class EpisodeRequest(BaseModel):
    season_number: int
    episode_number: int
    name: str
    description: Optional[str] = None
    duration_secs: Optional[int] = None


class EpisodeSourceRequest(BaseModel):
    provider_id: int
    provider_stream_id: str
    container_extension: str = "mp4"


@router.get("/xc-credentials/", dependencies=_GUARDS)
async def get_xc_credentials():
    """A representative valid XC credential pair, used to build in-app
    preview/copy-URL links — any enabled client's credentials work
    identically for that purpose since they all see the same pool. Not tied
    to any particular downstream Dispatcharr instance; see /clients/ for
    per-instance credential management."""
    client = vod_db.get_default_xc_client()
    if client is None:
        raise HTTPException(503, detail="no XC clients configured yet — add one under Connected Instances")
    return {"username": client["username"], "password": client["password"]}


# ── XC clients (per-instance credentials) ───────────────────────────────────

def _client_out(c: dict) -> dict:
    return {
        "id": c["id"],
        "label": c["label"],
        "username": c["username"],
        "password": c["password"],
        "enabled": bool(c["enabled"]),
        "ip_allowlist": c["ip_allowlist"],
        "category_allowlist": c.get("category_allowlist"),
        "created_at": c["created_at"],
        "last_seen_at": c["last_seen_at"],
        "last_seen_ip": c["last_seen_ip"],
    }


@router.get("/clients/", dependencies=_GUARDS)
async def list_xc_clients():
    return [_client_out(c) for c in vod_db.list_xc_clients()]


@router.post("/clients/", dependencies=_GUARDS)
async def create_xc_client(body: XcClientRequest):
    label = body.label.strip()
    if not label:
        raise HTTPException(400, detail="label is required")
    client = vod_db.create_xc_client(label, body.ip_allowlist)
    return _client_out(client)


@router.patch("/clients/{client_id}/", dependencies=_GUARDS)
async def update_xc_client(client_id: int, body: XcClientUpdateRequest):
    if not vod_db.get_xc_client(client_id):
        raise HTTPException(404, detail="client not found")
    vod_db.update_xc_client(
        client_id,
        label=body.label.strip() if body.label is not None else None,
        enabled=body.enabled,
        ip_allowlist=body.ip_allowlist,
        clear_ip_allowlist=body.clear_ip_allowlist,
        category_allowlist=body.category_allowlist,
        clear_category_allowlist=body.clear_category_allowlist,
    )
    return _client_out(vod_db.get_xc_client(client_id))


@router.post("/clients/{client_id}/regenerate/", dependencies=_GUARDS)
async def regenerate_xc_client(client_id: int):
    if not vod_db.get_xc_client(client_id):
        raise HTTPException(404, detail="client not found")
    return _client_out(vod_db.regenerate_xc_client_secret(client_id))


@router.delete("/clients/{client_id}/", dependencies=_GUARDS)
async def delete_xc_client(client_id: int):
    if not vod_db.get_xc_client(client_id):
        raise HTTPException(404, detail="client not found")
    vod_db.delete_xc_client(client_id)
    return {"ok": True}


# ── Dispatcharr connections ─────────────────────────────────────────────────
# Who VOD Manager itself reaches out to -- the other side of xc_clients
# above (who's allowed to pull from VOD Manager). See vod_db.py's comment
# on the dispatcharr_connections table for what each is used for.

def _redact_connection(c: dict) -> dict:
    # Mirrors _redact_provider below -- this is a real bearer credential for
    # an external system's API, not something the browser needs sitting in
    # an already-fetched query cache. The frontend's "reveal" button fetches
    # the real value on demand via the dedicated route below instead.
    c = dict(c)
    c["has_token"] = bool(c.pop("token", None))
    return c


@router.get("/dispatcharr-connections/", dependencies=_GUARDS)
async def list_dispatcharr_connections():
    return [_redact_connection(c) for c in vod_db.list_dispatcharr_connections()]


@router.get("/dispatcharr-connections/{connection_id}/token/", dependencies=_GUARDS)
async def reveal_dispatcharr_connection_token(connection_id: int):
    connection = vod_db.get_dispatcharr_connection(connection_id)
    if not connection:
        raise HTTPException(404, detail="connection not found")
    return {"token": connection["token"]}


@router.post("/dispatcharr-connections/", dependencies=_GUARDS)
async def create_dispatcharr_connection(body: DispatcharrConnectionRequest):
    label = body.label.strip()
    url = body.url.strip()
    token = body.token.strip()
    if not label or not url or not token:
        raise HTTPException(400, detail="label, url, and token are all required")
    connection_id = vod_db.create_dispatcharr_connection(label, url, token)
    return _redact_connection(vod_db.get_dispatcharr_connection(connection_id))


@router.post("/dispatcharr-connections/connect/", dependencies=_GUARDS)
async def connect_dispatcharr_instance(body: ConnectDispatcharrInstanceRequest):
    """Automated one-shot setup: creates the XC client + Dispatcharr-side
    M3U account + saved connection in one step, instead of doing all three
    by hand. See vod_sync.connect_dispatcharr_instance."""
    label = body.label.strip()
    url = body.url.strip()
    token = body.token.strip()
    public_url = body.vod_manager_public_url.strip()
    if not label or not url or not token or not public_url:
        raise HTTPException(400, detail="label, url, token, and vod_manager_public_url are all required")
    try:
        result = await vod_sync.connect_dispatcharr_instance(label, url, token, public_url)
    except Exception as exc:
        raise HTTPException(502, detail=f"Failed to connect: {exc}")
    result["connection"] = _redact_connection(result["connection"])
    return result


@router.patch("/dispatcharr-connections/{connection_id}/", dependencies=_GUARDS)
async def update_dispatcharr_connection(connection_id: int, body: DispatcharrConnectionUpdateRequest):
    if not vod_db.get_dispatcharr_connection(connection_id):
        raise HTTPException(404, detail="connection not found")
    vod_db.update_dispatcharr_connection(
        connection_id,
        label=body.label.strip() if body.label is not None else None,
        url=body.url.strip() if body.url is not None else None,
        token=body.token.strip() if body.token is not None else None,
        vod_relay_account_id=body.vod_relay_account_id,
        clear_vod_relay_account_id=body.clear_vod_relay_account_id,
    )
    return _redact_connection(vod_db.get_dispatcharr_connection(connection_id))


@router.delete("/dispatcharr-connections/{connection_id}/", dependencies=_GUARDS)
async def delete_dispatcharr_connection(connection_id: int):
    if not vod_db.get_dispatcharr_connection(connection_id):
        raise HTTPException(404, detail="connection not found")
    if vod_db.get_dvr_provider_for_connection(connection_id):
        raise HTTPException(409, detail="DVR is still enabled for this connection -- disable it first")
    vod_db.delete_dispatcharr_connection(connection_id)
    return {"ok": True}


@router.post("/dispatcharr-connections/{connection_id}/dvr/", dependencies=_GUARDS)
async def enable_dvr_for_connection(connection_id: int, body: EnableDvrRequest):
    """The only way a dispatcharr_dvr providers row ever gets created --
    see vod_db.enable_dvr_for_connection's docstring. Safe to call again
    with different settings; it edits the existing row rather than erroring,
    so the admin UI's enable form and edit form are the same submit action."""
    try:
        provider_id = vod_db.enable_dvr_for_connection(
            connection_id, body.dvr_local_path, body.dvr_movie_category_id, body.dvr_series_category_id,
            body.dvr_remote_recordings_root, body.priority, body.dvr_delete_after_copy,
        )
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))
    return vod_db.get_provider(provider_id)


@router.delete("/dispatcharr-connections/{connection_id}/dvr/", dependencies=_GUARDS)
async def disable_dvr_for_connection(connection_id: int):
    vod_db.disable_dvr_for_connection(connection_id)
    return {"ok": True}


@router.get("/activity/", dependencies=_GUARDS)
async def list_activity():
    """Currently open VOD stream relays — in-memory only, cleared on
    restart, same as the underlying session tracking in xc_server.py."""
    return get_active_sessions()


@router.post("/activity/{conn_id}/kill/", dependencies=_GUARDS)
async def kill_activity(conn_id: str):
    """Force-closes a stuck/rogue relay -- a closed player doesn't always
    tear down the underlying connection promptly (confirmed live: a closed
    preview kept relaying real bytes from the upstream provider afterward),
    and disconnect detection alone isn't a substitute for a manual escape
    hatch."""
    if not kill_session(conn_id):
        raise HTTPException(404, detail="session not found (it may have already closed)")
    return {"ok": True}


@router.get("/stream-failures/", dependencies=_GUARDS)
async def list_stream_failures():
    """Recent failed VOD playback attempts -- every source exhausted, or a
    mid-stream relay crash. Persisted (unlike Activity above), so these
    survive a restart and don't require watching logs live to notice."""
    return vod_db.list_stream_failures()


@router.delete("/stream-failures/{failure_id}/", dependencies=_GUARDS)
async def dismiss_stream_failure(failure_id: int):
    vod_db.delete_stream_failure(failure_id)
    return {"ok": True}


@router.delete("/stream-failures/", dependencies=_GUARDS)
async def clear_stream_failures():
    vod_db.clear_stream_failures()
    return {"ok": True}


# Deliberately NOT under dependencies=_GUARDS (the X-Session-Token HEADER
# guard) -- a plain <img src> can't attach a custom header, so this takes
# the session token as a `?token=` query param instead, same pattern as
# portal_routes.py's /library/{kind}/{item_id}/stream/. See
# xc_server.fetch_proxied_image's docstring for why this route exists at
# all (poster_url is plain http://, which an HTTPS-fronted deployment's
# browser hard-blocks as mixed content).
@router.get("/image-proxy")
async def image_proxy(url: str, token: str):
    if has_credentials() and not verify_session(token):
        raise HTTPException(401, detail="unauthorized")
    return await fetch_proxied_image(url)


@router.get("/tmdb-settings/", dependencies=_GUARDS)
async def get_tmdb_settings():
    return {"has_api_key": bool(get_tmdb_api_key())}


@router.post("/tmdb-settings/", dependencies=_GUARDS)
async def save_tmdb_settings(body: TmdbApiKeyRequest):
    save_tmdb_api_key(body.api_key)
    return {"ok": True}


@router.get("/smtp-settings/", dependencies=_GUARDS)
async def get_smtp_settings_route():
    """The real password is never sent back to the browser (same pattern
    as tmdb-settings above) -- has_password tells the settings form
    whether one's already configured, so it can show a "leave blank to
    keep the current password" placeholder instead of an empty required
    field. Powers notifications.notify_quota_threshold's admin recipient
    list, see the user's 'Both' call, 2026-07-28."""
    settings = get_smtp_settings()
    return {
        "host": settings["host"], "port": settings["port"], "username": settings["username"],
        "has_password": bool(settings["password"]), "from_address": settings["from_address"],
        "use_tls": settings["use_tls"], "admin_recipients": settings["admin_recipients"],
    }


@router.post("/smtp-settings/", dependencies=_GUARDS)
async def save_smtp_settings_route(body: SmtpSettingsRequest):
    save_smtp_settings(
        body.host, body.port, body.username, body.password,
        body.from_address, body.use_tls, body.admin_recipients,
    )
    return {"ok": True}


@router.get("/stream-priority-mode/", dependencies=_GUARDS)
async def get_stream_priority_mode_route():
    return {"mode": get_stream_priority_mode()}


class StreamPriorityModeRequest(BaseModel):
    mode: str


@router.post("/stream-priority-mode/", dependencies=_GUARDS)
async def save_stream_priority_mode_route(body: StreamPriorityModeRequest):
    try:
        save_stream_priority_mode(body.mode)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return {"ok": True}


@router.get("/default-categories-prompt/", dependencies=_GUARDS)
async def get_default_categories_prompt():
    # Only relevant if seeding actually created a catch-all category --
    # on an upgrade where categories already existed, seeding is a correct
    # no-op (see vod_db._seed_default_categories), and without this check
    # the prompt would still fire for a category that was never created.
    if get_default_categories_prompt_dismissed():
        return {"show": False}
    has_catchall = bool(await asyncio.to_thread(vod_db.list_catchall_category_ids))
    return {"show": has_catchall}


@router.get("/hide-dvr-tab/", dependencies=_GUARDS)
async def get_hide_dvr_tab_setting():
    return {"hidden": get_hide_dvr_tab()}


@router.post("/hide-dvr-tab/", dependencies=_GUARDS)
async def set_hide_dvr_tab_setting(body: HideDvrTabRequest):
    set_hide_dvr_tab(body.hidden)
    return {"ok": True}


@router.post("/default-categories-prompt/", dependencies=_GUARDS)
async def answer_default_categories_prompt(body: DefaultCategoriesAdultRequest):
    results = await asyncio.to_thread(vod_db.set_catchall_include_adult, body.include_adult)
    set_default_categories_prompt_dismissed()
    return {"ok": True, "results": results}


@router.get("/import-language-exclusion/", dependencies=_GUARDS)
async def get_import_language_exclusion_settings():
    return get_import_language_exclusion()


@router.post("/import-language-exclusion/", dependencies=_GUARDS)
async def save_import_language_exclusion_settings(body: ImportLanguageExclusionRequest):
    save_import_language_exclusion(body.exclude_prefixes, body.exclude_non_latin)
    return {"ok": True}


@router.get("/import-language-exclusion/prefixes/", dependencies=_GUARDS)
async def list_import_language_exclusion_prefixes():
    return vod_db.list_all_pool_prefixes()


@router.post("/import-exclusions/apply-now/", dependencies=_GUARDS)
async def apply_import_exclusions_now():
    """Retroactively applies the current global language rules and every
    provider's own category rules across the WHOLE existing pool, not just
    future imports -- for someone turning this on after already having a
    large catalog. Just re-runs the normal import for every active
    provider: bulk_import_movies/series already apply the archive-upgrade
    check on already-existing matched rows (see vod_db.bulk_import_movies),
    so this reuses that exact logic instead of a separate bespoke purge
    path, and correctly picks up series category names too (which aren't
    persisted anywhere, only known live from the provider at import time).
    Runs as a background job (see apply_exclusions_job.py) since a real
    catalog re-import across every provider can take minutes -- returns
    immediately with a job id the frontend polls for progress."""
    job_id = apply_exclusions_job.start_job()
    return {"job_id": job_id}


@router.get("/import-exclusions/apply-now/{job_id}/", dependencies=_GUARDS)
async def get_apply_import_exclusions_status(job_id: str):
    job = apply_exclusions_job.get_job(job_id)
    if not job:
        raise HTTPException(404, detail="job not found")
    return {
        "status": job["status"], "total": job["total"], "completed": job["completed"],
        "current_provider": job["current_provider"], "results": job["results"], "error": job["error"],
    }


@router.get("/ai-settings/", dependencies=_GUARDS)
async def get_ai_settings():
    return {
        "provider": get_ai_provider(),
        "model": get_ai_model(),
        "has_anthropic_key": bool(get_anthropic_api_key()),
        "has_openai_key": bool(get_openai_api_key()),
        "has_gemini_key": bool(get_gemini_api_key()),
    }


@router.post("/ai-settings/", dependencies=_GUARDS)
async def save_ai_settings(body: AiProviderRequest):
    save_ai_provider(body.provider, body.model)
    return {"ok": True}


@router.post("/ai-settings/key/", dependencies=_GUARDS)
async def save_ai_key(body: AiApiKeyRequest):
    if body.provider == "anthropic":
        save_anthropic_api_key(body.api_key)
    elif body.provider == "openai":
        save_openai_api_key(body.api_key)
    elif body.provider == "gemini":
        save_gemini_api_key(body.api_key)
    else:
        raise HTTPException(400, detail=f"unknown AI provider '{body.provider}'")
    return {"ok": True}


@router.post("/ai/suggest-category-rule/", dependencies=_GUARDS)
async def suggest_category_rule(body: SuggestCategoryRuleRequest):
    if body.content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    try:
        return await ai_assist.suggest_category_rule(body.description, body.content_type)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.warning("[vod_routes] AI category rule suggestion failed: %s", exc)
        raise HTTPException(502, detail=f"AI request failed: {exc}")


@router.post("/categories/{category_id}/ai-evaluate/", dependencies=_GUARDS)
async def ai_evaluate_category(category_id: int, body: AiEvaluateCategoryRequest):
    category = vod_db.get_category(category_id)
    if not category:
        raise HTTPException(404, detail="category not found")

    limit = max(1, min(body.limit, 2000))  # hard ceiling -- real per-item AI cost, never unbounded
    candidates, total_before_cap = vod_db.get_ai_candidate_rows(category["content_type"], body.prefilter_rule_json, limit)

    try:
        matched_ids = await ai_assist.evaluate_candidates_for_category(body.description, category["content_type"], candidates)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.warning("[vod_routes] AI category evaluation failed: %s", exc)
        raise HTTPException(502, detail=f"AI request failed: {exc}")

    if category["content_type"] == "movie":
        newly_placed = vod_db.bulk_place_movies_in_category(matched_ids, category_id)
    else:
        newly_placed = vod_db.bulk_place_series_in_category(matched_ids, category_id)
    vod_db.set_category_ai_description(category_id, body.description)
    vod_db.mark_category_evaluated(category_id)

    return {
        "considered": len(candidates),
        "total_before_cap": total_before_cap,
        "capped": total_before_cap > len(candidates),
        "matched": len(matched_ids),
        "newly_placed": newly_placed,
    }


@router.get("/lockout-settings/", dependencies=_GUARDS)
async def get_lockout_settings_route():
    return get_lockout_settings()


@router.post("/lockout-settings/", dependencies=_GUARDS)
async def save_lockout_settings_route(body: LockoutSettingsRequest):
    save_lockout_settings(
        body.lockout_max_attempts,
        body.lockout_window_seconds,
        body.lockout_duration_seconds,
    )
    return {"ok": True}


@router.get("/refresh-settings/", dependencies=_GUARDS)
async def get_refresh_settings_route():
    return get_refresh_settings()


@router.post("/refresh-settings/", dependencies=_GUARDS)
async def save_refresh_settings_route(body: RefreshSettingsRequest):
    save_refresh_settings(
        body.catalog_refresh_seconds_xc,
        body.catalog_refresh_seconds_plex,
        body.catalog_refresh_seconds_emby,
        body.catalog_refresh_seconds_jellyfin,
        body.enrichment_ttl_seconds,
        body.tmdb_sync_interval_seconds,
    )
    return {"ok": True}


@router.post("/categories/{category_id}/sync-source/", dependencies=_GUARDS)
async def set_category_sync_source(category_id: int, sync_source: Optional[str] = None):
    if not vod_db.get_category(category_id):
        raise HTTPException(404, detail="category not found")
    vod_db.set_category_sync_source(category_id, sync_source or None)
    return {"ok": True}


@router.post("/categories/{category_id}/sync-now/", dependencies=_GUARDS)
async def sync_category_now(category_id: int):
    if not vod_db.get_category(category_id):
        raise HTTPException(404, detail="category not found")
    try:
        return await tmdb_sync.sync_category(category_id)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(502, detail=f"TMDB sync failed: {exc}")


# ── Providers ────────────────────────────────────────────────────────────────

def _redact_provider(p: dict) -> dict:
    p = dict(p)
    p["has_password"] = bool(p.pop("password", None))
    return p


@router.get("/providers/", dependencies=_GUARDS)
async def list_providers():
    return [_redact_provider(p) for p in vod_db.list_providers()]


@router.post("/providers/", dependencies=_GUARDS)
async def upsert_provider(body: ProviderRequest):
    if body.provider_type == "dispatcharr_dvr":
        raise HTTPException(
            400, detail="DVR isn't a provider you add here -- enable it on a Dispatcharr Connection in Configuration instead.",
        )
    password = body.password.strip()
    if not password:
        existing = next((p for p in vod_db.list_providers() if p["name"] == body.name), None)
        password = existing["password"] if existing else ""
        # Real bug found live 2026-07-29 (confirmed against a test copy, not
        # this instance's real data at the time, but a genuine latent risk
        # given Backup & Restore lets config.json and the database be
        # restored independently of each other): list_providers() decrypts
        # with whatever key config.json currently holds. If that key doesn't
        # match what actually encrypted this password (e.g. an old config
        # backup was restored, rolling the key back, while keeping a newer
        # database), decrypt_value's fallback silently returns the raw
        # ciphertext AS IF it were plaintext -- upsert_provider below would
        # then encrypt that ciphertext AGAIN, permanently corrupting the
        # credential into a double-encrypted, forever-unusable value with no
        # error or warning.
        #
        # Deliberately uses looks_like_fernet_token, NOT is_encrypted, here:
        # is_encrypted only recognizes ciphertext under the CURRENT key, so
        # it correctly catches "this password was already double-encrypted
        # under today's key by an earlier bug" but misses the cross-key
        # restore case entirely (a value encrypted under a DIFFERENT key
        # decrypts to garbage under this one, which is just as much "not a
        # real password" but is_encrypted() would call it False, i.e. "looks
        # like real plaintext," and let it through to be corrupted).
        # looks_like_fernet_token checks Fernet's structural signature
        # (version byte + length), which holds regardless of which key
        # produced it. Verified live: caught the real double-encryption bug
        # this comment describes, and a direct test confirmed is_encrypted
        # alone would have missed a genuine cross-key mismatch.
        if password and looks_like_fernet_token(password):
            raise HTTPException(
                409,
                detail=(
                    f"'{body.name}'s saved password can't be read with the current encryption key "
                    "(likely a config backup was restored separately from the database) -- re-enter "
                    "the password to fix this provider."
                ),
            )
    provider_id = vod_db.upsert_provider(
        body.name, body.base_url, body.username, password, body.max_streams, body.priority, body.provider_type,
    )

    sync_error = None
    try:
        await vod_sync.sync_provider(provider_id)
    except vod_sync.VodXcAccountNotConfigured:
        sync_error = "no Dispatcharr connection has a VOD-relay account configured — profile not synced"
    except Exception as exc:
        logger.warning("[vod_routes] sync_provider(%s) failed: %s", provider_id, exc)
        sync_error = str(exc)

    return {"id": provider_id, "sync_error": sync_error}


@router.post("/providers/{provider_id}/priority/", dependencies=_GUARDS)
async def set_provider_priority(provider_id: int, priority: int):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_priority(provider_id, priority)
    return {"ok": True}


@router.post("/providers/{provider_id}/name/", dependencies=_GUARDS)
async def set_provider_name(provider_id: int, name: str):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, detail="name cannot be empty")
    vod_db.set_provider_name(provider_id, name)
    return {"ok": True}


@router.post("/providers/{provider_id}/base-url/", dependencies=_GUARDS)
async def set_provider_base_url(provider_id: int, base_url: str):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    base_url = base_url.strip()
    if not base_url:
        raise HTTPException(400, detail="base_url cannot be empty")
    vod_db.set_provider_base_url(provider_id, base_url)
    return {"ok": True}


@router.post("/providers/{provider_id}/max-streams/", dependencies=_GUARDS)
async def set_provider_max_streams(provider_id: int, max_streams: int):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_max_streams(provider_id, max_streams)
    return {"ok": True}


@router.post("/providers/{provider_id}/shared-limit/", dependencies=_GUARDS)
async def set_provider_shared_limit(provider_id: int, shared_connection_limit: Optional[int] = None):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_shared_limit(provider_id, shared_connection_limit)
    return {"ok": True}


@router.get("/providers/{provider_id}/live-accounts/", dependencies=_GUARDS)
async def list_provider_live_accounts(provider_id: int):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    return vod_db.list_provider_live_accounts(provider_id)


@router.post("/providers/{provider_id}/live-accounts/", dependencies=_GUARDS)
async def set_provider_live_account(provider_id: int, body: ProviderLiveAccountRequest):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    connection = vod_db.get_dispatcharr_connection(body.dispatcharr_connection_id)
    if not connection:
        raise HTTPException(404, detail="dispatcharr connection not found")
    link_id = vod_db.set_provider_live_account(
        provider_id, body.dispatcharr_connection_id, body.dispatcharr_account_id, body.dispatcharr_profile_id,
    )
    return {"id": link_id}


@router.delete("/providers/live-accounts/{link_id}/", dependencies=_GUARDS)
async def remove_provider_live_account(link_id: int):
    vod_db.remove_provider_live_account(link_id)
    return {"ok": True}


@router.get("/dispatcharr-connections/{connection_id}/accounts/{account_id}/profiles/", dependencies=_GUARDS)
async def list_dispatcharr_account_profiles(connection_id: int, account_id: int):
    """Real Dispatcharr M3U profiles under this account -- lets a provider's
    live-account link be scoped to ONE profile instead of the whole account,
    for the case where one Dispatcharr M3U source actually bundles several
    separate real upstream logins as distinct profiles (see
    vod_db.set_provider_live_account's docstring)."""
    connection = vod_db.get_dispatcharr_connection(connection_id)
    if not connection:
        raise HTTPException(404, detail="connection not found")
    try:
        return await dispatcharr_dvr_client.list_m3u_account_profiles(connection, account_id)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))


@router.get("/dispatcharr-connections/{connection_id}/discover-accounts/", dependencies=_GUARDS)
async def discover_dispatcharr_accounts(connection_id: int):
    """Real upstream XC logins already configured in Dispatcharr on this
    connection, one entry per PROFILE -- see
    vod_sync.list_discoverable_profiles for what's filtered out and why,
    and why profile rather than account."""
    connection = vod_db.get_dispatcharr_connection(connection_id)
    if not connection:
        raise HTTPException(404, detail="connection not found")
    try:
        return await vod_sync.list_discoverable_profiles(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))


@router.post("/dispatcharr-connections/{connection_id}/discover-accounts/import/", dependencies=_GUARDS)
async def import_discovered_dispatcharr_accounts(connection_id: int, body: ImportDiscoveredProfilesRequest):
    connection = vod_db.get_dispatcharr_connection(connection_id)
    if not connection:
        raise HTTPException(404, detail="connection not found")
    try:
        candidates = await vod_sync.list_discoverable_profiles(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))
    by_key = {(c["dispatcharr_account_id"], c["dispatcharr_profile_id"]): c for c in candidates}
    imported = []
    skipped = []
    for key in body.profiles:
        profile = by_key.get((key.dispatcharr_account_id, key.dispatcharr_profile_id))
        if not profile:
            continue
        try:
            imported.append(vod_sync.import_discovered_profile(connection_id, profile))
        except ValueError as exc:
            skipped.append({"name": profile["name"], "reason": str(exc)})
    return {"imported": imported, "skipped": skipped}


@router.post("/dispatcharr-connections/{connection_id}/discover-accounts/recheck/", dependencies=_GUARDS)
async def recheck_discovered_dispatcharr_credentials(connection_id: int):
    connection = vod_db.get_dispatcharr_connection(connection_id)
    if not connection:
        raise HTTPException(404, detail="connection not found")
    try:
        return await vod_sync.recheck_discovered_credentials(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))


@router.post("/providers/{provider_id}/user-agent/", dependencies=_GUARDS)
async def set_provider_custom_user_agent(provider_id: int, custom_user_agent: Optional[str] = None):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_custom_user_agent(provider_id, custom_user_agent.strip() if custom_user_agent else None)
    return {"ok": True}


@router.post("/providers/{provider_id}/auto-create-categories/", dependencies=_GUARDS)
async def set_provider_auto_create_categories(provider_id: int, enabled: bool):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_auto_create_categories(provider_id, enabled)
    return {"ok": True}


@router.post("/providers/{provider_id}/archive-new-categories/", dependencies=_GUARDS)
async def set_provider_archive_new_categories(provider_id: int, enabled: bool):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_archive_new_categories(provider_id, enabled)
    return {"ok": True}


@router.get("/providers/{provider_id}/available-categories/", dependencies=_GUARDS)
async def get_provider_available_categories(provider_id: int):
    """Live category names from the provider itself (both movie and series
    categories, combined/deduped) -- powers the exclude-categories picker
    with what this specific provider actually calls things, not a guessed
    or stale list. XC-only: Plex/Emby don't have this category concept."""
    provider = vod_db.get_provider(provider_id)
    if not provider:
        raise HTTPException(404, detail="provider not found")
    if provider.get("provider_type") not in (None, "xc"):
        return {"categories": []}
    client = vod_importer.XCProviderClient(provider)
    movie_categories = await client.get_vod_categories()
    series_categories = await client.get_series_categories()
    names = sorted({c["category_name"] for c in movie_categories} | {c["category_name"] for c in series_categories})
    return {"categories": names}


@router.post("/providers/{provider_id}/import-exclude-categories/", dependencies=_GUARDS)
async def set_provider_import_exclude_categories(provider_id: int, body: ProviderImportExcludeCategoriesRequest):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_import_exclude_categories(provider_id, body.category_names, body.exclude_uncategorized)
    return {"ok": True}


@router.post("/providers/{provider_id}/deactivate/", dependencies=_GUARDS)
async def deactivate_provider(provider_id: int):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_active(provider_id, False)
    return {"ok": True}


@router.post("/providers/{provider_id}/activate/", dependencies=_GUARDS)
async def activate_provider(provider_id: int):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_active(provider_id, True)
    return {"ok": True}


@router.delete("/providers/{provider_id}/", dependencies=_GUARDS)
async def delete_provider(provider_id: int):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    # A large provider's sourceless-purge cleanup (see delete_provider's own
    # docstring) can take real time even with the movie_id/episode_id
    # indexes -- off the event loop so it doesn't stall every other request
    # (including the Activity poll) while it runs.
    await asyncio.to_thread(vod_db.delete_provider, provider_id)
    return {"ok": True}


@router.post("/providers/{provider_id}/sync/", dependencies=_GUARDS)
async def sync_provider(provider_id: int):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    try:
        results = await vod_sync.sync_provider(provider_id)
    except vod_sync.VodXcAccountNotConfigured as exc:
        raise HTTPException(400, detail=str(exc))
    return {"results_by_connection": results}


@router.post("/providers/{provider_id}/import/", dependencies=_GUARDS)
async def import_provider_catalog(provider_id: int):
    provider = vod_db.get_provider(provider_id)
    if not provider:
        raise HTTPException(404, detail="provider not found")
    try:
        if provider.get("provider_type") == "plex":
            result = await plex_importer.import_plex_library(provider_id)
        elif provider.get("provider_type") in ("emby", "jellyfin"):
            result = await emby_vod_importer.import_emby_library(provider_id)
        elif provider.get("provider_type") == "dispatcharr_dvr":
            result = await dispatcharr_dvr_importer.import_dvr_recordings(provider_id)
        else:
            result = await vod_importer.import_provider_catalog(provider_id)
    except Exception as exc:
        logger.error("[vod_routes] import_provider_catalog(%s) failed: %s", provider_id, exc)
        raise HTTPException(502, detail=str(exc))
    # Without this, the periodic catalog refresher (main.py) treats a
    # manually-imported provider as still "never refreshed" and redundantly
    # re-imports it again on its very next cycle -- a real, if minor, wasted
    # hit against the actual provider's API. Also re-evaluate the catch-all
    # categories now rather than waiting for that same background cycle, so
    # a manual import's content shows up in Dispatcharr-visible categories
    # right away instead of up to a full refresh interval later.
    await asyncio.to_thread(vod_db.mark_provider_catalog_refreshed, provider_id)
    await vod_importer.resweep_smart_categories()
    return result


# ── DVR recording profiles (Phase 2) ────────────────────────────────────────
# Per-person/per-schedule routing on top of a DVR provider's own default
# categories -- see vod_db.match_recording_profiles and
# dispatcharr_dvr_client.schedule_channel_recordings.

def _require_dvr_connection(provider_id: int) -> tuple[dict, dict]:
    provider = vod_db.get_provider(provider_id)
    if not provider:
        raise HTTPException(404, detail="provider not found")
    if not provider.get("dispatcharr_connection_id"):
        raise HTTPException(400, detail="provider has no linked Dispatcharr connection configured")
    connection = vod_db.get_dispatcharr_connection(provider["dispatcharr_connection_id"])
    if not connection:
        raise HTTPException(400, detail="provider's linked Dispatcharr connection no longer exists")
    return provider, connection


def _parse_epg_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _sweep_max_overlap(intervals: list[tuple[datetime, datetime]]) -> tuple[int, Optional[datetime]]:
    """Classic interval-overlap sweep -- returns (worst-case simultaneous
    count, the moment it peaks). Touching endpoints (one airing ends exactly
    when another starts) are treated as NOT overlapping -- back-to-back
    episodes on the same channel are a real, common EPG shape and shouldn't
    read as "concurrent." Malformed/zero-length intervals are skipped rather
    than raising, since this only ever informs a soft prediction."""
    events: list[tuple[datetime, int]] = []
    for start, end in intervals:
        if not start or not end or end <= start:
            continue
        events.append((start, 1))
        events.append((end, 0))  # sorts before a start at the identical instant (0 < 1)
    events.sort()
    count = 0
    max_count = 0
    max_time: Optional[datetime] = None
    for t, delta in events:
        if delta == 0:
            count -= 1
        else:
            count += 1
            if count > max_count:
                max_count = count
                max_time = t
    return max_count, max_time


async def _predict_stream_conflict(
    provider_id: int, connection: dict, dispatcharr_user_id: int, candidate: dict,
) -> Optional[str]:
    """Best-effort, creation-time-only prediction of whether adding this
    profile could push a person's DVR recordings past their assigned
    Dispatcharr stream budget -- see dvr_user_limits' docstring for why this
    can't be a hard runtime guarantee (Dispatcharr never checks User.stream_
    limit for DVR recordings at all, confirmed live, so nothing enforces this
    except VOD Manager predicting ahead of time from the same EPG data
    Dispatcharr itself will schedule against). Returns an error detail string
    if the prediction exceeds budget, else None (also None -- no check at all
    -- if this person has no dvr_user_limits row, since the feature is
    opt-in)."""
    limit_row = vod_db.get_dvr_user_limit(provider_id, dispatcharr_user_id)
    if not limit_row:
        return None
    try:
        users = await dispatcharr_dvr_client.list_users(connection)
    except Exception as exc:
        logger.warning("[vod_routes] _predict_stream_conflict: couldn't fetch Dispatcharr users: %s", exc)
        return None
    user = next((u for u in users if u.get("id") == dispatcharr_user_id), None)
    stream_limit = (user or {}).get("stream_limit") or 0
    if stream_limit <= 0:
        # Dispatcharr's own convention: 0 means unlimited for this account --
        # nothing to predict against.
        return None
    budget = stream_limit - limit_row["stream_reserve"]

    other_profiles = [
        p for p in vod_db.list_recording_profiles(provider_id)
        if p.get("dispatcharr_user_id") == dispatcharr_user_id
    ]
    rule_specs = [candidate] + [
        {"title": p["title"], "channel_id": p["channel_id"]}
        for p in other_profiles
    ]

    # search_epg_programs, channel-scoped, not preview_series_rule -- see
    # dispatcharr_dvr_client.create_recording's docstring for why Series
    # Rules' own preview endpoint is confirmed unreliable for a channel-
    # scoped rule (can silently return 0 matches for a real, currently-
    # airing program). A spec with no channel_id can't be scoped safely
    # either way, so it's skipped rather than searched unscoped -- an
    # unscoped search would pull in every affiliate/feed carrying the title,
    # wildly overcounting this person's real simultaneous-recording risk.
    intervals: list[tuple[datetime, datetime]] = []
    for spec in rule_specs:
        if not spec.get("channel_id"):
            continue
        try:
            matches = await dispatcharr_dvr_client.search_epg_programs(
                connection, spec["title"], limit=100, channel_id=spec["channel_id"],
            )
        except Exception as exc:
            logger.warning("[vod_routes] _predict_stream_conflict: search failed for %r: %s", spec["title"], exc)
            continue
        for match in matches:
            start = _parse_epg_datetime(match.get("start_time"))
            end = _parse_epg_datetime(match.get("end_time"))
            if start and end:
                intervals.append((start, end))

    max_count, max_time = _sweep_max_overlap(intervals)
    if max_count > budget:
        username = limit_row["dispatcharr_username"]
        when = max_time.strftime("%Y-%m-%d %H:%M UTC") if max_time else "an upcoming time"
        return (
            f"Adding this profile could require {max_count} simultaneous recordings around {when}, "
            f"but {username} is only allowed {budget} (stream limit {stream_limit} minus a "
            f"{limit_row['stream_reserve']}-stream reserve)."
        )
    return None


@router.get("/dvr-recording-profiles/", dependencies=_GUARDS)
async def list_recording_profiles(provider_id: Optional[int] = None):
    return vod_db.list_recording_profiles(provider_id)


@router.post("/dvr-recording-profiles/preview/", dependencies=_GUARDS)
async def preview_recording_profile(body: RecordingProfilePreviewRequest):
    """What this profile would actually schedule, without saving anything --
    search_epg_programs, channel-scoped when channel_id is given, not
    Dispatcharr's own Series Rules preview endpoint. See
    dispatcharr_dvr_client.create_recording's docstring for why that
    endpoint is confirmed unreliable for a channel-scoped rule (can report 0
    matches for a program that's really airing right now); this preview
    needs to show the same thing create_recording_profile will actually do,
    or it isn't a preview of anything real."""
    _, connection = _require_dvr_connection(body.provider_id)
    try:
        matches = await dispatcharr_dvr_client.search_epg_programs(
            connection, body.title, limit=100, channel_id=body.channel_id,
        )
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))
    return {"matches": matches, "total": len(matches)}


@router.post("/dvr-recording-profiles/", dependencies=_GUARDS)
async def create_recording_profile(body: RecordingProfileRequest):
    """Schedules real Dispatcharr Recordings for this profile FIRST -- only
    saves the local profile row once that succeeds, so a failed remote call
    never leaves a dangling profile pointing at nothing.

    channel_id is required, not optional, despite being an Optional field on
    the request/DB schema for backward compatibility with pre-existing rows.
    A blank-channel profile is exactly the original bug report this
    redesign exists to fix: matching a title with no channel scope pulls in
    every affiliate/feed carrying it, producing a separate duplicate
    recording per channel (confirmed live, multiple times, this session).
    See dispatcharr_dvr_client.schedule_channel_recordings/create_recording
    for the channel-scoped approach that replaces Dispatcharr's own Series
    Rules feature (confirmed broken for channel-scoped matching on both
    v0.27.2 and v0.28.2) entirely.

    No collision guard against a duplicate (title, channel_id) profile
    anymore -- schedule_channel_recordings is naturally idempotent (it skips
    any airing already scheduled, see episode_identity_key), so a second
    profile for the same show+channel just means a second person wants it,
    which vod_db.match_recording_profiles' fan-out already handles by
    design, not a collision to block.

    If dispatcharr_user_id is set AND that person has a dvr_user_limits row,
    also runs a best-effort prediction (_predict_stream_conflict) of whether
    this profile could push their simultaneous-recordings count past their
    assigned Dispatcharr stream budget, before ever touching Dispatcharr --
    opt-in and predictive only, see that function's docstring for why."""
    if not body.channel_id:
        raise HTTPException(
            400,
            detail="A specific channel is required. Search the EPG and pick the exact airing/channel you want "
                   "recorded -- matching a title with no channel scope records every affiliate carrying it as a "
                   "separate duplicate.",
        )
    _, connection = _require_dvr_connection(body.provider_id)
    if body.dispatcharr_user_id:
        conflict = await _predict_stream_conflict(
            body.provider_id, connection, body.dispatcharr_user_id,
            {"title": body.title, "channel_id": body.channel_id},
        )
        if conflict:
            raise HTTPException(409, detail=conflict)
    scheduled_by = None
    if body.dispatcharr_user_id:
        try:
            users = await dispatcharr_dvr_client.list_users(connection)
            user = next((u for u in users if u.get("id") == body.dispatcharr_user_id), None)
            scheduled_by = {
                "dispatcharr_user_id": body.dispatcharr_user_id,
                "dispatcharr_username": user["username"] if user else None,
                "profile_label": body.label,
            }
        except Exception as exc:
            logger.warning("[vod_routes] create_recording_profile: couldn't resolve username for scheduled_by: %s", exc)
    backfill_check = None
    if body.backfill_mode:
        # profile row doesn't exist yet at this point -- _try_backfill only
        # needs these specific fields (see its docstring), so a lightweight
        # dict standing in for the not-yet-persisted profile is enough;
        # avoids creating the DB row before we even know Dispatcharr will
        # accept the schedule call below.
        pseudo_profile = {
            "title": body.title, "provider_id": body.provider_id, "label": body.label,
            "backfill_mode": body.backfill_mode,
            "target_movie_category_id": body.target_movie_category_id,
            "target_series_category_id": body.target_series_category_id,
        }
        backfill_check = lambda program: dispatcharr_dvr_importer._try_backfill(program, pseudo_profile)
    try:
        schedule_result = await dispatcharr_dvr_client.schedule_channel_recordings(
            connection, body.channel_id, body.title, body.mode, scheduled_by=scheduled_by,
            backfill_check=backfill_check,
        )
    except Exception as exc:
        raise HTTPException(502, detail=f"Dispatcharr rejected the recording: {exc}")
    profile_id = vod_db.create_recording_profile(
        body.provider_id, body.label, body.title, body.tvg_id, body.title_mode,
        body.description, body.description_mode, body.mode, body.channel_id,
        body.target_movie_category_id, body.target_series_category_id,
        body.dispatcharr_user_id, body.backfill_mode,
    )
    profile = vod_db.get_recording_profile(profile_id)
    # Surface whether this actually scheduled anything just now, rather than
    # reporting bare success either way -- a channel-scoped search can
    # legitimately find 0 (e.g. a "new episodes only" profile with nothing
    # new airing right now, or a show genuinely on hiatus), which is worth
    # showing the admin rather than silently implying it worked identically
    # either way. Not persisted -- purely informational for this response.
    profile["scheduled_now"] = schedule_result.get("scheduled", 0)
    profile["total_matches"] = schedule_result.get("total_matches", 0)
    profile["skipped_conflicts"] = schedule_result.get("skipped_conflicts", 0)
    profile["backfilled_now"] = schedule_result.get("backfilled", 0)
    return profile


@router.delete("/dvr-recording-profiles/{profile_id}/", dependencies=_GUARDS)
async def delete_recording_profile(profile_id: int):
    """Removing a profile also cancels the real Dispatcharr Recordings it
    scheduled -- best-effort: if the remote call fails (connection gone,
    etc.) the local profile is still deleted, since the user's clear intent
    here is "get rid of this," not to be blocked by a remote cleanup
    failure. There's no single Series Rule resource to delete anymore (see
    create_recording_profile) -- instead this finds this profile's own
    still-future, not-yet-started Recordings on its channel by matching
    custom_properties.program.title (the same shape create_recording writes)
    and removes each one individually. Already-completed recordings are
    left alone -- deleting a profile shouldn't touch content already in the
    VOD pool."""
    profile = vod_db.get_recording_profile(profile_id)
    if not profile:
        raise HTTPException(404, detail="recording profile not found")
    if profile.get("channel_id"):
        try:
            _, connection = _require_dvr_connection(profile["provider_id"])
            upcoming = await dispatcharr_dvr_client.list_scheduled_recordings(connection)
            now = datetime.now(timezone.utc)
            target_title = (profile["title"] or "").strip().lower()
            for r in upcoming:
                if r.get("channel") != profile["channel_id"]:
                    continue
                program = (r.get("custom_properties") or {}).get("program") or {}
                if (program.get("title") or "").strip().lower() != target_title:
                    continue
                start = _parse_epg_datetime(r.get("start_time"))
                if start and start <= now:
                    continue  # already aired/recording -- leave it alone
                try:
                    await dispatcharr_dvr_client.delete_recording(connection, r["id"])
                except Exception as exc:
                    logger.warning("[vod_routes] delete_recording_profile(%s): failed to remove recording %s: %s",
                                    profile_id, r.get("id"), exc)
        except Exception as exc:
            logger.warning("[vod_routes] delete_recording_profile(%s): failed to clean up Dispatcharr recordings: %s",
                            profile_id, exc)
    vod_db.delete_recording_profile(profile_id)
    return {"ok": True}


@router.post("/dvr-recording-profiles/{profile_id}/monitored/", dependencies=_GUARDS)
async def set_recording_profile_monitored(profile_id: int, monitored: bool):
    """Toggle-only -- doesn't touch scheduling. See vod_db.
    set_recording_profile_monitored's docstring for why this is separate
    from delete."""
    profile = vod_db.get_recording_profile(profile_id)
    if not profile:
        raise HTTPException(404, detail="recording profile not found")
    vod_db.set_recording_profile_monitored(profile_id, monitored)
    return {"ok": True}


@router.get("/dispatcharr-users/", dependencies=_GUARDS)
async def list_dispatcharr_users(provider_id: int):
    """Real Dispatcharr login accounts for this DVR provider's connection --
    used both by the recording-profile form's "person" picker and by the DVR
    limits form (to show someone's real current stream_limit as context when
    an admin sets their reserve)."""
    _, connection = _require_dvr_connection(provider_id)
    try:
        return await dispatcharr_dvr_client.list_users(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))


@router.get("/epg-search/", dependencies=_GUARDS)
async def search_epg_programs(provider_id: int, title: str, channel_id: Optional[int] = None):
    """Real upcoming airings for a title, across every channel carrying it
    (or scoped to one, when channel_id is given -- e.g. the Metrics
    subpage's on-demand rule-health check, which needs to know whether a
    specific rule's own channel still has real upcoming matches, not
    whether the title exists anywhere) -- powers the recording-profile
    form's channel picker so a user selects a specific real (title, tvg_id,
    channel_id) combination instead of typing a bare title and leaving
    channel as an easy-to-skip afterthought. See dispatcharr_dvr_client.
    create_recording's docstring for why a specific channel_id (not just
    tvg_id) matters."""
    if not title.strip():
        raise HTTPException(400, detail="title is required")
    _, connection = _require_dvr_connection(provider_id)
    try:
        return await dispatcharr_dvr_client.search_epg_programs(connection, title, channel_id=channel_id)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))


@router.get("/channel-profiles/", dependencies=_GUARDS)
async def list_channel_profiles(provider_id: int):
    """Real Dispatcharr Channel Profiles -- a person doesn't always have the
    full channel lineup (confirmed live: a real profile with 2395 total
    channel memberships but only 81 enabled). Used to mark/prioritize a
    selected person's own visible channels in the EPG search picker."""
    _, connection = _require_dvr_connection(provider_id)
    try:
        return await dispatcharr_dvr_client.list_channel_profiles(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))


# ── DVR Library missing-episode view (Sonarr/Radarr-style gap browsing) ─────

@router.get("/series/{series_id}/missing-episodes/", dependencies=_GUARDS)
async def list_missing_episodes(series_id: int):
    """Sonarr/Radarr-style gap view: every canonical (TMDB) episode for
    this series, each flagged in_pool True/False against what VOD Manager
    already has. Requires the series to already carry a tmdb_id (set during
    normal enrichment) -- a series with none has nothing canonical to diff
    against, so this 400s rather than guessing at episode counts."""
    series = vod_db.get_series(series_id)
    if not series:
        raise HTTPException(404, detail="series not found")
    if not series.get("tmdb_id"):
        raise HTTPException(400, detail="this series has no TMDB id yet -- nothing canonical to diff against")
    try:
        canonical = await tmdb_sync.get_series_episode_list(series["tmdb_id"])
    except Exception as exc:
        raise HTTPException(502, detail=f"TMDB lookup failed: {exc}")
    have = {
        (e["season_number"], e["episode_number"])
        for e in vod_db.list_episodes_for_series_ids([series_id]).get(series_id, [])
    }
    unresolved = {(u["season_number"], u["episode_number"]) for u in vod_db.list_unresolved_missing_episodes(series_id)}
    return [
        {
            **ep,
            "in_pool": (ep["season_number"], ep["episode_number"]) in have,
            "flagged_unresolved": (ep["season_number"], ep["episode_number"]) in unresolved,
        }
        for ep in canonical
    ]


def _matches_episode(m: dict, season_number: int, episode_number: int) -> bool:
    props = m.get("custom_properties") or {}
    season, episode = props.get("season"), props.get("episode")
    if season is not None and episode is not None:
        return season == season_number and episode == episode_number
    return True  # no structured season/episode on this listing -- surface it, let the admin eyeball it


@router.post("/series/{series_id}/missing-episodes/resolve/", dependencies=_GUARDS)
async def resolve_missing_episode(series_id: int, body: MissingEpisodeResolveRequest):
    """The cascade the user asked for, 2026-07-27, refined per their own
    follow-up the same day: (1) already in the pool from a regular
    (non-DVR) provider? Backfill it (pointer or download, per whatever an
    existing Recording Rule for this show says, defaulting to pointer when
    no rule exists) and place it straight into a category -- done, no EPG
    search needed. (2) not in the pool, but an existing Recording Rule for
    this show already names a specific channel -- that's a real, existing
    admin decision about where this show is normally watched/recorded from,
    so check THAT channel's own EPG (scoped, same call
    schedule_channel_recordings itself makes) for a season+episode match
    within the 7-day guide horizon. A hit there is high-confidence enough
    to auto-schedule directly, no picker needed -- the user's own insight,
    2026-07-27: "we know...the channel they want to record the series
    from...wouldnt we be able to use smart logic to find the scheduled
    recording in the schedule to confirm it is going to air at that time on
    that channel." (3) neither the pool nor the rule's own channel has it
    (or there's no rule at all, so there's no "usual channel" signal) --
    fall back to an unscoped search across every channel and return
    candidates for the admin to pick from; nothing auto-schedules from an
    unscoped result, since guessing a channel there is the exact per-
    affiliate duplication problem create_recording's own docstring
    documents. (4) nothing anywhere -- flag it in
    dvr_unresolved_missing_episodes for admin visibility and say so, rather
    than the attempt silently vanishing."""
    series = vod_db.get_series(series_id)
    if not series:
        raise HTTPException(404, detail="series not found")
    program = {"title": series["name"], "custom_properties": {"season": body.season_number, "episode": body.episode_number}}

    match = vod_db.find_pool_backfill_match(series["name"], program)
    if match:
        rule = vod_db.find_recording_profile_for_title(body.provider_id, series["name"])
        mode = (rule or {}).get("backfill_mode") or "pointer"
        provider = vod_db.get_provider(body.provider_id)
        # Same three-tier fallback as the main import pass (rule's own
        # category -> rule owner's personal default -> provider default) --
        # see dispatcharr_dvr_importer.py's Phase 2 comment for why the
        # personal-default tier matters (portal users have no way to set a
        # category on their own rules at all).
        user_default_series_category_id = None
        if rule and rule.get("dispatcharr_user_id"):
            owner_limit = vod_db.get_dvr_user_limit(body.provider_id, rule["dispatcharr_user_id"])
            user_default_series_category_id = (owner_limit or {}).get("default_series_category_id")
        target_category_id = (
            (rule or {}).get("target_series_category_id")
            or user_default_series_category_id
            or (provider or {}).get("dvr_series_category_id")
        )
        try:
            if mode == "download":
                await dispatcharr_dvr_importer._apply_download_backfill(match, body.provider_id)
            else:
                await dispatcharr_dvr_importer._apply_pointer_backfill(match)
            if target_category_id:
                vod_db.place_series_in_category(match["series_id"], target_category_id)
        except Exception as exc:
            raise HTTPException(502, detail=f"Found in the pool but backfill failed: {exc}")
        vod_db.clear_unresolved_missing_episode(series_id, body.season_number, body.episode_number)
        return {"resolved": True, "mode": mode, "candidates": [], "message": None}

    _, connection = _require_dvr_connection(body.provider_id)
    rule = vod_db.find_recording_profile_for_title(body.provider_id, series["name"])
    if rule and rule.get("channel_id"):
        try:
            scoped_matches = await dispatcharr_dvr_client.search_epg_programs(
                connection, series["name"], channel_id=rule["channel_id"],
            )
        except Exception as exc:
            raise HTTPException(502, detail=f"EPG search failed: {exc}")
        scoped_candidates = [m for m in scoped_matches if _matches_episode(m, body.season_number, body.episode_number)]
        if scoped_candidates:
            if await dispatcharr_dvr_client.is_already_scheduled(connection, rule["channel_id"], scoped_candidates[0]):
                # Already a real Recording for this exact airing (e.g. an
                # earlier rescan already caught it) -- nothing new to do,
                # but it's genuinely resolved, not missing.
                vod_db.clear_unresolved_missing_episode(series_id, body.season_number, body.episode_number)
                return {"resolved": True, "mode": "already_scheduled", "candidates": [], "message": None}
            scheduled_by = None
            if rule.get("dispatcharr_user_id"):
                try:
                    users = await dispatcharr_dvr_client.list_users(connection)
                    user = next((u for u in users if u.get("id") == rule["dispatcharr_user_id"]), None)
                    scheduled_by = {
                        "dispatcharr_user_id": rule["dispatcharr_user_id"],
                        "dispatcharr_username": user["username"] if user else None,
                        "profile_label": rule["label"],
                    }
                except Exception as exc:
                    logger.warning("[vod_routes] resolve_missing_episode: couldn't resolve username for scheduled_by: %s", exc)
            try:
                await dispatcharr_dvr_client.create_recording(connection, rule["channel_id"], scoped_candidates[0], scheduled_by)
            except Exception as exc:
                raise HTTPException(502, detail=f"Found on this show's usual channel but Dispatcharr rejected the recording: {exc}")
            vod_db.clear_unresolved_missing_episode(series_id, body.season_number, body.episode_number)
            return {"resolved": True, "mode": "recorded", "candidates": [], "message": None}

    try:
        matches = await dispatcharr_dvr_client.search_epg_programs(connection, series["name"], limit=50)
    except Exception as exc:
        raise HTTPException(502, detail=f"EPG search failed: {exc}")

    candidates = [m for m in matches if _matches_episode(m, body.season_number, body.episode_number)]
    if candidates:
        message = (
            "Not airing on this show's usual channel within the 7-day guide -- these are matches on other channels; "
            "pick one to record it there instead." if rule and rule.get("channel_id") else None
        )
        return {"resolved": False, "mode": None, "candidates": candidates, "message": message}

    vod_db.record_unresolved_missing_episode(series_id, body.season_number, body.episode_number, body.episode_name)
    return {
        "resolved": False, "mode": None, "candidates": [],
        "message": "Not found in the pool or the EPG -- flagged for review. Worth checking manually against "
                    "Plex/Emby/Jellyfin if one of those is also configured for this instance.",
    }


@router.post("/series/{series_id}/missing-episodes/schedule/", dependencies=_GUARDS)
async def schedule_missing_episode(series_id: int, body: MissingEpisodeScheduleRequest):
    """Schedules exactly one specific EPG candidate the admin picked from
    resolve_missing_episode's results -- a genuine one-off Recording (see
    dispatcharr_dvr_client.create_recording), not a full recurring rule."""
    series = vod_db.get_series(series_id)
    if not series:
        raise HTTPException(404, detail="series not found")
    _, connection = _require_dvr_connection(body.provider_id)
    try:
        recording = await dispatcharr_dvr_client.create_recording(connection, body.channel_id, body.program)
    except Exception as exc:
        raise HTTPException(502, detail=f"Dispatcharr rejected the recording: {exc}")
    props = body.program.get("custom_properties") or {}
    season, episode = props.get("season"), props.get("episode")
    if season is not None and episode is not None:
        vod_db.clear_unresolved_missing_episode(series_id, season, episode)
    return recording


@router.get("/dvr-unresolved-missing-episodes/", dependencies=_GUARDS)
async def list_dvr_unresolved_missing_episodes(series_id: Optional[int] = None):
    """Admin-visible flag list -- everything resolve_missing_episode
    couldn't find anywhere (see its own docstring)."""
    return vod_db.list_unresolved_missing_episodes(series_id)


@router.get("/dvr-recording-failures/", dependencies=_GUARDS)
async def list_dvr_recording_failures(provider_id: Optional[int] = None):
    """Admin-visible log of every recording dispatcharr_dvr_importer.
    reschedule_failed_recordings has ever detected as genuinely failed --
    both outcomes ('rescheduled' onto a replacement channel, or still
    'unresolved' and retried on the next poll cycle), see that function's
    own docstring."""
    return vod_db.list_recording_failures(provider_id)


@router.get("/dvr-upcoming/", dependencies=_GUARDS)
async def list_dvr_upcoming(provider_id: int):
    """Real scheduled-but-not-yet-run recordings, for the DVR tab's
    upcoming-recordings agenda view -- confirmed live these have no
    'scheduled' status string of their own, just an absent/None status."""
    _, connection = _require_dvr_connection(provider_id)
    try:
        return await dispatcharr_dvr_client.list_scheduled_recordings(connection)
    except Exception as exc:
        raise HTTPException(502, detail=str(exc))


@router.get("/watch-sessions/", dependencies=_GUARDS)
async def list_watch_sessions(dispatcharr_user_id: Optional[int] = None, active_only: bool = False):
    """VOD Manager's own persisted watch-session history -- see
    main.py's _watch_session_poller and vod_db.watch_sessions' table
    comment for why this exists (Dispatcharr's own connection stats are
    real-time only). Powers the DVR Metrics subpage's per-person
    recorded-vs-watched view."""
    return vod_db.list_watch_sessions(dispatcharr_user_id, active_only)


@router.get("/dvr-user-limits/", dependencies=_GUARDS)
async def list_dvr_user_limits(provider_id: Optional[int] = None):
    return vod_db.list_dvr_user_limits(provider_id)


@router.post("/dvr-user-limits/", dependencies=_GUARDS)
async def create_dvr_user_limit(body: DvrUserLimitRequest):
    """One row per (provider, real Dispatcharr person) -- opt-in, see the
    table's schema comment. UNIQUE(provider_id, dispatcharr_user_id) means a
    second attempt for the same person just fails with a clear conflict
    instead of silently creating a duplicate row an admin would have to
    puzzle over later."""
    existing = vod_db.get_dvr_user_limit(body.provider_id, body.dispatcharr_user_id)
    if existing:
        raise HTTPException(
            409, detail=f"{body.dispatcharr_username} already has DVR limits configured for this provider.",
        )
    if body.quota_policy not in ("hard_fail", "delete_oldest"):
        raise HTTPException(422, detail="quota_policy must be 'hard_fail' or 'delete_oldest'")
    limit_id = vod_db.create_dvr_user_limit(
        body.provider_id, body.dispatcharr_user_id, body.dispatcharr_username,
        body.stream_reserve, body.disk_quota_bytes,
        body.retention_max_age_days, body.retention_max_episodes_per_show,
        body.default_movie_category_id, body.default_series_category_id,
        body.quota_policy,
    )
    return vod_db.get_dvr_user_limit(body.provider_id, body.dispatcharr_user_id) or {"id": limit_id}


@router.post("/dvr-user-limits/{limit_id}/", dependencies=_GUARDS)
async def update_dvr_user_limit(limit_id: int, body: DvrUserLimitUpdateRequest):
    if body.quota_policy is not None and body.quota_policy not in ("hard_fail", "delete_oldest"):
        raise HTTPException(422, detail="quota_policy must be 'hard_fail' or 'delete_oldest'")
    vod_db.update_dvr_user_limit(
        limit_id, body.stream_reserve, body.disk_quota_bytes,
        body.retention_max_age_days, body.retention_max_episodes_per_show,
        body.default_movie_category_id, body.default_series_category_id,
        body.quota_policy,
    )
    return {"ok": True}


@router.delete("/dvr-user-limits/{limit_id}/", dependencies=_GUARDS)
async def delete_dvr_user_limit(limit_id: int):
    vod_db.delete_dvr_user_limit(limit_id)
    return {"ok": True}


@router.get("/dvr-user-limits/{limit_id}/retention-candidates/", dependencies=_GUARDS)
async def get_retention_candidates(limit_id: int):
    """Dry-run only -- see vod_db.find_retention_candidates for why nothing
    is deleted by this call, just listed for review."""
    limits = [lim for lim in vod_db.list_dvr_user_limits() if lim["id"] == limit_id]
    if not limits:
        raise HTTPException(404, detail="DVR limit not found")
    lim = limits[0]
    return vod_db.find_retention_candidates(lim["provider_id"], lim["dispatcharr_user_id"])


@router.post("/dvr-user-limits/{limit_id}/apply-retention/", dependencies=_GUARDS)
async def apply_retention(limit_id: int, body: ApplyRetentionRequest):
    """The confirm step after get_retention_candidates -- deletes exactly
    the movie/episode sources the admin reviewed and submitted back, no
    re-scanning or re-deciding here. See vod_db.apply_retention_deletions."""
    limits = [lim for lim in vod_db.list_dvr_user_limits() if lim["id"] == limit_id]
    if not limits:
        raise HTTPException(404, detail="DVR limit not found")
    return vod_db.apply_retention_deletions(body.movies, body.episodes)


@router.get("/dvr-user-limits/{limit_id}/usage/", dependencies=_GUARDS)
async def get_dvr_user_limit_usage(limit_id: int):
    """Current disk usage for this person -- sum of file_size_bytes across
    whatever's actually sitting in the categories their profiles target
    (vod_db.dvr_user_disk_usage_bytes), computed live so it always reflects
    the current pool state rather than a cached counter that could drift.
    Split into actual_bytes (real local copies) vs virtual_bytes (backfill
    pointers into another provider's stream, nothing stored locally) --
    total_bytes is what quota enforcement compares against; the split is
    for display so an admin can see how much is really costing disk."""
    limits = [lim for lim in vod_db.list_dvr_user_limits() if lim["id"] == limit_id]
    if not limits:
        raise HTTPException(404, detail="DVR limit not found")
    limit_row = limits[0]
    usage = vod_db.dvr_user_disk_usage_bytes(limit_row["provider_id"], limit_row["dispatcharr_user_id"])
    return {
        "usage_bytes": usage["total_bytes"],
        "actual_bytes": usage["actual_bytes"],
        "virtual_bytes": usage["virtual_bytes"],
        "total_bytes": usage["total_bytes"],
    }


# ── Portal accounts (admin-side provisioning) ───────────────────────────────
# Creates/manages the end-user DVR portal's own login accounts -- see
# backend/portal_routes.py and backend/portal_auth.py for the portal's own
# (deliberately separate) auth system these rows drive. Admin-only, same
# _GUARDS as everything else in this file -- a portal account itself can
# never reach these, only /api/portal/* (require_portal_auth).

def _redact_portal_account(account: dict) -> dict:
    return {k: v for k, v in account.items() if k not in ("password_salt", "password_hash", "totp_secret")}


@router.get("/portal-accounts/", dependencies=_GUARDS)
async def list_portal_accounts(provider_id: Optional[int] = None):
    return [_redact_portal_account(a) for a in vod_db.list_portal_accounts(provider_id)]


@router.post("/portal-accounts/", dependencies=_GUARDS)
async def create_portal_account(body: PortalAccountRequest):
    if vod_db.get_portal_account_by_username(body.username):
        raise HTTPException(409, detail=f"'{body.username}' is already taken")
    existing = [
        a for a in vod_db.list_portal_accounts(body.provider_id)
        if a["dispatcharr_user_id"] == body.dispatcharr_user_id
    ]
    if existing:
        raise HTTPException(409, detail="This person already has a portal account for this provider")
    salt, hashed = hash_password(body.password)
    account_id = vod_db.create_portal_account(
        body.provider_id, body.dispatcharr_user_id, body.username, salt, hashed, body.email,
    )
    return _redact_portal_account(vod_db.get_portal_account(account_id))


@router.post("/portal-accounts/{account_id}/email/", dependencies=_GUARDS)
async def update_portal_account_email(account_id: int, body: PortalAccountEmailRequest):
    """Admin edit of the same field portal_routes.portal_update_email lets
    the person set for themselves -- e.g. onboarding them with an email up
    front at creation, or fixing/adding one later without waiting for them
    to log in and do it themselves."""
    if not vod_db.get_portal_account(account_id):
        raise HTTPException(404, detail="portal account not found")
    vod_db.set_portal_account_email(account_id, (body.email or "").strip() or None)
    return _redact_portal_account(vod_db.get_portal_account(account_id))


@router.post("/portal-accounts/{account_id}/reset-password/", dependencies=_GUARDS)
async def reset_portal_account_password(account_id: int, body: PortalAccountPasswordRequest):
    if not vod_db.get_portal_account(account_id):
        raise HTTPException(404, detail="portal account not found")
    salt, hashed = hash_password(body.password)
    vod_db.set_portal_account_password(account_id, salt, hashed)
    # A session token issued before this reset must not keep working for the
    # rest of its TTL just because the password it was issued under changed.
    portal_auth.revoke_sessions_for_account(account_id)
    return {"ok": True}


@router.post("/portal-accounts/{account_id}/reset-mfa/", dependencies=_GUARDS)
async def reset_portal_account_mfa(account_id: int):
    """Revokes MFA enrollment -- e.g. the person lost their authenticator
    device. Their next login is forced back through enrollment (see
    portal_routes.portal_login's enrollment_required flag) before they can
    do anything else, same as a brand-new account."""
    if not vod_db.get_portal_account(account_id):
        raise HTTPException(404, detail="portal account not found")
    vod_db.set_portal_account_totp(account_id, None, totp_enabled=False)
    # Same reasoning as reset-password above -- an existing session survived
    # by the old (now-revoked) MFA enrollment shouldn't keep working either.
    portal_auth.revoke_sessions_for_account(account_id)
    return {"ok": True}


@router.delete("/portal-accounts/{account_id}/", dependencies=_GUARDS)
async def delete_portal_account(account_id: int):
    if not vod_db.get_portal_account(account_id):
        raise HTTPException(404, detail="portal account not found")
    vod_db.delete_portal_account(account_id)
    return {"ok": True}


# ── Categories ───────────────────────────────────────────────────────────────

@router.get("/categories/", dependencies=_GUARDS)
async def list_categories(content_type: Optional[str] = None):
    return vod_db.list_categories(content_type)


@router.post("/categories/", dependencies=_GUARDS)
async def upsert_category(body: CategoryRequest):
    if body.content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    category_id = vod_db.upsert_category(
        body.name, body.content_type, body.is_smart, body.sort_order, body.rule_json,
    )
    if body.sync_source is not None:
        vod_db.set_category_sync_source(category_id, body.sync_source or None)
    # Auto-evaluate on create/edit instead of leaving a brand-new (or just
    # changed) rule sitting empty until someone remembers to click "Evaluate
    # rule now" -- real user ask: creating a rule should show its results
    # immediately, same as the built-in All Movies/All TV Shows catch-alls
    # already do (see vod_db._seed_default_categories). Best-effort: a bad
    # rule_json shouldn't block the category itself from being saved.
    if body.is_smart and body.rule_json:
        try:
            vod_db.evaluate_smart_category(category_id)
        except Exception as exc:
            logger.warning("[vod_routes] auto-evaluate on save failed for category=%s: %s", category_id, exc)
    return {"id": category_id}


@router.delete("/categories/{category_id}/", dependencies=_GUARDS)
async def delete_category(category_id: int):
    if not vod_db.get_category(category_id):
        raise HTTPException(404, detail="category not found")
    vod_db.delete_category(category_id)
    return {"ok": True}


@router.post("/categories/{category_id}/name/", dependencies=_GUARDS)
async def rename_category(category_id: int, name: str):
    if not vod_db.get_category(category_id):
        raise HTTPException(404, detail="category not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, detail="name cannot be empty")
    vod_db.set_category_name(category_id, name)
    return {"ok": True}


@router.post("/categories/{category_id}/active/", dependencies=_GUARDS)
async def set_category_active(category_id: int, is_active: bool):
    if not vod_db.get_category(category_id):
        raise HTTPException(404, detail="category not found")
    try:
        vod_db.set_category_active(category_id, is_active)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return {"ok": True}


class BulkCategoryActiveRequest(BaseModel):
    category_ids: list[int]
    is_active: bool


class BulkCategoryIdsRequest(BaseModel):
    category_ids: list[int]


@router.post("/categories/bulk-active/", dependencies=_GUARDS)
async def bulk_set_categories_active(body: BulkCategoryActiveRequest):
    try:
        return vod_db.bulk_set_category_active(body.category_ids, body.is_active)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))


@router.post("/categories/bulk-delete/", dependencies=_GUARDS)
async def bulk_delete_categories(body: BulkCategoryIdsRequest):
    deleted = vod_db.bulk_delete_categories(body.category_ids)
    return {"deleted": deleted}


@router.post("/categories/{category_id}/schedule/", dependencies=_GUARDS)
async def set_category_schedule(category_id: int, start_mmdd: Optional[str] = None, end_mmdd: Optional[str] = None):
    if not vod_db.get_category(category_id):
        raise HTTPException(404, detail="category not found")
    try:
        vod_db.set_category_schedule(category_id, start_mmdd or None, end_mmdd or None)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return {"ok": True}


@router.post("/categories/{category_id}/sort-order/", dependencies=_GUARDS)
async def set_category_sort_order(category_id: int, sort_order: int):
    if not vod_db.get_category(category_id):
        raise HTTPException(404, detail="category not found")
    vod_db.set_category_sort_order(category_id, sort_order)
    return {"ok": True}


@router.post("/categories/{category_id}/evaluate/", dependencies=_GUARDS)
async def evaluate_smart_category(category_id: int):
    if not vod_db.get_category(category_id):
        raise HTTPException(404, detail="category not found")
    try:
        result = vod_db.evaluate_smart_category(category_id)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return result


class CategoryScheduleRequest(BaseModel):
    schedule_interval_seconds: Optional[int] = None  # null = manual only (default, unchanged)
    use_ai_evaluation: bool = False  # opt-in, default off -- see vod_db.set_category_schedule_interval


@router.post("/categories/{category_id}/eval-schedule/", dependencies=_GUARDS)
async def set_category_eval_schedule(category_id: int, body: CategoryScheduleRequest):
    """Distinct from POST .../schedule/ above (that's the annual on/off
    Halloween/Christmas-style date-range schedule) -- this is "how often
    should this smart category's RULE re-evaluate," an unrelated concept
    that happens to also be called a schedule."""
    if not vod_db.get_category(category_id):
        raise HTTPException(404, detail="category not found")
    if body.schedule_interval_seconds is not None and body.schedule_interval_seconds < 300:
        raise HTTPException(400, detail="Minimum schedule interval is 5 minutes.")
    vod_db.set_category_schedule_interval(category_id, body.schedule_interval_seconds, body.use_ai_evaluation)
    return {"ok": True}


# ── Year review ──────────────────────────────────────────────────────────────
# Items imported with no year, where more than one existing pool entry shares
# the same name -- too ambiguous to auto-merge, so they're held out of every
# category (see vod_db.place_*_in_category) until a human picks the right
# year, usually from a real TMDB suggestion rather than having to research it.

@router.get("/needs-review/", dependencies=_GUARDS)
async def list_needs_year_review(content_type: Optional[str] = None):
    return vod_db.list_needs_year_review(content_type)


# ── Orphan checker ───────────────────────────────────────────────────────────
# Self-service scan/purge for dead rows a provider deletion (or a bug
# elsewhere) can leave behind -- see vod_db.find_orphans/purge_orphans.

@router.get("/orphans/", dependencies=_GUARDS)
async def scan_orphans():
    return vod_db.find_orphans()


@router.post("/orphans/purge/", dependencies=_GUARDS)
async def purge_orphans_route():
    return vod_db.purge_orphans()


# ── Uncategorized checker ────────────────────────────────────────────────────
# Different from orphans above (sourceless rows) -- these have real sources
# but zero category placements, so Dispatcharr can't see them at all (see
# vod_db.find_uncategorized's docstring). No purge action here: the fix is
# either the catch-all sweep catching up, or a human resolving Year Review.

@router.get("/uncategorized/", dependencies=_GUARDS)
async def scan_uncategorized():
    return vod_db.find_uncategorized()


@router.post("/uncategorized/resweep/", dependencies=_GUARDS)
async def resweep_uncategorized():
    """Manual trigger for the same sweep the independent background loop
    runs on its own interval (main.py's _uncategorized_sweep_loop) -- lets
    an admin get an immediate result instead of waiting for the next tick."""
    await vod_importer.resweep_smart_categories()
    return vod_db.find_uncategorized()


# ── Duplicate finder ─────────────────────────────────────────────────────────
# Self-service scan/merge for pool entries that look like the same real
# title split into two rows -- cosmetic punctuation variants, adjacent-year
# mislabeling, or both -- see vod_db.find_duplicate_groups/merge_duplicate_group.

@router.get("/duplicates/", dependencies=_GUARDS)
async def scan_duplicates(content_type: str):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    # Real bug found live 2026-07-31: this ran inline on the event loop.
    # find_duplicate_groups takes 60+s against a real ~250K-item catalog,
    # and since it's plain sync sqlite3 code, that froze the ENTIRE server
    # (every other request, including unrelated ones) for the whole scan --
    # not just this endpoint. to_thread hands it to a worker thread instead.
    return await asyncio.to_thread(vod_db.find_duplicate_groups, content_type)


@router.post("/duplicates/merge/", dependencies=_GUARDS)
async def merge_duplicates(body: MergeDuplicateGroupRequest):
    if body.content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    return vod_db.merge_duplicate_group(body.content_type, body.keep_id, body.merge_ids)


@router.get("/duplicates/ignored/", dependencies=_GUARDS)
async def list_ignored_duplicates(content_type: str):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    return vod_db.list_ignored_duplicate_signatures(content_type)


@router.post("/duplicates/ignore/", dependencies=_GUARDS)
async def ignore_duplicate_group(body: IgnoreDuplicateGroupRequest):
    if body.content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    vod_db.ignore_duplicate_group(body.content_type, body.item_ids)
    return {"ok": True}


@router.get("/duplicates/tmdb-details/", dependencies=_GUARDS)
async def duplicate_tmdb_details(content_type: str, tmdb_ids: str):
    """Lets the frontend flag which candidate in a duplicate group actually
    matches TMDB, on both the year (a provider can mislabel a year while
    still getting the title match right) and the title itself (used to
    pick a confident auto-merge target rather than falling back to a
    weaker heuristic like source count)."""
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    ids = [i for i in tmdb_ids.split(",") if i.strip()]
    if not ids:
        return {}
    try:
        return await tmdb_sync.get_tmdb_details_for_ids(ids, content_type)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))


class MergeConfirmedDuplicatesRequest(BaseModel):
    content_type: str
    groups: list[MergeDuplicateGroupPair]


def _merge_confirmed_groups(content_type: str, groups: list) -> dict:
    merged_groups = 0
    merged_items = 0
    for pair in groups:
        result = vod_db.merge_duplicate_group(content_type, pair.keep_id, pair.merge_ids)
        merged_groups += 1
        merged_items += result["merged_count"]
    return {"merged_groups": merged_groups, "merged_items": merged_items}


@router.post("/duplicates/merge-confirmed/", dependencies=_GUARDS)
async def merge_confirmed_duplicates(body: MergeConfirmedDuplicatesRequest):
    if body.content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    # Real bug found live 2026-07-31 (same shape as scan_duplicates): a real
    # confirm-scan against the full catalog can hand back thousands of
    # confirmed groups (2273 in this instance's own real data) -- looping
    # vod_db.merge_duplicate_group synchronously inline blocked the whole
    # server for ~19s measured directly against real data (8.35ms/merge x
    # 2273). asyncio.to_thread offloads the whole batch to a worker thread.
    return await asyncio.to_thread(_merge_confirmed_groups, body.content_type, body.groups)


@router.post("/duplicates/confirm-scan/", dependencies=_GUARDS)
async def start_duplicate_confirm_scan(content_type: str):
    """Kicks off the background TMDB-confirmed-match check (see
    duplicate_confirm.py) -- a real catalog scan can surface thousands of
    candidate tmdb_ids, far too slow/rate-limit-risky to check inline in
    one blocking request, so this returns immediately with a job id the
    frontend polls for progress."""
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    if not get_tmdb_api_key():
        raise HTTPException(400, detail="Set the TMDB API key in Configuration first")
    job_id = duplicate_confirm.start_job(content_type)
    return {"job_id": job_id}


@router.get("/duplicates/confirm-scan/{job_id}/", dependencies=_GUARDS)
async def get_duplicate_confirm_scan(job_id: str):
    job = duplicate_confirm.get_job(job_id)
    if not job:
        raise HTTPException(404, detail="job not found")
    return {
        "status": job["status"], "checked": job["checked"], "total": job["total"],
        "confirmed": job["confirmed"] if job["status"] == "done" else [],
        "error": job["error"],
    }


@router.post("/duplicates/confirm-scan/{job_id}/cancel/", dependencies=_GUARDS)
async def cancel_duplicate_confirm_scan(job_id: str):
    duplicate_confirm.cancel_job(job_id)
    return {"ok": True}


@router.get("/needs-review/{content_type}/{item_id}/suggestions/", dependencies=_GUARDS)
async def year_review_suggestions(content_type: str, item_id: int, q: Optional[str] = None):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    item = vod_db.get_movie(item_id) if content_type == "movie" else vod_db.get_series(item_id)
    if not item:
        raise HTTPException(404, detail=f"{content_type} not found")
    try:
        # q lets a reviewer search a different title than what's stored --
        # the same content is sometimes released under a different name in a
        # different region (e.g. international vs. North American title),
        # and the default search (item's own stored name) won't find a match
        # TMDB's index doesn't already associate with that exact string.
        return await tmdb_sync.search_title((q or item["name"]).strip(), content_type)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(502, detail=f"TMDB search failed: {exc}")


@router.get("/needs-review/{content_type}/{item_id}/ai-suggest/", dependencies=_GUARDS)
async def year_review_ai_suggest(content_type: str, item_id: int, q: Optional[str] = None):
    """Asks Claude to pick the most likely correct match among the same TMDB
    candidates the normal suggestions/ endpoint already surfaces -- purely a
    recommendation for the reviewer to weigh, never applied automatically
    (see resolve/ above, still a separate explicit action)."""
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    item = vod_db.get_movie(item_id) if content_type == "movie" else vod_db.get_series(item_id)
    if not item:
        raise HTTPException(404, detail=f"{content_type} not found")
    try:
        candidates = await tmdb_sync.search_title((q or item["name"]).strip(), content_type)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(502, detail=f"TMDB search failed: {exc}")
    if not candidates:
        return {"best_match_index": None, "reasoning": "No TMDB candidates to choose from.", "confidence": "low"}
    try:
        return await ai_assist.suggest_year_review_match(item["name"], None, content_type, candidates)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.warning("[vod_routes] AI year-review suggestion failed: %s", exc)
        raise HTTPException(502, detail=f"AI request failed: {exc}")


@router.post("/needs-review/{content_type}/{item_id}/resolve/", dependencies=_GUARDS)
async def resolve_year_review(content_type: str, item_id: int, body: ResolveYearReviewRequest):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    try:
        return vod_db.resolve_year_review(content_type, item_id, body.year, body.tmdb_id)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))


# ── Missing artwork ──────────────────────────────────────────────────────────
# Browse-and-fix queue for movies/series with no poster — see
# vod_db.list_missing_artwork's docstring for why this can't just be an
# automatic pass.

def _split_prefixes(prefixes: Optional[str]) -> Optional[list[str]]:
    return [p for p in prefixes.split(",") if p] if prefixes else None


@router.get("/missing-artwork/", dependencies=_GUARDS)
async def list_missing_artwork(
    content_type: str, limit: int = 30, offset: int = 0, search: Optional[str] = None,
    excluded: bool = False, script: Optional[str] = None, prefixes: Optional[str] = None,
):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    prefix_list = _split_prefixes(prefixes)
    return vod_db.list_missing_artwork_page(content_type, limit=limit, offset=offset, search=search, excluded=excluded, script=script, prefixes=prefix_list)


@router.get("/missing-artwork/prefixes/", dependencies=_GUARDS)
async def missing_artwork_prefixes(content_type: str, search: Optional[str] = None, excluded: bool = False, script: Optional[str] = None):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    return vod_db.list_missing_artwork_prefixes(content_type, search=search, excluded=excluded, script=script)


@router.post("/missing-artwork/bulk-poster/", dependencies=_GUARDS)
async def bulk_apply_missing_artwork_poster(body: BulkMissingArtworkPosterRequest):
    if body.content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    if not body.poster_url.strip():
        raise HTTPException(400, detail="poster_url is required")
    prefix_list = _split_prefixes(body.prefixes)
    ids = body.ids if body.ids is not None else vod_db.list_missing_artwork_ids(body.content_type, search=body.search, excluded=body.excluded, script=body.script, prefixes=prefix_list)
    applied = vod_db.bulk_set_poster_url(body.content_type, ids, body.poster_url.strip())
    return {"applied": applied}


@router.post("/missing-artwork/bulk-exclude/", dependencies=_GUARDS)
async def bulk_exclude_missing_artwork(body: BulkMissingArtworkExcludeRequest):
    if body.content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    prefix_list = _split_prefixes(body.prefixes)
    ids = body.ids if body.ids is not None else vod_db.list_missing_artwork_ids(body.content_type, search=body.search, excluded=body.excluded, script=body.script, prefixes=prefix_list)
    # Always run the sibling check on archive, regardless of which filter
    # (prefix chip, script checkbox, or plain search text) produced the
    # candidate set -- a plain-search-only path used to skip this check
    # entirely, which reopened the exact "archived N titles with zero
    # sibling protection" bug already fixed for /library-language/. Only an
    # un-archive (never destructive) applies directly.
    if body.dry_run:
        result = vod_db.smart_bulk_exclude(body.content_type, ids, _split_prefixes(body.keep_codes), dry_run=True)
        return {"changed": result["archived"], "skipped": result["skipped"], "skipped_examples": result["skipped_examples"]}
    if body.set_excluded:
        result = vod_db.smart_bulk_exclude(body.content_type, ids, _split_prefixes(body.keep_codes))
        return {"changed": result["archived"], "skipped": result["skipped"], "skipped_examples": result["skipped_examples"]}
    changed = vod_db.bulk_set_review_excluded(body.content_type, ids, body.set_excluded)
    return {"changed": changed}


# ── Whole-library language filter ───────────────────────────────────────────
# Same script/prefix filtering as Missing Artwork, but over the entire pool
# (a title with a real poster is just as much "not in my language" as one
# without) -- see vod_db.list_library_filtered's docstring.

@router.get("/library-language/", dependencies=_GUARDS)
async def list_library_language(
    content_type: str, limit: int = 30, offset: int = 0, search: Optional[str] = None,
    excluded: Optional[bool] = None, script: Optional[str] = None, prefixes: Optional[str] = None,
):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    prefix_list = _split_prefixes(prefixes)
    return vod_db.list_library_page(content_type, limit=limit, offset=offset, search=search, excluded=excluded, script=script, prefixes=prefix_list)


@router.get("/library-language/prefixes/", dependencies=_GUARDS)
async def library_language_prefixes(content_type: str, search: Optional[str] = None, excluded: Optional[bool] = None, script: Optional[str] = None):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    return vod_db.list_library_prefixes(content_type, search=search, excluded=excluded, script=script)


@router.post("/library-language/bulk-exclude/", dependencies=_GUARDS)
async def bulk_exclude_library(body: BulkLibraryExcludeRequest):
    if body.content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    prefix_list = _split_prefixes(body.prefixes)
    ids = body.ids if body.ids is not None else vod_db.list_library_ids(body.content_type, search=body.search, excluded=body.excluded, script=body.script, prefixes=prefix_list)
    # dry_run always goes through the (read-only) smart-exclude path -- see
    # the missing-artwork route's identical comment.
    if body.dry_run:
        result = vod_db.smart_bulk_exclude(body.content_type, ids, _split_prefixes(body.keep_codes), dry_run=True)
        return {"changed": result["archived"], "skipped": result["skipped"], "skipped_examples": result["skipped_examples"]}
    # This modal exists specifically for language-based archiving -- always
    # run the sibling check on archive, regardless of which filter (prefix
    # chip, script checkbox, or plain search text) produced the candidate
    # set. Un-archiving is never destructive, so it always applies directly.
    if body.set_excluded:
        result = vod_db.smart_bulk_exclude(body.content_type, ids, _split_prefixes(body.keep_codes))
        return {"changed": result["archived"], "skipped": result["skipped"], "skipped_examples": result["skipped_examples"]}
    changed = vod_db.bulk_set_review_excluded(body.content_type, ids, body.set_excluded)
    return {"changed": changed}


@router.get("/missing-artwork/{content_type}/{item_id}/suggestions/", dependencies=_GUARDS)
async def missing_artwork_suggestions(content_type: str, item_id: int, q: Optional[str] = None):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    item = vod_db.get_movie(item_id) if content_type == "movie" else vod_db.get_series(item_id)
    if not item:
        raise HTTPException(404, detail=f"{content_type} not found")
    try:
        return await tmdb_sync.search_title((q or item["name"]).strip(), content_type)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(502, detail=f"TMDB search failed: {exc}")


@router.get("/missing-artwork/{content_type}/{item_id}/ai-suggest/", dependencies=_GUARDS)
async def missing_artwork_ai_suggest(content_type: str, item_id: int, q: Optional[str] = None):
    """Same pattern as the Needs Review AI-suggest route: asks the configured
    AI provider to pick the most likely correct match among real TMDB search
    results, purely a recommendation the reviewer still has to click to
    apply (see resolve/ below)."""
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    item = vod_db.get_movie(item_id) if content_type == "movie" else vod_db.get_series(item_id)
    if not item:
        raise HTTPException(404, detail=f"{content_type} not found")
    try:
        candidates = await tmdb_sync.search_title((q or item["name"]).strip(), content_type)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(502, detail=f"TMDB search failed: {exc}")
    if not candidates:
        return {"best_match_index": None, "reasoning": "No TMDB candidates to choose from.", "confidence": "low"}
    try:
        return await ai_assist.suggest_year_review_match(item["name"], None, content_type, candidates)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.warning("[vod_routes] AI missing-artwork suggestion failed: %s", exc)
        raise HTTPException(502, detail=f"AI request failed: {exc}")


@router.post("/missing-artwork/{content_type}/{item_id}/resolve/", dependencies=_GUARDS)
async def resolve_missing_artwork(content_type: str, item_id: int, body: ResolveMissingArtworkRequest):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    if not body.poster_url.strip():
        raise HTTPException(400, detail="poster_url is required")
    try:
        return vod_db.resolve_missing_artwork(
            content_type, item_id, body.poster_url.strip(), body.tmdb_id, body.name, body.year,
        )
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))


# ── Movies ───────────────────────────────────────────────────────────────────

@router.get("/movies/", dependencies=_GUARDS)
async def list_movies(
    limit: int = 50, offset: int = 0, search: Optional[str] = None, category_id: Optional[int] = None,
    provider_id: Optional[int] = None, archived: bool = False,
):
    movies = vod_db.list_movies(limit=limit, offset=offset, search=search, category_id=category_id, provider_id=provider_id, archived=archived)
    ids = [m["id"] for m in movies]
    sources_by_id    = vod_db.list_movie_sources_for_ids(ids)
    placements_by_id = vod_db.list_movie_placements_for_ids(ids)
    for m in movies:
        m["sources"]    = sources_by_id.get(m["id"], [])
        m["placements"] = placements_by_id.get(m["id"], [])
    return {
        "items": movies,
        "total": vod_db.count_movies(search=search, category_id=category_id, provider_id=provider_id, archived=archived),
        "limit": limit,
        "offset": offset,
    }


@router.post("/movies/{movie_id}/archive/", dependencies=_GUARDS)
async def set_movie_archived(movie_id: int, archived: bool):
    if not vod_db.get_movie(movie_id):
        raise HTTPException(404, detail="movie not found")
    vod_db.bulk_set_review_excluded("movie", [movie_id], archived)
    return {"ok": True}


@router.post("/movies/bulk-place/", dependencies=_GUARDS)
async def bulk_place_movies(body: BulkPlaceRequest):
    if not vod_db.get_category(body.category_id):
        raise HTTPException(404, detail="category not found")
    ids = body.ids if body.ids is not None else vod_db.list_all_movie_ids(search=body.search, category_id=body.source_category_id, provider_id=body.source_provider_id)
    newly_placed = vod_db.bulk_place_movies_in_category(ids, body.category_id)
    return {"matched": len(ids), "newly_placed": newly_placed}


@router.post("/bulk-archive/", dependencies=_GUARDS)
async def bulk_archive(body: BulkArchiveRequest):
    """Movies/TV list's multi-select 'Archive selected'/'Un-archive selected'
    -- same underlying action as each row's own single-item archive toggle
    (see set_movie_archived/set_series_archived), just applied to many rows
    at once from the picked-checkbox set."""
    if body.content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    changed = vod_db.bulk_set_review_excluded(body.content_type, body.ids, body.archived)
    return {"changed": changed}


@router.post("/movies/", dependencies=_GUARDS)
async def upsert_movie(body: MovieRequest):
    fields = body.model_dump(exclude={"name", "year"}, exclude_none=True)
    movie_id = vod_db.upsert_movie(body.name, body.year, **fields)
    return {"id": movie_id}


@router.get("/movies/{movie_id}/", dependencies=_GUARDS)
async def get_movie(movie_id: int):
    movie = vod_db.get_movie(movie_id)
    if not movie:
        raise HTTPException(404, detail="movie not found")
    movie["sources"] = vod_db.list_movie_sources(movie_id)
    movie["placements"] = vod_db.list_movie_placements(movie_id)
    return movie


@router.post("/movies/{movie_id}/sources/", dependencies=_GUARDS)
async def add_movie_source(movie_id: int, body: MovieSourceRequest):
    if not vod_db.get_movie(movie_id):
        raise HTTPException(404, detail="movie not found")
    vod_db.add_movie_source(movie_id, body.provider_id, body.provider_stream_id, body.container_extension)
    return {"ok": True}


@router.delete("/movies/{movie_id}/sources/{source_id}/", dependencies=_GUARDS)
async def delete_movie_source(movie_id: int, source_id: int):
    if not vod_db.get_movie(movie_id):
        raise HTTPException(404, detail="movie not found")
    vod_db.delete_movie_source(movie_id, source_id)
    return {"ok": True}


@router.post("/movies/{movie_id}/sources/{source_id}/move/", dependencies=_GUARDS)
async def move_movie_source(movie_id: int, source_id: int, body: MoveMovieSourceRequest):
    if not vod_db.get_movie(movie_id):
        raise HTTPException(404, detail="movie not found")
    try:
        vod_db.move_movie_source(source_id, movie_id, body.target_movie_id)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))
    return {"ok": True}


@router.post("/movies/{movie_id}/categories/", dependencies=_GUARDS)
async def place_movie_in_category(movie_id: int, body: PlacementRequest):
    if not vod_db.get_movie(movie_id):
        raise HTTPException(404, detail="movie not found")
    export_stream_id = vod_db.place_movie_in_category(movie_id, body.category_id)
    return {"export_stream_id": export_stream_id}


@router.delete("/movies/{movie_id}/categories/{category_id}/", dependencies=_GUARDS)
async def remove_movie_from_category(movie_id: int, category_id: int):
    if not vod_db.get_movie(movie_id):
        raise HTTPException(404, detail="movie not found")
    vod_db.remove_movie_from_category(movie_id, category_id)
    return {"ok": True}


@router.post("/movies/{movie_id}/adult/", dependencies=_GUARDS)
async def set_movie_adult(movie_id: int, is_adult: bool):
    if not vod_db.get_movie(movie_id):
        raise HTTPException(404, detail="movie not found")
    vod_db.set_movie_adult(movie_id, is_adult)
    return {"ok": True}


@router.post("/movies/{movie_id}/rename/", dependencies=_GUARDS)
async def rename_movie(movie_id: int, body: RenameRequest):
    try:
        return vod_db.rename_item("movie", movie_id, body.name, body.year)
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 400, detail=str(exc))


@router.post("/movies/{movie_id}/tmdb-id/clear/", dependencies=_GUARDS)
async def clear_movie_tmdb_id(movie_id: int):
    """Manual undo for a wrong tmdb_id (GH issue #6) -- name/year/sources/
    poster untouched, just breaks the bad match so it stops being trusted
    as confirmed. See vod_db.clear_tmdb_id's docstring for what this can't
    fix (an already-executed merge)."""
    try:
        return vod_db.clear_tmdb_id("movie", movie_id)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))


@router.post("/movies/{movie_id}/tmdb-title/", dependencies=_GUARDS)
async def apply_tmdb_title_movie(movie_id: int):
    """Manual, one-click 'use TMDB's own title' -- real user request
    2026-07-31. Only ever runs on demand against a single already-confirmed
    tmdb_id (never automatically on import/enrichment, which would risk
    fighting a user's own Title & Metadata Rules or a manual rename with no
    way to opt out). Also adopts TMDB's own release year alongside the
    title -- renaming to TMDB's title while keeping a provider-mislabeled
    year would just create a different kind of inconsistency."""
    movie = vod_db.get_movie(movie_id)
    if not movie:
        raise HTTPException(404, detail="movie not found")
    if not movie.get("tmdb_id"):
        raise HTTPException(400, detail="no confirmed TMDB id for this movie")
    # Real bug found live 2026-07-31 (browser-testing this feature): with no
    # TMDB API key configured, get_tmdb_details_for_ids raises ValueError,
    # which without this went completely unhandled -- a raw 500 instead of
    # the same clean 400 duplicate_tmdb_details already gives for the exact
    # same underlying condition (see its identical except clause above).
    try:
        details = await tmdb_sync.get_tmdb_details_for_ids([movie["tmdb_id"]], "movie")
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    detail = details.get(movie["tmdb_id"], {})
    if not detail.get("title"):
        raise HTTPException(502, detail="TMDB lookup failed or returned no title")
    try:
        result = vod_db.rename_item("movie", movie_id, detail["title"], detail.get("year") or movie["year"])
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 400, detail=str(exc))
    if "merged_into" in result:
        vod_db.backfill_tmdb_id_if_missing("movie", result["merged_into"], movie["tmdb_id"])
    return result


@router.post("/movies/tmdb-title/bulk-apply/", dependencies=_GUARDS)
async def bulk_apply_tmdb_title_movies(after_id: int = 0, limit: int = 100):
    """Batch counterpart to apply_tmdb_title_movie (GH issue #1: catch the
    whole library up in one pass instead of one click per movie). Same
    manual-only, already-confirmed-tmdb_id-only philosophy as the single-item
    version -- still never runs automatically on import/enrichment. Cursor
    paginated (after_id) and bounded per call (default 100) so one request
    can't fan out into an unbounded number of TMDB lookups; the frontend
    loops, passing back the highest id seen, until has_more is false."""
    candidates = await asyncio.to_thread(vod_db.list_movies_with_tmdb_id, after_id, limit)
    if not candidates:
        return {"checked": 0, "renamed": 0, "has_more": False, "last_id": after_id}
    try:
        details = await tmdb_sync.get_tmdb_details_for_ids([c["tmdb_id"] for c in candidates], "movie")
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    renamed = 0
    for c in candidates:
        detail = details.get(c["tmdb_id"], {})
        title = detail.get("title")
        year = detail.get("year") or c["year"]
        if not title or (title == c["name"] and year == c["year"]):
            continue
        try:
            result = vod_db.rename_item("movie", c["id"], title, year)
        except ValueError:
            continue
        if "merged_into" in result:
            vod_db.backfill_tmdb_id_if_missing("movie", result["merged_into"], c["tmdb_id"])
        renamed += 1
    return {"checked": len(candidates), "renamed": renamed, "has_more": len(candidates) == limit, "last_id": candidates[-1]["id"]}


@router.delete("/movies/{movie_id}/", dependencies=_GUARDS)
async def delete_movie(movie_id: int):
    if not vod_db.get_movie(movie_id):
        raise HTTPException(404, detail="movie not found")
    try:
        vod_db.delete_movie(movie_id)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return {"ok": True}


@router.post("/movies/{movie_id}/enrich/", dependencies=_GUARDS)
async def enrich_movie(movie_id: int, force: bool = False):
    if not vod_db.get_movie(movie_id):
        raise HTTPException(404, detail="movie not found")
    fetched = await vod_importer.enrich_movie(movie_id, force=force)
    return {"fetched": fetched, "movie": vod_db.get_movie(movie_id)}


# ── Series ───────────────────────────────────────────────────────────────────

@router.get("/series/", dependencies=_GUARDS)
async def list_series(
    limit: int = 50, offset: int = 0, search: Optional[str] = None, category_id: Optional[int] = None,
    provider_id: Optional[int] = None, archived: bool = False,
):
    series = vod_db.list_series(limit=limit, offset=offset, search=search, category_id=category_id, provider_id=provider_id, archived=archived)
    ids = [s["id"] for s in series]
    episodes_by_id   = vod_db.list_episodes_for_series_ids(ids)
    placements_by_id = vod_db.list_series_placements_for_ids(ids)
    episode_ids = [e["id"] for eps in episodes_by_id.values() for e in eps]
    episode_sources_by_id = vod_db.list_episode_sources_for_episode_ids(episode_ids)
    for s in series:
        s["episodes"] = episodes_by_id.get(s["id"], [])
        for e in s["episodes"]:
            e["sources"] = episode_sources_by_id.get(e["id"], [])
        s["placements"] = placements_by_id.get(s["id"], [])
    return {
        "items": series,
        "total": vod_db.count_series(search=search, category_id=category_id, provider_id=provider_id, archived=archived),
        "limit": limit,
        "offset": offset,
    }


@router.post("/series/{series_id}/archive/", dependencies=_GUARDS)
async def set_series_archived(series_id: int, archived: bool):
    if not vod_db.get_series(series_id):
        raise HTTPException(404, detail="series not found")
    vod_db.bulk_set_review_excluded("series", [series_id], archived)
    return {"ok": True}


@router.post("/series/bulk-place/", dependencies=_GUARDS)
async def bulk_place_series(body: BulkPlaceRequest):
    if not vod_db.get_category(body.category_id):
        raise HTTPException(404, detail="category not found")
    ids = body.ids if body.ids is not None else vod_db.list_all_series_ids(search=body.search, category_id=body.source_category_id, provider_id=body.source_provider_id)
    newly_placed = vod_db.bulk_place_series_in_category(ids, body.category_id)
    return {"matched": len(ids), "newly_placed": newly_placed}


@router.post("/series/", dependencies=_GUARDS)
async def upsert_series(body: SeriesRequest):
    fields = body.model_dump(exclude={"name", "year"}, exclude_none=True)
    series_id = vod_db.upsert_series(body.name, body.year, **fields)
    return {"id": series_id}


@router.get("/series/{series_id}/", dependencies=_GUARDS)
async def get_series(series_id: int):
    series = vod_db.get_series(series_id)
    if not series:
        raise HTTPException(404, detail="series not found")
    series["episodes"] = vod_db.list_episodes(series_id)
    episode_sources_by_id = vod_db.list_episode_sources_for_episode_ids([e["id"] for e in series["episodes"]])
    for e in series["episodes"]:
        e["sources"] = episode_sources_by_id.get(e["id"], [])
    series["placements"] = vod_db.list_series_placements_for_ids([series_id]).get(series_id, [])
    return series


@router.post("/series/{series_id}/episodes/", dependencies=_GUARDS)
async def add_episode(series_id: int, body: EpisodeRequest):
    if not vod_db.get_series(series_id):
        raise HTTPException(404, detail="series not found")
    fields = body.model_dump(exclude={"season_number", "episode_number", "name"}, exclude_none=True)
    episode_id = vod_db.add_episode(series_id, body.season_number, body.episode_number, body.name, **fields)
    return {"id": episode_id}


@router.post("/episodes/{episode_id}/sources/", dependencies=_GUARDS)
async def add_episode_source(episode_id: int, body: EpisodeSourceRequest):
    vod_db.add_episode_source(episode_id, body.provider_id, body.provider_stream_id, body.container_extension)
    return {"ok": True}


@router.delete("/episodes/{episode_id}/sources/{source_id}/", dependencies=_GUARDS)
async def delete_episode_source(episode_id: int, source_id: int):
    vod_db.delete_episode_source(episode_id, source_id)
    return {"ok": True}


@router.post("/episodes/{episode_id}/sources/{source_id}/move/", dependencies=_GUARDS)
async def move_episode_source(episode_id: int, source_id: int, body: MoveEpisodeSourceRequest):
    try:
        target_episode_id = vod_db.move_episode_source(
            source_id, episode_id, body.target_series_id, body.season_number, body.episode_number, body.name,
        )
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))
    return {"ok": True, "target_episode_id": target_episode_id}


@router.get("/series/{series_id}/failing-sources/", dependencies=_GUARDS)
async def get_failing_episode_sources(series_id: int):
    if not vod_db.get_series(series_id):
        raise HTTPException(404, detail="series not found")
    return vod_db.list_failing_episode_sources_for_series(series_id)


@router.delete("/series/{series_id}/sources/by-provider/{provider_id}/", dependencies=_GUARDS)
async def remove_series_provider_sources(series_id: int, provider_id: int):
    if not vod_db.get_series(series_id):
        raise HTTPException(404, detail="series not found")
    removed = vod_db.remove_provider_sources_from_series(series_id, provider_id)
    return {"removed": removed}


@router.post("/series/{series_id}/categories/", dependencies=_GUARDS)
async def place_series_in_category(series_id: int, body: PlacementRequest):
    if not vod_db.get_series(series_id):
        raise HTTPException(404, detail="series not found")
    export_series_id = vod_db.place_series_in_category(series_id, body.category_id)
    return {"export_series_id": export_series_id}


@router.delete("/series/{series_id}/categories/{category_id}/", dependencies=_GUARDS)
async def remove_series_from_category(series_id: int, category_id: int):
    if not vod_db.get_series(series_id):
        raise HTTPException(404, detail="series not found")
    vod_db.remove_series_from_category(series_id, category_id)
    return {"ok": True}


@router.post("/series/{series_id}/adult/", dependencies=_GUARDS)
async def set_series_adult(series_id: int, is_adult: bool):
    if not vod_db.get_series(series_id):
        raise HTTPException(404, detail="series not found")
    vod_db.set_series_adult(series_id, is_adult)
    return {"ok": True}


@router.post("/series/{series_id}/rename/", dependencies=_GUARDS)
async def rename_series(series_id: int, body: RenameRequest):
    try:
        return vod_db.rename_item("series", series_id, body.name, body.year)
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 400, detail=str(exc))


@router.post("/series/{series_id}/tmdb-id/clear/", dependencies=_GUARDS)
async def clear_series_tmdb_id(series_id: int):
    """See clear_movie_tmdb_id's identical docstring -- same reasoning."""
    try:
        return vod_db.clear_tmdb_id("series", series_id)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))


@router.post("/series/{series_id}/tmdb-title/", dependencies=_GUARDS)
async def apply_tmdb_title_series(series_id: int):
    """See apply_tmdb_title_movie's identical docstring -- same reasoning."""
    series = vod_db.get_series(series_id)
    if not series:
        raise HTTPException(404, detail="series not found")
    if not series.get("tmdb_id"):
        raise HTTPException(400, detail="no confirmed TMDB id for this series")
    try:
        details = await tmdb_sync.get_tmdb_details_for_ids([series["tmdb_id"]], "series")
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    detail = details.get(series["tmdb_id"], {})
    if not detail.get("title"):
        raise HTTPException(502, detail="TMDB lookup failed or returned no title")
    try:
        result = vod_db.rename_item("series", series_id, detail["title"], detail.get("year") or series["year"])
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 400, detail=str(exc))
    if "merged_into" in result:
        vod_db.backfill_tmdb_id_if_missing("series", result["merged_into"], series["tmdb_id"])
    return result


@router.post("/series/tmdb-title/bulk-apply/", dependencies=_GUARDS)
async def bulk_apply_tmdb_title_series(after_id: int = 0, limit: int = 100):
    """See bulk_apply_tmdb_title_movies' identical docstring -- same reasoning."""
    candidates = await asyncio.to_thread(vod_db.list_series_with_tmdb_id, after_id, limit)
    if not candidates:
        return {"checked": 0, "renamed": 0, "has_more": False, "last_id": after_id}
    try:
        details = await tmdb_sync.get_tmdb_details_for_ids([c["tmdb_id"] for c in candidates], "series")
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    renamed = 0
    for c in candidates:
        detail = details.get(c["tmdb_id"], {})
        title = detail.get("title")
        year = detail.get("year") or c["year"]
        if not title or (title == c["name"] and year == c["year"]):
            continue
        try:
            result = vod_db.rename_item("series", c["id"], title, year)
        except ValueError:
            continue
        if "merged_into" in result:
            vod_db.backfill_tmdb_id_if_missing("series", result["merged_into"], c["tmdb_id"])
        renamed += 1
    return {"checked": len(candidates), "renamed": renamed, "has_more": len(candidates) == limit, "last_id": candidates[-1]["id"]}


@router.delete("/series/{series_id}/", dependencies=_GUARDS)
async def delete_series(series_id: int):
    if not vod_db.get_series(series_id):
        raise HTTPException(404, detail="series not found")
    try:
        vod_db.delete_series(series_id)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return {"ok": True}


@router.post("/series/{series_id}/enrich/", dependencies=_GUARDS)
async def enrich_series(series_id: int, force: bool = False):
    if not vod_db.get_series(series_id):
        raise HTTPException(404, detail="series not found")
    result = await vod_importer.enrich_series(series_id, force=force)
    series = vod_db.get_series(series_id)
    series["episodes"] = vod_db.list_episodes(series_id)
    episode_sources_by_id = vod_db.list_episode_sources_for_episode_ids([e["id"] for e in series["episodes"]])
    for e in series["episodes"]:
        e["sources"] = episode_sources_by_id.get(e["id"], [])
    return {"fetched": result["fetched"], "reason": result["reason"], "series": series}


# ── Bulk enrichment ──────────────────────────────────────────────────────────

@router.post("/enrich-all/", dependencies=_GUARDS)
async def enrich_all(force: bool = False, concurrency: int = 8):
    if vod_importer.get_enrich_progress()["running"]:
        raise HTTPException(409, detail="bulk enrichment already running")
    asyncio.create_task(vod_importer.bulk_enrich_all(concurrency=concurrency, force=force))
    return {"started": True}


@router.get("/enrich-all/status/", dependencies=_GUARDS)
async def enrich_all_status():
    return vod_importer.get_enrich_progress()


# ── Metadata rewrite rules ───────────────────────────────────────────────────

@router.get("/metadata-rules/", dependencies=_GUARDS)
async def list_metadata_rules(content_type: Optional[str] = None):
    return vod_db.list_metadata_rules(content_type)


@router.post("/metadata-rules/", dependencies=_GUARDS)
async def create_metadata_rule(body: MetadataRuleRequest):
    if body.content_type not in ("movie", "series", "both"):
        raise HTTPException(400, detail="content_type must be 'movie', 'series', or 'both'")
    if body.field not in vod_db.REWRITABLE_FIELDS:
        raise HTTPException(400, detail=f"field must be one of {vod_db.REWRITABLE_FIELDS}")
    # Only meaningful for is_regex=True -- a literal pattern is always
    # re.escape()'d before use, so it can never fail to compile. This only
    # catches a syntactically broken regex; it was never able to catch (and
    # still can't) a syntactically VALID but semantically wrong one, like
    # an unescaped "|" -- that's what preview_metadata_rule is for.
    if body.is_regex:
        import re
        try:
            re.compile(body.pattern)
        except re.error as exc:
            raise HTTPException(400, detail=f"invalid regex: {exc}")
    rule_id = vod_db.create_metadata_rule(
        body.content_type, body.field, body.pattern, body.replacement, body.sort_order, body.is_regex,
    )
    return {"id": rule_id}


@router.post("/metadata-rules/preview/", dependencies=_GUARDS)
async def preview_metadata_rule(body: MetadataRulePreviewRequest):
    if body.content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    if body.field not in vod_db.REWRITABLE_FIELDS:
        raise HTTPException(400, detail=f"field must be one of {vod_db.REWRITABLE_FIELDS}")
    return vod_db.preview_metadata_rule(body.content_type, body.field, body.pattern, body.replacement, body.is_regex)


@router.post("/metadata-rules/{rule_id}/active/", dependencies=_GUARDS)
async def set_metadata_rule_active(rule_id: int, is_active: bool):
    vod_db.set_metadata_rule_active(rule_id, is_active)
    return {"ok": True}


@router.delete("/metadata-rules/{rule_id}/", dependencies=_GUARDS)
async def delete_metadata_rule(rule_id: int):
    vod_db.delete_metadata_rule(rule_id)
    return {"ok": True}


@router.post("/metadata-rules/apply/", dependencies=_GUARDS)
async def apply_metadata_rules(content_type: str, force: bool = False):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    # Same event-loop-blocking shape as scan_duplicates/merge_confirmed_duplicates
    # (2026-07-31) -- measured 1.3s+ against this instance's real 247K-movie
    # pool even with zero changes to write; a real run with thousands of
    # writes would be worse. Small next to the other two, but the same
    # anti-pattern, so offloaded the same way rather than left as a known gap.
    return await asyncio.to_thread(vod_db.apply_metadata_rules_to_pool, content_type, force)

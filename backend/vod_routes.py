import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config import get_tmdb_api_key, get_vod_xc_account_id, save_tmdb_api_key, save_vod_xc_account_id
from routes import require_auth, require_configured
import emby_vod_importer
import plex_importer
import tmdb_sync
import vod_db
import vod_importer
import vod_sync
from xc_server import get_active_sessions

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/vod", tags=["vod-manager"])

_GUARDS = [Depends(require_configured), Depends(require_auth)]

vod_db.init_db()


# ── Request models ──────────────────────────────────────────────────────────

class VodSettingsRequest(BaseModel):
    xc_account_id: int


class TmdbApiKeyRequest(BaseModel):
    api_key: str


class XcClientRequest(BaseModel):
    label: str
    ip_allowlist: Optional[str] = None


class XcClientUpdateRequest(BaseModel):
    label: Optional[str] = None
    enabled: Optional[bool] = None
    ip_allowlist: Optional[str] = None
    clear_ip_allowlist: bool = False


class MetadataRuleRequest(BaseModel):
    content_type: str  # 'movie', 'series', or 'both'
    field: str
    pattern: str
    replacement: str = ""
    sort_order: int = 0


class ProviderRequest(BaseModel):
    name: str
    base_url: str
    username: str
    password: str
    max_streams: int = 0
    priority: int = 0
    provider_type: str = "xc"


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


class PlacementRequest(BaseModel):
    category_id: int


class BulkPlaceRequest(BaseModel):
    category_id: int
    ids: Optional[list[int]] = None
    search: Optional[str] = None
    source_category_id: Optional[int] = None
    source_provider_id: Optional[int] = None


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


# ── Settings ─────────────────────────────────────────────────────────────────

@router.get("/settings/", dependencies=_GUARDS)
async def get_vod_settings():
    return {"xc_account_id": get_vod_xc_account_id()}


@router.post("/settings/", dependencies=_GUARDS)
async def save_vod_settings(body: VodSettingsRequest):
    save_vod_xc_account_id(body.xc_account_id)
    return {"ok": True}


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


@router.get("/activity/", dependencies=_GUARDS)
async def list_activity():
    """Currently open VOD stream relays — in-memory only, cleared on
    restart, same as the underlying session tracking in xc_server.py."""
    return get_active_sessions()


@router.get("/tmdb-settings/", dependencies=_GUARDS)
async def get_tmdb_settings():
    return {"has_api_key": bool(get_tmdb_api_key())}


@router.post("/tmdb-settings/", dependencies=_GUARDS)
async def save_tmdb_settings(body: TmdbApiKeyRequest):
    save_tmdb_api_key(body.api_key)
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
    password = body.password.strip()
    if not password:
        existing = next((p for p in vod_db.list_providers() if p["name"] == body.name), None)
        password = existing["password"] if existing else ""
    provider_id = vod_db.upsert_provider(
        body.name, body.base_url, body.username, password, body.max_streams, body.priority, body.provider_type,
    )

    sync_error = None
    try:
        await vod_sync.sync_provider(provider_id)
    except vod_sync.VodXcAccountNotConfigured:
        sync_error = "vod_xc_account_id not configured — profile not synced to Dispatcharr"
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


@router.post("/providers/{provider_id}/connection-sharing/", dependencies=_GUARDS)
async def set_provider_connection_sharing(
    provider_id: int, dispatcharr_live_account_id: Optional[int] = None, shared_connection_limit: Optional[int] = None,
):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_connection_sharing(provider_id, dispatcharr_live_account_id, shared_connection_limit)
    return {"ok": True}


@router.post("/providers/{provider_id}/user-agent/", dependencies=_GUARDS)
async def set_provider_custom_user_agent(provider_id: int, custom_user_agent: Optional[str] = None):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    vod_db.set_provider_custom_user_agent(provider_id, custom_user_agent.strip() if custom_user_agent else None)
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
    vod_db.delete_provider(provider_id)
    return {"ok": True}


@router.post("/providers/{provider_id}/sync/", dependencies=_GUARDS)
async def sync_provider(provider_id: int):
    if not vod_db.get_provider(provider_id):
        raise HTTPException(404, detail="provider not found")
    try:
        profile = await vod_sync.sync_provider(provider_id)
    except vod_sync.VodXcAccountNotConfigured as exc:
        raise HTTPException(400, detail=str(exc))
    return {"dispatcharr_profile": profile}


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
        else:
            result = await vod_importer.import_provider_catalog(provider_id)
    except Exception as exc:
        logger.error("[vod_routes] import_provider_catalog(%s) failed: %s", provider_id, exc)
        raise HTTPException(502, detail=str(exc))
    return result


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


@router.post("/needs-review/{content_type}/{item_id}/resolve/", dependencies=_GUARDS)
async def resolve_year_review(content_type: str, item_id: int, body: ResolveYearReviewRequest):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    try:
        return vod_db.resolve_year_review(content_type, item_id, body.year, body.tmdb_id)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))


# ── Movies ───────────────────────────────────────────────────────────────────

@router.get("/movies/", dependencies=_GUARDS)
async def list_movies(
    limit: int = 50, offset: int = 0, search: Optional[str] = None, category_id: Optional[int] = None,
    provider_id: Optional[int] = None,
):
    movies = vod_db.list_movies(limit=limit, offset=offset, search=search, category_id=category_id, provider_id=provider_id)
    ids = [m["id"] for m in movies]
    sources_by_id    = vod_db.list_movie_sources_for_ids(ids)
    placements_by_id = vod_db.list_movie_placements_for_ids(ids)
    for m in movies:
        m["sources"]    = sources_by_id.get(m["id"], [])
        m["placements"] = placements_by_id.get(m["id"], [])
    return {
        "items": movies,
        "total": vod_db.count_movies(search=search, category_id=category_id, provider_id=provider_id),
        "limit": limit,
        "offset": offset,
    }


@router.post("/movies/bulk-place/", dependencies=_GUARDS)
async def bulk_place_movies(body: BulkPlaceRequest):
    if not vod_db.get_category(body.category_id):
        raise HTTPException(404, detail="category not found")
    ids = body.ids if body.ids is not None else vod_db.list_all_movie_ids(search=body.search, category_id=body.source_category_id, provider_id=body.source_provider_id)
    newly_placed = vod_db.bulk_place_movies_in_category(ids, body.category_id)
    return {"matched": len(ids), "newly_placed": newly_placed}


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


@router.delete("/movies/{movie_id}/", dependencies=_GUARDS)
async def delete_movie(movie_id: int):
    if not vod_db.get_movie(movie_id):
        raise HTTPException(404, detail="movie not found")
    vod_db.delete_movie(movie_id)
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
    provider_id: Optional[int] = None,
):
    series = vod_db.list_series(limit=limit, offset=offset, search=search, category_id=category_id, provider_id=provider_id)
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
        "total": vod_db.count_series(search=search, category_id=category_id, provider_id=provider_id),
        "limit": limit,
        "offset": offset,
    }


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


@router.delete("/series/{series_id}/", dependencies=_GUARDS)
async def delete_series(series_id: int):
    if not vod_db.get_series(series_id):
        raise HTTPException(404, detail="series not found")
    vod_db.delete_series(series_id)
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
    import re
    try:
        re.compile(body.pattern)
    except re.error as exc:
        raise HTTPException(400, detail=f"invalid regex: {exc}")
    rule_id = vod_db.create_metadata_rule(body.content_type, body.field, body.pattern, body.replacement, body.sort_order)
    return {"id": rule_id}


@router.post("/metadata-rules/{rule_id}/active/", dependencies=_GUARDS)
async def set_metadata_rule_active(rule_id: int, is_active: bool):
    vod_db.set_metadata_rule_active(rule_id, is_active)
    return {"ok": True}


@router.delete("/metadata-rules/{rule_id}/", dependencies=_GUARDS)
async def delete_metadata_rule(rule_id: int):
    vod_db.delete_metadata_rule(rule_id)
    return {"ok": True}


@router.post("/metadata-rules/apply/", dependencies=_GUARDS)
async def apply_metadata_rules(content_type: str):
    if content_type not in ("movie", "series"):
        raise HTTPException(400, detail="content_type must be 'movie' or 'series'")
    return vod_db.apply_metadata_rules_to_pool(content_type)

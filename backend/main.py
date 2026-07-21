import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backup import router as backup_router
from config import get_last_enrichment_run, save_last_enrichment_run
import emby_vod_importer
import plex_importer
from routes import router
import tmdb_sync
import vod_db
import vod_importer
from vod_routes import router as vod_router
from xc_server import hls_sweep_loop, router as xc_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

_PASSWORD_QS_RE = re.compile(r"(password=)[^&\s\"]*")
# xc_server.py's stream/preview routes are all /.../{username}/{password}/...
# -- the XC protocol's own convention (client library requires it in the
# URL), not ours. vod_db._generate_xc_username always produces "vm-" + 8 hex
# chars, which is specific enough to match the password segment that
# immediately follows without needing to enumerate every route prefix here.
_PASSWORD_PATH_RE = re.compile(r"(/vm-[0-9a-f]{8}/)[^/\s\"]+(/)")


class _RedactPasswordFilter(logging.Filter):
    """xc_server's XC-protocol auth puts the password in the URL query string
    or path (the client library's own convention, not ours) -- uvicorn's
    built-in access log otherwise writes that raw URL, password included,
    straight to stdout/container logs on every request."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                _PASSWORD_PATH_RE.sub(r"\1***\2", _PASSWORD_QS_RE.sub(r"\1***", arg)) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactPasswordFilter())

logger     = logging.getLogger("vod_manager")
STATIC_DIR = Path(__file__).parent / "static"

_CATALOG_REFRESH_POLL_SECONDS = 300


async def _vod_catalog_refresher() -> None:
    """Background task: periodically re-imports each active VOD provider's
    catalog so new titles show up without a manual 'Import catalog' click.
    Each provider_type has its own configurable interval (Settings -> Refresh
    Schedule) -- a Plex/Emby library scan can take 18+ minutes of real disk
    I/O, so forcing it onto the same cadence as a cheap XC catalog pull
    either starves XC providers waiting on Plex's schedule or rescans
    Plex/Emby far more often than needed. This polls every
    _CATALOG_REFRESH_POLL_SECONDS and refreshes whichever providers are
    actually due (tracked per-provider via last_catalog_refresh_at), rather
    than looping every active provider on one shared sleep."""
    await asyncio.sleep(15)
    while True:
        try:
            providers = [p for p in await asyncio.to_thread(vod_db.list_providers) if p["is_active"]]
            now = time.time()
            due = [
                p for p in providers
                if now - float(p.get("last_catalog_refresh_at") or 0)
                   >= vod_db.get_catalog_refresh_interval_seconds(p.get("provider_type", "xc"))
            ]
            if due:
                logger.info("[vod_catalog_refresher] refreshing %d of %d active provider(s)…", len(due), len(providers))
                for p in due:
                    try:
                        if p.get("provider_type") == "plex":
                            result = await plex_importer.import_plex_library(p["id"])
                        elif p.get("provider_type") in ("emby", "jellyfin"):
                            result = await emby_vod_importer.import_emby_library(p["id"])
                        else:
                            result = await vod_importer.import_provider_catalog(p["id"])
                        await asyncio.to_thread(vod_db.mark_provider_catalog_refreshed, p["id"])
                        logger.info("[vod_catalog_refresher] %s: %s", p["name"], result)
                    except Exception as exc:
                        logger.warning("[vod_catalog_refresher] provider=%s failed: %s", p["name"], exc)
        except Exception as exc:
            logger.warning("[vod_catalog_refresher] cycle failed: %s", exc)

        await asyncio.sleep(_CATALOG_REFRESH_POLL_SECONDS)


async def _vod_enrichment_scheduler() -> None:
    """Background task: periodically runs bulk_enrich_all so newly-imported or
    stale (past ENRICHMENT_TTL_SECONDS) items get enriched without a manual
    click. Cheap to run on this interval — anything still fresh is skipped
    by enrich_movie/enrich_series's own TTL check, so most runs are a no-op
    scan rather than a real re-fetch.

    The due time is anchored to the last real run (persisted in config), not
    to when this process happened to start — otherwise every container
    restart resets the clock and fires a full pass ~45s later regardless of
    how recently it last ran, which is exactly the kind of background write
    load that competes with anything else the app is doing at that moment."""
    last_run = get_last_enrichment_run()
    if last_run is not None:
        due_in = vod_db.get_enrichment_ttl_seconds() - (time.time() - last_run)
        await asyncio.sleep(max(due_in, 45))
    else:
        await asyncio.sleep(45)
    while True:
        try:
            await vod_importer.bulk_enrich_all()
            save_last_enrichment_run(time.time())
        except Exception as exc:
            logger.warning("[vod_enrichment_scheduler] run failed: %s", exc)
        await asyncio.sleep(vod_db.get_enrichment_ttl_seconds())


_TMDB_SYNC_DISABLED_POLL_SECONDS = 300


async def _tmdb_sync_scheduler() -> None:
    """Background task: periodically re-syncs every category with a TMDB
    Lists sync_source configured (see tmdb_sync.py). Disabled by default
    (Settings -> Refresh Schedule) -- this is new background API traffic
    that didn't run at all before this was exposed, so it's opt-in rather
    than silently started for existing deployments. Re-checks whether it's
    been turned on every _TMDB_SYNC_DISABLED_POLL_SECONDS while disabled."""
    while True:
        interval = vod_db.get_tmdb_sync_interval_seconds()
        if not interval:
            await asyncio.sleep(_TMDB_SYNC_DISABLED_POLL_SECONDS)
            continue
        try:
            results = await tmdb_sync.sync_all()
            if results:
                logger.info("[tmdb_sync_scheduler] synced %d categor(y/ies): %s", len(results), results)
        except Exception as exc:
            logger.warning("[tmdb_sync_scheduler] run failed: %s", exc)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("VOD Manager started")
    tasks = [
        asyncio.create_task(_vod_catalog_refresher()),
        asyncio.create_task(_vod_enrichment_scheduler()),
        asyncio.create_task(_tmdb_sync_scheduler()),
        asyncio.create_task(hls_sweep_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="VOD Manager", version="0.1.00", lifespan=lifespan)
app.include_router(router)
app.include_router(vod_router)
app.include_router(xc_router)
app.include_router(backup_router)

if os.environ.get("VODMANAGER_TEST_UPSTREAM"):
    from test_upstream import router as test_upstream_router
    logger.warning("[main] VODMANAGER_TEST_UPSTREAM set — fake upstream test router is mounted")
    app.include_router(test_upstream_router)

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")
    # Bundled placeholder art (e.g. a properly poster-shaped logo for
    # bulk-applying to content that will never have a real per-title
    # poster) -- see frontend/public/placeholders/.
    if (STATIC_DIR / "placeholders").exists():
        app.mount("/placeholders", StaticFiles(directory=str(STATIC_DIR / "placeholders")), name="placeholders")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        return FileResponse(str(STATIC_DIR / "index.html"))

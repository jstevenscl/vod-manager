import asyncio
import logging
import os
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
import vod_db
import vod_importer
from vod_routes import router as vod_router
from xc_server import router as xc_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

logger     = logging.getLogger("vod_manager")
STATIC_DIR = Path(__file__).parent / "static"

_VOD_CATALOG_REFRESH_INTERVAL_SECONDS = 6 * 3600


async def _vod_catalog_refresher() -> None:
    """Background task: periodically re-import every active VOD provider's
    catalog so new titles show up without a manual 'Import catalog' click."""
    await asyncio.sleep(15)
    while True:
        try:
            providers = [p for p in await asyncio.to_thread(vod_db.list_providers) if p["is_active"]]
            if providers:
                logger.info("[vod_catalog_refresher] refreshing %d provider(s)…", len(providers))
                for p in providers:
                    try:
                        if p.get("provider_type") == "plex":
                            result = await plex_importer.import_plex_library(p["id"])
                        elif p.get("provider_type") in ("emby", "jellyfin"):
                            result = await emby_vod_importer.import_emby_library(p["id"])
                        else:
                            result = await vod_importer.import_provider_catalog(p["id"])
                        logger.info("[vod_catalog_refresher] %s: %s", p["name"], result)
                    except Exception as exc:
                        logger.warning("[vod_catalog_refresher] provider=%s failed: %s", p["name"], exc)
        except Exception as exc:
            logger.warning("[vod_catalog_refresher] cycle failed: %s", exc)

        logger.info("[vod_catalog_refresher] next refresh in %.0f min", _VOD_CATALOG_REFRESH_INTERVAL_SECONDS / 60)
        await asyncio.sleep(_VOD_CATALOG_REFRESH_INTERVAL_SECONDS)


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
        due_in = vod_db.ENRICHMENT_TTL_SECONDS - (time.time() - last_run)
        await asyncio.sleep(max(due_in, 45))
    else:
        await asyncio.sleep(45)
    while True:
        try:
            await vod_importer.bulk_enrich_all()
            save_last_enrichment_run(time.time())
        except Exception as exc:
            logger.warning("[vod_enrichment_scheduler] run failed: %s", exc)
        await asyncio.sleep(vod_db.ENRICHMENT_TTL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("VOD Manager started")
    tasks = [
        asyncio.create_task(_vod_catalog_refresher()),
        asyncio.create_task(_vod_enrichment_scheduler()),
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

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        return FileResponse(str(STATIC_DIR / "index.html"))

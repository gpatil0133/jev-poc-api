"""Sogo-lite: a local stand-in for the Sogolytics Design module, used to test the
eight Design classification scopes against the classification gateway.

    uvicorn sogo_lite.app:app --port 8020        (run from the ui/ directory)

See docs/sogo-lite-mock-platform-plan.md.
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sogo_lite import db, modules, seed
from sogo_lite.config import TOKEN_ENV, gateway_token, get_settings
from sogo_lite.routes import design, participate, tools
from sogo_lite.web import PACKAGE_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx logs every request line at INFO; the Inspector is the record of gateway calls.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _sync_seeded_class_sets() -> None:
    try:
        counts = seed.sync_class_sets()
        if counts["saved"] or counts["failed"]:
            logger.info("Class sets saved to the gateway: %s, failed: %s", counts["saved"], counts["failed"])
    except Exception:
        logger.exception("Could not save the class sets to the gateway at start-up")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db.init()
    modules.load()
    seeded = seed.seed_if_empty()
    logger.info("Sogo-lite ready: gateway=%s db=%s seeded=%s", settings.gateway_base_url, settings.db_file, seeded)
    if gateway_token():
        # Off the start-up path: the app must come up even when the gateway is unreachable.
        threading.Thread(target=_sync_seeded_class_sets, name="class-set-sync", daemon=True).start()
    else:
        logger.warning("%s is not set: every gateway call will fall back with reason no_token", TOKEN_ENV)
    yield


app = FastAPI(title="Sogo-lite mock platform", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
app.include_router(design.router)
app.include_router(participate.router)
app.include_router(tools.router)

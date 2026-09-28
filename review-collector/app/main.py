"""Application entry point.

Starting this one process gives you: the database, the REST API for A3, the
admin dashboard, and the automatic 15-minute scheduler. Nothing else to run.

    uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import hashlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from app.api import admin, health, reviews
from app.assets import asset_version as _asset_version
from app.collector.registry import get_backend_status
from app.config import BASE_DIR, get_settings
from app.database.database import close_orphaned_checks, init_db, sync_businesses_from_config
from app.logging_setup import setup_logging
from app.scheduler.scheduler import get_scheduler, reset_scheduler

logger = logging.getLogger("main")

DASHBOARD_DIR = BASE_DIR / "dashboard"


class NoCacheStaticFiles(StaticFiles):
    """Serve the dashboard without browser caching.

    The dashboard is three small files fetched from localhost, so caching buys
    nothing -- and a stale cached app.js against a fresh index.html silently
    breaks the page (buttons render but do nothing). Correctness over a few
    saved kilobytes.
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings)

    logger.info("=" * 70)
    logger.info("A3 Review Collector starting")
    logger.info("=" * 70)

    init_db()
    close_orphaned_checks()
    sync_businesses_from_config()
    logger.info("Database ready at %s", settings.database_url)

    if not settings.a3_api_key:
        logger.error(
            "A3_API_KEY is NOT set. Every API request will be rejected with HTTP 503. "
            "Generate one with: python -c \"import secrets;print(secrets.token_urlsafe(32))\""
        )

    for backend in get_backend_status(settings):
        marker = "SELECTED" if backend["selected"] else "        "
        state = "available" if backend["available"] else "UNAVAILABLE"
        logger.info("Backend %-10s [%s] %-11s -- %s", backend["name"], marker, state, backend["reason"])

    scheduler = get_scheduler()
    if settings.scheduler_enabled:
        scheduler.start()
    else:
        logger.warning("SCHEDULER_ENABLED=false -- automatic checks are OFF.")

    logger.info("Dashboard:  http://%s:%d/", settings.host, settings.port)
    logger.info("Health:     http://%s:%d/health", settings.host, settings.port)
    logger.info("A3 API:     http://%s:%d/api/reviews/new", settings.host, settings.port)

    try:
        yield
    finally:
        logger.info("Shutting down; stopping scheduler")
        reset_scheduler()


app = FastAPI(
    title="A3 Review Collector",
    description=(
        "Collects new Google Business Profile reviews for BMW of Fort Walton Beach and "
        "Mercedes-Benz of Fort Walton Beach and serves them to the A3 Review Responder. "
        "This service does not generate responses."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # API-key protected; the dashboard is served same-origin.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(reviews.router)
app.include_router(admin.router)


@app.get("/", include_in_schema=False)
def dashboard():
    index = DASHBOARD_DIR / "index.html"
    if not index.exists():
        return JSONResponse({"detail": "Dashboard not found."}, status_code=404)

    html = index.read_text(encoding="utf-8")
    version = _asset_version()
    html = re.sub(r"/static/app\.js(\?v=[a-f0-9]+)?", f"/static/app.js?v={version}", html)
    html = re.sub(r"/static/style\.css(\?v=[a-f0-9]+)?", f"/static/style.css?v={version}", html)

    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


if DASHBOARD_DIR.exists():
    app.mount("/static", NoCacheStaticFiles(directory=str(DASHBOARD_DIR)), name="static")

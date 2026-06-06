"""FastAPI app entry point for fc-inventory v3.0.0.

Wires up:
  - structured logging (structlog + rotating file)
  - a shared `httpx.AsyncClient` for the FC client pool
  - a `JobManager` singleton for the single-user collection pipeline
  - CORS (opt-in via settings)
  - Jinja2 templates (moved into `app/templates/`)
  - the static assets at `/static`
  - exception handlers that match v1.0.0's error JSON shape
  - the routes in `app.api.routes_pages` and `app.api.routes_api`
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .api import routes_api, routes_pages
from .core.config import Settings, get_settings
from .core.jobs import JobManager
from .core.logging import configure_logging

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Set up the shared httpx pool, the JobManager, and the logger.

    Mirrors the single-user localhost tool design: one FC HTTP pool per
    process, one JobManager per process. Multiple workers (uvicorn
    `--workers N`) will each have their own.
    """
    settings: Settings = get_settings()
    configure_logging(
        log_file=settings.log_file,
        log_level=settings.log_level,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )

    # Shared httpx pool. `verify=False` matches the v1.0.0 behaviour
    # (FC ships with self-signed certs); see fc_client.py docstring.
    app.state.http = httpx.AsyncClient(
        verify=False,
        timeout=settings.request_timeout_seconds,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json;version=v1.0;charset=UTF-8",
        },
    )
    app.state.jobs = JobManager()
    app.state.version = __version__
    # Templates directory is `app/templates` relative to the repo root.
    app.state.templates = Jinja2Templates(directory="app/templates")

    logger.info(
        "startup",
        version=__version__,
        bind=settings.bind,
        port=settings.port,
        log_level=settings.log_level,
    )
    try:
        yield
    finally:
        logger.info("shutdown")
        await app.state.http.aclose()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="FC Inventory Tool",
        version=__version__,
        description=(
            "Web-based Huawei FusionCompute inventory collector with multi-sheet "
            "Excel export (RVTools-style)."
        ),
        contact={
            "name": "FC Inventory",
            "url": "https://github.com/sukritphiboon/fc-inventory",
        },
        license_info={"name": "MIT"},
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS: only enabled when `FC_INVENTORY_CORS_ORIGINS` is non-empty.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    # Static assets (CSS / JS / screenshots). Mounted at /static.
    static_dir = Path("static")
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Routers.
    app.include_router(routes_pages.router)
    app.include_router(routes_api.router)

    # ── Exception handlers (mirror v1.0.0's JSON error shape) ──

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": "Invalid request body.",
                "detail": str(exc.errors()),
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled.exception", path=str(request.url))
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error.", "detail": str(exc)},
        )

    return app


# Uvicorn / `python -m app.main` entrypoint.
app = create_app()

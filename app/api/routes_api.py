"""JSON API routes: `/api/collect`, `/api/progress`, `/api/cancel`,
`/api/download`, `/api/version`, `/api/changelog`, `/api/health`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import FileResponse, PlainTextResponse

from .. import __version__
from ..core.collector import InventoryCollector
from ..core.config import Settings
from ..core.excel_builder import build_excel
from ..core.jobs import Job, JobAlreadyRunning, JobManager
from ..models.requests import CollectRequest
from ..models.responses import (
    CancelResponse,
    CollectResponse,
    HealthResponse,
    ProgressResponse,
    VersionResponse,
)
from .deps import get_job_manager, get_settings_dep

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["api"])


# ── /api/collect ───────────────────────────────────────────


@router.post(
    "/collect",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CollectResponse,
    responses={
        202: {"description": "Collection started; poll /api/progress."},
        400: {"description": "Invalid request body."},
        409: {"description": "A collection is already in progress."},
    },
)
async def collect(
    request: Request,
    body: CollectRequest,
    background_tasks: BackgroundTasks,
    jm: Annotated[JobManager, Depends(get_job_manager)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> CollectResponse:
    """Start a new inventory collection. Runs the pipeline in the background."""
    try:
        job = await jm.try_start()
    except JobAlreadyRunning as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "A collection is already in progress.", "job_id": exc.job_id},
        ) from exc

    # Build the FC client and attach the shared httpx pool from the lifespan.
    collector = InventoryCollector(
        host=body.host,
        username=body.username,
        password=body.password.get_secret_value(),
        port=body.port,
    )
    collector.attach(request.app.state.http)
    job.collector = collector

    # Run the actual collection after returning 202.
    background_tasks.add_task(_run_and_finalise, job, collector, jm, settings)
    logger.info("api.collect.started", job_id=job.id, host=body.host, port=body.port)
    return CollectResponse(status="started", job_id=job.id)


async def _run_and_finalise(
    job: Job,
    collector: InventoryCollector,
    jm: JobManager,
    settings: Settings,
) -> None:
    """Background task: run the collector, write the .xlsx, and finalise the job state.

    Mirrors the try/except flow at app.py:78-82 of v1.0.0 (success /
    InterruptedError / generic Exception), with `asyncio.to_thread` for
    the sync `build_excel` call (openpyxl is sync and CPU-bound).
    """
    try:
        data = await collector.collect_all()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = Path(settings.output_dir) / f"FC_Inventory_{ts}.xlsx"
        # Ensure output dir exists, offload openpyxl to a thread.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(build_excel, data, out_path)
        await jm.finish(job, output_file=out_path, error=None)
        logger.info("api.collect.done", job_id=job.id, output=str(out_path))
    except InterruptedError:
        await jm.finish(job, output_file=None, error=None)
        logger.info("api.collect.cancelled", job_id=job.id)
    except Exception as exc:
        logger.exception("api.collect.error", job_id=job.id)
        await jm.finish(job, output_file=None, error=str(exc))


# ── /api/progress ──────────────────────────────────────────


@router.get(
    "/progress",
    response_model=ProgressResponse,
    responses={200: {"description": "Always 200; status field reflects job state."}},
)
async def progress(
    jm: Annotated[JobManager, Depends(get_job_manager)],
) -> ProgressResponse:
    """Return the current job's progress. Mirrors `GET /api/progress` of v1.0.0."""
    job = jm.current
    if job is None:
        return ProgressResponse(status="idle", percent=0, current_step="", error=None)
    return ProgressResponse(
        status=job.progress.get("status", "idle"),
        percent=job.progress.get("percent", 0),
        current_step=job.progress.get("current_step", ""),
        error=job.progress.get("error"),
    )


# ── /api/cancel ────────────────────────────────────────────


@router.post(
    "/cancel",
    response_model=CancelResponse,
    responses={
        200: {"description": "Cancellation requested, or no job / job already finished."},
    },
)
async def cancel(
    jm: Annotated[JobManager, Depends(get_job_manager)],
) -> CancelResponse:
    """Request cancellation of the current job. Mirrors `POST /api/cancel` of v1.0.0."""
    job = await jm.cancel_current()
    if job is None:
        # No job was running.
        current = jm.current
        if current and current.status in ("done", "cancelled", "error"):
            return CancelResponse(status="already_finished", job_id=current.id)
        return CancelResponse(status="no_job_running", job_id=None)
    return CancelResponse(status="cancelling", job_id=job.id)


# ── /api/download ──────────────────────────────────────────


@router.get(
    "/download",
    responses={
        200: {
            "description": "The produced .xlsx file.",
            "content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}},
        },
        404: {"description": "No finished collection is available for download."},
    },
)
async def download(
    jm: Annotated[JobManager, Depends(get_job_manager)],
) -> FileResponse:
    """Stream the produced .xlsx file. Mirrors `GET /api/download` of v1.0.0."""
    job = jm.current
    if (
        job is None
        or job.status != "done"
        or job.output_file is None
        or not job.output_file.exists()
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "No finished collection is available for download."},
        )
    return FileResponse(
        path=job.output_file,
        filename=job.output_file.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── /api/version ───────────────────────────────────────────


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    """Return the service version. Mirrors `GET /api/version` of v1.0.0."""
    return VersionResponse(version=__version__, api_version="v1")


# ── /api/changelog ──────────────────────────────────────────


@router.get(
    "/changelog",
    response_class=PlainTextResponse,
    responses={
        200: {"content": {"text/plain": {}}},
        404: {"description": "CHANGELOG.md not found."},
        500: {"description": "Failed to read CHANGELOG.md."},
    },
)
async def changelog_text() -> PlainTextResponse:
    """Stream the contents of CHANGELOG.md as `text/plain; charset=utf-8`.

    Mirrors `GET /api/changelog` of v1.0.0.
    """
    path = Path("CHANGELOG.md")
    # File I/O in an async function: offload to a thread so we don't
    # block the event loop on a large CHANGELOG.md.
    import asyncio

    loop = asyncio.get_running_loop()
    exists = await loop.run_in_executor(None, path.exists)
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "CHANGELOG.md not found."},
        )
    try:
        content = await loop.run_in_executor(None, lambda: path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to read CHANGELOG.md.", "detail": str(exc)},
        ) from exc
    return PlainTextResponse(content=content, media_type="text/plain; charset=utf-8")


# ── /api/health ────────────────────────────────────────────


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={200: {"description": "ok: no job. busy: job running. degraded: last job errored."}},
)
async def health(
    jm: Annotated[JobManager, Depends(get_job_manager)],
) -> HealthResponse:
    """Health endpoint for k8s probes / load balancers.

    Returns 200 with `status="busy"` if a job is running, `status="degraded"`
    if the last job ended in error, and `status="ok"` otherwise.
    """
    job = jm.current
    if job is None:
        return HealthResponse(status="ok", job_status=None, job_id=None)
    if job.status == "running":
        return HealthResponse(status="busy", job_status="running", job_id=job.id)
    if job.status == "error":
        return HealthResponse(status="degraded", job_status="error", job_id=job.id)
    return HealthResponse(status="ok", job_status=job.status, job_id=job.id)

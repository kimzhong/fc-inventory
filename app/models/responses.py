"""Pydantic response models for the /api/* routes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CollectResponse(BaseModel):
    """Response from `POST /api/collect`."""

    status: Literal["started"] = "started"
    job_id: str = Field(..., description="Opaque job identifier; use it to poll /api/progress.")


class ProgressResponse(BaseModel):
    """Response from `GET /api/progress`."""

    status: str = Field(
        ...,
        description="idle | running | done | cancelled | error",
        examples=["running"],
    )
    percent: int = Field(..., ge=0, le=100, description="0..100 progress percent.")
    current_step: str = Field(..., description="Human-readable current step description.")
    error: str | None = Field(None, description="Error message when status=error.")


class CancelResponse(BaseModel):
    """Response from `POST /api/cancel`."""

    status: Literal["cancelling", "no_job_running", "already_finished"] = Field(...)
    job_id: str | None = Field(None, description="The job that was cancelled (if any).")


class VersionResponse(BaseModel):
    """Response from `GET /api/version`."""

    version: str = Field(..., description="Service semver (e.g. 3.0.0).")
    api_version: str = Field(default="v1", description="API contract version.")


class HealthResponse(BaseModel):
    """Response from `GET /api/health`."""

    status: Literal["ok", "busy", "degraded"] = Field(
        ...,
        description="ok: no job. busy: a job is running. degraded: last job ended in error.",
    )
    job_status: str | None = Field(None, description="Current job status if any.")
    job_id: str | None = Field(None, description="Current job id if any.")


class ErrorResponse(BaseModel):
    """Standard error envelope for 4xx/5xx responses."""

    error: str = Field(..., description="Short error label.")
    detail: str | None = Field(None, description="Long-form error detail (e.g. validation errors).")

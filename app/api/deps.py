"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request

from ..core.config import Settings, get_settings
from ..core.jobs import JobManager


def get_settings_dep() -> Settings:
    """FastAPI dependency wrapper around `get_settings` (kept for clarity at call sites)."""
    return get_settings()


def get_job_manager(request: Request) -> JobManager:
    """Return the per-process `JobManager` stored on `app.state.jobs` by the lifespan."""
    return request.app.state.jobs

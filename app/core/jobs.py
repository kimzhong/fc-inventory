"""Job state machine for the single-user collection pipeline.

Replaces the `current_job` global dict at `app.py:51-55` of v1.0.0 with
a typed dataclass + a `JobManager` singleton. Single-user, single-job
semantics (matches the v1.0.0 design — only one collection at a time).

State transitions:

    idle -> running -> done        (happy path)
                  -> cancelled   (user cancel)
                  -> error      (anything else)

Cancellation: `Job.cancel()` flips an `asyncio.Event` and also sets
the legacy `collector.cancelled = True` flag (in case the collector
code reads it). The collector checks `cancelled` between resource
fetches.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .collector import InventoryCollector


JobStatus = str  # "idle" | "running" | "done" | "cancelled" | "error"


class JobAlreadyRunning(Exception):
    """Raised by `JobManager.try_start` when a job is already in flight."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"A collection is already in progress (job_id={job_id}).")
        self.job_id = job_id


@dataclass
class Job:
    """A single inventory-collection job.

    Mirrors the fields of the v1.0.0 `current_job` global: id, status,
    progress, output file, error.
    """

    id: str
    status: JobStatus = "idle"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output_file: Path | None = None
    error: str | None = None
    progress: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "idle",
            "current_step": "",
            "percent": 0,
            "error": None,
        }
    )
    collector: InventoryCollector | None = field(default=None, repr=False)
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    # ── Cancellation ────────────────────────────────────

    def cancel(self) -> None:
        """Idempotent. Sets the cancel flag the collector checks."""
        self._cancel_event.set()
        if self.collector is not None:
            self.collector.cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def update_progress(self, percent: int, step: str) -> None:
        """Mirror v1.0.0's `progress["percent"]` / `progress["current_step"]` writes."""
        self.progress["percent"] = percent
        self.progress["current_step"] = step


class JobManager:
    """Process-wide singleton. Serialises state mutations with an `asyncio.Lock`."""

    def __init__(self) -> None:
        self._current: Job | None = None
        self._lock = asyncio.Lock()

    @property
    def current(self) -> Job | None:
        return self._current

    async def try_start(self) -> Job:
        """Atomic: returns the new `Job` or raises `JobAlreadyRunning`."""
        async with self._lock:
            if self._current is not None and self._current.status == "running":
                raise JobAlreadyRunning(self._current.id)
            job = Job(id=uuid.uuid4().hex[:12])
            job.status = "running"
            job.started_at = datetime.now(timezone.utc)
            self._current = job
            return job

    async def finish(
        self,
        job: Job,
        output_file: Path | None,
        error: str | None,
    ) -> None:
        async with self._lock:
            job.finished_at = datetime.now(timezone.utc)
            job.output_file = output_file
            job.error = error
            if error:
                job.status = "error"
            elif job.is_cancelled:
                job.status = "cancelled"
            else:
                job.status = "done"
            job.progress["status"] = job.status
            if error:
                job.progress["error"] = error

    async def clear(self) -> None:
        """Forget a finished job so the user can start a new one."""
        async with self._lock:
            self._current = None

    async def cancel_current(self) -> Job | None:
        async with self._lock:
            if self._current and self._current.status == "running":
                self._current.cancel()
                return self._current
            return None


# Module-level singleton accessor. Wired into routes via FastAPI's
# `Depends(get_job_manager)` in `app.api.deps`.
_job_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    """Return the process-wide `JobManager` (created lazily on first use)."""
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager()
    return _job_manager

"""Tests for the JobManager state machine."""

from __future__ import annotations

import pytest

from app.core.jobs import Job, JobAlreadyRunning, JobManager


@pytest.fixture
def jm() -> JobManager:
    return JobManager()


async def test_try_start_returns_new_job(jm: JobManager) -> None:
    job = await jm.try_start()
    assert job.status == "running"
    assert job.id
    assert job.started_at is not None


async def test_try_start_raises_when_already_running(jm: JobManager) -> None:
    await jm.try_start()
    with pytest.raises(JobAlreadyRunning) as exc_info:
        await jm.try_start()
    assert exc_info.value.job_id


async def test_finish_done(jm: JobManager) -> None:
    job = await jm.try_start()
    await jm.finish(job, output_file=None, error=None)
    assert job.status == "done"
    assert job.progress["status"] == "done"


async def test_finish_error(jm: JobManager) -> None:
    job = await jm.try_start()
    await jm.finish(job, output_file=None, error="something went wrong")
    assert job.status == "error"
    assert job.progress["error"] == "something went wrong"


async def test_finish_cancelled(jm: JobManager) -> None:
    job = await jm.try_start()
    job.cancel()
    await jm.finish(job, output_file=None, error=None)
    assert job.status == "cancelled"


async def test_cancel_current_returns_running_job(jm: JobManager) -> None:
    job = await jm.try_start()
    cancelled = await jm.cancel_current()
    assert cancelled is job
    assert job.is_cancelled


async def test_cancel_current_returns_none_when_idle(jm: JobManager) -> None:
    assert jm.current is None
    assert await jm.cancel_current() is None


async def test_clear(jm: JobManager) -> None:
    await jm.try_start()
    await jm.clear()
    assert jm.current is None
    # Now we can start a new one.
    new = await jm.try_start()
    assert new.status == "running"


def test_job_is_cancelled_idempotent() -> None:
    job = Job(id="abc")
    job.cancel()
    job.cancel()
    assert job.is_cancelled


def test_job_update_progress_mutates_dict() -> None:
    job = Job(id="abc")
    job.update_progress(42, "logging in")
    assert job.progress["percent"] == 42
    assert job.progress["current_step"] == "logging in"

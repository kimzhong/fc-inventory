"""pytest fixtures shared across the test suite."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.jobs import JobManager
from app.core.logging import configure_logging
from app.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a canned FC JSON response from tests/fixtures/."""
    path = FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def make_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    """Build an `httpx.MockTransport` that delegates to `handler`.

    Mirrors what the Go branch used for offline E2E (testdata/mock_fc/).
    """
    return httpx.MockTransport(handler)


@pytest.fixture
def settings():
    """A Settings instance bound to the default test paths.

    Tests that need different settings should call `monkeypatch.setenv`
    or instantiate a fresh Settings directly.
    """
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch):
    """A `TestClient` for the FastAPI app with the lifespan started.

    The shared `app.state.http` is replaced with an `httpx.MockTransport`
    so route tests don't hit the network. The route handlers will call
    `_run_and_finalise` as a background task; for unit tests of routes
    that only need the sync part, the response is already returned
    before the background task runs.
    """
    # Quiet logs during tests.
    configure_logging("fc_inventory.test.log", "WARNING", max_bytes=1024 * 1024, backup_count=1)

    app = create_app()

    # Replace app.state.http AFTER the lifespan has installed it; we use
    # TestClient as a context manager so the lifespan runs and then we
    # swap the transport.
    transport = make_transport(lambda req: httpx.Response(200, json={}))
    with TestClient(app) as client:
        # Build a real AsyncClient backed by the MockTransport so the
        # background pipeline can still call FC client methods.
        async_client = httpx.AsyncClient(
            transport=transport,
            verify=False,
            timeout=10.0,
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )
        # TestClient.__enter__ already ran the lifespan, so app.state.http
        # exists; replace it.
        import asyncio

        # Ensure the lifespan-spawned AsyncClient is closed cleanly on exit.
        original = client.app.state.http
        client.app.state.http = async_client
        # Install a fresh JobManager so tests don't share state.
        client.app.state.jobs = JobManager()
        try:
            yield client, client.app
        finally:
            # Restore + close.
            client.app.state.http = original
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(async_client.aclose())
                loop.close()
            except Exception:
                pass

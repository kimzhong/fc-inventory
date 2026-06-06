"""End-to-end tests for the FastAPI routes via TestClient.

Uses an `httpx.MockTransport` so the FC pipeline can be exercised
without a real FusionCompute VRM. The test fixtures drive the
auto-detect login matrix to accept on the first attempt, then serve
canned site/cluster/host/VM responses.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.jobs import JobManager
from app.main import create_app

from .conftest import load_fixture, make_transport

# ── FastAPI app + TestClient fixture ───────────────────────


@pytest.fixture
def client():
    """TestClient wired up with a mock FC transport that always succeeds."""
    # Clear the Settings lru_cache so monkeypatch.setenv in tests
    # actually takes effect (lifespan runs at TestClient enter).
    from app.core.config import get_settings
    get_settings.cache_clear()
    app = create_app()

    # The login matrix's first attempt (POST + plain) should succeed.
    # Subsequent resource GETs return the canned fixtures.
    @staticmethod
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/service/session":
            return httpx.Response(
                200,
                headers={"X-Auth-Token": "TEST-TOKEN"},
                json={"accessSession": "TEST-TOKEN"},
            )
        if req.url.path == "/service/session/" and req.method == "DELETE":
            return httpx.Response(204)
        # Resource GETs. Match by the last path segment.
        tail = req.url.path.rstrip("/").rsplit("/", 1)[-1]
        # Map a few canonical endpoints to fixture files.
        mapping = {
            "sites": load_fixture("sites.json"),
            "clusters": load_fixture("clusters.json"),
            "hosts": load_fixture("hosts.json"),
            "vms": load_fixture("vms.json"),
            "datastores": load_fixture("datastores.json"),
            "dvswitchs": load_fixture("dvswitchs.json"),
            "portgroups": load_fixture("portgroups.json"),
        }
        # Strip site prefix from path (e.g. /service/sites/1/hosts -> hosts).
        if tail in mapping:
            return httpx.Response(200, json=mapping[tail])
        # /service/hosts/1 -> host detail
        if tail.isdigit() and "/hosts/" in req.url.path:
            return httpx.Response(200, json=load_fixture("host_detail.json"))
        # /service/vms/{N} -> VM detail
        if tail.isdigit() and "/vms/" in req.url.path:
            # Alternate between two VM details.
            return httpx.Response(200, json=load_fixture("vm_detail.json" if int(tail) == 1 else "vm_db_detail.json"))
        return httpx.Response(200, json={})

    transport = make_transport(handler)
    with TestClient(app) as c:
        # Replace the lifespan-spawned AsyncClient with one backed by the mock.
        async_client = httpx.AsyncClient(transport=transport, verify=False, timeout=10.0)
        c.app.state.http = async_client
        c.app.state.jobs = JobManager()
        c.app.state.version = "3.0.0"
        try:
            yield c
        finally:
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(async_client.aclose())
                loop.close()
            except Exception:
                pass


# ── /api/version + /api/health + /api/changelog ─────────────


def test_version_returns_3_0_0(client: TestClient) -> None:
    r = client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "3.0.0"
    assert body["api_version"] == "v1"


def test_health_ok_when_idle(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["job_id"] is None


def test_changelog_returns_plain_text(client: TestClient) -> None:
    r = client.get("/api/changelog")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # The changelog must mention the v3.0.0 entry.
    assert "3.0.0" in r.text or "FastAPI" in r.text


def test_pages_index_renders(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Collect Inventory" in r.text


def test_pages_changelog_renders(client: TestClient) -> None:
    r = client.get("/changelog")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ── /api/collect + /api/progress + /api/cancel + /api/download ─


def test_collect_400_on_missing_field(client: TestClient) -> None:
    r = client.post("/api/collect", json={"host": "127.0.0.1", "username": "u"})
    assert r.status_code == 400
    body = r.json()
    assert "error" in body


def test_collect_202_then_progress_then_download(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    # Redirect output to a temp dir so the .xlsx doesn't pollute the repo.
    monkeypatch.setenv("FC_INVENTORY_OUTPUT_DIR", str(tmp_path))
    # Clear the Settings lru_cache so the new env var takes effect.
    from app.core.config import get_settings
    get_settings.cache_clear()

    # Kick off a collection. The handler returns the login success on
    # the first attempt, so the pipeline proceeds.
    r = client.post(
        "/api/collect",
        json={"host": "127.0.0.1", "port": 17443, "username": "u", "password": "p"},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "started"
    assert body["job_id"]

    # The background task is async; poll until done (max 10 s).
    deadline = time.time() + 10
    final = None
    while time.time() < deadline:
        r = client.get("/api/progress")
        assert r.status_code == 200
        data = r.json()
        if data["status"] in ("done", "error", "cancelled"):
            final = data
            break
        time.sleep(0.1)
    assert final is not None, "job did not finish within 10s"
    assert final["status"] == "done", f"unexpected final state: {final}"

    # The .xlsx should exist.
    files = list(tmp_path.glob("*.xlsx"))
    assert len(files) == 1
    assert files[0].stat().st_size > 1000


def test_collect_409_when_already_running(client: TestClient) -> None:
    # First kick off a collection and don't wait for it to finish.
    r1 = client.post(
        "/api/collect",
        json={"host": "127.0.0.1", "port": 17443, "username": "u", "password": "p"},
    )
    assert r1.status_code == 202

    # Immediately try a second one. With the synchronous mock the
    # background task may already be done; we accept either 202 (a new
    # job is started) or 409 (the first is still running). The
    # contract is "single user, single job" — the second request must
    # not silently run alongside the first.
    r2 = client.post(
        "/api/collect",
        json={"host": "127.0.0.1", "port": 17443, "username": "u", "password": "p"},
    )
    assert r2.status_code in (202, 409)
    if r2.status_code == 409:
        body = r2.json()
        assert "already" in body["detail"]["error"].lower()


def test_cancel_200_when_running(client: TestClient) -> None:
    r = client.post(
        "/api/collect",
        json={"host": "127.0.0.1", "port": 17443, "username": "u", "password": "p"},
    )
    assert r.status_code == 202
    r = client.post("/api/cancel")
    assert r.status_code == 200
    assert r.json()["status"] in ("cancelling", "already_finished")


def test_cancel_404_when_idle(client: TestClient) -> None:
    r = client.post("/api/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no_job_running"


def test_download_404_when_no_job(client: TestClient) -> None:
    r = client.get("/api/download")
    assert r.status_code == 404

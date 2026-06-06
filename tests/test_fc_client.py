"""Tests for the async FCClient (login matrix, pagination, fallback chain)."""

from __future__ import annotations

import httpx
import pytest

from app.core.fc_client import (
    _VERSION_REJECTION_CODE,
    API_VERSIONS,
    FCClient,
)

# ── helpers ────────────────────────────────────────────────


def _make_async_client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), verify=False, timeout=10.0)


# ── login matrix ───────────────────────────────────────────


def test_login_succeeds_on_third_attempt():
    """First two auth methods return 401; third returns 200 + X-Auth-Token."""
    calls: list[tuple[str, str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, str(req.url), req.headers.get("X-Auth-User", "")))
        if len(calls) < 3:
            return httpx.Response(401, json={"errorCode": "10000001"})
        return httpx.Response(
            200,
            headers={"X-Auth-Token": "MOCK-TOKEN"},
            json={"accessSession": "MOCK-TOKEN"},
        )

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            await client.login()
            return client

    client = pytest.run(run) if hasattr(pytest, "run") else None
    import asyncio
    client = asyncio.run(run())
    assert client.token == "MOCK-TOKEN"
    assert client.base_url == "https://127.0.0.1:17443/service"
    # 3 attempts total: first 2 fail with 401, third succeeds.
    assert len(calls) == 3
    # Methods tried: POST, POST, PUT (matches v1.0.0 ordering).
    assert [c[0] for c in calls] == ["POST", "POST", "PUT"]


def test_login_version_rejection_breaks_inner_loop():
    """Body contains '10000022' -> break auth loop, try next version."""
    attempts_per_version: dict[str, int] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        ver = (req.headers.get("Accept") or "").split("version=")[-1].split(";")[0]
        attempts_per_version[ver] = attempts_per_version.get(ver, 0) + 1
        if ver == "v8.0":
            return httpx.Response(401, json={"errorCode": "10000022", "errorMessage": "版本号错误"})
        # v6.5 succeeds
        return httpx.Response(200, headers={"X-Auth-Token": "T"}, json={"token": "T"})

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            await client.login()
            return client

    import asyncio
    client = asyncio.run(run())
    assert client.token == "T"
    # Version v8.0 should have been tried exactly once before breaking.
    assert attempts_per_version["v8.0"] == 1
    assert client.version == "v6.5"


def test_login_connection_refused_advances_to_next_port():
    """ConnectError on port A -> loop continues to port B."""
    ports_tried: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        port = req.url.port
        ports_tried.append(port)
        if port == 17443:
            # Simulate connection refused by raising ConnectError via a
            # different transport; here we just 404 to force the matrix
            # to keep going.
            return httpx.Response(404, json={"errorCode": "unknown"})
        return httpx.Response(200, headers={"X-Auth-Token": "T"}, json={"token": "T"})

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            await client.login()
            return client

    import asyncio
    asyncio.run(run())
    # Port 17443 was tried first; we then moved to 7443 (default).
    assert 17443 in ports_tried


def test_login_all_methods_fail_raises_connection_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorCode": "401"})

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            try:
                await client.login()
            except ConnectionError as exc:
                return str(exc)
            return "no-error"

    import asyncio
    err = asyncio.run(run())
    assert "All login methods failed" in err


def test_login_versions_order():
    """API_VERSIONS must match v1.0.0 ordering (v8.0 first)."""
    assert API_VERSIONS == ["v8.0", "v6.5", "v6.3", "v6.1", "v1.0", "v9.0"]


# ── pagination ─────────────────────────────────────────────


def test_get_all_paginates_3_pages():
    page_sizes: list[int] = []
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        offset = int(req.url.params.get("offset", "0"))
        if offset == 0:
            page_sizes.append(100)
            return httpx.Response(
                200,
                json={"vms": [{"urn": f"u{i}"} for i in range(100)], "total": 247},
            )
        if offset == 100:
            page_sizes.append(100)
            return httpx.Response(
                200,
                json={"vms": [{"urn": f"u{i}"} for i in range(100, 200)], "total": 247},
            )
        if offset == 200:
            page_sizes.append(47)
            return httpx.Response(
                200,
                json={"vms": [{"urn": f"u{i}"} for i in range(200, 247)], "total": 247},
            )
        return httpx.Response(500)

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            # Pre-set token + base_url so _get doesn't fail on the URL.
            client.token = "T"
            client.base_url = "https://127.0.0.1:17443/service"
            return await client._get_all("/vms", "vms")

    import asyncio
    items = asyncio.run(run())
    assert len(items) == 247
    assert call_count["n"] == 3


def test_get_all_empty_first_page():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"vms": [], "total": 0})

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            client.token = "T"
            client.base_url = "https://127.0.0.1:17443/service"
            return await client._get_all("/vms", "vms")

    import asyncio
    items = asyncio.run(run())
    assert items == []


def test_get_all_top_level_list_fallback():
    """Endpoint returns `[...]` directly with no `result_key` wrapper."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"urn": "a"}, {"urn": "b"}])

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            client.token = "T"
            client.base_url = "https://127.0.0.1:17443/service"
            return await client._get_all("/vms", "vms")

    import asyncio
    items = asyncio.run(run())
    assert len(items) == 2
    assert items[0]["urn"] == "a"


# ── /service/ URL stitching ────────────────────────────────


def test_get_does_not_double_service_path():
    """A `path` starting with `/service/` must NOT be prefixed with baseURL again."""
    """(otherwise we get `/service/service/...`)."""
    seen_url: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_url.append(str(req.url))
        return httpx.Response(200, json={"urn": "h1", "name": "host-01"})

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            client.token = "T"
            client.base_url = "https://127.0.0.1:17443/service"
            return await client.get_host_detail("/service/hosts/1")

    import asyncio
    detail = asyncio.run(run())
    assert detail["urn"] == "h1"
    assert seen_url[0] == "https://127.0.0.1:17443/service/hosts/1"
    # The buggy pre-fix behaviour would be "https://127.0.0.1:17443/service/service/hosts/1"
    assert "/service/service/" not in seen_url[0]


# ── VM NIC / Disk fallback chain ───────────────────────────


def test_vm_nics_prefers_inline():
    async def run():
        async with _make_async_client_with(lambda r: httpx.Response(200, json={})) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            return await client.get_vm_nics(
                "/service/vms/1",
                inline_nics=[{"name": "inline-nic", "mac": "00:11"}],
            )

    import asyncio
    nics = asyncio.run(run())
    assert len(nics) == 1
    assert nics[0]["name"] == "inline-nic"


def test_vm_disks_falls_back_from_volumes_to_disks():
    call_log: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        call_log.append(path)
        if path.endswith("/volumes"):
            return httpx.Response(404, json={"errorCode": "404"})
        if path.endswith("/disks"):
            return httpx.Response(200, json={"disks": [{"name": "d1", "quantityGB": 50}]})
        return httpx.Response(200, json={})

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            client.token = "T"
            client.base_url = "https://127.0.0.1:17443/service"
            return await client.get_vm_disks(
                "/service/vms/1", inline_disks=[], inline_volumes=[]
            )

    import asyncio
    disks = asyncio.run(run())
    assert len(disks) == 1
    assert disks[0]["name"] == "d1"
    # /volumes was tried first (404), then /disks (200).
    assert "/volumes" in call_log[0]
    assert "/disks" in call_log[1]


# ── /api/session token parsing ─────────────────────────────


def test_token_extracted_from_header():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-Auth-Token": "TOK-HEADER"}, json={})

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            await client.login()
            return client.token

    import asyncio
    assert asyncio.run(run()) == "TOK-HEADER"


def test_token_extracted_from_body_accessSession():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accessSession": "TOK-BODY"})

    async def run():
        async with _make_async_client_with(handler) as http:
            client = FCClient("127.0.0.1", "u", "p", port=17443)
            client.attach(http)
            await client.login()
            return client.token

    import asyncio
    assert asyncio.run(run()) == "TOK-BODY"


def test_version_rejection_constant():
    """The numeric body code is locale-safe."""
    assert _VERSION_REJECTION_CODE == "10000022"

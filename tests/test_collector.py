"""Tests for the async InventoryCollector (the orchestration logic)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from app.core.collector import InventoryCollector

from .conftest import load_fixture, make_transport


def _make_handler(fixture_by_tail: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/service/session":
            return httpx.Response(
                200,
                headers={"X-Auth-Token": "TEST-TOKEN"},
                json={"accessSession": "TEST-TOKEN"},
            )
        if req.url.path == "/service/session/" and req.method == "DELETE":
            return httpx.Response(204)
        # Resource GETs.
        tail = req.url.path.rstrip("/").rsplit("/", 1)[-1]
        if tail in fixture_by_tail:
            return httpx.Response(200, json=fixture_by_tail[tail])
        if tail.isdigit() and "/hosts/" in req.url.path:
            return httpx.Response(200, json=load_fixture("host_detail.json"))
        if tail.isdigit() and "/vms/" in req.url.path:
            vm_id = int(tail)
            fixture = "vm_detail.json" if vm_id == 1 else "vm_db_detail.json"
            return httpx.Response(200, json=load_fixture(fixture))
        return httpx.Response(200, json={})
    return handler


async def _run_collector() -> dict[str, list[dict[str, Any]]]:
    """Run a collector end-to-end with the canned fixtures."""
    fixture_by_tail = {
        "sites": load_fixture("sites.json"),
        "clusters": load_fixture("clusters.json"),
        "hosts": load_fixture("hosts.json"),
        "vms": load_fixture("vms.json"),
        "datastores": load_fixture("datastores.json"),
        "dvswitchs": load_fixture("dvswitchs.json"),
        "portgroups": load_fixture("portgroups.json"),
    }
    transport = make_transport(_make_handler(fixture_by_tail))
    async with httpx.AsyncClient(transport=transport, verify=False, timeout=10.0) as http:
        collector = InventoryCollector("127.0.0.1", "u", "p", port=17443)
        collector.attach(http)
        return await collector.collect_all()


def test_collect_all_produces_all_10_sheets():
    sheets = asyncio.run(_run_collector())
    expected = {
        "vSummary", "vInfo", "vCPU", "vMemory", "vDisk", "vNetwork",
        "vHost", "vCluster", "vDatastore", "vSwitch",
    }
    assert set(sheets.keys()) == expected


def test_vinfo_has_power_state_per_vm():
    sheets = asyncio.run(_run_collector())
    rows = sheets["vInfo"]
    assert len(rows) == 2  # two VMs in vms.json
    # First VM is "running" -> "ON"; second is "stopped" -> "OFF".
    by_name = {r["VM Name"]: r for r in rows}
    assert by_name["vm-app-01"]["Power State"] == "ON"
    assert by_name["vm-db-01"]["Power State"] == "OFF"


def test_vcpu_sockets_calculated():
    sheets = asyncio.run(_run_collector())
    rows = sheets["vCPU"]
    assert len(rows) == 2
    by_name = {r["VM Name"]: r for r in rows}
    # vm-app-01: 4 CPUs / 2 cores-per-socket = 2 sockets
    assert by_name["vm-app-01"]["Sockets"] == 2


def test_vsummary_counts_vms():
    sheets = asyncio.run(_run_collector())
    rows = sheets["vSummary"]
    by_item = {r["Item"]: r["Count"] for r in rows}
    assert by_item["Total VMs"] == 2
    assert by_item["Power ON"] == 1
    assert by_item["Power OFF"] == 1
    assert by_item["Total Hosts"] == 1
    assert by_item["Total Clusters"] == 1
    assert by_item["Total Datastores"] == 1


def test_vhost_running_vms_counted_locally():
    """When the host detail payload doesn't include runningVmCount, fall back
    to the locally-computed host_vm_count.

    The host_detail.json fixture has `runningVmCount: 1` (FC-reported
    value), so the vHost sheet uses that — matches v1.0.0 precedence:
    `runningVmCount or local_count`."""
    sheets = asyncio.run(_run_collector())
    rows = sheets["vHost"]
    assert len(rows) == 1
    # host_detail.json reports runningVmCount=1; 2 VMs are in vms.json
    # but the FC-reported value wins per the v1.0.0 formula.
    assert rows[0]["Running VMs"] == 1


def test_vdatastore_uses_capacityGB_and_freeSpaceGB_directly():
    """The fixture datastores.json has capacityGB / freeSpaceGB set, so the
    vDatastore sheet must show those values verbatim (no MB conversion)."""
    sheets = asyncio.run(_run_collector())
    rows = sheets["vDatastore"]
    assert len(rows) == 1
    assert rows[0]["Capacity (GB)"] == 51200
    assert rows[0]["Free (GB)"] == 32768


def test_cancellation_raises_interrupted_error():
    """Setting `cancelled=True` before the pipeline should abort with InterruptedError."""
    async def run():
        transport = make_transport(_make_handler({
            "sites": load_fixture("sites.json"),
        }))
        async with httpx.AsyncClient(transport=transport, verify=False, timeout=10.0) as http:
            collector = InventoryCollector("127.0.0.1", "u", "p", port=17443)
            collector.attach(http)
            collector.cancelled = True  # pre-cancel
            try:
                await collector.collect_all()
            except InterruptedError:
                return "cancelled"
            return "not-cancelled"
    assert asyncio.run(run()) == "cancelled"

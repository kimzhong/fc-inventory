"""Async inventory collector.

Port of `collector.py` (783L) from v1.0.0. The 10 sheet builders
(`_build_v*`) are pure data transformation and stay **sync** — they
don't perform I/O. Only the resource-fetching pipeline in
`collect_all` is async, and it now uses `asyncio.gather()` to fetch
multiple resources concurrently (the main perf win over v1.0.0).
"""

from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from typing import TYPE_CHECKING, Any

import structlog

from . import field_map
from .fc_client import FCClient

if TYPE_CHECKING:
    import httpx

logger = structlog.get_logger(__name__)


class InventoryCollector:
    """Drives a single inventory collection pipeline.

    The collector holds:
      - the FC REST client (async, shared via `attach()`)
      - the raw fetched data (`self.sites`, `self.hosts`, `self.vms`, …)
      - the cross-reference lookup maps (`self.host_map`, …)
      - a progress dict + a `cancelled` flag for the web UI
    """

    def __init__(self, host: str, username: str, password: str, port: int = 7443) -> None:
        self.client = FCClient(host, username, password, port=port)
        self.cancelled = False
        self.progress: dict[str, Any] = {
            "status": "idle",
            "current_step": "",
            "percent": 0,
            "error": None,
        }

        # Raw data from the API.
        self.sites: list[dict[str, Any]] = []
        self.clusters: list[dict[str, Any]] = []
        self.hosts: list[dict[str, Any]] = []
        self.host_details: dict[str, dict[str, Any]] = {}
        self.vms: list[dict[str, Any]] = []
        self.vm_details: dict[str, dict[str, Any]] = {}
        self.vm_nics: dict[str, list[dict[str, Any]]] = {}
        self.vm_disks: dict[str, list[dict[str, Any]]] = {}
        self.datastores: list[dict[str, Any]] = []
        self.dvswitches: list[dict[str, Any]] = []
        self.portgroups: list[dict[str, Any]] = []

        # Lookup maps (URN -> name).
        self.host_map: dict[str, str] = {}
        self.cluster_map: dict[str, str] = {}
        self.datastore_map: dict[str, str] = {}

        # State for the portgroup sample-log throttling (mirrors the
        # `_pg_logged` flag in v1.0.0).
        self._pg_logged = False

    def attach(self, http: httpx.AsyncClient) -> None:
        """Inject the shared `httpx.AsyncClient` into the wrapped `FCClient`.

        Convenience wrapper so the route handler can do
        `collector.attach(app.state.http)` without reaching into
        `collector.client` directly.
        """
        self.client.attach(http)

    def cancel(self) -> None:
        """Idempotent. Sets the cancel flag the pipeline checks."""
        self.cancelled = True

    # ── Progress ───────────────────────────────────────────

    def _check_cancelled(self) -> None:
        if self.cancelled:
            raise InterruptedError("Collection cancelled by user.")

    def _update_progress(self, percent: int, step: str) -> None:
        self._check_cancelled()
        self.progress["percent"] = percent
        self.progress["current_step"] = step
        logger.info("collector.progress", percent=percent, step=step)

    def _log_sample(self, label: str, obj: Any) -> None:
        """Log the top-level keys of a sample object for debugging.

        Mirrors v1.0.0's `_log_sample` (collector.py:278-291).
        """
        if not obj:
            logger.info("collector.sample", label=label, empty=True)
            return
        keys: Any = list(obj.keys()) if isinstance(obj, dict) else type(obj).__name__
        logger.info("collector.sample", label=label, keys=keys)
        flat = field_map.flatten_dict(obj)
        for k, v in list(flat.items())[:80]:
            logger.info("collector.sample.field", label=label, key=k, value=repr(v)[:100])

    # ── Pipeline ───────────────────────────────────────────

    async def collect_all(self) -> dict[str, list[dict[str, Any]]]:
        """Run the full collection pipeline. Returns dict of sheet data.

        Mirrors `InventoryCollector.collect_all` (collector.py:293-452)
        with concurrent resource fetches via `asyncio.gather()`. The
        progress percentage brackets (5/10/15/20/20-30/35/40/45/50-90/92/98/100)
        are preserved so the v1.0.0 polling UI still works.
        """
        self.progress["status"] = "running"
        try:
            # 1. Login (sequential; everything depends on the token).
            self._update_progress(5, "Logging in to FusionCompute...")
            await self.client.login()

            # 2. Sites (sequential; everything depends on site_uri).
            self._update_progress(10, "Fetching sites...")
            self.sites = await self.client.get_sites()
            if self.sites:
                self._log_sample("SITE", self.sites[0])

            # 3-7. For each site, fetch the major resources in parallel,
            # then host details / port groups / VM list.
            for site in self.sites:
                site_uri = site.get("uri", "")
                if not site_uri:
                    continue

                # ── Concurrent per-site fan-out ────────────────
                # (mirrors the sequential block at collector.py:312-321,
                # but now run in parallel — ~4x faster on a slow link)
                self._update_progress(15, "Fetching clusters...")
                self._update_progress(20, "Fetching hosts...")
                self._update_progress(35, "Fetching datastores...")
                self._update_progress(40, "Fetching networks...")

                cluster_task = asyncio.create_task(
                    self._safe_get(self.client.get_clusters, "clusters", site_uri)
                )
                host_task = asyncio.create_task(
                    self._safe_get(self.client.get_hosts, "hosts", site_uri)
                )
                ds_task = asyncio.create_task(
                    self._safe_get(self.client.get_datastores, "datastores", site_uri)
                )
                dvs_task = asyncio.create_task(
                    self.client.get_dvswitches(site_uri)
                )
                clusters, hosts, datastores, dvswitches = await asyncio.gather(
                    cluster_task, host_task, ds_task, dvs_task
                )
                self.clusters.extend(clusters)
                self.hosts.extend(hosts)
                self.datastores.extend(datastores)
                self.dvswitches.extend(dvswitches)
                if clusters:
                    self._log_sample("CLUSTER", clusters[0])
                if hosts:
                    self._log_sample("HOST list", hosts[0])
                if datastores:
                    self._log_sample("DATASTORE", datastores[0])
                if dvswitches:
                    self._log_sample("DVSWITCH", dvswitches[0])

                # ── Host details (concurrent per host) ─────────
                if hosts:
                    results = await asyncio.gather(
                        *[self._safe_host_detail(i, h) for i, h in enumerate(hosts)],
                        return_exceptions=True,
                    )
                    for r in results:
                        if isinstance(r, Exception):
                            logger.warning("host_detail.failed", err=str(r))

                # ── Port groups (concurrent per DVS) ───────────
                pg_results = await asyncio.gather(
                    *[self.client.get_portgroups(d.get("uri", "")) for d in dvswitches],
                    return_exceptions=True,
                )
                for dvs, result in zip(dvswitches, pg_results, strict=False):
                    if isinstance(result, Exception):
                        logger.warning(
                            "portgroups.failed", dvs=dvs.get("name"), err=str(result)
                        )
                        continue
                    for pg in result:
                        pg["_dvswitch_name"] = dvs.get("name", "")
                    self.portgroups.extend(result)
                    if result and not self._pg_logged:
                        self._log_sample("PORTGROUP", result[0])
                        self._pg_logged = True

                # Site-level portgroup fallback (mirrors collector.py:367-377).
                if not self.portgroups:
                    try:
                        site_pgs = await self.client.get_site_portgroups(site_uri)
                        for pg in site_pgs:
                            pg.setdefault("_dvswitch_name", "")
                        self.portgroups.extend(site_pgs)
                        if site_pgs and not self._pg_logged:
                            self._log_sample("PORTGROUP (site-level)", site_pgs[0])
                            self._pg_logged = True
                    except Exception as exc:
                        logger.warning("site_portgroups.failed", err=str(exc))

                # ── VM list ───────────────────────────────────
                self._update_progress(45, "Fetching VM list...")
                vms = await self.client.get_vms(site_uri)
                self.vms.extend(vms)
                if vms:
                    self._log_sample("VM list", vms[0])

                # ── VM details (concurrent per VM) ────────────
                # 50%..90% progress across all VMs in this site.
                if vms:
                    tasks = [
                        asyncio.create_task(self._safe_vm_detail(i, vm, len(vms)))
                        for i, vm in enumerate(vms)
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in results:
                        if isinstance(r, Exception):
                            logger.warning("vm_detail.failed", err=str(r))

            # 9. Pure-data sheet builders (sync; no I/O).
            self._update_progress(92, "Processing collected data...")
            self._build_lookup_maps()
            result = self._build_all_sheets()

            # 10. Logout.
            self._update_progress(98, "Logging out...")
            await self.client.logout()

            self.progress["percent"] = 100
            self.progress["current_step"] = "Collection complete!"
            self.progress["status"] = "done"
            return result

        except InterruptedError:
            self.progress["status"] = "cancelled"
            self.progress["current_step"] = "Cancelled"
            try:
                await self.client.logout()
            except Exception:
                pass
            raise

        except Exception as e:
            logger.exception("collector.failed")
            self.progress["status"] = "error"
            self.progress["error"] = str(e)
            self.progress["current_step"] = "Error occurred"
            try:
                await self.client.logout()
            except Exception:
                pass
            raise

    # ── Per-resource helpers (concurrent) ──────────────────

    async def _safe_get(self, fn, _label: str, *args) -> list[dict[str, Any]]:
        """Run a client GET and log (not raise) failures.

        Mirrors the `try/except` blocks at collector.py:335-336 etc.
        """
        try:
            return await fn(*args)
        except Exception as exc:
            logger.warning("collector.resource.failed", label=_label, err=str(exc))
            return []

    async def _safe_host_detail(self, i: int, host: dict[str, Any]) -> None:
        """Fetch one host's detail; record progress; on failure, log and continue."""
        host_uri = host.get("uri", "")
        host_name = host.get("name", host_uri)
        urn = host.get("urn", "")
        pct = 20 + int(((i + 1) / max(len(self.hosts), 1)) * 10)
        self._update_progress(pct, f"Fetching host detail: {host_name}...")
        try:
            detail = await self.client.get_host_detail(host_uri)
        except Exception as exc:
            logger.warning("host_detail.failed", host=host_name, err=str(exc))
            return
        self.host_details[urn] = detail
        if i == 0:
            self._log_sample("HOST detail", detail)

    async def _safe_vm_detail(self, i: int, vm: dict[str, Any], total: int) -> None:
        """Fetch one VM's detail + extract inline NICs/disks; advance progress.

        Mirrors collector.py:387-417.
        """
        vm_uri = vm.get("uri", "")
        vm_name = vm.get("name", vm_uri)
        urn = vm.get("urn", "")
        pct = 50 + int(((i + 1) / max(total, 1)) * 40)
        self._update_progress(pct, f"Fetching VM detail ({i + 1}/{total}): {vm_name}...")
        try:
            detail = await self.client.get_vm_detail(vm_uri)
        except Exception as exc:
            logger.warning("vm_detail.failed", vm=vm_name, err=str(exc))
            return
        self.vm_details[urn] = detail
        if i == 0:
            self._log_sample("VM detail", detail)

        # Extract NICs and disks from vmConfig (inline) — mirrors
        # collector.py:402-407. Fall back to the FC client helpers if
        # the inline blocks are empty.
        vm_config = detail.get("vmConfig", {}) or {}
        inline_nics = vm_config.get("nics") or detail.get("nics") or []
        inline_disks = (
            vm_config.get("disks")
            or vm_config.get("volumes")
            or detail.get("disks")
            or []
        )
        nics = await self.client.get_vm_nics(vm_uri, inline_nics=inline_nics)
        disks = await self.client.get_vm_disks(
            vm_uri, inline_disks=inline_disks, inline_volumes=None
        )
        self.vm_nics[urn] = nics
        self.vm_disks[urn] = disks
        if i == 0:
            logger.info(
                "first_vm.extracted",
                urn=urn,
                nics=len(nics),
                disks=len(disks),
            )
            if nics:
                logger.info("first_vm.nic_keys", keys=list(nics[0].keys()))
            if disks:
                logger.info("first_vm.disk_keys", keys=list(disks[0].keys()))

    # ── Lookup maps ───────────────────────────────────────

    def _build_lookup_maps(self) -> None:
        """Build URN -> name lookup maps for cross-referencing.

        Mirrors `_build_lookup_maps` (collector.py:454-461).
        """
        for h in self.hosts:
            self.host_map[h.get("urn", "")] = h.get("name", "")
        for c in self.clusters:
            self.cluster_map[c.get("urn", "")] = c.get("name", "")
        for d in self.datastores:
            self.datastore_map[d.get("urn", "")] = d.get("name", "")

    # ── Sheet assembly ────────────────────────────────────

    def _build_all_sheets(self) -> dict[str, list[dict[str, Any]]]:
        """Build all Excel sheet data from the raw collected data.

        Mirrors `_build_all_sheets` (collector.py:463-477). The 10
        `_build_v*` methods are sync, pure data-transformation — they
        are kept sync so they run in the same coroutine as the
        orchestrating event loop without blocking on I/O.
        """
        vinfo = self._build_vinfo()
        return {
            "vSummary":  self._build_vsummary(vinfo),
            "vInfo":     vinfo,
            "vCPU":      self._build_vcpu(),
            "vMemory":   self._build_vmemory(),
            "vDisk":     self._build_vdisk(),
            "vNetwork":  self._build_vnetwork(),
            "vHost":     self._build_vhost(),
            "vCluster":  self._build_vcluster(),
            "vDatastore": self._build_vdatastore(),
            "vSwitch":   self._build_vswitch(),
        }

    # ── Sheet builders (sync, pure data) ───────────────────
    #
    # Each builder is a near-verbatim translation of the corresponding
    # Python method (collector.py:481-783). The differences are:
    #   - dicts use `field_map.try_paths` / `field_map.try_string` / etc.
    #     instead of the module-level `_try_paths` helper.
    #   - row construction uses a plain dict (Excel writer preserves
    #     first-seen insertion order via `OrderedDict`).
    #   - `OrderedDict` is used for the vSummary rows so the Item/Count
    #     columns line up.

    def _build_vsummary(self, vinfo: list[dict[str, Any]]) -> list[dict[str, Any]]:
        power_count: Counter[str] = Counter()
        cluster_power: dict[str, dict[str, int]] = {}
        for vm in vinfo:
            power = vm.get("Power State", "")
            cluster = vm.get("Cluster", "N/A") or "N/A"
            power_count[power] += 1
            cp = cluster_power.setdefault(cluster, {"ON": 0, "OFF": 0, "Other": 0})
            if power == "ON":
                cp["ON"] += 1
            elif power == "OFF":
                cp["OFF"] += 1
            else:
                cp["Other"] += 1

        rows: list[dict[str, Any]] = [
            {"Item": "Total VMs",          "Count": len(vinfo)},
            {"Item": "Power ON",           "Count": power_count.get("ON", 0)},
            {"Item": "Power OFF",          "Count": power_count.get("OFF", 0)},
            {"Item": "Total Hosts",        "Count": len(self.hosts)},
            {"Item": "Total Clusters",     "Count": len(self.clusters)},
            {"Item": "Total Datastores",   "Count": len(self.datastores)},
            {"Item": "Total DVSwitches",   "Count": len(self.dvswitches)},
            {"Item": "Total Port Groups",  "Count": len(self.portgroups)},
            {"Item": "",                   "Count": ""},
            {"Item": "=== Power State by Cluster ===", "Count": ""},
        ]
        for cluster, counts in sorted(cluster_power.items()):
            rows.append({
                "Item": f"  {cluster}",
                "Count": f"ON: {counts['ON']}  OFF: {counts['OFF']}",
            })
        return rows

    def _merged_vm(self, vm: dict[str, Any]) -> dict[str, Any]:
        """Merge the VM list payload with the detail payload (detail wins)."""
        urn = vm.get("urn", "")
        detail = self.vm_details.get(urn) or {}
        merged = {**vm, **detail}
        return merged

    def _build_vinfo(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for vm in self.vms:
            merged = self._merged_vm(vm)
            urn = vm.get("urn", "")
            disks = self.vm_disks.get(urn, [])
            nics = self.vm_nics.get(urn, [])

            total_disk = sum(_to_float(d.get("quantityGB", 0)) for d in disks)
            ip_list = ", ".join(n.get("ip", "") for n in nics if n.get("ip"))

            row: dict[str, Any] = OrderedDict()
            row["VM Name"]         = field_map.try_paths(merged, ["name"])
            row["UUID"]            = field_map.try_paths(merged, ["uuid"])
            row["Power State"]     = field_map.power_state(field_map.try_paths(merged, ["status"]))
            row["Status"]          = field_map.try_paths(merged, ["status"])
            row["Guest OS"]        = field_map.try_paths(merged, ["osOptions.osType", "vmConfig.osOptions.osType"])
            row["CPUs"]            = field_map.try_paths(merged, ["vmConfig.cpu.quantity", "cpu.quantity"])
            row["Cores Per Socket"] = field_map.try_paths(merged, ["vmConfig.cpu.coresPerSocket"])
            row["Memory (MB)"]     = field_map.try_paths(merged, ["vmConfig.memory.quantityMB", "memory.quantityMB"])
            row["Total Disk (GB)"] = total_disk
            row["NICs"]            = len(nics)
            row["IP Addresses"]    = ip_list
            host_urn = field_map.try_string(merged, ["locationUrn", "hostUrn"])
            host_name = self.host_map.get(host_urn, "") or field_map.try_string(merged, ["hostName", "locationName"])
            row["Host"]            = host_name
            cluster_urn = field_map.try_string(merged, ["clusterUrn"])
            cluster_name = self.cluster_map.get(cluster_urn, "") or field_map.try_string(merged, ["clusterName"])
            row["Cluster"]         = cluster_name
            row["VM Tools"]        = field_map.try_paths(merged, ["toolsVersion", "pvDriverStatus", "toolInstallStatus"])
            row["Description"]     = field_map.try_paths(merged, ["description"])
            row["Create Date"]     = field_map.try_paths(merged, ["createTime"])
            row["URN"]             = urn
            rows.append(row)
        return rows

    def _build_vcpu(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for vm in self.vms:
            merged = self._merged_vm(vm)
            host_urn = field_map.try_string(merged, ["locationUrn", "hostUrn"])
            cluster_urn = field_map.try_string(merged, ["clusterUrn"])
            qty = field_map.try_paths(merged, ["vmConfig.cpu.quantity"])
            cps = field_map.try_paths(merged, ["vmConfig.cpu.coresPerSocket"])
            sockets: Any = ""
            try:
                if qty not in (None, "", 0) and cps not in (None, "", 0):
                    q_int, c_int = _to_int(qty), _to_int(cps)
                    if q_int and c_int:
                        sockets = q_int // c_int
                        if q_int % c_int:
                            sockets = f"{q_int / c_int:.2f}"
            except (ValueError, TypeError):
                sockets = ""

            row: dict[str, Any] = OrderedDict()
            row["VM Name"]              = field_map.try_paths(merged, ["name"])
            row["UUID"]                 = field_map.try_paths(merged, ["uuid"])
            row["Power"]                = field_map.power_state(field_map.try_paths(merged, ["status"]))
            row["Total CPUs"]           = qty
            row["Cores Per Socket"]     = cps
            row["Sockets"]              = sockets
            row["CPU Reservation (MHz)"] = field_map.try_paths(merged, ["vmConfig.cpu.reservation"])
            row["CPU Limit (MHz)"]      = field_map.try_paths(merged, ["vmConfig.cpu.limit"])
            row["CPU Weight"]           = field_map.try_paths(merged, ["vmConfig.cpu.weight"])
            row["CPU Hot Plug"]         = field_map.try_paths(merged, ["vmConfig.cpu.cpuHotPlug"])
            row["CPU Bind Type"]        = field_map.try_paths(merged, ["vmConfig.cpu.cpuBindType"])
            row["CPU Policy"]           = field_map.try_paths(merged, ["vmConfig.cpu.cpuPolicy"])
            row["Host"]                 = self.host_map.get(host_urn, "")
            row["Cluster"]              = self.cluster_map.get(cluster_urn, "")
            rows.append(row)
        return rows

    def _build_vmemory(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for vm in self.vms:
            merged = self._merged_vm(vm)
            host_urn = field_map.try_string(merged, ["locationUrn", "hostUrn"])
            cluster_urn = field_map.try_string(merged, ["clusterUrn"])
            row: dict[str, Any] = OrderedDict()
            row["VM Name"]         = field_map.try_paths(merged, ["name"])
            row["UUID"]            = field_map.try_paths(merged, ["uuid"])
            row["Power"]           = field_map.power_state(field_map.try_paths(merged, ["status"]))
            row["Memory (MB)"]     = field_map.try_paths(merged, ["vmConfig.memory.quantityMB"])
            row["Reservation (MB)"] = field_map.try_paths(merged, ["vmConfig.memory.reservation"])
            row["Limit (MB)"]      = field_map.try_paths(merged, ["vmConfig.memory.limit"])
            row["Weight"]          = field_map.try_paths(merged, ["vmConfig.memory.weight"])
            row["Memory Hot Plug"] = field_map.try_paths(merged, ["vmConfig.memory.memHotPlug"])
            row["Huge Page"]       = field_map.try_paths(merged, ["vmConfig.memory.hugePage"])
            row["Host"]            = self.host_map.get(host_urn, "")
            row["Cluster"]         = self.cluster_map.get(cluster_urn, "")
            rows.append(row)
        return rows

    def _build_vdisk(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for vm in self.vms:
            urn = vm.get("urn", "")
            disks = self.vm_disks.get(urn, [])
            for disk in disks:
                ds_urn = field_map.try_string(disk, ["datastoreUrn"])
                row: dict[str, Any] = OrderedDict()
                row["VM Name"]       = vm.get("name", "")
                row["Power"]         = field_map.power_state(vm.get("status", ""))
                row["Disk Name"]     = field_map.try_paths(disk, ["name", "volumeName"])
                row["Disk UUID"]     = field_map.try_paths(disk, ["volumeUuid"])
                row["Capacity (GB)"] = field_map.try_paths(disk, ["quantityGB"])
                row["Bus Type"]      = field_map.try_paths(disk, ["pciType", "busType"])
                row["Thin Provision"] = field_map.try_paths(disk, ["isThin", "thinFlag"])
                row["Sequence"]      = field_map.try_paths(disk, ["sequenceNum"])
                row["Datastore"]     = self.datastore_map.get(ds_urn, ds_urn)
                row["Datastore URN"] = ds_urn
                row["Storage Type"]  = field_map.try_paths(disk, ["storageType"])
                row["Independent"]   = field_map.try_paths(disk, ["indepDisk"])
                row["Persistent"]    = field_map.try_paths(disk, ["persistentDisk"])
                row["Volume URN"]    = field_map.try_paths(disk, ["volumeUrn"])
                rows.append(row)
        return rows

    def _build_vnetwork(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for vm in self.vms:
            merged = self._merged_vm(vm)
            vm_uuid = field_map.try_string(merged, ["uuid"])
            nics = self.vm_nics.get(vm.get("urn", ""), [])
            for nic in nics:
                row: dict[str, Any] = OrderedDict()
                row["VM Name"]           = vm.get("name", "")
                row["VM UUID"]           = vm_uuid
                row["Power"]             = field_map.power_state(vm.get("status", ""))
                row["NIC Name"]          = field_map.try_paths(nic, ["name"])
                row["MAC Address"]       = field_map.try_paths(nic, ["mac"])
                row["IP Address"]        = field_map.try_paths(nic, ["ip"])
                row["IP List"]           = field_map.try_paths(nic, ["ipList"])
                row["IPv6"]              = field_map.try_paths(nic, ["ipv6s"])
                row["Port Group"]        = field_map.try_paths(nic, ["portGroupName"])
                row["Port Group URN"]    = field_map.try_paths(nic, ["portGroupUrn"])
                row["Port Group Type"]   = field_map.try_paths(nic, ["portGroupType"])
                row["VLAN Range"]        = field_map.try_paths(nic, ["portGroupVlanRange"])
                row["Sequence"]          = field_map.try_paths(nic, ["sequenceNum"])
                row["VirtIO"]            = field_map.try_paths(nic, ["virtIo"])
                row["NIC Type"]          = field_map.try_paths(nic, ["nicType", "virtualNicType"])
                row["Connect at Power-On"] = field_map.try_paths(nic, ["connectAtPowerOn"])
                row["URN"]               = field_map.try_paths(nic, ["urn"])
                rows.append(row)
        return rows

    def _build_vhost(self) -> list[dict[str, Any]]:
        host_vm_count: dict[str, int] = {}
        for vm in self.vms:
            h = vm.get("locationUrn") or vm.get("hostUrn") or ""
            if h:
                host_vm_count[h] = host_vm_count.get(h, 0) + 1

        rows: list[dict[str, Any]] = []
        for host in self.hosts:
            urn = host.get("urn", "")
            detail = self.host_details.get(urn) or {}
            merged = {**host, **detail}
            cluster_urn = field_map.try_string(merged, ["clusterUrn"])
            running = field_map.try_paths(merged, ["runningVmCount"]) or host_vm_count.get(urn, 0)

            row: dict[str, Any] = OrderedDict()
            row["Host Name"]         = field_map.try_paths(merged, ["name"])
            row["IP Address"]        = field_map.try_paths(merged, ["ip"])
            row["Status"]            = field_map.try_paths(merged, ["status"])
            row["Cluster"]           = self.cluster_map.get(cluster_urn, "")
            row["CPU Model"]         = field_map.try_paths(merged, ["cpuModel", "cpuType"])
            row["CPU Cores"]         = field_map.try_paths(merged, ["cpuQuantity", "cpuCores"])
            row["CPU MHz"]           = field_map.try_paths(merged, ["cpuMHz", "cpuFrequency"])
            row["Memory Total (MB)"] = field_map.try_paths(merged, ["memoryQuantityMB", "memoryCapacity"])
            row["Memory Used (MB)"]  = field_map.try_paths(merged, ["memoryUsedMB"])
            row["Running VMs"]       = running
            row["BMC IP"]            = field_map.try_paths(merged, ["bmcIp"])
            row["Maintenance"]       = field_map.try_paths(merged, ["isMaintaining"])
            row["Hypervisor"]        = field_map.try_paths(merged, ["hypervisor"])
            row["URN"]               = urn
            rows.append(row)
        return rows

    def _build_vcluster(self) -> list[dict[str, Any]]:
        cluster_host_count: dict[str, int] = {}
        for h in self.hosts:
            c = h.get("clusterUrn", "")
            if c:
                cluster_host_count[c] = cluster_host_count.get(c, 0) + 1

        rows: list[dict[str, Any]] = []
        for cluster in self.clusters:
            urn = cluster.get("urn", "")
            host_num = field_map.try_paths(cluster, ["hostNum"])
            if host_num in (None, "", 0):
                host_num = cluster_host_count.get(urn, 0)

            row: dict[str, Any] = OrderedDict()
            row["Cluster Name"]       = field_map.try_paths(cluster, ["name"])
            row["Description"]        = field_map.try_paths(cluster, ["description"])
            row["Tag"]                = field_map.try_paths(cluster, ["tag"])
            row["HA Enabled"]         = field_map.try_paths(cluster, ["isEnableHa", "isHA"])
            row["DRS Enabled"]        = field_map.try_paths(cluster, ["isEnableDrs", "isDRS"])
            row["Mem Overcommit"]     = field_map.try_paths(cluster, ["isMemOvercommit"])
            row["Auto Adjust NUMA"]   = field_map.try_paths(cluster, ["isAutoAdjustNuma"])
            row["DRS Level"]          = field_map.try_paths(cluster, ["drsSetting.drsLevel"])
            row["CPU Reservation"]    = field_map.try_paths(cluster, ["haResSetting.cpuReservation"])
            row["Memory Reservation"] = field_map.try_paths(cluster, ["haResSetting.memoryReservation"])
            row["Total Hosts"]        = host_num
            row["URN"]                = urn
            rows.append(row)
        return rows

    def _build_vdatastore(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ds in self.datastores:
            # Capacity: try GB first, then MB conversion.
            cap = field_map.try_paths(ds, ["capacityGB"])
            if cap in (None, ""):
                cap_mb = field_map.try_paths(ds, ["capacityMB", "totalSizeMB"])
                if cap_mb not in (None, ""):
                    f, ok = _to_float_ok(cap_mb)
                    cap = round(f / 1024, 2) if ok else ""

            # Free: try multiple field names, then MB fallback.
            free = field_map.try_paths(ds, ["freeSpaceGB", "freeSpace", "freeCapacityGB", "freeSizeGB"])
            if free in (None, ""):
                free_mb = field_map.try_paths(ds, ["freeSpaceMB", "freeSizeMB"])
                if free_mb not in (None, ""):
                    f, ok = _to_float_ok(free_mb)
                    free = round(f / 1024, 2) if ok else ""

            # Used %.
            used_pct: Any = ""
            cf, cok = _to_float_ok(cap)
            ff, fok = _to_float_ok(free)
            if cok and fok and cf > 0:
                used_pct = round((1 - ff / cf) * 100, 1)

            row: dict[str, Any] = OrderedDict()
            row["Datastore Name"] = field_map.try_paths(ds, ["name"])
            row["Storage Type"]   = field_map.try_paths(ds, ["storageType"])
            row["Capacity (GB)"]  = cap
            row["Free (GB)"]      = free
            row["Used %"]         = used_pct
            row["Status"]         = field_map.try_paths(ds, ["status"])
            row["Thin Support"]   = field_map.try_paths(ds, ["thinProvisionSupport"])
            row["Description"]    = field_map.try_paths(ds, ["description"])
            row["URN"]            = field_map.try_paths(ds, ["urn"])
            rows.append(row)
        return rows

    def _build_vswitch(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for dvs in self.dvswitches:
            row: dict[str, Any] = OrderedDict()
            row["Name"]        = field_map.try_paths(dvs, ["name"])
            row["Type"]        = "DVSwitch"
            row["VLAN ID"]     = ""
            row["MTU"]         = field_map.try_paths(dvs, ["mtu"])
            row["Description"] = field_map.try_paths(dvs, ["description"])
            row["Parent"]      = ""
            row["URN"]         = field_map.try_paths(dvs, ["urn"])
            rows.append(row)
        for pg in self.portgroups:
            row: dict[str, Any] = OrderedDict()
            row["Name"]        = field_map.try_paths(pg, ["name"])
            row["Type"]        = "Port Group"
            row["VLAN ID"]     = field_map.try_paths(pg, ["vlanId"])
            row["MTU"]         = field_map.try_paths(pg, ["mtu"])
            row["Description"] = field_map.try_paths(pg, ["description"])
            row["Parent"]      = pg.get("_dvswitch_name", "")
            row["URN"]         = field_map.try_paths(pg, ["urn"])
            rows.append(row)
        return rows


# ── Numeric coercion helpers (mirror v1.0.0 try/except float patterns) ──


def _to_float(v: Any) -> float:
    """Coerce a JSON number-or-string to float. Returns 0 on failure."""
    _, ok = _to_float_ok(v)
    return _ if ok else 0.0


def _to_float_ok(v: Any) -> tuple[float, bool]:
    """Return (value, ok). Mirrors the try/except float() blocks in v1.0.0."""
    if v is None:
        return 0.0, False
    if isinstance(v, bool):
        return float(v), True
    if isinstance(v, (int, float)):
        return float(v), True
    if isinstance(v, str):
        try:
            return float(v), True
        except (ValueError, TypeError):
            return 0.0, False
    return 0.0, False


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def round(f: float, n: int) -> float:
    """Round to `n` decimal places without importing the `round` builtin shadow."""
    p = 10 ** n
    return float(int(f * p + 0.5)) / p if f >= 0 else -float(int(-f * p + 0.5)) / p

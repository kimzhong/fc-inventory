"""
Inventory Collector
Orchestrates data collection from FusionCompute and builds flat data for Excel sheets.

Uses a "hybrid field mapping" approach:
- Each Excel column maps to a list of candidate JSON paths (try in order)
- Unknown/extra fields from API responses are also captured (raw key fallback)
- This works regardless of FusionCompute version field name variations
"""

import logging
import re
from collections import OrderedDict

from fc_client import FCClient

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────

def _get_path(d, path):
    """Get value from nested dict using dot path: 'vmConfig.cpu.quantity'."""
    if not isinstance(d, dict) or not path:
        return None
    parts = path.split(".")
    cur = d
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
        if cur is None:
            return None
    return cur


def _try_paths(d, paths):
    """Try multiple dot paths, return first non-None non-empty value."""
    for p in paths:
        v = _get_path(d, p)
        if v is not None and v != "":
            return v
    return ""


def _flatten_dict(d, parent="", sep="."):
    """Flatten nested dict for raw key inspection. Skips list-of-dicts."""
    items = {}
    if not isinstance(d, dict):
        return items
    for k, v in d.items():
        new_key = f"{parent}{sep}{k}" if parent else k
        if isinstance(v, dict):
            items.update(_flatten_dict(v, new_key, sep))
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                # Skip lists of dicts (handled separately as sub-tables)
                continue
            items[new_key] = ", ".join(str(x) for x in v) if v else ""
        else:
            items[new_key] = v
    return items


def _power_state(status):
    """Convert FC status string to ON/OFF."""
    if status in ("running", "started", "Running"):
        return "ON"
    if status in ("stopped", "shutOff", "Stopped"):
        return "OFF"
    return status or ""


# ── Field Mappings ──────────────────────────────────────────

VM_FIELDS = OrderedDict([
    ("VM Name",          ["name"]),
    ("Guest OS",         ["osOptions.osType", "vmConfig.osOptions.osType"]),
    ("CPUs",             ["vmConfig.cpu.quantity", "cpu.quantity"]),
    ("Cores Per Socket", ["vmConfig.cpu.coresPerSocket", "cpu.coresPerSocket"]),
    ("Memory (MB)",      ["vmConfig.memory.quantityMB", "memory.quantityMB"]),
    ("VM Tools",         ["toolsVersion", "pvDriverStatus", "toolInstallStatus", "vmToolsVersion"]),
    ("UUID",             ["uuid"]),
    ("Description",      ["description"]),
    ("Create Date",      ["createTime"]),
    ("Host URN",         ["locationUrn", "hostUrn", "location"]),
    ("Cluster URN",      ["clusterUrn"]),
    ("URN",              ["urn"]),
])

CPU_FIELDS = OrderedDict([
    ("VM Name",              ["name"]),
    ("Total CPUs",           ["vmConfig.cpu.quantity"]),
    ("Cores Per Socket",     ["vmConfig.cpu.coresPerSocket"]),
    ("CPU Reservation MHz",  ["vmConfig.cpu.reservation"]),
    ("CPU Limit MHz",        ["vmConfig.cpu.limit"]),
    ("CPU Weight",           ["vmConfig.cpu.weight"]),
    ("CPU Hot Plug",         ["vmConfig.cpu.cpuHotPlug"]),
    ("CPU Bind Type",        ["vmConfig.cpu.cpuBindType"]),
    ("CPU Policy",           ["vmConfig.cpu.cpuPolicy"]),
])

MEMORY_FIELDS = OrderedDict([
    ("VM Name",              ["name"]),
    ("Memory (MB)",          ["vmConfig.memory.quantityMB"]),
    ("Reservation (MB)",     ["vmConfig.memory.reservation"]),
    ("Limit (MB)",           ["vmConfig.memory.limit"]),
    ("Weight",               ["vmConfig.memory.weight"]),
    ("Memory Hot Plug",      ["vmConfig.memory.memHotPlug"]),
    ("Huge Page",            ["vmConfig.memory.hugePage"]),
])

DISK_FIELDS = OrderedDict([
    ("Disk Name",        ["volumeUuid", "volumeUrn", "name"]),
    ("Capacity (GB)",    ["quantityGB"]),
    ("Bus Type",         ["pciType", "busType"]),
    ("Thin Provision",   ["isThin", "thinFlag"]),
    ("Sequence",         ["sequenceNum"]),
    ("Datastore URN",    ["datastoreUrn"]),
    ("Storage Type",     ["storageType"]),
    ("Independent",      ["indepDisk"]),
    ("Persistent",       ["persistentDisk"]),
    ("Volume URN",       ["volumeUrn"]),
])

NIC_FIELDS = OrderedDict([
    ("NIC Name",         ["name"]),
    ("MAC Address",      ["mac"]),
    ("IP Address",       ["ip"]),
    ("IP List",          ["ipList"]),
    ("IPv6",             ["ipv6s"]),
    ("Port Group",       ["portGroupName"]),
    ("Port Group URN",   ["portGroupUrn"]),
    ("Port Group Type",  ["portGroupType"]),
    ("VLAN Range",       ["portGroupVlanRange"]),
    ("Sequence",         ["sequenceNum"]),
    ("VirtIO",           ["virtIo"]),
    ("NIC Type",         ["nicType", "virtualNicType"]),
    ("Connect at PowerOn", ["connectAtPowerOn"]),
    ("URN",              ["urn"]),
])

HOST_FIELDS = OrderedDict([
    ("Host Name",        ["name"]),
    ("IP Address",       ["ip"]),
    ("Status",           ["status"]),
    ("CPU Model",        ["cpuModel", "cpuType"]),
    ("CPU Cores",        ["cpuQuantity", "cpuCores"]),
    ("CPU MHz",          ["cpuMHz", "cpuFrequency"]),
    ("Memory Total (MB)", ["memoryQuantityMB", "memoryCapacity", "memResource.totalSizeMB"]),
    ("Memory Used (MB)", ["memoryUsedMB", "memResource.usedSizeMB"]),
    ("Running VMs",      ["runningVmCount"]),
    ("Cluster URN",      ["clusterUrn"]),
    ("BMC IP",           ["bmcIp"]),
    ("Maintenance",      ["isMaintaining"]),
    ("Hypervisor",       ["hypervisor"]),
    ("URN",              ["urn"]),
])

CLUSTER_FIELDS = OrderedDict([
    ("Cluster Name",       ["name"]),
    ("Description",        ["description"]),
    ("Tag",                ["tag"]),
    ("HA Enabled",         ["isEnableHa", "isHA"]),
    ("DRS Enabled",        ["isEnableDrs", "isDRS"]),
    ("Mem Overcommit",     ["isMemOvercommit"]),
    ("Auto Adjust NUMA",   ["isAutoAdjustNuma"]),
    ("Resource Strategy",  ["resStrategy"]),
    ("DRS Level",          ["drsSetting.drsLevel"]),
    ("CPU Reservation",    ["haResSetting.cpuReservation"]),
    ("Memory Reservation", ["haResSetting.memoryReservation"]),
    ("URN",                ["urn"]),
])

DATASTORE_FIELDS = OrderedDict([
    ("Datastore Name",   ["name"]),
    ("Storage Type",     ["storageType"]),
    ("Capacity (GB)",    ["capacityGB"]),
    ("Free (GB)",        ["freeSpaceGB", "freeSpace", "freeCapacityGB", "freeSizeGB"]),
    ("Status",           ["status"]),
    ("Thin Support",     ["thinProvisionSupport"]),
    ("Description",      ["description"]),
    ("URN",              ["urn"]),
])


def _build_row(data, field_map, extras_allowed=True):
    """
    Build a row dict using field_map (try multiple candidates per column).
    Optionally include any extra raw fields not consumed by the mapping.
    """
    row = OrderedDict()
    consumed = set()

    for column, candidates in field_map.items():
        value = ""
        for path in candidates:
            v = _get_path(data, path)
            if v is not None and v != "":
                value = v
                consumed.add(path)
                break
        row[column] = value

    if extras_allowed:
        flat = _flatten_dict(data)
        for k, v in flat.items():
            if k in consumed:
                continue
            if v is None or v == "":
                continue
            pretty = _prettify_key(k)
            if pretty not in row:
                row[pretty] = v

    return row


def _prettify_key(key):
    """Convert 'vmConfig.cpu.quantity' -> 'CPU - Quantity'."""
    parts = key.split(".")
    if len(parts) >= 2:
        label = ".".join(parts[-2:])
    else:
        label = parts[-1]
    label = re.sub(r"([a-z])([A-Z])", r"\1 \2", label)
    label = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", label)
    label = label.replace(".", " - ").replace("_", " ").title()
    return label


# ── Collector ───────────────────────────────────────────────

class InventoryCollector:
    """Collects inventory data from FusionCompute and flattens it for Excel export."""

    def __init__(self, host, username, password, port=7443):
        self.client = FCClient(host, username, password, port=port)
        self.cancelled = False
        self.progress = {
            "status": "idle",
            "current_step": "",
            "percent": 0,
            "error": None,
        }

        # Raw data from API
        self.sites = []
        self.clusters = []
        self.hosts = []
        self.host_details = {}
        self.vms = []
        self.vm_details = {}
        self.vm_nics = {}
        self.vm_disks = {}
        self.datastores = []
        self.dvswitches = []
        self.portgroups = []

        # Lookup maps (URN -> name)
        self.host_map = {}
        self.cluster_map = {}
        self.datastore_map = {}

    def cancel(self):
        self.cancelled = True

    def _check_cancelled(self):
        if self.cancelled:
            raise InterruptedError("Collection cancelled by user.")

    def _update_progress(self, percent, step):
        self._check_cancelled()
        self.progress["percent"] = percent
        self.progress["current_step"] = step
        logger.info(f"[{percent}%] {step}")

    def _log_sample(self, label, obj):
        """Log full flat keys of a sample object for debugging."""
        if not obj:
            logger.info(f"=== SAMPLE {label}: empty ===")
            return
        logger.info(f"=== SAMPLE {label} top-level keys: {list(obj.keys()) if isinstance(obj, dict) else type(obj)} ===")
        flat = _flatten_dict(obj)
        for k, v in list(flat.items())[:80]:
            logger.info(f"  {k} = {repr(v)[:100]}")
        # Also log list-of-dict children
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    logger.info(f"  [LIST] {k}: {len(v)} items, first keys = {list(v[0].keys())}")

    def collect_all(self):
        """Run the full collection pipeline. Returns dict of sheet data."""
        self.progress["status"] = "running"

        try:
            # Step 1: Login
            self._update_progress(5, "Logging in to FusionCompute...")
            self.client.login()

            # Step 2: Get sites
            self._update_progress(10, "Fetching sites...")
            self.sites = self.client.get_sites()
            if self.sites:
                self._log_sample("SITE", self.sites[0])

            for site in self.sites:
                site_uri = site.get("uri", "")

                # Step 3: Clusters
                self._update_progress(15, "Fetching clusters...")
                clusters = self.client.get_clusters(site_uri)
                self.clusters.extend(clusters)
                if clusters:
                    self._log_sample("CLUSTER", clusters[0])

                # Step 4: Hosts
                self._update_progress(20, "Fetching hosts...")
                hosts = self.client.get_hosts(site_uri)
                self.hosts.extend(hosts)
                if hosts:
                    self._log_sample("HOST list", hosts[0])

                for i, host in enumerate(hosts):
                    host_uri = host.get("uri", "")
                    host_name = host.get("name", host_uri)
                    pct = 20 + int((i / max(len(hosts), 1)) * 10)
                    self._update_progress(pct, f"Fetching host detail: {host_name}...")
                    try:
                        detail = self.client.get_host_detail(host_uri)
                        self.host_details[host.get("urn")] = detail
                        if i == 0:
                            self._log_sample("HOST detail", detail)
                    except Exception as e:
                        logger.warning(f"Failed to get host detail {host_name}: {e}")

                # Step 5: Datastores
                self._update_progress(35, "Fetching datastores...")
                datastores = self.client.get_datastores(site_uri)
                self.datastores.extend(datastores)
                if datastores:
                    self._log_sample("DATASTORE", datastores[0])

                # Step 6: Networks
                self._update_progress(40, "Fetching networks...")
                dvswitches = self.client.get_dvswitches(site_uri)
                self.dvswitches.extend(dvswitches)
                if dvswitches:
                    self._log_sample("DVSWITCH", dvswitches[0])

                for dvs in dvswitches:
                    dvs_uri = dvs.get("uri", "")
                    dvs_name = dvs.get("name", dvs_uri)
                    try:
                        pgs = self.client.get_portgroups(dvs_uri)
                        for pg in pgs:
                            pg["_dvswitch_name"] = dvs_name
                        self.portgroups.extend(pgs)
                        if pgs and not getattr(self, "_pg_logged", False):
                            self._log_sample("PORTGROUP", pgs[0])
                            self._pg_logged = True
                    except Exception as e:
                        logger.warning(f"Failed to get portgroups for DVS {dvs_name}: {e}")

                # Fallback: try fetching all portgroups at site level
                if not self.portgroups:
                    try:
                        site_pgs = self.client.get_site_portgroups(site_uri)
                        for pg in site_pgs:
                            pg.setdefault("_dvswitch_name", "")
                        self.portgroups.extend(site_pgs)
                        if site_pgs and not getattr(self, "_pg_logged", False):
                            self._log_sample("PORTGROUP (site-level)", site_pgs[0])
                            self._pg_logged = True
                    except Exception as e:
                        logger.warning(f"Site portgroup fallback failed: {e}")

                # Step 7: VMs
                self._update_progress(45, "Fetching VM list...")
                vms = self.client.get_vms(site_uri)
                self.vms.extend(vms)
                if vms:
                    self._log_sample("VM list", vms[0])

                # Step 8: VM details (NICs and disks come from vmConfig)
                total_vms = len(vms)
                for i, vm in enumerate(vms):
                    vm_uri = vm.get("uri", "")
                    vm_name = vm.get("name", vm_uri)
                    vm_urn = vm.get("urn", "")
                    pct = 50 + int((i / max(total_vms, 1)) * 40)
                    self._update_progress(pct, f"Fetching VM detail ({i+1}/{total_vms}): {vm_name}...")

                    try:
                        detail = self.client.get_vm_detail(vm_uri)
                        self.vm_details[vm_urn] = detail

                        if i == 0:
                            self._log_sample("VM detail", detail)

                        # Extract NICs and Disks from vmConfig (inline)
                        vm_config = detail.get("vmConfig", {}) or {}
                        nics = vm_config.get("nics") or detail.get("nics") or []
                        disks = vm_config.get("disks") or vm_config.get("volumes") or detail.get("disks") or []
                        self.vm_nics[vm_urn] = nics
                        self.vm_disks[vm_urn] = disks

                        if i == 0:
                            logger.info(f"=== First VM has {len(nics)} NICs, {len(disks)} disks ===")
                            if nics:
                                logger.info(f"  NIC[0] keys: {list(nics[0].keys())}")
                            if disks:
                                logger.info(f"  DISK[0] keys: {list(disks[0].keys())}")

                    except Exception as e:
                        logger.warning(f"Failed to get VM detail {vm_name}: {e}")

            # Step 9: Build lookup maps and flatten data
            self._update_progress(92, "Processing collected data...")
            self._build_lookup_maps()
            result = self._build_all_sheets()

            # Step 10: Logout
            self._update_progress(98, "Logging out...")
            self.client.logout()

            self.progress["percent"] = 100
            self.progress["current_step"] = "Collection complete!"
            self.progress["status"] = "done"

            return result

        except InterruptedError:
            self.progress["status"] = "cancelled"
            self.progress["current_step"] = "Cancelled"
            try:
                self.client.logout()
            except Exception:
                pass
            raise

        except Exception as e:
            logger.exception("Collection failed")
            self.progress["status"] = "error"
            self.progress["error"] = str(e)
            self.progress["current_step"] = "Error occurred"
            try:
                self.client.logout()
            except Exception:
                pass
            raise

    def _build_lookup_maps(self):
        """Build URN -> name lookup maps for cross-referencing."""
        for h in self.hosts:
            self.host_map[h.get("urn", "")] = h.get("name", "")
        for c in self.clusters:
            self.cluster_map[c.get("urn", "")] = c.get("name", "")
        for d in self.datastores:
            self.datastore_map[d.get("urn", "")] = d.get("name", "")

    def _build_all_sheets(self):
        """Build all Excel sheet data from collected raw data."""
        vinfo = self._build_vinfo()
        return {
            "vSummary": self._build_vsummary(vinfo),
            "vInfo": vinfo,
            "vCPU": self._build_vcpu(),
            "vMemory": self._build_vmemory(),
            "vDisk": self._build_vdisk(),
            "vNetwork": self._build_vnetwork(),
            "vHost": self._build_vhost(),
            "vCluster": self._build_vcluster(),
            "vDatastore": self._build_vdatastore(),
            "vSwitch": self._build_vswitch(),
        }

    # ── Sheet builders ───────────────────────────────────

    def _build_vsummary(self, vinfo):
        from collections import Counter
        power_count = Counter()
        cluster_power = {}

        for vm in vinfo:
            power = vm.get("Power State", "")
            cluster = vm.get("Cluster", "N/A") or "N/A"
            power_count[power] += 1
            cluster_power.setdefault(cluster, {"ON": 0, "OFF": 0, "Other": 0})
            if power == "ON":
                cluster_power[cluster]["ON"] += 1
            elif power == "OFF":
                cluster_power[cluster]["OFF"] += 1
            else:
                cluster_power[cluster]["Other"] += 1

        rows = [
            {"Item": "Total VMs", "Count": len(vinfo)},
            {"Item": "Power ON", "Count": power_count.get("ON", 0)},
            {"Item": "Power OFF", "Count": power_count.get("OFF", 0)},
            {"Item": "Total Hosts", "Count": len(self.hosts)},
            {"Item": "Total Clusters", "Count": len(self.clusters)},
            {"Item": "Total Datastores", "Count": len(self.datastores)},
            {"Item": "Total DVSwitches", "Count": len(self.dvswitches)},
            {"Item": "Total Port Groups", "Count": len(self.portgroups)},
            {"Item": "", "Count": ""},
            {"Item": "=== Power State by Cluster ===", "Count": ""},
        ]
        for cluster, counts in sorted(cluster_power.items()):
            rows.append({
                "Item": f"  {cluster}",
                "Count": f"ON: {counts['ON']}  OFF: {counts['OFF']}",
            })
        return rows

    def _merged_vm(self, vm):
        """Merge VM list entry with detail (detail takes precedence)."""
        urn = vm.get("urn", "")
        detail = self.vm_details.get(urn, {}) or {}
        merged = {**vm, **detail}
        return merged

    def _build_vinfo(self):
        rows = []
        for vm in self.vms:
            merged = self._merged_vm(vm)
            urn = vm.get("urn", "")
            disks = self.vm_disks.get(urn, [])
            nics = self.vm_nics.get(urn, [])

            total_disk = sum(d.get("quantityGB", 0) or 0 for d in disks)
            ip_list = ", ".join(n.get("ip", "") for n in nics if n.get("ip"))

            row = OrderedDict()
            row["VM Name"] = _try_paths(merged, ["name"])
            row["UUID"] = _try_paths(merged, ["uuid"])
            row["Power State"] = _power_state(_try_paths(merged, ["status"]))
            row["Status"] = _try_paths(merged, ["status"])
            row["Guest OS"] = _try_paths(merged, ["osOptions.osType", "vmConfig.osOptions.osType"])
            row["CPUs"] = _try_paths(merged, ["vmConfig.cpu.quantity", "cpu.quantity"])
            row["Cores Per Socket"] = _try_paths(merged, ["vmConfig.cpu.coresPerSocket"])
            row["Memory (MB)"] = _try_paths(merged, ["vmConfig.memory.quantityMB", "memory.quantityMB"])
            row["Total Disk (GB)"] = total_disk
            row["NICs"] = len(nics)
            row["IP Addresses"] = ip_list
            row["Host"] = self.host_map.get(_try_paths(merged, ["locationUrn", "hostUrn"]), "") or _try_paths(merged, ["hostName", "locationName"])
            row["Cluster"] = self.cluster_map.get(_try_paths(merged, ["clusterUrn"]), "") or _try_paths(merged, ["clusterName"])
            row["VM Tools"] = _try_paths(merged, ["toolsVersion", "pvDriverStatus", "toolInstallStatus"])
            row["Description"] = _try_paths(merged, ["description"])
            row["Create Date"] = _try_paths(merged, ["createTime"])
            row["URN"] = urn
            rows.append(row)
        return rows

    def _build_vcpu(self):
        rows = []
        for vm in self.vms:
            merged = self._merged_vm(vm)
            row = OrderedDict()
            row["VM Name"] = _try_paths(merged, ["name"])
            row["UUID"] = _try_paths(merged, ["uuid"])
            row["Power"] = _power_state(_try_paths(merged, ["status"]))
            qty = _try_paths(merged, ["vmConfig.cpu.quantity"])
            cps = _try_paths(merged, ["vmConfig.cpu.coresPerSocket"])
            row["Total CPUs"] = qty
            row["Cores Per Socket"] = cps
            try:
                row["Sockets"] = int(qty) // max(int(cps), 1) if qty and cps else ""
            except (ValueError, TypeError):
                row["Sockets"] = ""
            row["CPU Reservation (MHz)"] = _try_paths(merged, ["vmConfig.cpu.reservation"])
            row["CPU Limit (MHz)"] = _try_paths(merged, ["vmConfig.cpu.limit"])
            row["CPU Weight"] = _try_paths(merged, ["vmConfig.cpu.weight"])
            row["CPU Hot Plug"] = _try_paths(merged, ["vmConfig.cpu.cpuHotPlug"])
            row["CPU Bind Type"] = _try_paths(merged, ["vmConfig.cpu.cpuBindType"])
            row["CPU Policy"] = _try_paths(merged, ["vmConfig.cpu.cpuPolicy"])
            row["Host"] = self.host_map.get(_try_paths(merged, ["locationUrn", "hostUrn"]), "")
            row["Cluster"] = self.cluster_map.get(_try_paths(merged, ["clusterUrn"]), "")
            rows.append(row)
        return rows

    def _build_vmemory(self):
        rows = []
        for vm in self.vms:
            merged = self._merged_vm(vm)
            row = OrderedDict()
            row["VM Name"] = _try_paths(merged, ["name"])
            row["UUID"] = _try_paths(merged, ["uuid"])
            row["Power"] = _power_state(_try_paths(merged, ["status"]))
            row["Memory (MB)"] = _try_paths(merged, ["vmConfig.memory.quantityMB"])
            row["Reservation (MB)"] = _try_paths(merged, ["vmConfig.memory.reservation"])
            row["Limit (MB)"] = _try_paths(merged, ["vmConfig.memory.limit"])
            row["Weight"] = _try_paths(merged, ["vmConfig.memory.weight"])
            row["Memory Hot Plug"] = _try_paths(merged, ["vmConfig.memory.memHotPlug"])
            row["Huge Page"] = _try_paths(merged, ["vmConfig.memory.hugePage"])
            row["Host"] = self.host_map.get(_try_paths(merged, ["locationUrn", "hostUrn"]), "")
            row["Cluster"] = self.cluster_map.get(_try_paths(merged, ["clusterUrn"]), "")
            rows.append(row)
        return rows

    def _build_vdisk(self):
        rows = []
        for vm in self.vms:
            urn = vm.get("urn", "")
            disks = self.vm_disks.get(urn, [])
            for disk in disks:
                row = OrderedDict()
                row["VM Name"] = vm.get("name", "")
                row["Power"] = _power_state(vm.get("status", ""))
                row["Disk Name"] = _try_paths(disk, ["name", "volumeName"])
                row["Disk UUID"] = _try_paths(disk, ["volumeUuid"])
                row["Capacity (GB)"] = _try_paths(disk, ["quantityGB"])
                row["Bus Type"] = _try_paths(disk, ["pciType", "busType"])
                row["Thin Provision"] = _try_paths(disk, ["isThin", "thinFlag"])
                row["Sequence"] = _try_paths(disk, ["sequenceNum"])
                ds_urn = _try_paths(disk, ["datastoreUrn"])
                row["Datastore"] = self.datastore_map.get(ds_urn, ds_urn)
                row["Datastore URN"] = ds_urn
                row["Storage Type"] = _try_paths(disk, ["storageType"])
                row["Independent"] = _try_paths(disk, ["indepDisk"])
                row["Persistent"] = _try_paths(disk, ["persistentDisk"])
                row["Volume URN"] = _try_paths(disk, ["volumeUrn"])
                rows.append(row)
        return rows

    def _build_vnetwork(self):
        rows = []
        for vm in self.vms:
            urn = vm.get("urn", "")
            merged = self._merged_vm(vm)
            vm_uuid = _try_paths(merged, ["uuid"])
            nics = self.vm_nics.get(urn, [])
            for nic in nics:
                row = OrderedDict()
                row["VM Name"] = vm.get("name", "")
                row["VM UUID"] = vm_uuid
                row["Power"] = _power_state(vm.get("status", ""))
                row["NIC Name"] = _try_paths(nic, ["name"])
                row["MAC Address"] = _try_paths(nic, ["mac"])
                row["IP Address"] = _try_paths(nic, ["ip"])
                row["IP List"] = _try_paths(nic, ["ipList"])
                row["IPv6"] = _try_paths(nic, ["ipv6s"])
                row["Port Group"] = _try_paths(nic, ["portGroupName"])
                row["Port Group URN"] = _try_paths(nic, ["portGroupUrn"])
                row["Port Group Type"] = _try_paths(nic, ["portGroupType"])
                row["VLAN Range"] = _try_paths(nic, ["portGroupVlanRange"])
                row["Sequence"] = _try_paths(nic, ["sequenceNum"])
                row["VirtIO"] = _try_paths(nic, ["virtIo"])
                row["NIC Type"] = _try_paths(nic, ["nicType", "virtualNicType"])
                row["Connect at Power-On"] = _try_paths(nic, ["connectAtPowerOn"])
                row["URN"] = _try_paths(nic, ["urn"])
                rows.append(row)
        return rows

    def _build_vhost(self):
        rows = []
        # Count VMs per host
        host_vm_count = {}
        for vm in self.vms:
            h = vm.get("locationUrn") or vm.get("hostUrn") or ""
            if h:
                host_vm_count[h] = host_vm_count.get(h, 0) + 1

        for host in self.hosts:
            urn = host.get("urn", "")
            detail = self.host_details.get(urn, {}) or {}
            merged = {**host, **detail}

            row = OrderedDict()
            row["Host Name"] = _try_paths(merged, ["name"])
            row["IP Address"] = _try_paths(merged, ["ip"])
            row["Status"] = _try_paths(merged, ["status"])
            row["Cluster"] = self.cluster_map.get(_try_paths(merged, ["clusterUrn"]), "")
            row["CPU Model"] = _try_paths(merged, ["cpuModel", "cpuType"])
            row["CPU Cores"] = _try_paths(merged, ["cpuQuantity", "cpuCores"])
            row["CPU MHz"] = _try_paths(merged, ["cpuMHz", "cpuFrequency"])
            row["Memory Total (MB)"] = _try_paths(merged, ["memoryQuantityMB", "memoryCapacity"])
            row["Memory Used (MB)"] = _try_paths(merged, ["memoryUsedMB"])
            row["Running VMs"] = _try_paths(merged, ["runningVmCount"]) or host_vm_count.get(urn, 0)
            row["BMC IP"] = _try_paths(merged, ["bmcIp"])
            row["Maintenance"] = _try_paths(merged, ["isMaintaining"])
            row["Hypervisor"] = _try_paths(merged, ["hypervisor"])
            row["URN"] = urn
            rows.append(row)
        return rows

    def _build_vcluster(self):
        rows = []
        # Count hosts per cluster
        cluster_host_count = {}
        for h in self.hosts:
            c = h.get("clusterUrn", "")
            if c:
                cluster_host_count[c] = cluster_host_count.get(c, 0) + 1

        for cluster in self.clusters:
            urn = cluster.get("urn", "")
            row = OrderedDict()
            row["Cluster Name"] = _try_paths(cluster, ["name"])
            row["Description"] = _try_paths(cluster, ["description"])
            row["Tag"] = _try_paths(cluster, ["tag"])
            row["HA Enabled"] = _try_paths(cluster, ["isEnableHa", "isHA"])
            row["DRS Enabled"] = _try_paths(cluster, ["isEnableDrs", "isDRS"])
            row["Mem Overcommit"] = _try_paths(cluster, ["isMemOvercommit"])
            row["Auto Adjust NUMA"] = _try_paths(cluster, ["isAutoAdjustNuma"])
            row["DRS Level"] = _try_paths(cluster, ["drsSetting.drsLevel"])
            row["CPU Reservation"] = _try_paths(cluster, ["haResSetting.cpuReservation"])
            row["Memory Reservation"] = _try_paths(cluster, ["haResSetting.memoryReservation"])
            row["Total Hosts"] = cluster_host_count.get(urn, _try_paths(cluster, ["hostNum"]))
            row["URN"] = urn
            rows.append(row)
        return rows

    def _build_vdatastore(self):
        rows = []
        for ds in self.datastores:
            row = OrderedDict()
            row["Datastore Name"] = _try_paths(ds, ["name"])
            row["Storage Type"] = _try_paths(ds, ["storageType"])

            # Capacity: try GB first, then MB conversion
            cap = _try_paths(ds, ["capacityGB"])
            if not cap:
                cap_mb = _try_paths(ds, ["capacityMB", "totalSizeMB"])
                if cap_mb:
                    try:
                        cap = round(float(cap_mb) / 1024, 2)
                    except (ValueError, TypeError):
                        cap = ""
            row["Capacity (GB)"] = cap

            # Free: try multiple field names
            free = _try_paths(ds, ["freeSpaceGB", "freeSpace", "freeCapacityGB", "freeSizeGB"])
            if not free:
                free_mb = _try_paths(ds, ["freeSpaceMB", "freeSizeMB"])
                if free_mb:
                    try:
                        free = round(float(free_mb) / 1024, 2)
                    except (ValueError, TypeError):
                        free = ""
            row["Free (GB)"] = free

            # Used %
            used_pct = ""
            try:
                if cap and free and float(cap) > 0:
                    used_pct = round((1 - float(free) / float(cap)) * 100, 1)
            except (ValueError, TypeError):
                pass
            row["Used %"] = used_pct

            row["Status"] = _try_paths(ds, ["status"])
            row["Thin Support"] = _try_paths(ds, ["thinProvisionSupport"])
            row["Description"] = _try_paths(ds, ["description"])
            row["URN"] = _try_paths(ds, ["urn"])
            rows.append(row)
        return rows

    def _build_vswitch(self):
        rows = []
        for dvs in self.dvswitches:
            row = OrderedDict()
            row["Name"] = _try_paths(dvs, ["name"])
            row["Type"] = "DVSwitch"
            row["VLAN ID"] = ""
            row["MTU"] = _try_paths(dvs, ["mtu"])
            row["Description"] = _try_paths(dvs, ["description"])
            row["Parent"] = ""
            row["URN"] = _try_paths(dvs, ["urn"])
            rows.append(row)

        for pg in self.portgroups:
            row = OrderedDict()
            row["Name"] = _try_paths(pg, ["name"])
            row["Type"] = "Port Group"
            row["VLAN ID"] = _try_paths(pg, ["vlanId"])
            row["MTU"] = _try_paths(pg, ["mtu"])
            row["Description"] = _try_paths(pg, ["description"])
            row["Parent"] = pg.get("_dvswitch_name", "")
            row["URN"] = _try_paths(pg, ["urn"])
            rows.append(row)
        return rows

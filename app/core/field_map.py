"""Hybrid field mapping tables and path-walking helpers.

This is a 1:1 port of the OrderedDict tables in `collector.py:76-184` of
the v1.0.0 Python tool, plus the `_get_path` / `_try_paths` /
`_flatten_dict` / `_prettify_key` helpers at `collector.py:21-72` /
`219-229`. The order of paths within each key encodes priority
(first non-empty wins); the order of keys is determined by the
caller (the Excel writer and the sheet builders).
"""

from __future__ import annotations

import re
from collections import OrderedDict

# ── Helpers ─────────────────────────────────────────────────


def get_path(d: object, path: str) -> object | None:
    """Walk a dot-separated path inside a nested dict.

    Mirrors `collector._get_path` (collector.py:21-34).
    """
    if not isinstance(d, dict) or not path:
        return None
    cur: object = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)  # type: ignore[union-attr]
        if cur is None:
            return None
    return cur


def try_paths(d: object, paths: list[str]) -> object:
    """Return the first non-None, non-empty value found at any of the given paths.

    Mirrors `collector._try_paths` (collector.py:37-43). Empty string is
    treated as "missing" so fields like `description: ""` don't get
    rendered as the literal value.
    """
    for p in paths:
        v = get_path(d, p)
        if v is None:
            continue
        if isinstance(v, str) and v == "":
            continue
        return v
    return ""


def try_string(d: object, paths: list[str]) -> str:
    """Stringify the first matching value (numbers/bools via `str()`).

    Mirrors `collector._try_paths` + the `str(v)` formatting used by
    the sheet builders.
    """
    v = try_paths(d, paths)
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def flatten_dict(d: object, parent: str = "", sep: str = ".") -> dict[str, object]:
    """Flatten a nested dict. Lists-of-dicts are skipped; primitive lists are joined with `, `.

    Mirrors `collector._flatten_dict` (collector.py:46-62).
    """
    out: dict[str, object] = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        new_key = f"{parent}{sep}{k}" if parent else k
        if isinstance(v, dict):
            out.update(flatten_dict(v, new_key, sep))
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                # Skip sub-tables (handled separately)
                continue
            out[new_key] = ", ".join(str(x) for x in v) if v else ""
        else:
            out[new_key] = v
    return out


def power_state(status: object) -> str:
    """Convert an FC status string to ON/OFF.

    Mirrors `collector._power_state` (collector.py:65-71).
    """
    s = status if isinstance(status, str) else ""
    if s in ("running", "started", "Running", "RUNNING"):
        return "ON"
    if s in ("stopped", "shutOff", "Stopped", "STOPPED"):
        return "OFF"
    return s


# ── Field Mappings ──────────────────────────────────────────
#
# These are the 8 column -> candidate-paths tables. Each key is the
# Excel column header; the list value is the priority-ordered list of
# candidate JSON paths to try. The first path that resolves to a
# non-empty value wins.

# vInfo sheet (collector.py:76-89)
VM_FIELDS: OrderedDict[str, list[str]] = OrderedDict(
    [
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
    ]
)

# vCPU sheet (collector.py:91-101)
CPU_FIELDS: OrderedDict[str, list[str]] = OrderedDict(
    [
        ("VM Name",              ["name"]),
        ("Total CPUs",           ["vmConfig.cpu.quantity"]),
        ("Cores Per Socket",     ["vmConfig.cpu.coresPerSocket"]),
        ("CPU Reservation MHz",  ["vmConfig.cpu.reservation"]),
        ("CPU Limit MHz",        ["vmConfig.cpu.limit"]),
        ("CPU Weight",           ["vmConfig.cpu.weight"]),
        ("CPU Hot Plug",         ["vmConfig.cpu.cpuHotPlug"]),
        ("CPU Bind Type",        ["vmConfig.cpu.cpuBindType"]),
        ("CPU Policy",           ["vmConfig.cpu.cpuPolicy"]),
    ]
)

# vMemory sheet (collector.py:103-111)
MEMORY_FIELDS: OrderedDict[str, list[str]] = OrderedDict(
    [
        ("VM Name",          ["name"]),
        ("Memory (MB)",      ["vmConfig.memory.quantityMB"]),
        ("Reservation (MB)", ["vmConfig.memory.reservation"]),
        ("Limit (MB)",       ["vmConfig.memory.limit"]),
        ("Weight",           ["vmConfig.memory.weight"]),
        ("Memory Hot Plug",  ["vmConfig.memory.memHotPlug"]),
        ("Huge Page",        ["vmConfig.memory.hugePage"]),
    ]
)

# vDisk sheet (collector.py:113-124)
DISK_FIELDS: OrderedDict[str, list[str]] = OrderedDict(
    [
        ("Disk Name",      ["volumeUuid", "volumeUrn", "name"]),
        ("Capacity (GB)",  ["quantityGB"]),
        ("Bus Type",       ["pciType", "busType"]),
        ("Thin Provision", ["isThin", "thinFlag"]),
        ("Sequence",       ["sequenceNum"]),
        ("Datastore URN",  ["datastoreUrn"]),
        ("Storage Type",   ["storageType"]),
        ("Independent",    ["indepDisk"]),
        ("Persistent",     ["persistentDisk"]),
        ("Volume URN",     ["volumeUrn"]),
    ]
)

# vNetwork sheet (collector.py:126-141)
NIC_FIELDS: OrderedDict[str, list[str]] = OrderedDict(
    [
        ("NIC Name",           ["name"]),
        ("MAC Address",        ["mac"]),
        ("IP Address",         ["ip"]),
        ("IP List",            ["ipList"]),
        ("IPv6",               ["ipv6s"]),
        ("Port Group",         ["portGroupName"]),
        ("Port Group URN",     ["portGroupUrn"]),
        ("Port Group Type",    ["portGroupType"]),
        ("VLAN Range",         ["portGroupVlanRange"]),
        ("Sequence",           ["sequenceNum"]),
        ("VirtIO",             ["virtIo"]),
        ("NIC Type",           ["nicType", "virtualNicType"]),
        ("Connect at PowerOn", ["connectAtPowerOn"]),
        ("URN",                ["urn"]),
    ]
)

# vHost sheet (collector.py:143-158)
HOST_FIELDS: OrderedDict[str, list[str]] = OrderedDict(
    [
        ("Host Name",         ["name"]),
        ("IP Address",        ["ip"]),
        ("Status",            ["status"]),
        ("CPU Model",         ["cpuModel", "cpuType"]),
        ("CPU Cores",         ["cpuQuantity", "cpuCores"]),
        ("CPU MHz",           ["cpuMHz", "cpuFrequency"]),
        ("Memory Total (MB)", ["memoryQuantityMB", "memoryCapacity", "memResource.totalSizeMB"]),
        ("Memory Used (MB)",  ["memoryUsedMB", "memResource.usedSizeMB"]),
        ("Running VMs",       ["runningVmCount"]),
        ("Cluster URN",       ["clusterUrn"]),
        ("BMC IP",            ["bmcIp"]),
        ("Maintenance",       ["isMaintaining"]),
        ("Hypervisor",        ["hypervisor"]),
        ("URN",               ["urn"]),
    ]
)

# vCluster sheet (collector.py:160-173)
CLUSTER_FIELDS: OrderedDict[str, list[str]] = OrderedDict(
    [
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
    ]
)

# vDatastore sheet (collector.py:175-184)
DATASTORE_FIELDS: OrderedDict[str, list[str]] = OrderedDict(
    [
        ("Datastore Name", ["name"]),
        ("Storage Type",   ["storageType"]),
        ("Capacity (GB)",  ["capacityGB"]),
        ("Free (GB)",      ["freeSpaceGB", "freeSpace", "freeCapacityGB", "freeSizeGB"]),
        ("Status",         ["status"]),
        ("Thin Support",   ["thinProvisionSupport"]),
        ("Description",    ["description"]),
        ("URN",            ["urn"]),
    ]
)


def build_row(
    data: object,
    field_map: OrderedDict[str, list[str]],
    extras_allowed: bool = True,
) -> OrderedDict[str, object]:
    """Build a row dict using field_map (first non-empty candidate per column).

    Optionally append any extra raw fields not consumed by the mapping,
    with prettified names. Mirrors `collector._build_row`
    (collector.py:187-216).
    """
    row: OrderedDict[str, object] = OrderedDict()
    consumed: set[str] = set()

    for column, candidates in field_map.items():
        value: object = ""
        for path in candidates:
            v = get_path(data, path)
            if v is None:
                continue
            if isinstance(v, str) and v == "":
                continue
            value = v
            consumed.add(path)
            break
        row[column] = value

    if extras_allowed:
        flat = flatten_dict(data)
        for k, v in flat.items():
            if k in consumed:
                continue
            if v is None or v == "":
                continue
            pretty = prettify_key(k)
            if pretty not in row:
                row[pretty] = v

    return row


def prettify_key(key: str) -> str:
    """Convert a dotted JSON path to a readable column header.

    Mirrors `collector._prettify_key` (collector.py:219-229).
    """
    parts = key.split(".")
    if len(parts) >= 2:
        label = ".".join(parts[-2:])
    else:
        label = parts[-1]
    label = re.sub(r"([a-z])([A-Z])", r"\1 \2", label)
    label = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", label)
    label = label.replace(".", " - ").replace("_", " ").title()
    return label

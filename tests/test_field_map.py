"""Tests for the hybrid field mapper (path helpers + 8 *_FIELDS tables)."""

from __future__ import annotations

import pytest

from app.core import field_map

# ── get_path ────────────────────────────────────────────────


def test_get_path_simple_key():
    assert field_map.get_path({"name": "vm-01"}, "name") == "vm-01"


def test_get_path_dotted():
    data = {"vmConfig": {"cpu": {"quantity": 4}}}
    assert field_map.get_path(data, "vmConfig.cpu.quantity") == 4


def test_get_path_missing_returns_none():
    assert field_map.get_path({}, "x.y.z") is None
    assert field_map.get_path({"a": 1}, "a.b") is None


def test_get_path_empty_path():
    assert field_map.get_path({"a": 1}, "") is None


def test_get_path_non_dict_intermediate():
    assert field_map.get_path({"a": 5}, "a.b") is None


# ── try_paths ──────────────────────────────────────────────


def test_try_paths_first_hit_wins():
    data = {"vmConfig": {"cpu": {"quantity": 4}}}
    assert field_map.try_paths(data, ["vmConfig.cpu.quantity", "cpu.quantity"]) == 4


def test_try_paths_falls_back_to_second():
    data = {"cpu": {"quantity": 8}}
    assert field_map.try_paths(data, ["vmConfig.cpu.quantity", "cpu.quantity"]) == 8


def test_try_paths_empty_string_treated_as_missing():
    data = {"a": "", "b": "real"}
    assert field_map.try_paths(data, ["a", "b"]) == "real"


def test_try_paths_all_missing_returns_empty_string():
    assert field_map.try_paths({}, ["a", "b"]) == ""


# ── try_string ─────────────────────────────────────────────


def test_try_string_formats_numbers():
    data = {"vmConfig": {"cpu": {"quantity": 4}}}
    assert field_map.try_string(data, ["vmConfig.cpu.quantity"]) == "4"


def test_try_string_returns_string_as_is():
    data = {"name": "vm-01"}
    assert field_map.try_string(data, ["name"]) == "vm-01"


def test_try_string_missing_returns_empty_string():
    assert field_map.try_string({}, ["x"]) == ""


# ── flatten_dict ───────────────────────────────────────────


def test_flatten_dict_skips_list_of_dicts():
    data = {"a": "x", "b": [{"skip": True}], "c": {"d": "y"}}
    out = field_map.flatten_dict(data)
    assert out == {"a": "x", "c.d": "y"}


def test_flatten_dict_joins_primitive_lists():
    data = {"f": [1, 2, 3]}
    out = field_map.flatten_dict(data)
    assert out == {"f": "1, 2, 3"}


def test_flatten_dict_empty_list():
    # v1.0.0 behaviour: an empty list becomes the empty string under
    # the key, not absent. (Mirrors `_flatten_dict` at collector.py:59.)
    assert field_map.flatten_dict({"f": []}) == {"f": ""}


# ── power_state ────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,expected",
    [
        ("running", "ON"),
        ("Running", "ON"),
        ("started", "ON"),
        ("RUNNING", "ON"),
        ("stopped", "OFF"),
        ("shutOff", "OFF"),
        ("Stopped", "OFF"),
        ("unknown", "unknown"),
        ("", ""),
    ],
)
def test_power_state(status, expected):
    assert field_map.power_state(status) == expected


# ── VM_FIELDS / *_FIELDS tables ────────────────────────────


def test_vm_fields_table_has_expected_columns():
    expected = {
        "VM Name", "Guest OS", "CPUs", "Cores Per Socket", "Memory (MB)",
        "VM Tools", "UUID", "Description", "Create Date",
        "Host URN", "Cluster URN", "URN",
    }
    assert set(field_map.VM_FIELDS.keys()) == expected


def test_cpu_fields_uses_only_nested_paths():
    for col, paths in field_map.CPU_FIELDS.items():
        if col == "VM Name":
            # VM Name is a top-level scalar (not nested under vmConfig).
            assert paths == ["name"]
            continue
        for p in paths:
            assert p.startswith("vmConfig.cpu."), (
                f"CPU_FIELDS[{col}] path {p!r} should be under vmConfig.cpu.*"
            )


def test_memory_fields_uses_vmconfig_memory():
    for col, paths in field_map.MEMORY_FIELDS.items():
        if col == "VM Name":
            assert paths == ["name"]
            continue
        for p in paths:
            assert p.startswith("vmConfig.memory."), (
                f"MEMORY_FIELDS[{col}] path {p!r} should be under vmConfig.memory.*"
            )


def test_host_fields_drslevel_fallback():
    """HOST_FIELDS['CPU MHz'] should try 'cpuMHz' first then 'cpuFrequency'."""
    assert field_map.HOST_FIELDS["CPU MHz"] == ["cpuMHz", "cpuFrequency"]


def test_datastore_free_gb_tries_multiple_field_names():
    """DATASTORE_FIELDS['Free (GB)'] should try 4 candidate paths."""
    assert field_map.DATASTORE_FIELDS["Free (GB)"] == [
        "freeSpaceGB", "freeSpace", "freeCapacityGB", "freeSizeGB"
    ]


# ── build_row ──────────────────────────────────────────────


def test_build_row_first_candidate_wins():
    data = {"vmConfig": {"cpu": {"quantity": 4}}, "cpu": {"quantity": 99}}
    row = field_map.build_row(data, field_map.VM_FIELDS, extras_allowed=False)
    assert row["CPUs"] == 4


def test_build_row_falls_back_to_second_candidate():
    data = {"cpu": {"quantity": 8}}
    row = field_map.build_row(data, field_map.VM_FIELDS, extras_allowed=False)
    assert row["CPUs"] == 8


def test_build_row_all_mapped_columns_present():
    data = {"name": "vm-01"}
    row = field_map.build_row(data, field_map.VM_FIELDS, extras_allowed=False)
    for col in field_map.VM_FIELDS:
        assert col in row


def test_build_row_extras_appended_when_enabled():
    data = {
        "name": "vm-01",
        "vmConfig": {"cpu": {"newField": "future-version"}},
    }
    row = field_map.build_row(data, field_map.VM_FIELDS, extras_allowed=True)
    # v1.0.0 prettify uses the last 2 dotted segments: "cpu.newField" -> "Cpu - New Field".
    assert "Cpu - New Field" in row


def test_build_row_extras_disabled():
    data = {
        "name": "vm-01",
        "vmConfig": {"cpu": {"newField": "future-version"}},
    }
    row = field_map.build_row(data, field_map.VM_FIELDS, extras_allowed=False)
    assert "VmConfig Cpu Newfield" not in row


# ── prettify_key ──────────────────────────────────────────


def test_prettify_key_dotted():
    # v1.0.0 uses the last 2 dotted segments and Title-cases them.
    assert field_map.prettify_key("vmConfig.cpu.quantity") == "Cpu - Quantity"


def test_prettify_key_single():
    assert field_map.prettify_key("name") == "Name"


def test_prettify_key_camel_case():
    # camelCase in the leaf gets split before .title().
    out = field_map.prettify_key("vmConfig.cpu.isHotPlug")
    # isHotPlug -> "is Hot Plug"; joined with the second-to-last segment.
    assert "Hot Plug" in out

"""Tests for the openpyxl-based Excel writer."""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from app.core.excel_builder import SHEET_ORDER, build_excel


def test_sheet_order_matches_rvtools():
    assert SHEET_ORDER == [
        "vSummary", "vInfo", "vCPU", "vMemory", "vDisk", "vNetwork",
        "vHost", "vCluster", "vDatastore", "vSwitch",
    ]


def test_build_writes_all_sheets(tmp_path: Path) -> None:
    data = {
        "vSummary": [{"Item": "Total VMs", "Count": 3}],
        "vInfo": [
            {"VM Name": "vm-1", "Power State": "ON"},
            {"VM Name": "vm-2", "Power State": "OFF"},
        ],
        "vCPU":   [{"VM Name": "vm-1", "Total CPUs": 4}],
        "vMemory": [],
        "vDisk":   [],
        "vNetwork": [],
        "vHost":   [],
        "vCluster": [],
        "vDatastore": [],
        "vSwitch": [],
    }
    out = tmp_path / "out.xlsx"
    build_excel(data, out)
    assert out.exists()
    assert out.stat().st_size > 0


def test_build_roundtrip_via_openpyxl(tmp_path: Path) -> None:
    """Open the produced .xlsx with openpyxl and check the cells."""
    data = {
        "vInfo": [
            {"VM Name": "vm-1", "Power State": "ON", "CPUs": 4},
            {"VM Name": "vm-2", "Power State": "OFF", "CPUs": 2},
        ],
    }
    out = tmp_path / "out.xlsx"
    build_excel(data, out)

    wb = load_workbook(out, read_only=True)
    try:
        assert wb.sheetnames == SHEET_ORDER
        ws = wb["vInfo"]
        rows = list(ws.iter_rows(values_only=True))
        # Header row + 2 data rows.
        assert len(rows) == 3
        # Headers are the sorted union of keys.
        header = list(rows[0])
        assert "VM Name" in header
        assert "Power State" in header
        assert "CPUs" in header
        # The first data row's VM Name is "vm-1".
        vm_name_col = header.index("VM Name")
        assert rows[1][vm_name_col] == "vm-1"
    finally:
        wb.close()


def test_build_empty_sheet_writes_placeholder(tmp_path: Path) -> None:
    data = {name: [] for name in SHEET_ORDER}
    out = tmp_path / "out.xlsx"
    build_excel(data, out)
    wb = load_workbook(out, read_only=True)
    try:
        for name in SHEET_ORDER:
            ws = wb[name]
            rows = list(ws.iter_rows(values_only=True))
            assert len(rows) == 1
            assert rows[0][0] == "No data collected"
    finally:
        wb.close()


def test_build_header_is_bold_white_on_dark_blue(tmp_path: Path) -> None:
    data = {"vInfo": [{"VM Name": "vm-1"}]}
    out = tmp_path / "out.xlsx"
    build_excel(data, out)
    wb = load_workbook(out)
    try:
        ws = wb["vInfo"]
        cell = ws["A1"]
        assert cell.font.bold is True
        # openpyxl returns the colour string; "FFFFFF" or "FFFFFFFF" both indicate white.
        color = (cell.font.color.value or "").upper()
        assert ("F" in color and "FFFFFF" in color) or color.endswith("FFFFFF")
        # Fill is dark blue (#2C3E50).
        fill = cell.fill
        assert fill.fgColor is not None
    finally:
        wb.close()


def test_build_freeze_panes_is_a2(tmp_path: Path) -> None:
    data = {"vInfo": [{"VM Name": "vm-1"}]}
    out = tmp_path / "out.xlsx"
    build_excel(data, out)
    wb = load_workbook(out)
    try:
        ws = wb["vInfo"]
        assert ws.freeze_panes == "A2"
    finally:
        wb.close()


def test_build_creates_output_dir(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deeper" / "out.xlsx"
    build_excel({"vInfo": [{"VM Name": "x"}]}, out)
    assert out.exists()

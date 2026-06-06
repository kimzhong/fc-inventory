"""Build a multi-sheet `.xlsx` workbook from the collector's sheet data.

1:1 port of `excel_builder.py` (97L) from v1.0.0:
  - SHEET_ORDER (RVTools convention; same 10 sheets)
  - header style: bold white on #2C3E50, center-aligned, size 11
  - autofilter on the data range
  - freeze row 1
  - column autosize: sample first 100 rows, cap at 50, floor at 10
"""

from __future__ import annotations

import os
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# Header style constants (mirrors excel_builder.py:11-14).
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(fgColor="2C3E50", fill_type="solid")
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center")

# Sheet order (mirrors SHEET_ORDER in excel_builder.py:17-20).
SHEET_ORDER: list[str] = [
    "vSummary", "vInfo", "vCPU", "vMemory", "vDisk", "vNetwork",
    "vHost", "vCluster", "vDatastore", "vSwitch",
]

# Autosize tuning.
COL_SAMPLE_ROWS = 100
COL_MAX_WIDTH = 50
COL_MIN_WIDTH = 10
COL_PADDING = 3


def build_excel(data: dict, output_path: str | os.PathLike) -> None:
    """Write data to a multi-sheet .xlsx at output_path.

    Args:
        data: dict of sheet-name -> list of row dicts, as produced by
            the collector's `build_all_sheets()`.
        output_path: destination path for the .xlsx file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    # openpyxl creates a default "Sheet"; remove it so SHEET_ORDER
    # controls the workbook end-to-end (mirrors excel_builder.py:34-35).
    wb.remove(wb.active)

    for sheet_name in SHEET_ORDER:
        rows = data.get(sheet_name, [])
        ws = wb.create_sheet(title=sheet_name)

        if not rows:
            ws.append(["No data collected"])
            continue

        # Build the header list: union of all keys, preserve first-seen order
        # (mirrors excel_builder.py:46-53). Note: Python dicts are
        # insertion-ordered, so iterating the rows in order preserves the
        # order of keys within a row. Across rows, the *first row's* key
        # order wins; this is the same behaviour the v1.0.0 tool had.
        seen: set[str] = set()
        headers: list[str] = []
        for row in rows:
            for key in row.keys():
                if key in seen:
                    continue
                seen.add(key)
                headers.append(key)
        ws.append(headers)

        # Write data rows.
        for row in rows:
            ws.append([row.get(h, "") for h in headers])

        # Apply header styling.
        _style_header(ws)
        _auto_size_columns(ws, len(rows))
        # Autofilter on the data range.
        ws.auto_filter.ref = ws.dimensions
        # Freeze top row.
        ws.freeze_panes = "A2"

    wb.save(output_path)


def _style_header(ws) -> None:
    """Apply the dark-blue header style to row 1.

    Mirrors excel_builder._style_header (excel_builder.py:74-79).
    """
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT


def _auto_size_columns(ws, row_count: int) -> None:
    """Auto-size each column by sampling up to COL_SAMPLE_ROWS data rows.

    Mirrors excel_builder._auto_size_columns (excel_builder.py:82-97).
    """
    sample_rows = min(row_count + 1, COL_SAMPLE_ROWS)

    for col_idx, col_cells in enumerate(ws.iter_cols(min_row=1, max_row=sample_rows), 1):
        max_length = 0
        for cell in col_cells:
            if cell.value is None:
                continue
            length = len(str(cell.value))
            if length > max_length:
                max_length = length

        adjusted = min(max_length + COL_PADDING, COL_MAX_WIDTH)
        adjusted = max(adjusted, COL_MIN_WIDTH)
        ws.column_dimensions[get_column_letter(col_idx)].width = adjusted

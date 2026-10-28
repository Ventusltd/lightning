#!/usr/bin/env python3
"""Per-month density grid derived from the one-year archive's day shards.

``data/history/grid/YYYY-MM.json`` (schema ``uk-lightning-history-grid.v1``)
holds, for one UTC calendar month, the number of archived optical flashes in
each 0.1-degree cell on each UTC day. Any UTC-day window therefore sums
exactly from at most 13 small files instead of hundreds of thousands of
points.

Contract:
- the grid is a pure function of the index rows and shard files of its month
  (no timestamps of its own), so an unchanged month is byte-identical;
- ``days`` lists every UTC date of the month inside retention (cutoff..today)
  with the index row's ``coverage``, ``count`` and the shard ``sha256`` list it
  was built from. A client compares those with the current index: a day whose
  row differs is STALE and must be treated as unknown, never as zero;
- ``count`` null (coverage none, or shard_lost) means never searched: unknown,
  not zero. Such days contribute no cells;
- each day's cell counts sum exactly to its row ``count``;
- ``cells`` is ``[[cell, day, n, day, n, ...], ...]`` sorted by cell then day,
  where ``cell = row * COLS + col``, ``col = floor((lon - WEST) / 0.1)``,
  ``row = floor((lat - SOUTH) / 0.1)`` (clamped to the grid) and ``day`` is
  the day of the month (1-31).

An observed flash is NOT a proven cloud-to-ground strike, and a cell count is
a density of OBSERVED optical flashes, not of ground strikes.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date
from pathlib import Path

from collect import UK_BOUNDS
from history import SCHEMA_INDEX, _load_old_index, _read_features, _serialise, _write_if_changed

SCHEMA_GRID = "uk-lightning-history-grid.v1"
CELL_DEG = 0.1
WEST, SOUTH, EAST, NORTH = UK_BOUNDS
COLS = int(round((EAST - WEST) / CELL_DEG))   # 140
ROWS = int(round((NORTH - SOUTH) / CELL_DEG))  # 120
GRID_DIR = "grid"
MONTH_PATTERN = re.compile(r"^(\d{4})-(\d{2})\.json$")
GRID_NOTE = (
    "Counts of archived satellite OPTICAL lightning flashes per 0.1-degree cell (about 7 x 11 km) per UTC day; "
    "not ground strikes. Days with count null were never searched: unknown, not zero. A day whose coverage, "
    "count or sha256 differs from the current index is stale and must be treated as unknown."
)


def cell_of(lon: float, lat: float) -> int:
    """Grid cell index of a coordinate (rounded first, so 0.1-steps never fall into the cell below)."""
    col = math.floor(round((lon - WEST) / CELL_DEG, 6))
    row = math.floor(round((lat - SOUTH) / CELL_DEG, 6))
    col = min(max(col, 0), COLS - 1)
    row = min(max(row, 0), ROWS - 1)
    return row * COLS + col


def row_digests(row: dict) -> list[str]:
    if row.get("path"):
        return [row.get("sha256") or ""]
    return [p.get("sha256") or "" for p in row.get("parts") or []]


def day_entry(row: dict) -> dict:
    return {"date": row["date"], "coverage": row.get("coverage"), "count": row.get("count"),
            "sha256": row_digests(row)}


def day_cells(archive_dir: Path, row: dict) -> dict[int, int]:
    """Cell counts of one indexed day, read from its shard file(s); must sum to the row count."""
    cells: dict[int, int] = {}
    if not row.get("count"):
        return cells
    files = [row["path"]] if row.get("path") else [p["path"] for p in row.get("parts") or []]
    total = 0
    for rel in files:
        for feature in _read_features(Path(archive_dir) / rel, row["date"]):
            lon, lat = feature["geometry"]["coordinates"][:2]
            key = cell_of(float(lon), float(lat))
            cells[key] = cells.get(key, 0) + 1
            total += 1
    if total != row["count"]:
        raise ValueError(f"{row['date']}: shards hold {total} flashes but the index count is {row['count']}")
    return cells


def month_content(month: str, entries: list[dict], cells_by_day: dict[str, dict[int, int]]) -> str:
    by_cell: dict[int, list[int]] = {}
    for entry in entries:
        dom = int(entry["date"][8:10])
        for cell, n in cells_by_day.get(entry["date"], {}).items():
            by_cell.setdefault(cell, []).extend((dom, n))
    cells = []
    for cell in sorted(by_cell):
        pairs = by_cell[cell]
        ordered = sorted(zip(pairs[0::2], pairs[1::2]))
        cells.append([cell] + [v for pair in ordered for v in pair])
    return _serialise({
        "schema": SCHEMA_GRID,
        "month": month,
        "observation": "satellite optical lightning flashes, not ground strikes",
        "bounds_wsen": list(UK_BOUNDS),
        "cell_deg": CELL_DEG,
        "cols": COLS,
        "rows": ROWS,
        "cell_rule": "cell=row*cols+col; col=floor((lon-west)/cell_deg); row=floor((lat-south)/cell_deg); clamped",
        "note": GRID_NOTE,
        "total": sum(e["count"] or 0 for e in entries),
        "days": entries,
        "cells": cells,
    })


def parse_grid(text: str, month: str) -> tuple[list[dict], dict[str, dict[int, int]]]:
    """Validate a grid file and return (days, per-day cell counts)."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_GRID or data.get("month") != month:
        raise ValueError(f"grid {month}: schema or month mismatch")
    if data.get("cols") != COLS or data.get("rows") != ROWS or data.get("cell_deg") != CELL_DEG:
        raise ValueError(f"grid {month}: geometry mismatch")
    entries = data.get("days")
    if not isinstance(entries, list):
        raise ValueError(f"grid {month}: invalid days")
    by_dom = {}
    for e in entries:
        if not isinstance(e, dict) or not str(e.get("date", "")).startswith(month + "-"):
            raise ValueError(f"grid {month}: invalid day entry")
        by_dom[int(e["date"][8:10])] = e["date"]
    cells_by_day: dict[str, dict[int, int]] = {}
    for item in data.get("cells") or []:
        if not isinstance(item, list) or len(item) < 3 or len(item) % 2 != 1:
            raise ValueError(f"grid {month}: invalid cell row")
        cell = item[0]
        if not isinstance(cell, int) or not 0 <= cell < COLS * ROWS:
            raise ValueError(f"grid {month}: cell out of range")
        for dom, n in zip(item[1::2], item[2::2]):
            if dom not in by_dom or not isinstance(n, int) or n < 1:
                raise ValueError(f"grid {month}: invalid day count")
            cells_by_day.setdefault(by_dom[dom], {})[cell] = n
    for e in entries:
        if sum(cells_by_day.get(e["date"], {}).values()) != (e.get("count") or 0):
            raise ValueError(f"grid {month}: {e['date']} cells do not sum to its count")
    return entries, cells_by_day


def grid_path(month: str) -> str:
    return f"{GRID_DIR}/{month}.json"


def _month_rows(index: dict) -> dict[str, list[dict]]:
    months: dict[str, list[dict]] = {}
    for row in index.get("days") or []:
        if isinstance(row, dict) and isinstance(row.get("date"), str):
            months.setdefault(row["date"][:7], []).append(row)
    return months


def refresh_grids(archive_dir: Path, index: dict) -> dict:
    """Bring every month grid in line with ``index``; re-read only days whose row changed.

    Months outside retention are removed. Returns {"written": [...], "removed": [...], "unchanged": n,
    "days_read": n}.
    """
    archive_dir = Path(archive_dir)
    if index.get("schema") != SCHEMA_INDEX:
        raise ValueError("grid: index schema mismatch")
    grid_dir = archive_dir / GRID_DIR
    written, removed, unchanged, days_read = [], [], 0, 0
    months = _month_rows(index)
    for month, rows in sorted(months.items()):
        target = archive_dir / grid_path(month)
        old_entries, old_cells = [], {}
        if target.exists():
            try:
                old_entries, old_cells = parse_grid(target.read_text(encoding="utf-8"), month)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                old_entries, old_cells = [], {}  # invalid: rebuilt from the shards
        old_by_date = {e["date"]: e for e in old_entries}
        entries, cells_by_day = [], {}
        for row in sorted(rows, key=lambda r: r["date"]):
            entry = day_entry(row)
            entries.append(entry)
            if not entry["count"]:
                continue
            if old_by_date.get(entry["date"]) == entry:
                cells_by_day[entry["date"]] = old_cells.get(entry["date"], {})
            else:
                cells_by_day[entry["date"]] = day_cells(archive_dir, row)
                days_read += 1
        if _write_if_changed(target, month_content(month, entries, cells_by_day)):
            written.append(grid_path(month))
        else:
            unchanged += 1
    if grid_dir.exists():
        for f in sorted(grid_dir.iterdir()):
            m = MONTH_PATTERN.fullmatch(f.name)
            if f.is_file() and (not m or f.name[:7] not in months):
                f.unlink()
                removed.append(f"{GRID_DIR}/{f.name}")
        if not any(grid_dir.iterdir()):
            grid_dir.rmdir()
    return {"written": written, "removed": removed, "unchanged": unchanged, "days_read": days_read}


def verify_grids(archive_dir: Path, index: dict | None = None, *, deep: bool = False) -> dict:
    """Strict check: one grid per index month whose days equal the index rows.

    ``deep`` also recounts every day from its shards.
    """
    archive_dir = Path(archive_dir)
    index = index or _load_old_index(archive_dir / "index.json")
    if index is None:
        raise ValueError("no index.json")
    months = _month_rows(index)
    checked = 0
    for month, rows in sorted(months.items()):
        target = archive_dir / grid_path(month)
        if not target.exists():
            raise ValueError(f"{target}: missing")
        entries, cells = parse_grid(target.read_text(encoding="utf-8"), month)
        expected = [day_entry(r) for r in sorted(rows, key=lambda r: r["date"])]
        if entries != expected:
            raise ValueError(f"{target}: days differ from the index (stale grid)")
        if deep:
            for row in rows:
                if (cells.get(row["date"]) or {}) != day_cells(archive_dir, row):
                    raise ValueError(f"{target}: {row['date']} cells differ from its shards")
        checked += 1
    extra = sorted(f.name for f in (archive_dir / GRID_DIR).glob("*.json") if f.name[:7] not in months) \
        if (archive_dir / GRID_DIR).exists() else []
    if extra:
        raise ValueError(f"grid files outside retention: {extra}")
    return {"months": checked}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="(Re)generate every month grid from the index and shards (idempotent)")
    build.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    check = sub.add_parser("verify", help="Check every month grid against the index")
    check.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    check.add_argument("--deep", action="store_true", help="Also recount every day from its shards")
    args = parser.parse_args(argv)
    index = _load_old_index(args.archive_dir / "index.json")
    if index is None:
        raise SystemExit("no index.json")
    if args.command == "build":
        print(json.dumps(refresh_grids(args.archive_dir, index)))
    else:
        print(json.dumps(verify_grids(args.archive_dir, index, deep=args.deep)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(f"GRID FAILURE: {error}", file=sys.stderr)
        raise SystemExit(2)

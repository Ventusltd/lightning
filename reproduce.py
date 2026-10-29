#!/usr/bin/env python3
"""Recompute the lightning archive's published numbers from the committed files alone.

    python reproduce.py check [--data-dir data] [--json OUT] [--expect SHA256]
    python reproduce.py compare-day --day YYYY-MM-DD --record RECORD.json [--data-dir data]

``check`` reads ``data/history`` (index, day shards, hourly parts, month grids)
and nothing else: no network, no EUMETSAT credentials, no third-party package
and no import of ``collect.py``, ``history.py`` or ``grid.py``. The archive
rules of docs/METHODS.md are re-implemented here independently, so a bug in
the writer is not silently repeated by the checker. It:

1. hashes every file under ``data/history`` (sha256) and derives a manifest
   digest over the sorted ``<sha256>  <path>`` lines;
2. re-reads every day shard and hourly part: schema, date, metadata, feature
   validity (Point inside the UK view rectangle, own UTC date, optical-flash
   kind), unique ids, canonical order and byte-canonical serialisation, and
   the 8 MiB split rule and 25 000 000-byte cap;
3. recomputes every index row (``scanned_seconds`` from ``scan_windows``,
   ``coverage``, ``count``, ``bytes``, ``sha256``, ``path``/``parts``,
   first/last observation, ``sealed``, ``shard_lost``) and every index total;
4. rebuilds every month grid from the shards (0.1-degree cell rule) and checks
   it byte for byte against the committed file;
5. derives the 12-month headline numbers (days searched, full/partial/none,
   flashes, searched hours, monthly totals) from the recomputed rows.

It prints a human summary and a canonical JSON summary whose ``summary_sha256``
is the sha256 of the canonical JSON of everything except that field and the
``run`` block (wall-clock time, Python version). Two people who run ``check``
on the same commit therefore get the same ``summary_sha256``; publishing that
digest next to the commit id is the "signature" of a reproduction. It is a
content hash, not a cryptographic identity signature (docs/METHODS.md shows
how to add one with ``ssh-keygen -Y sign``). Exit status 0 = every recomputed
value equals the committed value, 1 = at least one mismatch (listed), 2 =
unusable input.

``compare-day`` compares an independent re-download of one UTC date (a run
record written by ``python collect.py --rebuild-day D --run-record R`` with
your own EUMETSAT credentials) with the committed archive for that date. It
reports, never rewrites. See docs/METHODS.md, "Re-download a day".

An optical flash is not a ground strike, and a date that was never searched
is unknown, not zero: this script checks that the archive says so.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

SCHEMA_SUMMARY = "uk-lightning-reproduction.v1"
SCHEMA_COMPARE = "uk-lightning-day-comparison.v1"
SCHEMA_INDEX = "uk-lightning-history-index.v2"
SCHEMA_DAY = "uk-lightning-history-day.v1"
SCHEMA_GRID = "uk-lightning-history-grid.v1"
SCHEMA_RUN_RECORD = "uk-lightning-run-record.v1"

# Contract constants, restated (not imported) on purpose; see docs/METHODS.md.
DAY_SECONDS = 86400
SLOT_SECONDS = 600            # LI L2-LFL products are 10-minute sensing slots
SLOT_PHASE_SECONDS = 9        # slots run HH:M0:09 .. HH:(M+1)0:09 (flash-proof fallback only)
UK_BOUNDS = (-11.0, 49.5, 3.0, 61.5)  # west, south, east, north: a rectangle, not the UK border
CELL_DEG = 0.1
GRID_COLS = 140
GRID_ROWS = 120
PRODUCT_SLOT = re.compile(r"_OPE_(\d{14})_(\d{14})_")
DAY_PATH = re.compile(r"^days/(\d{4})/(\d{2})/(\d{2})\.json$")
PART_PATH = re.compile(r"^days/(\d{4})/(\d{2})/(\d{2})/(\d{2})\.json$")
GRID_PATH = re.compile(r"^grid/(\d{4})-(\d{2})\.json$")
QUARANTINE_PATH = re.compile(r"^quarantine/(\d{4}-\d{2}-\d{2})\.json$")
MAX_REPORTED_MISMATCHES = 200


# --- small helpers ---------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(obj) -> str:
    """The writer's serialisation: compact separators, UTF-8, one trailing newline."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"


def summary_digest(summary: dict) -> str:
    body = {k: v for k, v in summary.items() if k not in ("summary_sha256", "run")}
    return sha256_bytes(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def parse_utc(value: str) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def fmt_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def year_ago(current: date) -> date:
    try:
        return current.replace(year=current.year - 1)
    except ValueError:  # 29 February
        return current.replace(year=current.year - 1, day=28)


def inside_view(lon: float, lat: float) -> bool:
    w, s, e, n = UK_BOUNDS
    return math.isfinite(lon) and math.isfinite(lat) and w <= lon <= e and s <= lat <= n


def cell_of(lon: float, lat: float) -> int:
    """0.1-degree cell: col = floor((lon - west) / 0.1), row = floor((lat - south) / 0.1), clamped.
    The quotient is rounded to 6 decimals first so exact 0.1 steps never fall into the cell below."""
    col = math.floor(round((lon - UK_BOUNDS[0]) / CELL_DEG, 6))
    row = math.floor(round((lat - UK_BOUNDS[1]) / CELL_DEG, 6))
    col = min(max(col, 0), GRID_COLS - 1)
    row = min(max(row, 0), GRID_ROWS - 1)
    return row * GRID_COLS + col


def product_start(feature_id: str) -> datetime | None:
    """Nominal sensing start in the product name of ``<product>:<index>``, or None."""
    match = PRODUCT_SLOT.search(str(feature_id).rsplit(":", 1)[0])
    if not match:
        return None
    try:
        start = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        end = datetime.strptime(match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if end <= start or end - start > timedelta(hours=1):
        return None
    return start


def merged_seconds_in(windows: list[tuple[datetime, datetime]], lo: datetime, hi: datetime) -> float:
    total = 0.0
    for start, end in windows:
        s, e = max(start, lo), min(end, hi)
        if e > s:
            total += (e - s).total_seconds()
    return total


class Report:
    """Collects mismatches (blocking) and notes (informational)."""

    def __init__(self):
        self.mismatches: list[str] = []
        self.mismatch_total = 0

    def bad(self, message: str) -> None:
        self.mismatch_total += 1
        if len(self.mismatches) < MAX_REPORTED_MISMATCHES:
            self.mismatches.append(message)

    def expect(self, label: str, recomputed, committed) -> None:
        if recomputed != committed:
            self.bad(f"{label}: recomputed {recomputed!r} != committed {committed!r}")


# --- step 1: inventory ---------------------------------------------------------------------

def inventory(history_dir: Path) -> tuple[dict[str, bytes], list[dict], str]:
    files: dict[str, bytes] = {}
    for path in sorted(history_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(history_dir).as_posix()
            if rel.endswith(".tmp") or "__pycache__" in rel:
                continue
            files[rel] = path.read_bytes()
    manifest = [{"path": rel, "bytes": len(data), "sha256": sha256_bytes(data)} for rel, data in sorted(files.items())]
    lines = "".join(f"{m['sha256']}  {m['path']}\n" for m in manifest)
    return files, manifest, sha256_bytes(lines.encode("utf-8"))


# --- step 2: shards ------------------------------------------------------------------------

def read_shard(rel: str, data: bytes, expected_day: str, report: Report, *, hour: str | None) -> list[dict] | None:
    try:
        text = data.decode("utf-8")
        obj = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        report.bad(f"{rel}: unreadable JSON ({error})")
        return None
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA_DAY or obj.get("type") != "FeatureCollection":
        report.bad(f"{rel}: not a {SCHEMA_DAY} FeatureCollection")
        return None
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    features = obj.get("features")
    if not isinstance(features, list) or not features:
        report.bad(f"{rel}: features missing or empty (an empty day has no file)")
        return None
    if meta.get("date_utc") != expected_day:
        report.bad(f"{rel}: metadata.date_utc {meta.get('date_utc')!r} != path date {expected_day}")
    seen, previous = set(), None
    for i, feature in enumerate(features):
        label = f"{rel} feature {i}"
        try:
            fid = feature["id"]
            props = feature["properties"]
            geom = feature["geometry"]
            moment = parse_utc(props["time_utc"])
            lon, lat = (float(v) for v in geom["coordinates"][:2])
        except (KeyError, TypeError, ValueError, IndexError) as error:
            report.bad(f"{label}: malformed ({type(error).__name__}: {error})")
            continue
        if feature.get("type") != "Feature" or geom.get("type") != "Point" or len(geom["coordinates"]) != 2:
            report.bad(f"{label}: not a Point Feature")
        if not isinstance(fid, str) or not fid.strip():
            report.bad(f"{label}: empty id")
        if fid in seen:
            report.bad(f"{label}: duplicate id {fid}")
        seen.add(fid)
        if props.get("source_product") != "MTG LI L2-LFL" or props.get("observation_kind") != "optical_lightning_flash":
            report.bad(f"{label}: not an MTG LI L2-LFL optical flash")
        if not inside_view(lon, lat):
            report.bad(f"{label}: outside the UK view rectangle {UK_BOUNDS}")
        if moment.date().isoformat() != expected_day:
            report.bad(f"{label}: time {props['time_utc']} not on {expected_day} UTC")
        if hour is not None and f"{moment.hour:02d}" != hour:
            report.bad(f"{label}: time {props['time_utc']} not in hourly part {hour}")
        key = (props["time_utc"], fid)
        if previous is not None and key < previous:
            report.bad(f"{label}: not sorted by (time_utc, id)")
        previous = key
    expected_meta = {
        "date_utc": expected_day,
        "source": meta.get("source"),
        "observation": meta.get("observation"),
        "count": len(features),
        "first_observation_utc": min(f["properties"]["time_utc"] for f in features),
        "last_observation_utc": max(f["properties"]["time_utc"] for f in features),
    }
    if hour is not None:
        expected_meta.update({"hour_utc": hour, "part_of_day": True})
    report.expect(f"{rel} metadata", expected_meta, meta)
    if canonical(obj) != text:
        report.bad(f"{rel}: bytes are not the canonical serialisation of their content")
    return features


def flash_proof_seconds(features: list[dict]) -> int:
    """Lower bound of searched time for a day with flashes but no recorded window:
    each flash proves its product's 10-minute slot was examined."""
    slots = set()
    for feature in features:
        moment = parse_utc(feature["properties"]["time_utc"])
        anchor = product_start(feature.get("id", "")) or moment
        seconds = int(math.floor((anchor - day_start(moment.date())).total_seconds())) - SLOT_PHASE_SECONDS
        slots.add(seconds // SLOT_SECONDS)
    return min(DAY_SECONDS, len(slots) * SLOT_SECONDS)


# --- steps 3-5 -----------------------------------------------------------------------------

def check(data_dir: Path) -> dict:
    history_dir = Path(data_dir) / "history"
    if not (history_dir / "index.json").is_file():
        raise FileNotFoundError(f"{history_dir / 'index.json'} not found")
    report = Report()
    files, manifest, manifest_sha = inventory(history_dir)
    index_bytes = files["index.json"]
    index = json.loads(index_bytes.decode("utf-8"))
    if index.get("schema") != SCHEMA_INDEX:
        raise ValueError(f"index schema {index.get('schema')!r} is not {SCHEMA_INDEX}")
    if canonical(index) != index_bytes.decode("utf-8"):
        report.bad("index.json: bytes are not the canonical serialisation of their content")

    generated = parse_utc(index["generated_utc"])
    today = generated.date()
    cutoff = year_ago(today)
    report.expect("index.cutoff_date_utc", cutoff.isoformat(), index.get("cutoff_date_utc"))
    report.expect("index.bounds_wsen", list(UK_BOUNDS), index.get("bounds_wsen"))
    lookback_max_h = float(index.get("collection_lookback_hours_max", 48))
    target_bytes = int(index.get("target_shard_bytes", 8 * 1024 * 1024))
    max_bytes = int(index.get("max_shard_bytes", 25_000_000))

    # Scan windows: merged, ordered, inside retention.
    windows: list[tuple[datetime, datetime]] = []
    for i, w in enumerate(index.get("scan_windows") or []):
        s, e = parse_utc(w["start_utc"]), parse_utc(w["end_utc"])
        if not s < e:
            report.bad(f"scan_windows[{i}]: empty or reversed")
        if windows and s <= windows[-1][1]:
            report.bad(f"scan_windows[{i}]: overlaps or touches the previous window (not merged)")
        if s < day_start(cutoff) or e > generated:
            report.bad(f"scan_windows[{i}]: outside [cutoff, generated_utc]")
        if int(w.get("successful_runs", 0)) < 1:
            report.bad(f"scan_windows[{i}]: successful_runs < 1")
        windows.append((s, e))

    # Shard files on disk, grouped by date.
    on_disk: dict[str, dict] = {}
    for rel in files:
        m = DAY_PATH.match(rel)
        p = PART_PATH.match(rel)
        if m:
            on_disk.setdefault("-".join(m.groups()), {"file": None, "parts": {}})["file"] = rel
        elif p:
            on_disk.setdefault("-".join(p.groups()[:3]), {"file": None, "parts": {}})["parts"][p.group(4)] = rel
        elif not (rel == "index.json" or GRID_PATH.match(rel) or QUARANTINE_PATH.match(rel)):
            report.bad(f"{rel}: unexpected file in data/history")

    rows = index.get("days") or []
    lost_rows = sorted(r["date"] for r in rows if r.get("shard_lost"))
    expected_dates = []
    d = cutoff
    while d <= today:
        expected_dates.append(d.isoformat())
        d += timedelta(days=1)
    report.expect("index.days dates", expected_dates, [r.get("date") for r in rows])

    features_by_day: dict[str, list[dict]] = {}
    recomputed_rows: list[dict] = []
    same_place_time_other_id = 0
    legacy_ids = 0
    for row in rows:
        day = row.get("date")
        listed = on_disk.pop(day, None)
        features: list[dict] = []
        summary = None
        if listed and not row.get("shard_lost"):
            if listed["file"] and listed["parts"]:
                report.bad(f"{day}: both a whole-day file and hourly parts on disk")
            if listed["file"]:
                rel = listed["file"]
                got = read_shard(rel, files[rel], day, report, hour=None)
                if got is not None:
                    features = got
                    size = len(files[rel])
                    if size > target_bytes:
                        report.bad(f"{rel}: {size} bytes > target_shard_bytes {target_bytes} but not split")
                    summary = {"count": len(got), "bytes": size, "path": rel, "sha256": sha256_bytes(files[rel]),
                               "parts": None}
            else:
                parts = []
                for hour, rel in sorted(listed["parts"].items()):
                    got = read_shard(rel, files[rel], day, report, hour=hour) or []
                    features.extend(got)
                    if len(files[rel]) > max_bytes:
                        report.bad(f"{rel}: {len(files[rel])} bytes > max_shard_bytes {max_bytes}")
                    if got:
                        parts.append({"hour": hour, "path": rel, "count": len(got), "bytes": len(files[rel]),
                                      "sha256": sha256_bytes(files[rel]),
                                      "first_observation_utc": got[0]["properties"]["time_utc"],
                                      "last_observation_utc": got[-1]["properties"]["time_utc"]})
                whole = sum(len(files[p["path"]]) for p in parts)
                summary = {"count": len(features), "bytes": whole, "path": None, "sha256": None, "parts": parts}
                if parts:
                    # The writer splits a day only when its whole-day file would exceed target_shard_bytes.
                    first_meta = json.loads(files[parts[0]["path"]].decode("utf-8")).get("metadata") or {}
                    whole_day = canonical({"schema": SCHEMA_DAY, "type": "FeatureCollection", "metadata": {
                        "date_utc": day, "source": first_meta.get("source"),
                        "observation": first_meta.get("observation"), "count": len(features),
                        "first_observation_utc": parts[0]["first_observation_utc"],
                        "last_observation_utc": parts[-1]["last_observation_utc"]}, "features": features})
                    if len(whole_day.encode("utf-8")) <= target_bytes:
                        report.bad(f"{day}: split into hourly parts although the whole day fits {target_bytes} bytes")
            ids = [f["id"] for f in features]
            if len(ids) != len(set(ids)):
                report.bad(f"{day}: duplicate feature ids across its files")
            keys: dict[tuple, set] = {}
            for f in features:
                keys.setdefault((f["properties"]["time_utc"], tuple(f["geometry"]["coordinates"])), set()).add(
                    str(f["id"]).rsplit(":", 1)[0])
                if product_start(f["id"]) is None:
                    legacy_ids += 1
            same_place_time_other_id += sum(len(v) - 1 for v in keys.values() if len(v) > 1)
        elif listed and row.get("shard_lost"):
            report.bad(f"{day}: shard_lost but files remain on disk (unlisted): {listed}")
        features_by_day[day] = features

        # Recompute the row.
        d = date.fromisoformat(day)
        scanned = int(round(merged_seconds_in(windows, day_start(d), day_start(d) + timedelta(days=1))))
        scanned = max(0, min(DAY_SECONDS, scanned))
        if summary and summary["count"] and scanned <= 0:
            scanned = max(SLOT_SECONDS, flash_proof_seconds(features))
        if row.get("shard_lost"):
            scanned, summary = 0, None
        coverage = "none" if scanned <= 0 else ("full" if scanned >= DAY_SECONDS else "partial")
        count = None if coverage == "none" else (summary["count"] if summary and summary["count"] else 0)
        has = bool(summary and summary["count"])
        expect_row = {
            "date": day,
            "coverage": coverage,
            "scanned_seconds": scanned,
            "count": count,
            "bytes": summary["bytes"] if has else None,
            "path": summary["path"] if has else None,
            "sealed": coverage == "full" or generated >= day_start(d) + timedelta(days=1, hours=lookback_max_h),
            "first_observation_utc": min(f["properties"]["time_utc"] for f in features) if has else None,
            "last_observation_utc": max(f["properties"]["time_utc"] for f in features) if has else None,
            "sha256": summary["sha256"] if has else None,
        }
        if has and summary["parts"]:
            expect_row["parts"] = summary["parts"]
        if row.get("shard_lost"):
            expect_row["shard_lost"] = True
        report.expect(f"index row {day}", expect_row, row)
        recomputed_rows.append(expect_row)
    for day, listed in sorted(on_disk.items()):
        report.bad(f"{day}: shard file(s) on disk for a date with no index row: {listed}")

    # Index totals.
    with_flashes = [r for r in recomputed_rows if r["count"]]
    report.expect("index.archived_observations", sum(r["count"] for r in with_flashes), index.get("archived_observations"))
    report.expect("index.archived_days_with_flashes", len(with_flashes), index.get("archived_days_with_flashes"))
    report.expect("index.scanned_days", sum(1 for r in recomputed_rows if r["coverage"] != "none"), index.get("scanned_days"))
    report.expect("index.first_observation_utc",
                  min((r["first_observation_utc"] for r in with_flashes), default=None), index.get("first_observation_utc"))
    report.expect("index.last_observation_utc",
                  max((r["last_observation_utc"] for r in with_flashes), default=None), index.get("last_observation_utc"))
    report.expect("index.coverage_start_utc", fmt_utc(windows[0][0]) if windows else None, index.get("coverage_start_utc"))
    report.expect("index.last_successful_scan_end_utc", fmt_utc(max(e for _, e in windows)) if windows else None,
                  index.get("last_successful_scan_end_utc"))

    # Integrity block (sha256 per row is checked above; this is the shard_lost bookkeeping).
    integrity = index.get("integrity") or {}
    listed_lost = sorted(str(e.get("date")) for e in integrity.get("lost_days") or [])
    report.expect("integrity.lost_days", lost_rows, listed_lost)
    report.expect("integrity.state", "shard_lost" if lost_rows else "ok", integrity.get("state"))
    if lost_rows and not integrity.get("alert"):
        report.bad("integrity: shard_lost rows without an alert")
    if not lost_rows and integrity.get("alert"):
        report.bad("integrity: alert raised without shard_lost rows")
    for rel in files:
        q = QUARANTINE_PATH.match(rel)
        if q and q.group(1) not in lost_rows:
            report.bad(f"{rel}: quarantine file for a date that is not shard_lost")

    # Unknown is not zero: the row rules, stated as checks on the committed rows.
    for r in rows:
        if (r.get("count") is None) != (r.get("coverage") == "none"):
            report.bad(f"{r.get('date')}: count must be null iff coverage is none")
        if r.get("count") == 0 and not r.get("scanned_seconds"):
            report.bad(f"{r.get('date')}: count 0 without searched time")

    # Step 4: month grids, rebuilt from the shards and compared byte for byte.
    grids = {}
    months: dict[str, list[dict]] = {}
    for r in recomputed_rows:
        months.setdefault(r["date"][:7], []).append(r)
    for month, month_rows in sorted(months.items()):
        rel = f"grid/{month}.json"
        if rel not in files:
            report.bad(f"{rel}: missing")
            continue
        committed_text = files[rel].decode("utf-8")
        committed = json.loads(committed_text)
        entries = [{"date": r["date"], "coverage": r["coverage"], "count": r["count"],
                    "sha256": [r["sha256"] or ""] if r["path"] else [p["sha256"] or "" for p in r.get("parts") or []]}
                   for r in sorted(month_rows, key=lambda r: r["date"])]
        by_cell: dict[int, list[tuple[int, int]]] = {}
        for r in month_rows:
            if not r["count"]:
                continue
            counts: dict[int, int] = {}
            for f in features_by_day.get(r["date"], []):
                lon, lat = f["geometry"]["coordinates"][:2]
                c = cell_of(float(lon), float(lat))
                counts[c] = counts.get(c, 0) + 1
            for c, n in counts.items():
                by_cell.setdefault(c, []).append((int(r["date"][8:10]), n))
        cells = [[c] + [v for pair in sorted(by_cell[c]) for v in pair] for c in sorted(by_cell)]
        rebuilt = {
            "schema": SCHEMA_GRID,
            "month": month,
            "observation": committed.get("observation"),
            "bounds_wsen": list(UK_BOUNDS),
            "cell_deg": CELL_DEG,
            "cols": GRID_COLS,
            "rows": GRID_ROWS,
            "cell_rule": committed.get("cell_rule"),
            "note": committed.get("note"),
            "total": sum(e["count"] or 0 for e in entries),
            "days": entries,
            "cells": cells,
        }
        for key in ("schema", "month", "bounds_wsen", "cell_deg", "cols", "rows", "total", "days"):
            report.expect(f"{rel} {key}", rebuilt[key], committed.get(key))
        if rebuilt["cells"] != committed.get("cells"):
            report.bad(f"{rel}: cells differ from a recount of the shards")
        rebuilt_text = canonical(rebuilt)
        if rebuilt_text != committed_text:
            report.bad(f"{rel}: bytes differ from the grid rebuilt from the shards")
        grids[month] = {"total": rebuilt["total"], "cells": len(cells), "sha256": sha256_bytes(rebuilt_text.encode("utf-8"))}
    for rel in files:
        g = GRID_PATH.match(rel)
        if g and f"{g.group(1)}-{g.group(2)}" not in months:
            report.bad(f"{rel}: grid month outside retention")

    # Step 5: headline numbers over every retained UTC date (cutoff .. generated date).
    searched = [r for r in recomputed_rows if r["coverage"] != "none"]
    searched_seconds = sum(r["scanned_seconds"] for r in recomputed_rows)
    window_seconds = len(recomputed_rows) * DAY_SECONDS
    busiest = max(with_flashes, key=lambda r: (r["count"], r["date"]), default=None)
    headline = {
        "window_utc": {"first_date": recomputed_rows[0]["date"] if recomputed_rows else None,
                       "last_date": recomputed_rows[-1]["date"] if recomputed_rows else None,
                       "last_date_in_progress": True,
                       "index_generated_utc": index["generated_utc"]},
        "days_in_window": len(recomputed_rows),
        "days_searched": len(searched),
        "days_full": sum(1 for r in recomputed_rows if r["coverage"] == "full"),
        "days_partial": sum(1 for r in recomputed_rows if r["coverage"] == "partial"),
        "days_never_searched": sum(1 for r in recomputed_rows if r["coverage"] == "none"),
        "days_shard_lost": len(lost_rows),
        "days_with_flashes": len(with_flashes),
        "days_searched_without_flashes": sum(1 for r in searched if r["count"] == 0),
        "optical_flashes": sum(r["count"] for r in with_flashes),
        "searched_seconds": searched_seconds,
        "unsearched_seconds": window_seconds - searched_seconds,
        "searched_fraction_of_window": round(searched_seconds / window_seconds, 6) if window_seconds else None,
        "busiest_day": {"date": busiest["date"], "count": busiest["count"]} if busiest else None,
        "split_days": sorted(r["date"] for r in recomputed_rows if r.get("parts")),
        "same_time_and_position_under_another_product": same_place_time_other_id,
        "flash_ids_without_product_slot": legacy_ids,
        "monthly": {m: {"flashes": sum(r["count"] or 0 for r in rs),
                        "days": len(rs),
                        "days_searched": sum(1 for r in rs if r["coverage"] != "none"),
                        "days_partial": sum(1 for r in rs if r["coverage"] == "partial"),
                        "searched_seconds": sum(r["scanned_seconds"] for r in rs)}
                    for m, rs in sorted(months.items())},
    }
    summary = {
        "schema": SCHEMA_SUMMARY,
        "verdict": "reproduced" if report.mismatch_total == 0 else "mismatch",
        "mismatch_count": report.mismatch_total,
        "mismatches": report.mismatches,
        "inputs": {
            "history_files": len(manifest),
            "history_bytes": sum(m["bytes"] for m in manifest),
            "manifest_sha256": manifest_sha,
            "index_sha256": sha256_bytes(index_bytes),
            "index_generated_utc": index["generated_utc"],
        },
        "checked": {
            "index_rows": len(rows),
            "shard_files": sum(1 for rel in files if DAY_PATH.match(rel) or PART_PATH.match(rel)),
            "grid_months": len(grids),
            "scan_windows": len(windows),
        },
        "headline": headline,
        "grids": grids,
        "manifest": manifest,
    }
    summary["summary_sha256"] = summary_digest(summary)
    return summary


# --- compare-day ----------------------------------------------------------------------------

def _flash_key(feature: dict) -> tuple[str, float, float]:
    lon, lat = feature["geometry"]["coordinates"][:2]
    return (parse_utc(feature["properties"]["time_utc"]).isoformat(timespec="milliseconds"),
            round(float(lon), 6), round(float(lat), 6))


def compare_day(data_dir: Path, day: str, record_path: Path) -> dict:
    """Compare an independent whole-day re-download with the committed archive for ``day``."""
    history_dir = Path(data_dir) / "history"
    index = json.loads((history_dir / "index.json").read_text(encoding="utf-8"))
    row = next((r for r in index.get("days") or [] if r.get("date") == day), None)
    if row is None:
        raise ValueError(f"{day} is outside the archive's retention (no index row)")
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    if record.get("schema") != SCHEMA_RUN_RECORD:
        raise ValueError(f"{record_path}: not a {SCHEMA_RUN_RECORD}")
    if record.get("outcome") != "success":
        raise ValueError(f"{record_path}: outcome {record.get('outcome')!r}, error {record.get('error')!r}")
    if record.get("rebuild_day_utc") not in (None, day):
        raise ValueError(f"{record_path}: record is for {record.get('rebuild_day_utc')}, not {day}")
    d = date.fromisoformat(day)
    lo, hi = day_start(d), day_start(d) + timedelta(days=1)
    ranges = [(parse_utc(r["start_utc"]), parse_utc(r["end_utc"])) for r in record.get("scanned_ranges") or []]
    record_seconds = int(round(merged_seconds_in(ranges, lo, hi)))

    archived: list[dict] = []
    files = [row["path"]] if row.get("path") else [p["path"] for p in row.get("parts") or []]
    for rel in files:
        archived.extend(json.loads((history_dir / rel).read_text(encoding="utf-8"))["features"])
    fresh = [f for f in record.get("features") or []
             if lo <= parse_utc(f["properties"]["time_utc"]) < hi and
             inside_view(*(float(v) for v in f["geometry"]["coordinates"][:2]))]

    a_keys = {_flash_key(f) for f in archived}
    r_keys = {_flash_key(f) for f in fresh}
    a_ids = {f["id"] for f in archived}
    r_ids = {f["id"] for f in fresh}
    only_archive = sorted(a_keys - r_keys)
    only_record = sorted(r_keys - a_keys)
    if row.get("count") is None:
        verdict = "archive_unknown"  # never searched (or shard_lost): nothing to compare against, not zero
    elif not only_archive and not only_record and record_seconds == row.get("scanned_seconds"):
        verdict = "identical"
    elif not only_archive and not only_record:
        verdict = "same_flashes_different_coverage"
    else:
        verdict = "differs"
    return {
        "schema": SCHEMA_COMPARE,
        "date": day,
        "verdict": verdict,
        "archive": {"coverage": row.get("coverage"), "scanned_seconds": row.get("scanned_seconds"),
                    "count": row.get("count"), "distinct_time_position": len(a_keys),
                    "index_generated_utc": index.get("generated_utc")},
        "record": {"run_utc": record.get("run_utc"), "products_examined": record.get("products_examined"),
                   "slots_examined": record.get("slots_examined"), "products_unparsed": record.get("products_unparsed"),
                   "unparsed_slots_utc": record.get("unparsed_slots_utc"), "searched_seconds_in_day": record_seconds,
                   "flashes_in_day": len(fresh), "distinct_time_position": len(r_keys),
                   "outside_day_dropped": record.get("outside_day")},
        "matched_time_position": len(a_keys & r_keys),
        "matched_ids": len(a_ids & r_ids),
        "only_in_archive": len(only_archive),
        "only_in_record": len(only_record),
        "only_in_archive_sample": [list(k) for k in only_archive[:20]],
        "only_in_record_sample": [list(k) for k in only_record[:20]],
        "note": ("Flashes are matched on (time_utc to the millisecond, lon, lat to 6 decimals). The archive "
                 "keeps each flash once; an hourly-collected date is the union of several runs, so the "
                 "archive can hold flashes a later single search no longer returns, and the reverse when "
                 "products were missing or unparsed at collection time. Optical flashes, not ground strikes."),
    }


# --- CLI ------------------------------------------------------------------------------------

def print_human(summary: dict, out=None) -> None:
    out = out or sys.stdout
    h = summary["headline"]
    w = h["window_utc"]
    pct = 100 * (h["searched_fraction_of_window"] or 0)
    print(f"Lightning archive reproduction ({summary['schema']})", file=out)
    print(f"  index generated {summary['inputs']['index_generated_utc']}, "
          f"{summary['inputs']['history_files']} files, {summary['inputs']['history_bytes']:,} bytes", file=out)
    print(f"  checked {summary['checked']['index_rows']} index rows, {summary['checked']['shard_files']} shard files, "
          f"{summary['checked']['grid_months']} month grids, {summary['checked']['scan_windows']} scan windows", file=out)
    print(f"  window {w['first_date']} .. {w['last_date']} UTC (last date still being collected)", file=out)
    print(f"  {h['days_searched']} of {h['days_in_window']} UTC days searched "
          f"({h['days_full']} full, {h['days_partial']} partial, {h['days_never_searched']} never searched, "
          f"{h['days_shard_lost']} shard_lost)", file=out)
    print(f"  {h['optical_flashes']:,} optical flashes (not ground strikes) on {h['days_with_flashes']} days; "
          f"{h['days_searched_without_flashes']} searched days with none", file=out)
    print(f"  searched {h['searched_seconds'] / 3600:,.2f} h = {pct:.2f} % of the window; "
          f"unsearched {h['unsearched_seconds'] / 3600:,.2f} h (unknown, not zero)", file=out)
    if h["busiest_day"]:
        print(f"  busiest day {h['busiest_day']['date']}: {h['busiest_day']['count']:,} flashes", file=out)
    print(f"  verdict: {summary['verdict'].upper()} ({summary['mismatch_count']} mismatch(es))", file=out)
    for line in summary["mismatches"]:
        print(f"    - {line}", file=out)
    print(f"  manifest_sha256 {summary['inputs']['manifest_sha256']}", file=out)
    print(f"  index_sha256    {summary['inputs']['index_sha256']}", file=out)
    print(f"  summary_sha256  {summary['summary_sha256']}", file=out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    chk = sub.add_parser("check", help="Recompute every index total, coverage, grid and headline from the committed files")
    chk.add_argument("--data-dir", type=Path, default=Path("data"))
    chk.add_argument("--json", type=Path, default=None, help="Also write the full JSON summary here")
    chk.add_argument("--expect", default=None, metavar="SHA256", help="Fail unless summary_sha256 equals this")
    cmp_ = sub.add_parser("compare-day", help="Compare a fresh EUMETSAT re-download of one date with the archive")
    cmp_.add_argument("--day", required=True)
    cmp_.add_argument("--record", type=Path, required=True)
    cmp_.add_argument("--data-dir", type=Path, default=Path("data"))
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("check", "compare-day", "-h", "--help"):
        argv = ["check", *argv]  # "check" is the default command
    args = parser.parse_args(argv)
    try:
        if args.command == "compare-day":
            print(json.dumps(compare_day(args.data_dir, args.day, args.record), indent=2))
            return 0
        summary = check(args.data_dir)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"REPRODUCE FAILURE: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    summary["run"] = {"python": platform.python_version(),
                      "run_utc": fmt_utc(datetime.now(timezone.utc)),
                      "note": "not part of summary_sha256"}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print_human(summary)
    if args.expect and args.expect != summary["summary_sha256"]:
        print(f"  EXPECTED summary_sha256 {args.expect}: does not match", file=sys.stderr)
        return 1
    return 0 if summary["verdict"] == "reproduced" else 1


if __name__ == "__main__":
    raise SystemExit(main())

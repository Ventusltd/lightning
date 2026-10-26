#!/usr/bin/env python3
"""Append genuine EUMETSAT lightning observations to a 12-month, UTC-day archive.

A data-plane module only. It does not touch GridAtlas's shell, UI, map,
or legacy fetch bridge. Stored FeatureCollections use the .json extension
intentionally: the original GridAtlas fetch bridge intercepts ALL .geojson
URLs, including cross-origin ones, but not these namespaced .json files.

An observed flash is NOT a proven cloud-to-ground strike. Missing archives
mean UNKNOWN history, not zero lightning. This archive starts when enabled;
the previous seven-day rolling file is a seed, not a year's backfill.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

from collect import PROVIDER, SOURCE_URL, UK_BOUNDS, inside_uk_view, parse_utc

SCHEMA_INDEX = "uk-lightning-history-index.v1"
SCHEMA_DAY = "uk-lightning-history-day.v1"
WINDOW_NOTE = (
    "Successful EUMETSAT search windows document requests; they are not a "
    "guarantee that all flashes were detected or that coverage was complete."
)
HISTORY_NOTE = (
    "Only archived optical flash observations are included. Missing days "
    "or absent files mean unknown historical availability, not no lightning."
)
DAY_PATTERN = re.compile(r"^days/(\d{4})/(\d{2})/(\d{2})\.json$")
DAY_FORMAT = "%Y-%m-%d"


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("UTC timestamps must carry timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def year_ago(current: date) -> date:
    """Calendar-year cutoff, including leap-day handling."""
    try:
        return current.replace(year=current.year - 1)
    except ValueError:
        return current.replace(year=current.year - 1, day=28)


def day_path(day: date) -> str:
    return f"days/{day:%Y/%m/%d}.json"


def validate_feature(feature: object, now: datetime, cutoff: date) -> tuple[str, dict] | None:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        return None
    geometry = feature.get("geometry")
    props = feature.get("properties")
    if not isinstance(geometry, dict) or geometry.get("type") != "Point" or not isinstance(props, dict):
        return None
    if props.get("source_product") != "MTG LI L2-LFL" or props.get("observation_kind") != "optical_lightning_flash":
        return None
    identifier = feature.get("id")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    try:
        timestamp = parse_utc(props["time_utc"])
        coords = geometry["coordinates"]
        lon, lat = float(coords[0]), float(coords[1])
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat) and inside_uk_view(lon, lat)):
        return None
    if not (cutoff <= timestamp.date() <= now.date() and timestamp <= now):
        return None
    return (timestamp.date().isoformat(), feature)


def _write_if_changed(destination: Path, obj: object) -> bool:
    content = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    if destination.exists() and destination.read_text(encoding="utf-8") == content:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, destination)
    return True


def _read_features(file: Path) -> list[dict]:
    data = json.loads(file.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA_DAY or data.get("type") != "FeatureCollection":
        raise ValueError(f"{file}: invalid archive schema")
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"{file}: invalid archive features")
    return features


def _normalise_windows(prior: list, extra: dict | None, cutoff: datetime, now: datetime) -> list[dict]:
    records = []
    for rec in prior + ([extra] if extra else []):
        try:
            start = parse_utc(rec["start_utc"])
            end = parse_utc(rec["end_utc"])
            count = int(rec.get("successful_runs", 1))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if start >= end or end <= cutoff or start > now or count < 1:
            continue
        records.append((max(start, cutoff), min(end, now), count))
    records.sort(key=lambda row: (row[0], row[1]))
    merged = []
    for start, end, count in records:
        if merged and start <= merged[-1][1]:
            old = merged[-1]
            old[1] = max(old[1], end)
            old[2] += count
        else:
            merged.append([start, end, count])
    return [
        {"start_utc": format_utc(start), "end_utc": format_utc(end), "successful_runs": count}
        for start, end, count in merged
    ]


def archive_observations(
    archive_dir: Path,
    observations: Iterable[dict],
    *,
    now: datetime,
    scanned_window: dict | None = None,
) -> dict:
    """Merge genuine observations into day files; index every valid day.

    Read/validation failures abort (do not overwrite valid files with empty
    data). No sample points or synthetic historical dates are ever created.
    The one-year retention is rolling by UTC calendar date.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    cutoff = year_ago(now.date())
    cutoff_time = datetime.combine(cutoff, time.min, tzinfo=timezone.utc)
    archive_dir = Path(archive_dir)
    index_path = archive_dir / "index.json"
    old_index = None
    if index_path.exists():
        old_index = json.loads(index_path.read_text(encoding="utf-8"))
        if old_index.get("schema") != SCHEMA_INDEX:
            raise ValueError("Existing history index schema mismatch; refusing to overwrite it")

    incoming_by_day: dict[str, dict[str, dict]] = {}
    for feature in observations:
        valid = validate_feature(feature, now, cutoff)
        if valid:
            key, feature = valid
            incoming_by_day.setdefault(key, {})[feature["id"]] = feature

    # Inspect all files before modifying anything, so malformed archives fail
    # without silently deleting observations on the next scheduled run.
    existing_by_day: dict[str, dict[str, dict]] = {}
    expired_files: list[Path] = []
    if archive_dir.exists():
        for f in sorted(archive_dir.glob("days/*/*/*.json")):
            rel = f.relative_to(archive_dir).as_posix()
            m = DAY_PATTERN.fullmatch(rel)
            if not m:
                continue
            try:
                archive_day = date.fromisoformat("-".join(m.groups()))
            except ValueError as e:
                raise ValueError(f"{f}: invalid day path") from e
            if archive_day < cutoff:
                expired_files.append(f)
                continue
            if archive_day > now.date():
                raise ValueError(f"{f}: archive day lies in the future")
            archive_records = {}
            for record in _read_features(f):
                valid = validate_feature(record, now, cutoff)
                if valid is None or valid[0] != archive_day.isoformat():
                    raise ValueError(f"{f}: invalid, duplicated or wrong-day history observation")
                if record["id"] in archive_records:
                    raise ValueError(f"{f}: duplicate feature ID {record['id']}")
                archive_records[record["id"]] = record
            existing_by_day[archive_day.isoformat()] = archive_records

    changed_files = 0
    days = []
    total = 0
    oldest = newest = None
    for day in sorted(set(existing_by_day) | set(incoming_by_day)):
        features_by_id = existing_by_day.get(day, {}).copy()
        features_by_id.update(incoming_by_day.get(day, {}))
        records = sorted(features_by_id.values(), key=lambda f: (f["properties"]["time_utc"], f["id"]))
        if not records:
            continue
        first = records[0]["properties"]["time_utc"]
        last = records[-1]["properties"]["time_utc"]
        oldest = min(oldest, first) if oldest else first
        newest = max(newest, last) if newest else last
        total += len(records)
        day_meta = {
            "schema": SCHEMA_DAY,
            "type": "FeatureCollection",
            "metadata": {
                "date_utc": day,
                "source": PROVIDER,
                "observation": "optical satellite flash; not a confirmed ground strike",
                "count": len(records),
                "first_observation_utc": first,
                "last_observation_utc": last,
            },
            "features": records,
        }
        if _write_if_changed(archive_dir / day_path(date.fromisoformat(day)), day_meta):
            changed_files += 1
        days.append({
            "date": day,
            "path": day_path(date.fromisoformat(day)),
            "count": len(records),
            "first_observation_utc": first,
            "last_observation_utc": last,
        })
    for f in expired_files:
        f.unlink()
        changed_files += 1
    for year_dir in sorted((archive_dir / "days").glob("*") if (archive_dir / "days").exists() else []):
        for month_dir in sorted(year_dir.glob("*")):
            if month_dir.is_dir() and not any(month_dir.iterdir()):
                month_dir.rmdir()
        if year_dir.is_dir() and not any(year_dir.iterdir()):
            year_dir.rmdir()

    old_windows = old_index.get("scan_windows", []) if old_index else []
    merged_windows = _normalise_windows(old_windows, scanned_window, cutoff_time, now)
    index = {
        "schema": SCHEMA_INDEX,
        "generated_utc": format_utc(now),
        "archive_first_enabled_utc": (
            old_index.get("archive_first_enabled_utc") if old_index else format_utc(now)
        ),
        "retention": "one calendar year, UTC",
        "cutoff_date_utc": cutoff.isoformat(),
        "source": PROVIDER,
        "source_url": SOURCE_URL,
        "observation": "satellite optical lightning flashes, not ground strikes",
        "bounds_wsen": list(UK_BOUNDS),
        "first_observation_utc": oldest,
        "last_observation_utc": newest,
        "archived_observations": total,
        "archived_days_with_flashes": len(days),
        "days": days,
        "scan_windows": merged_windows,
        "scan_window_note": WINDOW_NOTE,
        "availability_note": HISTORY_NOTE,
        "status": "archive_partial_until_history_is_genuinely_collected_or_backfilled",
    }
    if _write_if_changed(index_path, index):
        changed_files += 1
    return {"index": index, "files_changed": changed_files, "days_expired": len(expired_files)}


def successful_scan(start_utc: str, end_utc: str, products_examined: int) -> dict:
    """Record a real successful EUMETSAT search (not a coverage guarantee)."""
    begin, end = parse_utc(start_utc), parse_utc(end_utc)
    if end <= begin or products_examined < 1:
        raise ValueError("Only a real successful, nonempty EUMETSAT search can be archived")
    return {"start_utc": format_utc(begin), "end_utc": format_utc(end), "successful_runs": 1}

#!/usr/bin/env python3
"""Append genuine EUMETSAT lightning observations to a one-year, UTC-day archive.

A data-plane module only. It does not touch GridAtlas's shell, UI, map, or
legacy fetch bridge. Stored FeatureCollections use the .json extension
intentionally: the original GridAtlas fetch bridge intercepts ALL .geojson
URLs, including cross-origin ones, but not these namespaced .json files.

Contract (uk-lightning-history-index.v2, see docs/HISTORY_ONE_YEAR.md):
- one index row per UTC date from the retention cutoff to today;
- ``coverage`` none/partial/full and ``scanned_seconds`` come from the
  recorded successful scan windows (examined 10-minute EUMETSAT slots);
- ``count`` is null iff coverage is none; 0 is only legal for scanned days;
- ``bytes``/``sha256`` describe the shard file; shards above
  ``MAX_SHARD_BYTES`` fail the run loudly; days above ``TARGET_SHARD_BYTES``
  are split into hourly parts ``days/YYYY/MM/DD/HH.json`` listed in the row;
- ``sealed`` says a shard can no longer change (safe for long HTTP caching).

An observed flash is NOT a proven cloud-to-ground strike. Missing archives
mean UNKNOWN history, not zero lightning. This archive is prospective: it
starts when enabled; the previous seven-day rolling feed is a seed over
half-scanned days, not a year of back-data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

from collect import (
    LOOKBACK_MAX_HOURS, LOOKBACK_MIN_HOURS, PROVIDER, RUN_RECORD_SCHEMA, SLOT_SECONDS, SOURCE_URL,
    UK_BOUNDS, inside_uk_view, parse_utc,
)

SCHEMA_INDEX = "uk-lightning-history-index.v2"
SCHEMA_INDEX_V1 = "uk-lightning-history-index.v1"  # accepted on read, upgraded on write
SCHEMA_DAY = "uk-lightning-history-day.v1"
MAX_SHARD_BYTES = 25_000_000       # hard failure (V6 builder rule, GitHub-safe)
TARGET_SHARD_BYTES = 8 * 1024 * 1024  # above this a day is split into hourly parts
COLLECTION_INTERVAL_HOURS = 1
UPDATE_GRACE_HOURS = 2  # GitHub cron drift observed up to ~2.4 h late
DAY_SECONDS = 86400
SLOT_PHASE_SECONDS = 9  # LI ARC slots run from HH:M0:09 to HH:(M+1)0:09
WINDOW_NOTE = (
    "Successful EUMETSAT search windows are the union of examined 10-minute product "
    "slots; they document what was searched, not a guarantee that every flash was detected."
)
HISTORY_NOTE = (
    "Only archived optical flash observations are included. coverage none / count null "
    "means never searched (unknown, not zero); count 0 is only reported for searched time."
)
SPLIT_NOTE = (
    "A row with count > 0 and path null is split into hourly parts listed in 'parts' "
    "(days/YYYY/MM/DD/HH.json); fetch the parts overlapping the wanted hours."
)
SEED_NOTE = (
    "Prospective archive: it begins at archive_first_enabled_utc; earlier dates come from the "
    "7-day live feed seed over partially scanned days. Nothing is backfilled or interpolated. "
    "A day with flashes but no recorded scan window gets coverage partial with scanned_seconds "
    "= the 10-minute slots its flashes prove were examined (a lower bound)."
)
STATUS_TEXT = "archive_partial_until_history_is_genuinely_collected_or_backfilled"
DAY_PATTERN = re.compile(r"^days/(\d{4})/(\d{2})/(\d{2})\.json$")
PART_PATTERN = re.compile(r"^days/(\d{4})/(\d{2})/(\d{2})/(\d{2})\.json$")


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


def part_path(day: date, hour: int) -> str:
    return f"days/{day:%Y/%m/%d}/{hour:02d}.json"


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


def _serialise(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _write_if_changed(destination: Path, content: str) -> bool:
    if destination.exists() and destination.read_text(encoding="utf-8") == content:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, destination)
    return True


def _read_features(file: Path, expected_day: str) -> list[dict]:
    data = json.loads(file.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA_DAY or data.get("type") != "FeatureCollection":
        raise ValueError(f"{file}: invalid archive schema")
    if data.get("metadata", {}).get("date_utc") != expected_day:
        raise ValueError(f"{file}: shard date does not match its path")
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"{file}: invalid archive features")
    return features


def _normalise_windows(prior: list, extra: list, cutoff: datetime, now: datetime) -> list[dict]:
    records = []
    for rec in list(prior) + list(extra):
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


def scanned_seconds_for_day(windows: list[dict], day: date) -> int:
    """Seconds of a UTC date covered by the (merged) successful scan windows."""
    day_start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    total = 0.0
    for window in windows:
        start = max(parse_utc(window["start_utc"]), day_start)
        end = min(parse_utc(window["end_utc"]), day_end)
        if end > start:
            total += (end - start).total_seconds()
    return max(0, min(DAY_SECONDS, int(round(total))))


def slots_proved_by_flashes(records: list[dict]) -> int:
    """Lower bound of searched seconds: every archived flash proves that the
    10-minute product slot containing it was examined."""
    slots = set()
    for record in records:
        moment = parse_utc(record["properties"]["time_utc"])
        seconds = moment.hour * 3600 + moment.minute * 60 + moment.second - SLOT_PHASE_SECONDS
        slots.add(seconds // SLOT_SECONDS)
    return min(DAY_SECONDS, len(slots) * SLOT_SECONDS)


def coverage_for(scanned: int) -> str:
    if scanned <= 0:
        return "none"
    return "full" if scanned >= DAY_SECONDS else "partial"


def is_sealed(day: date, coverage: str, now: datetime, lookback_max_hours: float = LOOKBACK_MAX_HOURS) -> bool:
    """A shard can no longer change once every slot was examined, or once the
    longest possible lookback window can no longer reach the date."""
    if coverage == "full":
        return True
    closes = datetime.combine(day, time.min, tzinfo=timezone.utc) + timedelta(days=1, hours=lookback_max_hours)
    return now >= closes


def _day_collection(day: str, records: list[dict], *, hour: int | None = None) -> dict:
    metadata = {
        "date_utc": day,
        "source": PROVIDER,
        "observation": "optical satellite flash; not a confirmed ground strike",
        "count": len(records),
        "first_observation_utc": records[0]["properties"]["time_utc"],
        "last_observation_utc": records[-1]["properties"]["time_utc"],
    }
    if hour is not None:
        metadata["hour_utc"] = f"{hour:02d}"
        metadata["part_of_day"] = True
    return {"schema": SCHEMA_DAY, "type": "FeatureCollection", "metadata": metadata, "features": records}


class ShardTooLarge(ValueError):
    """A shard would exceed MAX_SHARD_BYTES; the run must fail loudly."""


def _plan_shards(day: str, records: list[dict], *, max_bytes: int, target_bytes: int) -> list[tuple[str, str, dict]]:
    """Return [(relative path, content, summary)] for one day's records.

    A day at or below ``target_bytes`` is one file; above it the day is split
    into hourly parts. Any single file above ``max_bytes`` raises.
    """
    whole = _serialise(_day_collection(day, records))
    size = len(whole.encode("utf-8"))
    day_date = date.fromisoformat(day)
    if size <= target_bytes:
        if size > max_bytes:
            raise ShardTooLarge(f"{day_path(day_date)} would be {size} bytes > max_shard_bytes {max_bytes}")
        return [(day_path(day_date), whole, {
            "count": len(records), "bytes": size, "sha256": _sha256(whole),
            "first_observation_utc": records[0]["properties"]["time_utc"],
            "last_observation_utc": records[-1]["properties"]["time_utc"]})]
    by_hour: dict[int, list[dict]] = {}
    for record in records:
        by_hour.setdefault(parse_utc(record["properties"]["time_utc"]).hour, []).append(record)
    plan = []
    for hour in sorted(by_hour):
        content = _serialise(_day_collection(day, by_hour[hour], hour=hour))
        part_size = len(content.encode("utf-8"))
        if part_size > max_bytes:
            raise ShardTooLarge(
                f"{part_path(day_date, hour)} would be {part_size} bytes > max_shard_bytes {max_bytes}; "
                "refusing to write (feed unaffected; archive step fails loudly)")
        plan.append((part_path(day_date, hour), content, {
            "hour": f"{hour:02d}", "count": len(by_hour[hour]), "bytes": part_size, "sha256": _sha256(content),
            "first_observation_utc": by_hour[hour][0]["properties"]["time_utc"],
            "last_observation_utc": by_hour[hour][-1]["properties"]["time_utc"]}))
    return plan


def _verify_unchanged(archive_dir: Path, row: dict) -> None:
    """Cheap verification of an untouched day against its previous index row."""
    targets = []
    if row.get("path"):
        targets.append((row["path"], row.get("bytes"), row.get("sha256")))
    for part in row.get("parts") or []:
        targets.append((part["path"], part.get("bytes"), part.get("sha256")))
    for rel, size, digest in targets:
        file = archive_dir / rel
        if not file.exists():
            raise ValueError(f"{file}: listed in the index but missing on disk")
        content = file.read_text(encoding="utf-8")
        if size is not None and len(content.encode("utf-8")) != size:
            raise ValueError(f"{file}: size differs from the index; refusing to overwrite")
        if digest and _sha256(content) != digest:
            raise ValueError(f"{file}: sha256 differs from the index; refusing to overwrite")


def _load_old_index(index_path: Path) -> dict | None:
    if not index_path.exists():
        return None
    old = json.loads(index_path.read_text(encoding="utf-8"))
    if old.get("schema") not in (SCHEMA_INDEX, SCHEMA_INDEX_V1):
        raise ValueError("Existing history index schema mismatch; refusing to overwrite it")
    return old


def _discover_shards(archive_dir: Path, cutoff: date, today: date) -> tuple[dict[str, dict], list[Path]]:
    disk: dict[str, dict] = {}
    expired: list[Path] = []
    if not archive_dir.exists():
        return disk, expired
    for f in sorted(list(archive_dir.glob("days/*/*/*.json")) + list(archive_dir.glob("days/*/*/*/*.json"))):
        rel = f.relative_to(archive_dir).as_posix()
        m_day, m_part = DAY_PATTERN.fullmatch(rel), PART_PATTERN.fullmatch(rel)
        if not (m_day or m_part):
            continue
        groups = (m_day or m_part).groups()
        try:
            archive_day = date.fromisoformat("-".join(groups[:3]))
            hour = int(groups[3]) if m_part else None
            if hour is not None and not 0 <= hour <= 23:
                raise ValueError
        except ValueError as e:
            raise ValueError(f"{f}: invalid day path") from e
        if archive_day < cutoff:
            expired.append(f)
            continue
        if archive_day > today:
            raise ValueError(f"{f}: archive day lies in the future")
        entry = disk.setdefault(archive_day.isoformat(), {"file": None, "parts": {}})
        if hour is None:
            entry["file"] = f
        else:
            entry["parts"][hour] = f
    return disk, expired


def archive_observations(
    archive_dir: Path,
    observations: Iterable[dict],
    *,
    now: datetime,
    scanned_window: dict | None = None,
    scanned_windows: Iterable[dict] | None = None,
    seed_scan_windows: Iterable[dict] | None = None,
    lookback_hours: float | None = None,
    max_shard_bytes: int = MAX_SHARD_BYTES,
    target_shard_bytes: int = TARGET_SHARD_BYTES,
) -> dict:
    """Merge genuine observations into day shards and rebuild index v2.

    Read/validation failures abort (do not overwrite valid files with empty
    data). No sample points or synthetic historical dates are ever created.
    Observations before the one-year cutoff are clamped away and counted in
    ``dropped_before_cutoff`` instead of raising. ``seed_scan_windows`` are
    merged only when no index exists yet (one-off, idempotent afterwards).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    today = now.date()
    cutoff = year_ago(today)
    cutoff_time = datetime.combine(cutoff, time.min, tzinfo=timezone.utc)
    archive_dir = Path(archive_dir)
    index_path = archive_dir / "index.json"
    old_index = _load_old_index(index_path)
    old_rows = {row["date"]: row for row in (old_index or {}).get("days", []) if isinstance(row, dict) and "date" in row}

    incoming_by_day: dict[str, dict[str, dict]] = {}
    dropped_before_cutoff = dropped_invalid = 0
    for feature in observations:
        valid = validate_feature(feature, now, cutoff)
        if valid:
            key, feature = valid
            incoming_by_day.setdefault(key, {})[feature["id"]] = feature
            continue
        try:
            if parse_utc(feature["properties"]["time_utc"]).date() < cutoff:
                dropped_before_cutoff += 1
                continue
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        dropped_invalid += 1

    disk, expired_files = _discover_shards(archive_dir, cutoff, today)

    # Inspect before modifying anything so malformed archives fail without
    # silently deleting observations on the next scheduled run.
    changed_files = 0
    summaries: dict[str, dict] = {}
    split_days: list[str] = []
    # A shard the old index lists but the disk lacks is LOST: its count can no
    # longer be proven, so the day becomes unknown (count null), never zero or
    # a partial count rebuilt from whatever flashes arrive later. Sticky via
    # the row's ``shard_lost`` flag until the date leaves retention.
    window = (cutoff.isoformat(), today.isoformat())
    lost_days = {d for d, r in old_rows.items() if r.get("shard_lost") and window[0] <= d <= window[1]}
    indexed = {d for d, r in old_rows.items() if (r.get("path") or r.get("parts")) and window[0] <= d <= window[1]}
    dropped_lost_day = 0
    for day in sorted(set(disk) | set(incoming_by_day) | indexed):
        on_disk = disk.get(day, {"file": None, "parts": {}})
        old_row = old_rows.get(day)
        if day not in lost_days and old_row and (old_row.get("path") or old_row.get("parts")):
            listed = [old_row["path"]] if old_row.get("path") else [p["path"] for p in old_row.get("parts") or []]
            if any(not (archive_dir / rel).exists() for rel in listed):
                lost_days.add(day)
        if day in lost_days:
            dropped_lost_day += len(incoming_by_day.get(day, {}))
            continue  # leave any remaining files untouched (unlisted) for manual recovery from git history
        verifiable = (
            old_row is not None and day not in incoming_by_day
            and bool(old_row.get("path") or old_row.get("parts"))
            and bool(old_row.get("sha256") or old_row.get("parts"))
        )
        if verifiable:
            listed = {old_row["path"]} if old_row.get("path") else {p["path"] for p in old_row.get("parts") or []}
            present = ({on_disk["file"].relative_to(archive_dir).as_posix()} if on_disk["file"] else set()) | {
                p.relative_to(archive_dir).as_posix() for p in on_disk["parts"].values()}
            if listed == present:
                _verify_unchanged(archive_dir, old_row)
                summaries[day] = {
                    "count": old_row["count"], "bytes": old_row.get("bytes"), "sha256": old_row.get("sha256"),
                    "path": old_row.get("path"), "parts": old_row.get("parts"),
                    "first_observation_utc": old_row.get("first_observation_utc"),
                    "last_observation_utc": old_row.get("last_observation_utc"),
                    "records": None, "scanned_floor": old_row.get("scanned_seconds") or 0,
                }
                if old_row.get("parts"):
                    split_days.append(day)
                continue
        features_by_id: dict[str, dict] = {}
        files = ([on_disk["file"]] if on_disk["file"] else []) + [on_disk["parts"][h] for h in sorted(on_disk["parts"])]
        for f in files:
            for record in _read_features(f, day):
                valid = validate_feature(record, now, cutoff)
                if valid is None or valid[0] != day:
                    raise ValueError(f"{f}: invalid, duplicated or wrong-day history observation")
                if record["id"] in features_by_id:
                    raise ValueError(f"{f}: duplicate feature ID {record['id']}")
                features_by_id[record["id"]] = record
        for identifier, feature in incoming_by_day.get(day, {}).items():
            features_by_id.setdefault(identifier, feature)  # existing bytes win; dedupe by stable id
        records = sorted(features_by_id.values(), key=lambda f: (f["properties"]["time_utc"], f["id"]))
        if not records:
            continue
        plan = _plan_shards(day, records, max_bytes=max_shard_bytes, target_bytes=target_shard_bytes)
        planned_paths = {rel for rel, _, _ in plan}
        for rel, content, _ in plan:
            if _write_if_changed(archive_dir / rel, content):
                changed_files += 1
        for stale in files:
            if stale.relative_to(archive_dir).as_posix() not in planned_paths:
                stale.unlink()
                changed_files += 1
        if len(plan) == 1 and "hour" not in plan[0][2]:
            rel, _, summary = plan[0]
            summaries[day] = {**summary, "path": rel, "parts": None, "records": records}
        else:
            split_days.append(day)
            parts = [{"hour": s["hour"], "path": rel, "count": s["count"], "bytes": s["bytes"], "sha256": s["sha256"],
                      "first_observation_utc": s["first_observation_utc"], "last_observation_utc": s["last_observation_utc"]}
                     for rel, _, s in plan]
            summaries[day] = {
                "count": len(records), "bytes": sum(p["bytes"] for p in parts), "sha256": None, "path": None,
                "parts": parts, "first_observation_utc": records[0]["properties"]["time_utc"],
                "last_observation_utc": records[-1]["properties"]["time_utc"], "records": records,
            }
    for f in expired_files:
        f.unlink()
        changed_files += 1
    _remove_empty_dirs(archive_dir / "days")

    extra = list(scanned_windows or [])
    if scanned_window:
        extra.append(scanned_window)
    if old_index is None and seed_scan_windows:
        extra.extend(seed_scan_windows)
    old_windows = old_index.get("scan_windows", []) if old_index else []
    merged_windows = _normalise_windows(old_windows, extra, cutoff_time, now)
    last_scan_end = max((parse_utc(w["end_utc"]) for w in merged_windows), default=None)
    coverage_start = min((parse_utc(w["start_utc"]) for w in merged_windows), default=None)

    rows = []
    total = 0
    oldest = newest = None
    day = cutoff
    while day <= today:
        key = day.isoformat()
        scanned = scanned_seconds_for_day(merged_windows, day)
        summary = summaries.get(key)
        if summary:
            total += summary["count"]
            oldest = min(oldest, summary["first_observation_utc"]) if oldest else summary["first_observation_utc"]
            newest = max(newest, summary["last_observation_utc"]) if newest else summary["last_observation_utc"]
            if scanned <= 0:
                # Flashes prove their slots were examined even when no window was
                # recorded for the date (e.g. a seed older than the recorded windows).
                proven = slots_proved_by_flashes(summary["records"]) if summary.get("records") else SLOT_SECONDS
                scanned = max(SLOT_SECONDS, proven, int(summary.get("scanned_floor") or 0))
        if key in lost_days:
            scanned, summary = 0, None
        coverage = coverage_for(scanned)
        count = None if coverage == "none" else (summary["count"] if summary else 0)
        row = {
            "date": key,
            "coverage": coverage,
            "scanned_seconds": scanned,
            "count": count,
            "bytes": summary["bytes"] if summary else None,
            "path": summary["path"] if summary else None,
            "sealed": is_sealed(day, coverage, now, LOOKBACK_MAX_HOURS),
            "first_observation_utc": summary["first_observation_utc"] if summary else None,
            "last_observation_utc": summary["last_observation_utc"] if summary else None,
            "sha256": summary["sha256"] if summary else None,
        }
        if summary and summary.get("parts"):
            row["parts"] = summary["parts"]
        if key in lost_days:
            row["shard_lost"] = True
        rows.append(row)
        day += timedelta(days=1)

    first_enabled = (old_index or {}).get("archive_first_enabled_utc") or format_utc(now)
    index = {
        "schema": SCHEMA_INDEX,
        "generated_utc": format_utc(now),
        "collection_interval_hours": COLLECTION_INTERVAL_HOURS,
        "collection_lookback_hours": LOOKBACK_MIN_HOURS,
        "collection_lookback_hours_max": LOOKBACK_MAX_HOURS,
        "last_run_lookback_hours": lookback_hours,
        "update_grace_hours": UPDATE_GRACE_HOURS,
        "next_update_due_utc": format_utc(now + timedelta(hours=COLLECTION_INTERVAL_HOURS + UPDATE_GRACE_HOURS)),
        "cutoff_date_utc": cutoff.isoformat(),
        "retention": "one calendar year, UTC",
        "source": PROVIDER,
        "source_url": SOURCE_URL,
        "observation": "satellite optical lightning flashes, not ground strikes",
        "bounds_wsen": list(UK_BOUNDS),
        "max_shard_bytes": max_shard_bytes,
        "target_shard_bytes": target_shard_bytes,
        "archive_first_enabled_utc": first_enabled,
        "coverage_start_utc": format_utc(coverage_start) if coverage_start else None,
        "last_successful_scan_end_utc": format_utc(last_scan_end) if last_scan_end else None,
        "archived_observations": total,
        "archived_days_with_flashes": len(summaries),
        "scanned_days": sum(1 for r in rows if r["coverage"] != "none"),
        "first_observation_utc": oldest,
        "last_observation_utc": newest,
        "scan_windows": merged_windows,
        "scan_window_note": WINDOW_NOTE,
        "availability_note": HISTORY_NOTE,
        "split_note": SPLIT_NOTE,
        "seed_note": SEED_NOTE,
        "days": rows,
        "status": STATUS_TEXT,
    }
    if _write_if_changed(index_path, _serialise(index)):
        changed_files += 1
    return {
        "index": index, "files_changed": changed_files, "days_expired": len(expired_files),
        "dropped_before_cutoff": dropped_before_cutoff, "dropped_invalid": dropped_invalid,
        "split_days": sorted(set(split_days)),
        "lost_days": sorted(lost_days), "dropped_lost_day": dropped_lost_day,
    }


def _remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()


def successful_scan(start_utc: str, end_utc: str, products_examined: int) -> dict:
    """Record a real successful EUMETSAT search (not a coverage guarantee).

    Zero products means the search found nothing to examine (an outage or an
    empty catalogue), which is UNKNOWN coverage, so it cannot be recorded.
    """
    begin, end = parse_utc(start_utc), parse_utc(end_utc)
    if end <= begin or products_examined < 1:
        raise ValueError("Only a real successful, nonempty EUMETSAT search can be archived")
    return {"start_utc": format_utc(begin), "end_utc": format_utc(end), "successful_runs": 1}


def windows_from_run_record(record: dict) -> list[dict]:
    """Scan windows (one per examined range) from a collect.py run record."""
    if record.get("outcome") != "success" or int(record.get("products_examined") or 0) < 1:
        return []
    if record.get("slots_parsed") is False:
        # No product name carried a sensing slot, so scanned_ranges is the whole
        # requested window including the unpublished tail: coverage stays unknown.
        return []
    return [successful_scan(r["start_utc"], r["end_utc"], 1) for r in record.get("scanned_ranges", [])
            if parse_utc(r["end_utc"]) > parse_utc(r["start_utc"])]


def load_seed_windows(path: Path | None) -> list[dict]:
    """Reconstructed scan windows of runs that predate the archive (optional)."""
    if not path or not Path(path).exists():
        return []
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    windows = obj.get("scan_windows", obj) if isinstance(obj, dict) else obj
    return [successful_scan(w["start_utc"], w["end_utc"], int(w.get("products_examined", 1)))
            for w in windows]


def verify_archive(archive_dir: Path) -> dict:
    """Check every listed shard's bytes and sha256 against the index."""
    archive_dir = Path(archive_dir)
    index = _load_old_index(archive_dir / "index.json")
    if index is None:
        raise ValueError("no index.json")
    checked = 0
    for row in index["days"]:
        if row.get("path") or row.get("parts"):
            _verify_unchanged(archive_dir, row)
            checked += 1
    return {"rows": len(index["days"]), "shards_checked": checked}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="Append a collect.py run record to the archive")
    ingest.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    ingest.add_argument("--run-record", type=Path, required=True)
    ingest.add_argument("--seed-scan-windows", type=Path, default=Path("data/seed_scan_windows.json"),
                        help="Used only when no index exists yet")
    ingest.add_argument("--result-out", type=Path, default=None)
    ingest.add_argument("--now", default=None, help="ISO-8601 UTC override (tests/fixtures)")
    verify = sub.add_parser("verify", help="Verify shard bytes/sha256 against the index")
    verify.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    args = parser.parse_args(argv)
    if args.command == "verify":
        print(json.dumps(verify_archive(args.archive_dir)))
        return 0
    record = json.loads(args.run_record.read_text(encoding="utf-8"))
    if record.get("schema") != RUN_RECORD_SCHEMA:
        raise SystemExit("run record schema mismatch")
    if record.get("outcome") != "success":
        raise SystemExit(f"collection failed; nothing to archive: {record.get('error')}")
    now = parse_utc(args.now) if args.now else datetime.now(timezone.utc)
    result = archive_observations(
        args.archive_dir, record.get("features", []), now=now,
        scanned_windows=windows_from_run_record(record), seed_scan_windows=load_seed_windows(args.seed_scan_windows),
        lookback_hours=record.get("lookback_hours"),
    )
    summary = {k: v for k, v in result.items() if k != "index"}
    summary.update({
        "outcome": "success", "error": None,
        "archived_observations": result["index"]["archived_observations"],
        "index_generated_utc": result["index"]["generated_utc"],
        "last_successful_scan_end_utc": result["index"]["last_successful_scan_end_utc"],
    })
    if args.result_out:
        args.result_out.parent.mkdir(parents=True, exist_ok=True)
        args.result_out.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    print(f"Archived {summary['archived_observations']} dated optical observations; "
          f"{result['files_changed']} files changed, {result['days_expired']} days expired, "
          f"{result['dropped_before_cutoff']} observations older than the cutoff clamped away, "
          f"split days: {result['split_days'] or 'none'}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(f"ARCHIVE FAILURE: {error}", file=sys.stderr)
        raise SystemExit(2)

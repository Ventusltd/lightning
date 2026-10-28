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
import shutil
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

from collect import (
    LOOKBACK_MAX_HOURS, LOOKBACK_MIN_HOURS, PROVIDER, RUN_RECORD_SCHEMA, SLOT_EDGE_TOLERANCE_SECONDS, SLOT_SECONDS,
    FLASH_IN_PRODUCT_SPAN, FLASH_IN_UNEXAMINED_TIME, FLASH_NO_SLOT_OUTSIDE, FLASH_UNEXAMINED_PRODUCT,
    SOURCE_URL, UK_BOUNDS, classify_flash, flash_slot, inside_uk_view, parse_utc,
)

SCHEMA_INDEX = "uk-lightning-history-index.v2"
SCHEMA_INDEX_V1 = "uk-lightning-history-index.v1"  # accepted on read, upgraded on write
SCHEMA_DAY = "uk-lightning-history-day.v1"
SCHEMA_QUARANTINE = "uk-lightning-history-quarantine.v1"
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
    "7-day live feed seed over partially scanned days. Nothing is interpolated; a never-searched "
    "date is only filled by an explicit backfill (history.py backfill) from a fresh whole-day EUMETSAT "
    "search, with that search's examined slots as its coverage. "
    "A day with flashes but no recorded scan window gets coverage partial with scanned_seconds "
    "= the 10-minute slots its flashes prove were examined (a lower bound)."
)
STATUS_TEXT = "archive_partial_until_history_is_genuinely_collected_or_backfilled"
QUARANTINE_NOTE = (
    "Flashes observed for a shard_lost date after the loss. They are NOT counted in the index "
    "(the date stays unknown) until a verified recovery merges them (history.py recover)."
)
BACKFILL_MAX_DAYS = 31  # dates considered per backfill run (workflow input ``days``)
# The parallel backfill (backfill_parallel.py) may plan the whole retention year at once.
BACKFILL_PARALLEL_MAX_DAYS = 366
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


def _features_from_text(text: str, label: str, expected_day: str) -> list[dict]:
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_DAY or data.get("type") != "FeatureCollection":
        raise ValueError(f"{label}: invalid archive schema")
    if data.get("metadata", {}).get("date_utc") != expected_day:
        raise ValueError(f"{label}: shard date does not match {expected_day}")
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"{label}: invalid archive features")
    return features


def quarantine_path(day: str) -> str:
    return f"quarantine/{day}.json"


def _read_quarantine(file: Path, day: str, now: datetime, cutoff: date) -> dict[str, dict]:
    """Flashes held back for a shard_lost date, validated like shard features."""
    if not file.exists():
        return {}
    data = json.loads(file.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA_QUARANTINE or data.get("metadata", {}).get("date_utc") != day:
        raise ValueError(f"{file}: invalid quarantine file")
    held: dict[str, dict] = {}
    for record in data.get("features") or []:
        valid = validate_feature(record, now, cutoff)
        if valid is None or valid[0] != day or record["id"] in held:
            raise ValueError(f"{file}: invalid, duplicated or wrong-day quarantined observation")
        held[record["id"]] = record
    return held


def _quarantine_content(day: str, held: dict[str, dict]) -> str:
    records = sorted(held.values(), key=lambda f: (f["properties"]["time_utc"], f["id"]))
    return _serialise({"schema": SCHEMA_QUARANTINE, "type": "FeatureCollection",
                       "metadata": {"date_utc": day, "count": len(records), "note": QUARANTINE_NOTE},
                       "features": records})


def _evidence(row: dict, index_generated_utc: str | None) -> dict:
    """The last index entry recorded for a date: what a recovery must match."""
    return {
        "index_generated_utc": index_generated_utc,
        "path": row.get("path"), "parts": row.get("parts"), "count": row.get("count"),
        "bytes": row.get("bytes"), "sha256": row.get("sha256"),
        "first_observation_utc": row.get("first_observation_utc"),
        "last_observation_utc": row.get("last_observation_utc"),
    }


def integrity_alert(lost_dates: list[str]) -> str | None:
    if not lost_dates:
        return None
    n = len(lost_dates)
    return (f"INTEGRITY ALERT: {n} UTC date{'s' if n != 1 else ''} lost {'their' if n != 1 else 'its'} listed "
            f"archive shard ({', '.join(lost_dates)}); counts are unknown, not zero, until a verified "
            "recovery (history.py recover). Other dates are archived normally.")


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
    10-minute slot of ITS product was examined. The slot is taken from the
    product's nominal start (collect.flash_slot), the same attribution collect.py
    and the backfill gate use, so an edge flash timed just before its product's
    start does not prove the previous slot; ids without a product slot fall back
    to the flash's own time."""
    slots = set()
    for record in records:
        moment = parse_utc(record["properties"]["time_utc"])
        slot = flash_slot(record)
        anchor = slot[0] if slot else moment
        day_start = datetime.combine(moment.date(), time.min, tzinfo=timezone.utc)
        seconds = int(math.floor((anchor - day_start).total_seconds())) - SLOT_PHASE_SECONDS
        slots.add(seconds // SLOT_SECONDS)
    return min(DAY_SECONDS, len(slots) * SLOT_SECONDS)


def _phase_slot_start(moment: datetime) -> str:
    """Start (UTC, HH:M0:09 phase) of the 10-minute LI slot holding ``moment``."""
    anchor = datetime.combine(moment.date(), time.min, tzinfo=timezone.utc) + timedelta(seconds=SLOT_PHASE_SECONDS)
    slots = math.floor((moment - anchor).total_seconds() / SLOT_SECONDS)
    return format_utc(anchor + timedelta(seconds=slots * SLOT_SECONDS))


def _drop_cross_product_duplicates(found: dict[str, dict], kinds: dict[str, str]) -> int:
    """Remove copies of one flash (same time_utc and position) listed by more than
    one product, keeping the copy inside its own product's span (then the lowest
    id). Ids already make a flash unique within its product; this keeps a flash
    timed outside its product's span from being counted again when the product
    covering that time lists it too. Returns how many copies were removed."""
    groups: dict[tuple, list[str]] = {}
    for fid, feature in found.items():
        key = (feature["properties"]["time_utc"], tuple(feature["geometry"]["coordinates"]))
        groups.setdefault(key, []).append(fid)
    removed = 0
    for ids in groups.values():
        if len({i.rsplit(":", 1)[0] for i in ids}) < 2:
            continue
        keep = min(ids, key=lambda i: (kinds.get(i) != FLASH_IN_PRODUCT_SPAN, i))
        product = keep.rsplit(":", 1)[0]
        for fid in ids:
            if fid != keep and fid.rsplit(":", 1)[0] != product:
                del found[fid]
                removed += 1
    return removed


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


def _verify_unchanged(archive_dir: Path, row: dict, *, strict: bool = False) -> None:
    """Cheap verification of an untouched day against its previous index row.

    ``strict`` (used by ``verify``) refuses a listed file whose row lacks
    ``bytes`` or ``sha256`` instead of skipping that check.
    """
    targets = []
    if row.get("path"):
        targets.append((row["path"], row.get("bytes"), row.get("sha256")))
    for part in row.get("parts") or []:
        targets.append((part["path"], part.get("bytes"), part.get("sha256")))
    for rel, size, digest in targets:
        file = archive_dir / rel
        if not file.exists():
            raise ValueError(f"{file}: listed in the index but missing on disk")
        if strict and (size is None or not digest):
            raise ValueError(f"{file}: index row lacks {'bytes' if size is None else 'sha256'}; cannot verify")
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
    old_integrity = (old_index or {}).get("integrity") or {}
    lost_entries = {e["date"]: dict(e) for e in old_integrity.get("lost_days") or []
                    if isinstance(e, dict) and window[0] <= str(e.get("date")) <= window[1]}
    recoveries = [r for r in old_integrity.get("recoveries") or [] if isinstance(r, dict) and str(r.get("date")) >= window[0]]
    lost_days = {d for d, r in old_rows.items() if r.get("shard_lost") and window[0] <= d <= window[1]} | set(lost_entries)
    for d in lost_days - set(lost_entries):  # flagged before evidence was kept: no recorded entry to verify against
        lost_entries[d] = {"date": d, "reason": "flagged_without_evidence", "detected_utc": format_utc(now),
                           "missing_paths": None, "last_recorded": None}
    indexed = {d for d, r in old_rows.items() if (r.get("path") or r.get("parts")) and window[0] <= d <= window[1]}
    dropped_lost_day = quarantined_lost_day = 0
    for day in sorted(set(disk) | set(incoming_by_day) | indexed | lost_days):
        on_disk = disk.get(day, {"file": None, "parts": {}})
        old_row = old_rows.get(day)
        if day not in lost_days and old_row and (old_row.get("path") or old_row.get("parts")):
            listed = [old_row["path"]] if old_row.get("path") else [p["path"] for p in old_row.get("parts") or []]
            missing = [rel for rel in listed if not (archive_dir / rel).exists()]
            if missing:
                lost_days.add(day)
                lost_entries[day] = {
                    "date": day, "reason": "listed_file_missing", "detected_utc": format_utc(now),
                    "missing_paths": missing,
                    "last_recorded": _evidence(old_row, (old_index or {}).get("generated_utc")),
                }
        if day in lost_days:
            # Unknown, never zero, never a partial count: later flashes wait in
            # quarantine (not counted) until a verified recovery merges them.
            # Remaining shard files stay on disk, unlisted, for that recovery.
            q_file = archive_dir / quarantine_path(day)
            held = _read_quarantine(q_file, day, now, cutoff)
            new = {i: f for i, f in incoming_by_day.get(day, {}).items() if i not in held}
            if new:
                content = _quarantine_content(day, {**held, **new})
                if len(content.encode("utf-8")) > max_shard_bytes:
                    dropped_lost_day += len(new)  # counted, loud in status; never silently
                else:
                    _write_if_changed(q_file, content)
                    changed_files += 1
                    quarantined_lost_day += len(new)
                    held = {**held, **new}
            lost_entries[day]["quarantined_observations"] = len(held)
            lost_entries[day]["quarantine_path"] = quarantine_path(day) if held else None
            continue
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
    quarantine_dir = archive_dir / "quarantine"
    if quarantine_dir.exists():
        for f in sorted(quarantine_dir.glob("*.json")):
            if f.stem < window[0]:
                f.unlink()
                changed_files += 1
    _remove_empty_dirs(archive_dir / "days")
    _remove_empty_dirs(quarantine_dir)
    if quarantine_dir.exists() and not any(quarantine_dir.iterdir()):
        quarantine_dir.rmdir()

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

    lost_list = [lost_entries[d] for d in sorted(lost_days)]
    integrity = {
        "state": "shard_lost" if lost_list else "ok",
        "alert": integrity_alert(sorted(lost_days)),
        "lost_days": lost_list,
        "recoveries": recoveries,
    }
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
        "integrity": integrity,
        "days": rows,
        "status": STATUS_TEXT,
    }
    if _write_if_changed(index_path, _serialise(index)):
        changed_files += 1
    # Derived month density grids (grid/YYYY-MM.json). Only months whose rows
    # changed are rewritten and only changed days are re-read. A grid failure
    # never blocks the archive: clients see the grid's days differ from the
    # index and treat them as unknown (stale), never as zero.
    # Grid writes are reported in "grid", not in files_changed (shard/index files only).
    try:
        import grid  # local import: grid.py imports this module
        grid_result = grid.refresh_grids(archive_dir, index)
        grid_result["error"] = None
    except (ImportError, ValueError, OSError, KeyError, TypeError) as error:
        grid_result = {"written": [], "removed": [], "error": f"{type(error).__name__}: {error}"}
    return {
        "grid": grid_result,
        "index": index, "files_changed": changed_files, "days_expired": len(expired_files),
        "dropped_before_cutoff": dropped_before_cutoff, "dropped_invalid": dropped_invalid,
        "split_days": sorted(set(split_days)),
        "lost_days": sorted(lost_days), "dropped_lost_day": dropped_lost_day,
        "quarantined_lost_day": quarantined_lost_day,
        "integrity": {
            "state": integrity["state"], "alert": integrity["alert"], "lost_days": sorted(lost_days),
            "quarantined_observations": sum(e.get("quarantined_observations") or 0 for e in lost_list),
        },
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
    """Check every listed shard against the index before it is committed.

    Strict: bytes and sha256 must be present and match, each file's feature
    count must equal its row (and part) count, and ``count`` must be null iff
    coverage is none, with flashes always backed by a path or parts.
    """
    archive_dir = Path(archive_dir)
    index = _load_old_index(archive_dir / "index.json")
    if index is None:
        raise ValueError("no index.json")
    checked = 0
    lost_rows = sorted(r.get("date") for r in index["days"] if r.get("shard_lost"))
    integrity = index.get("integrity")
    if integrity is None:
        if lost_rows:
            raise ValueError(f"rows flagged shard_lost {lost_rows} but the index has no integrity block")
        state = "absent"  # index written before integrity tracking existed
    else:
        listed = sorted(str(e.get("date")) for e in integrity.get("lost_days") or [])
        if listed != lost_rows:
            raise ValueError(f"shard_lost rows {lost_rows} differ from integrity.lost_days {listed}; "
                             "a lost day is cleared only by history.py recover")
        state = integrity.get("state")
        if state != ("shard_lost" if lost_rows else "ok"):
            raise ValueError(f"integrity.state {state!r} contradicts shard_lost rows {lost_rows}")
        if lost_rows and not integrity.get("alert"):
            raise ValueError("shard_lost without an integrity alert")
    now = parse_utc(index["generated_utc"])
    cutoff = date.fromisoformat(index["cutoff_date_utc"])
    quarantine_dir = archive_dir / "quarantine"
    for f in sorted(quarantine_dir.glob("*.json")) if quarantine_dir.exists() else []:
        if f.stem not in lost_rows:
            raise ValueError(f"{f}: quarantine file for a date that is not shard_lost")
        _read_quarantine(f, f.stem, now, cutoff)
    for row in index["days"]:
        day, count = row.get("date"), row.get("count")
        if row.get("shard_lost") and (row.get("coverage") != "none" or count is not None or row.get("path")
                                      or row.get("parts") or row.get("sha256") or row.get("scanned_seconds")):
            raise ValueError(f"{day}: a shard_lost row must be coverage none, scanned_seconds 0, count null, no files")
        if (count is None) != (row.get("coverage") == "none"):
            raise ValueError(f"{day}: count must be null iff coverage is none")
        listed = [(row["path"], count)] if row.get("path") else [(p["path"], p.get("count")) for p in row.get("parts") or []]
        if count and not listed:
            raise ValueError(f"{day}: count {count} without path or parts")
        if not listed:
            continue
        _verify_unchanged(archive_dir, row, strict=True)
        found = 0
        for rel, expected in listed:
            n = len(_read_features(archive_dir / rel, day))
            if n != expected:
                raise ValueError(f"{archive_dir / rel}: {n} features but the index count is {expected}")
            found += n
        if found != count:
            raise ValueError(f"{day}: parts hold {found} features but the row count is {count}")
        checked += 1
    return {"rows": len(index["days"]), "shards_checked": checked, "integrity": state, "lost_days": lost_rows}


def _covered_gap_seconds(required: list[tuple[datetime, datetime]], cover: list[tuple[datetime, datetime]]) -> int:
    """Seconds of ``required`` intervals not inside the union of ``cover``."""
    gap = 0.0
    for start, end in required:
        cursor = start
        for c_start, c_end in sorted(cover):
            if c_end <= cursor or c_start >= end:
                continue
            if c_start > cursor:
                gap += (c_start - cursor).total_seconds()
            cursor = max(cursor, c_end)
            if cursor >= end:
                break
        if cursor < end:
            gap += (end - cursor).total_seconds()
    return int(math.ceil(gap))


def _expected_files(evidence: dict) -> list[dict]:
    if evidence.get("path"):
        return [{"path": evidence["path"], "bytes": evidence.get("bytes"), "sha256": evidence.get("sha256"),
                 "count": evidence.get("count")}]
    return [{"path": p["path"], "bytes": p.get("bytes"), "sha256": p.get("sha256"), "count": p.get("count")}
            for p in evidence.get("parts") or []]


def recover_day(
    archive_dir: Path,
    day: str,
    *,
    now: datetime,
    from_shards: Iterable[Path] | None = None,
    rebuild_record: Path | None = None,
    recorded_index: Path | None = None,
    accept_unanchored: bool = False,
    max_shard_bytes: int = MAX_SHARD_BYTES,
    target_shard_bytes: int = TARGET_SHARD_BYTES,
) -> dict:
    """Clear a shard_lost date only from verified evidence, with an audit record.

    ``from_shards``: earlier copies of the lost file(s) (e.g. ``git show``) whose
    bytes and sha256 must equal a previously recorded index entry and whose
    feature counts must equal the recorded counts. ``rebuild_record``: a fresh,
    independent collect.py run record whose examined slots cover every second
    the archive recorded as searched on that date and whose flash count equals
    the recorded count (``accept_unanchored`` only when no count was recorded).
    Every gate runs before anything is written; a refusal changes nothing.
    """
    if (from_shards is None) == (rebuild_record is None):
        raise ValueError("recover needs exactly one of from_shards / rebuild_record")
    now = now.astimezone(timezone.utc)
    cutoff = year_ago(now.date())
    archive_dir = Path(archive_dir)
    index = _load_old_index(archive_dir / "index.json")
    if index is None:
        raise ValueError("no index.json")
    day = date.fromisoformat(day).isoformat()
    row = next((r for r in index["days"] if r.get("date") == day), None)
    if not row or not row.get("shard_lost"):
        raise ValueError(f"{day} is not marked shard_lost; nothing to recover")
    integrity = index.setdefault("integrity", {"state": "shard_lost", "alert": None, "lost_days": [], "recoveries": []})
    entry = next((e for e in integrity.get("lost_days") or [] if e.get("date") == day), None) or {}

    evidence = entry.get("last_recorded")
    anchor: dict = {"source": "lost_entry"}
    if not (evidence and evidence.get("count")) and recorded_index:
        raw = Path(recorded_index).read_bytes()
        earlier = json.loads(raw)
        if not isinstance(earlier, dict) or earlier.get("schema") not in (SCHEMA_INDEX, SCHEMA_INDEX_V1):
            raise ValueError(f"{recorded_index}: not a lightning history index")
        old = next((r for r in earlier.get("days") or [] if r.get("date") == day and (r.get("path") or r.get("parts"))), None)
        if old:
            evidence = _evidence(old, earlier.get("generated_utc"))
            anchor = {"source": "recorded_index", "index_sha256": hashlib.sha256(raw).hexdigest()}
    if evidence and not evidence.get("count"):
        evidence = None
    if evidence:
        anchor.update({k: evidence.get(k) for k in ("index_generated_utc", "count", "bytes", "sha256", "path")})
        if evidence.get("parts"):
            anchor["parts"] = [{"path": p["path"], "sha256": p.get("sha256"), "count": p.get("count")} for p in evidence["parts"]]

    disk, _ = _discover_shards(archive_dir, cutoff, now.date())
    on_disk = disk.get(day, {"file": None, "parts": {}})
    present = {f.relative_to(archive_dir).as_posix(): f
               for f in ([on_disk["file"]] if on_disk["file"] else []) + list(on_disk["parts"].values())}
    held = _read_quarantine(archive_dir / quarantine_path(day), day, now, cutoff)

    def checked_features(text: str, label: str) -> list[dict]:
        out = []
        for record in _features_from_text(text, label, day):
            valid = validate_feature(record, now, cutoff)
            if valid is None or valid[0] != day:
                raise ValueError(f"{label}: invalid or wrong-day observation")
            out.append(record)
        return out

    writes: dict[str, str] = {}
    inputs: list[dict] = []
    audit: dict = {"date": day, "recovered_utc": format_utc(now), "lost_detected_utc": entry.get("detected_utc"),
                   "missing_paths": entry.get("missing_paths"), "verified_against": anchor if evidence else None}
    if from_shards is not None:
        if not evidence:
            raise ValueError(f"{day}: no previously recorded index entry (sha256 + count) to verify the shard against; "
                             "pass --recorded-index with an earlier index.json from git history, or use --rebuild")
        expected = _expected_files(evidence)
        if any(not e["sha256"] or e["bytes"] is None or e["count"] is None for e in expected):
            raise ValueError(f"{day}: the recorded entry lacks sha256/bytes/count; it cannot verify a shard")
        supplied: dict[str, bytes] = {}
        for path in from_shards:
            data = Path(path).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            match = next((e for e in expected if e["sha256"] == digest and e["bytes"] == len(data)), None)
            if match is None:
                raise ValueError(f"{path}: sha256 {digest} / {len(data)} bytes match no recorded file of {day} "
                                 f"({', '.join(e['path'] + ' ' + e['sha256'] for e in expected)}); refusing")
            supplied[match["path"]] = data
            inputs.append({"file": Path(path).name, "as": match["path"], "sha256": digest, "bytes": len(data)})
        unexpected = sorted(set(present) - {e["path"] for e in expected})
        if unexpected:
            raise ValueError(f"{unexpected}: files on disk for {day} that the recorded entry does not list; move them aside")
        ids: set[str] = set()
        total = 0
        for e in expected:
            if e["path"] in supplied:
                data = supplied[e["path"]]
            elif e["path"] in present:
                data = present[e["path"]].read_bytes()
                if hashlib.sha256(data).hexdigest() != e["sha256"] or len(data) != e["bytes"]:
                    raise ValueError(f"{e['path']}: surviving file differs from the recorded sha256; refusing")
                inputs.append({"file": e["path"], "as": e["path"], "sha256": e["sha256"], "bytes": e["bytes"],
                               "source": "survivor_on_disk"})
            else:
                raise ValueError(f"{e['path']}: recorded file is still missing; supply it with --from-shard")
            features = checked_features(data.decode("utf-8"), e["path"])
            if len(features) != e["count"]:
                raise ValueError(f"{e['path']}: {len(features)} features but the recorded count is {e['count']}")
            for f in features:
                if f["id"] in ids:
                    raise ValueError(f"{e['path']}: duplicate feature ID {f['id']}")
                ids.add(f["id"])
            total += len(features)
            writes[e["path"]] = data.decode("utf-8")
        if total != evidence["count"]:
            raise ValueError(f"{day}: files hold {total} features but the recorded count is {evidence['count']}")
        new_row = {k: evidence.get(k) for k in ("count", "bytes", "sha256", "path", "parts",
                                                 "first_observation_utc", "last_observation_utc")}
        audit.update({"method": "from_shard", "byte_identical": True})
    else:
        raw = Path(rebuild_record).read_bytes()
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("schema") != RUN_RECORD_SCHEMA:
            raise ValueError(f"{rebuild_record}: not a collect.py run record")
        if record.get("outcome") != "success" or int(record.get("products_examined") or 0) < 1:
            raise ValueError(f"{rebuild_record}: the rebuild collection did not succeed with examined products")
        if record.get("slots_parsed") is not True:
            raise ValueError(f"{rebuild_record}: slots_parsed is not true, so examined slots are unproven; refusing")
        run_utc = parse_utc(record["run_utc"])
        detected = entry.get("detected_utc")
        if detected and run_utc <= parse_utc(detected):
            raise ValueError(f"{rebuild_record}: run_utc {record['run_utc']} is not after the loss was detected "
                             f"({detected}); an independent rebuild must be a fresh EUMETSAT download")
        day_start = datetime.combine(date.fromisoformat(day), time.min, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        required = []
        for w in index.get("scan_windows") or []:
            start, end = max(parse_utc(w["start_utc"]), day_start), min(parse_utc(w["end_utc"]), day_end)
            if end > start:
                required.append((start, end))
        if not required:
            required = [(day_start, day_end)]  # nothing recorded: the rebuild must cover the whole date
        cover = [(parse_utc(w["start_utc"]), parse_utc(w["end_utc"])) for w in windows_from_run_record(record)]
        uncovered = _covered_gap_seconds(required, cover)
        if uncovered:
            raise ValueError(f"{rebuild_record}: rebuild does not cover {uncovered} s of the time the archive "
                             f"recorded as searched on {day}; refusing a partial count")
        found: dict[str, dict] = {}
        for feature in record.get("features") or []:
            valid = validate_feature(feature, now, cutoff)
            if valid and valid[0] == day:
                found.setdefault(feature["id"], feature)
        survivors: set[str] = set()
        for rel, f in present.items():
            survivors |= {r["id"] for r in _read_features(f, day)}
        lacking = (set(held) | survivors) - set(found)
        if lacking:
            raise ValueError(f"{rebuild_record}: rebuild lacks {len(lacking)} flash(es) already observed for {day} "
                             "(quarantined or still on disk); it is not complete")
        if evidence:
            if len(found) != evidence["count"]:
                raise ValueError(f"{rebuild_record}: rebuild found {len(found)} flashes on {day} but the recorded count "
                                 f"is {evidence['count']}; missing history is never replaced by a different count")
        elif not accept_unanchored:
            raise ValueError(f"{day}: no previously recorded count anchors the rebuild; re-run with "
                             "--accept-unanchored only after an independent check, or pass --recorded-index")
        if not found:
            raise ValueError(f"{rebuild_record}: rebuild found no flashes for a date that had a shard; refusing")
        records = sorted(found.values(), key=lambda f: (f["properties"]["time_utc"], f["id"]))
        plan = _plan_shards(day, records, max_bytes=max_shard_bytes, target_bytes=target_shard_bytes)
        writes = {rel: content for rel, content, _ in plan}
        if len(plan) == 1 and "hour" not in plan[0][2]:
            rel, _, summary = plan[0]
            new_row = {**{k: summary[k] for k in ("count", "bytes", "sha256", "first_observation_utc",
                                                    "last_observation_utc")}, "path": rel, "parts": None}
        else:
            parts = [{"hour": s["hour"], "path": rel, "count": s["count"], "bytes": s["bytes"], "sha256": s["sha256"],
                      "first_observation_utc": s["first_observation_utc"], "last_observation_utc": s["last_observation_utc"]}
                     for rel, _, s in plan]
            new_row = {"count": len(records), "bytes": sum(p["bytes"] for p in parts), "sha256": None, "path": None,
                       "parts": parts, "first_observation_utc": records[0]["properties"]["time_utc"],
                       "last_observation_utc": records[-1]["properties"]["time_utc"]}
        identical = bool(evidence) and sorted(
            (e["path"], e["sha256"]) for e in _expected_files(evidence)) == sorted(
            (rel, _sha256(content)) for rel, content in writes.items())
        inputs.append({"file": Path(rebuild_record).name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        audit.update({"method": "rebuild", "byte_identical": identical, "rebuild": {
            "run_utc": record["run_utc"], "products_examined": record.get("products_examined"),
            "required_seconds": int(sum((e - s).total_seconds() for s, e in required)), "uncovered_seconds": 0,
            "anchored": bool(evidence)}})

    # All gates passed: write the verified files, restore the row, audit, then re-index.
    for rel, f in present.items():
        if rel not in writes:
            f.unlink()
    for rel, content in writes.items():
        _write_if_changed(archive_dir / rel, content)
    row.pop("shard_lost", None)
    row.update({k: v for k, v in new_row.items() if k != "parts"})
    if new_row.get("parts"):
        row["parts"] = new_row["parts"]
    else:
        row.pop("parts", None)
    audit["inputs"] = inputs
    audit["result"] = {"count": new_row["count"], "bytes": new_row["bytes"], "sha256": new_row["sha256"],
                       "path": new_row["path"],
                       "parts": [{"path": p["path"], "sha256": p["sha256"], "count": p["count"]}
                                 for p in new_row.get("parts") or []] or None}
    audit["quarantined_merged"] = len(held)
    integrity["lost_days"] = [e for e in integrity.get("lost_days") or [] if e.get("date") != day]
    integrity["recoveries"] = list(integrity.get("recoveries") or []) + [audit]
    _write_if_changed(archive_dir / "index.json", _serialise(index))
    result = archive_observations(archive_dir, list(held.values()), now=now,
                                  lookback_hours=index.get("last_run_lookback_hours"),
                                  max_shard_bytes=max_shard_bytes, target_shard_bytes=target_shard_bytes)
    q_file = archive_dir / quarantine_path(day)
    if q_file.exists():
        q_file.unlink()
        _remove_empty_dirs(archive_dir / "quarantine")
        if not any((archive_dir / "quarantine").iterdir()):
            (archive_dir / "quarantine").rmdir()
    checked = verify_archive(archive_dir)
    return {"index": result["index"], "audit": audit, "verify": checked}


def _never_searched(row: dict | None, lost: set[str]) -> bool:
    return (row is not None and row.get("coverage") == "none" and row.get("count") is None
            and not row.get("shard_lost") and row.get("date") not in lost)


def backfill_plan(archive_dir: Path, *, end_date: date, days: int, today: date,
                  max_days: int = BACKFILL_MAX_DAYS) -> list[str]:
    """UTC dates in ``[end_date - days + 1, end_date]``, newest first, that the index
    marks never searched (coverage none / count null and not shard_lost).

    shard_lost dates are excluded: they need ``recover``, never a backfill. Dates
    before the retention cutoff or missing from the index are not listed.
    """
    if not 1 <= days <= max_days:
        raise ValueError(f"days must be 1..{max_days}")
    if end_date >= today:
        raise ValueError(f"end_date {end_date} must be a complete past UTC date (before {today})")
    index = _load_old_index(Path(archive_dir) / "index.json")
    if index is None:
        raise ValueError("no index.json: the hourly job creates the archive (and its seed) first; "
                         "a backfill only fills dates of an existing index")
    rows = {r.get("date"): r for r in index.get("days") or [] if isinstance(r, dict)}
    lost = {str(e.get("date")) for e in (index.get("integrity") or {}).get("lost_days") or [] if isinstance(e, dict)}
    cutoff = year_ago(today)
    plan = []
    day = end_date
    for _ in range(days):
        if day < cutoff:
            break
        if _never_searched(rows.get(day.isoformat()), lost):
            plan.append(day.isoformat())
        day -= timedelta(days=1)
    return plan


def backfill_day(
    archive_dir: Path,
    day: str,
    *,
    now: datetime,
    rebuild_record: Path,
    max_shard_bytes: int = MAX_SHARD_BYTES,
    target_shard_bytes: int = TARGET_SHARD_BYTES,
) -> dict:
    """Ingest a ``collect.py --rebuild-day`` record for a NEVER-SEARCHED date.

    Distinct from ``recover`` (shard_lost dates, verified against a recorded
    count): here nothing was ever recorded, so the date simply becomes searched
    with the rebuild's real coverage, i.e. the union of its examined 10-minute
    slots. Gaps in that search stay unknown; a count of 0 is only possible for
    searched time. Gates run first; the ingest and ``verify`` then run on a
    staging copy that replaces the archive only when both succeed, so a refusal
    or any failure (e.g. ``ShardTooLarge``) changes nothing.
    """
    now = now.astimezone(timezone.utc)
    archive_dir = Path(archive_dir)
    day = date.fromisoformat(day).isoformat()
    index = _load_old_index(archive_dir / "index.json")
    if index is None:
        raise ValueError("no index.json: the hourly job creates the archive (and its seed) first; nothing to backfill")
    row = next((r for r in index.get("days") or [] if r.get("date") == day), None)
    lost = {str(e.get("date")) for e in (index.get("integrity") or {}).get("lost_days") or [] if isinstance(e, dict)}
    if row is None:
        raise ValueError(f"{day} has no row in the index (outside retention or index out of date)")
    if row.get("shard_lost") or day in lost:
        raise ValueError(f"{day} is shard_lost: use history.py recover (verified recovery), not backfill")
    if not _never_searched(row, lost):
        raise ValueError(f"{day} was already searched (coverage {row.get('coverage')}, count {row.get('count')}); "
                         "backfill only fills never-searched dates")
    day_start = datetime.combine(date.fromisoformat(day), time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    if day_end > now:
        raise ValueError(f"{day} has not ended yet")

    raw = Path(rebuild_record).read_bytes()
    record = json.loads(raw)
    if not isinstance(record, dict) or record.get("schema") != RUN_RECORD_SCHEMA:
        raise ValueError(f"{rebuild_record}: not a collect.py run record")
    if record.get("rebuild_day_utc") != day:
        raise ValueError(f"{rebuild_record}: a rebuild of {record.get('rebuild_day_utc')}, not of {day}")
    if record.get("outcome") != "success" or int(record.get("products_examined") or 0) < 1:
        raise ValueError(f"{rebuild_record}: the rebuild collection did not succeed with examined products")
    if record.get("slots_parsed") is not True:
        raise ValueError(f"{rebuild_record}: slots_parsed is not true, so examined slots are unproven; refusing")
    if (parse_utc(record["query_start_utc"]) != day_start or parse_utc(record["query_end_utc"]) != day_end
            or parse_utc(record["run_utc"]) < day_end):
        raise ValueError(f"{rebuild_record}: not a search of the whole UTC date {day} made after it ended")
    windows = windows_from_run_record(record)
    if not windows:
        raise ValueError(f"{rebuild_record}: no examined slot; the date stays unknown")
    spans = [(parse_utc(w["start_utc"]), parse_utc(w["end_utc"])) for w in windows]
    if any(s < day_start or e > day_end for s, e in spans):
        raise ValueError(f"{rebuild_record}: scanned ranges leave {day}")
    examined = {parse_utc(s) for s in record.get("slots_examined_utc") or []}
    cutoff = year_ago(now.date())
    # A valid flash of the neighbouring UTC date (a product straddling midnight)
    # is not refused: it is dropped from this date and counted as outside_day.
    validity_cutoff = min(cutoff, date.fromisoformat(day) - timedelta(days=1))
    found: dict[str, dict] = {}
    kinds: dict[str, str] = {}
    outside_day = duplicate_ids = 0
    for feature in record.get("features") or []:
        valid = validate_feature(feature, now, validity_cutoff)
        if valid is None:
            raise ValueError(f"{rebuild_record}: invalid observation {feature.get('id') if isinstance(feature, dict) else feature!r}")
        # The flash's OWN time decides its UTC date (exactly one); a flash of the
        # neighbouring date is dropped from this date and counted, never refused.
        if valid[0] != day:
            outside_day += 1
            continue
        # Its product decides only whether it was examined; its own time is checked
        # against its product's nominal span +- SLOT_EDGE_TOLERANCE_SECONDS, then
        # against every examined span (collect.classify_flash).
        kind = classify_flash(feature, examined, spans)
        if kind == FLASH_UNEXAMINED_PRODUCT:
            raise ValueError(f"{rebuild_record}: flash {feature['id']} lies outside the examined slots "
                             "(its product is not among slots_examined_utc)")
        if kind == FLASH_NO_SLOT_OUTSIDE:
            raise ValueError(f"{rebuild_record}: flash {feature['id']} lies outside the examined slots "
                             "(no product slot in its id and outside every examined span)")
        if feature["id"] in found:
            duplicate_ids += 1
            continue
        found[feature["id"]] = feature
        kinds[feature["id"]] = kind
    cross_product_duplicates = _drop_cross_product_duplicates(found, kinds)
    expected_seconds = scanned_seconds_for_day(windows, date.fromisoformat(day))
    # A genuine flash of an examined product timed in time NO examined product
    # covers is kept and counted; its slot stays unsearched, so the date is
    # partial there (never zero, never extended to time nobody searched).
    unexamined = sorted(f["properties"]["time_utc"] for i, f in found.items() if kinds[i] == FLASH_IN_UNEXAMINED_TIME)
    if unexamined and expected_seconds >= DAY_SECONDS:  # impossible: full coverage covers every moment
        raise ValueError(f"{rebuild_record}: flashes outside every examined span on a fully searched date")
    unexamined_slots = sorted({_phase_slot_start(parse_utc(t)) for t in unexamined})

    disk, _ = _discover_shards(archive_dir, cutoff, now.date())
    if day in disk:
        raise ValueError(f"{day}: unlisted shard files exist on disk for a never-searched date; move them aside")

    staging = archive_dir.parent / f".{archive_dir.name}.backfill-{day}"
    retired = archive_dir.parent / f".{archive_dir.name}.backfill-retired"
    for leftover in (staging, retired):
        if leftover.exists():
            shutil.rmtree(leftover)
    shutil.copytree(archive_dir, staging)
    try:
        result = archive_observations(
            staging, list(found.values()), now=now, scanned_windows=windows,
            lookback_hours=index.get("last_run_lookback_hours"),
            max_shard_bytes=max_shard_bytes, target_shard_bytes=target_shard_bytes)
        checked = verify_archive(staging)
        new_row = next(r for r in result["index"]["days"] if r["date"] == day)
        if new_row["coverage"] == "none" or new_row["scanned_seconds"] != expected_seconds:
            raise ValueError(f"{day}: archived coverage {new_row['scanned_seconds']} s differs from the rebuild's "
                             f"examined {expected_seconds} s")
        if new_row["count"] != len(found):
            raise ValueError(f"{day}: archived count {new_row['count']} differs from the rebuild's {len(found)} flashes")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    os.replace(archive_dir, retired)
    os.replace(staging, archive_dir)
    shutil.rmtree(retired)
    index_after = result["index"]
    return {
        "outcome": "success", "error": None, "date": day, "row": new_row,
        "rebuild": {"run_utc": record["run_utc"], "products_examined": record.get("products_examined"),
                    "slots_examined": record.get("slots_examined"),
                    "record_sha256": hashlib.sha256(raw).hexdigest()},
        "unscanned_ranges": record.get("unscanned_ranges") or [],
        # Flashes timed outside the date: dropped by collect.py (record "outside_day")
        # plus any still present in the record and dropped here.
        "outside_day": outside_day + int(record.get("outside_day") or 0),
        "outside_day_in_record": outside_day,
        # Kept and counted flashes timed in unsearched time: their slots stay unknown.
        "unexamined_time_flashes": len(unexamined),
        "unexamined_time_slots_utc": unexamined_slots,
        # Same flash (time and position) listed by two products: archived once.
        "cross_product_duplicates": cross_product_duplicates,
        "duplicate_ids_in_record": duplicate_ids,
        "files_changed": result["files_changed"], "split_days": result["split_days"], "grid": result["grid"],
        "archived_observations": index_after["archived_observations"],
        "index_generated_utc": index_after["generated_utc"],
        "last_successful_scan_end_utc": index_after["last_successful_scan_end_utc"],
        "integrity": result["integrity"], "verify": checked,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    backfill = sub.add_parser(
        "backfill", help="Ingest a collect.py --rebuild-day record for a never-searched date (not shard_lost)")
    backfill.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    backfill.add_argument("--day", required=True, help="UTC date YYYY-MM-DD marked never searched (count null)")
    backfill.add_argument("--rebuild-record", type=Path, required=True)
    backfill.add_argument("--result-out", type=Path, default=None)
    backfill.add_argument("--now", default=None, help="ISO-8601 UTC override (tests/fixtures)")
    plan = sub.add_parser("backfill-plan", help="Print never-searched UTC dates, newest first, one per line")
    plan.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    plan.add_argument("--end-date", default="", help="Newest UTC date YYYY-MM-DD; empty = yesterday UTC")
    plan.add_argument("--days", type=int, default=7, help=f"Dates to consider back from end-date (1-{BACKFILL_MAX_DAYS})")
    plan.add_argument("--now", default=None, help="ISO-8601 UTC override (tests/fixtures)")
    ingest = sub.add_parser("ingest", help="Append a collect.py run record to the archive")
    ingest.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    ingest.add_argument("--run-record", type=Path, required=True)
    ingest.add_argument("--seed-scan-windows", type=Path, default=Path("data/seed_scan_windows.json"),
                        help="Used only when no index exists yet")
    ingest.add_argument("--result-out", type=Path, default=None)
    ingest.add_argument("--now", default=None, help="ISO-8601 UTC override (tests/fixtures)")
    verify = sub.add_parser("verify", help="Verify shard bytes/sha256 against the index")
    verify.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    recover = sub.add_parser(
        "recover", help="Clear a shard_lost date from verified evidence only (see docs/HISTORY_ONE_YEAR.md)")
    recover.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    recover.add_argument("--day", required=True, help="UTC date YYYY-MM-DD flagged shard_lost")
    source = recover.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-shard", type=Path, nargs="+",
                        help="Earlier copies of the lost file(s), e.g. from git show; must match the recorded sha256")
    source.add_argument("--rebuild", action="store_true",
                        help="Use an independent, fresh EUMETSAT collection given by --rebuild-record")
    recover.add_argument("--rebuild-record", type=Path, default=None, help="collect.py run record covering the date")
    recover.add_argument("--recorded-index", type=Path, default=None,
                         help="Earlier index.json (git history) holding the recorded entry, when the lost entry has none")
    recover.add_argument("--accept-unanchored", action="store_true",
                         help="Rebuild only: allow a date with no recorded count (audited as anchored=false)")
    recover.add_argument("--now", default=None, help="ISO-8601 UTC override (tests/fixtures)")
    args = parser.parse_args(argv)
    if args.command == "backfill-plan":
        now = parse_utc(args.now) if args.now else datetime.now(timezone.utc)
        today = now.date()
        end = date.fromisoformat(args.end_date.strip()) if args.end_date.strip() else today - timedelta(days=1)
        for day in backfill_plan(args.archive_dir, end_date=end, days=args.days, today=today):
            print(day)
        return 0
    if args.command == "backfill":
        now = parse_utc(args.now) if args.now else datetime.now(timezone.utc)
        result = backfill_day(args.archive_dir, args.day, now=now, rebuild_record=args.rebuild_record)
        if args.result_out:
            args.result_out.parent.mkdir(parents=True, exist_ok=True)
            args.result_out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        row = result["row"]
        print(f"Backfilled {result['date']}: coverage {row['coverage']} ({row['scanned_seconds']} s searched), "
              f"count {row['count']}, {len(result['unscanned_ranges'])} unsearched gap(s) left unknown, "
              f"{result['outside_day']} flash(es) outside the date dropped, "
              f"{result['unexamined_time_flashes']} flash(es) kept in unsearched slot(s) "
              f"{result['unexamined_time_slots_utc']}, {result['cross_product_duplicates']} cross-product "
              "duplicate(s) removed; verify ok.")
        if result["integrity"]["state"] != "ok":
            print(result["integrity"]["alert"], file=sys.stderr)
        return 0
    if args.command == "verify":
        checked = verify_archive(args.archive_dir)
        print(json.dumps(checked))
        if checked["integrity"] == "shard_lost":
            print(integrity_alert(checked["lost_days"]), file=sys.stderr)
        return 0
    if args.command == "recover":
        if args.rebuild and not args.rebuild_record:
            parser.error("--rebuild needs --rebuild-record")
        now = parse_utc(args.now) if args.now else datetime.now(timezone.utc)
        result = recover_day(args.archive_dir, args.day, now=now, from_shards=args.from_shard,
                             rebuild_record=args.rebuild_record if args.rebuild else None,
                             recorded_index=args.recorded_index, accept_unanchored=args.accept_unanchored)
        print(json.dumps({"recovered": result["audit"], "verify": result["verify"]}, indent=1))
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
    if result["integrity"]["state"] != "ok":
        print(result["integrity"]["alert"], file=sys.stderr)
    if result["grid"]["error"]:
        print(f"GRID WARNING (archive unaffected; grid days stay stale/unknown): {result['grid']['error']}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(f"ARCHIVE FAILURE: {error}", file=sys.stderr)
        raise SystemExit(2)

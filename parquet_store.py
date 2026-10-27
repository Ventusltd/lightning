#!/usr/bin/env python3
"""Parquet + DuckDB store of the genuine UK lightning archive (data plane only).

Built FROM the validated JSON archive written by history.py (index v2 + day
shards), never from anything else: every flash row is a record that history.py
accepted, re-validated here, and the per-day counts must equal the index.
Nothing is synthetic, interpolated or back-filled.

Layout (under --out, default data/parquet):

  flashes/year=YYYY/month=MM/part-0.parquet   one row per archived optical flash
  coverage/days.parquet                       one row per UTC date of the index
  coverage/scan_windows.parquet               merged successful search windows
  coverage/slots/year=YYYY/month=MM/part-0.parquet
                                              one row per 10-minute LI slot
  rollups/...                                 DuckDB rollups (see rollups.py, sql/)
  manifest.json                               every file: bytes, sha256, rows,
                                              min/max time, schema version

Coverage rule (inherited from index v2): never searched is NULL, never zero.
A count of 0 exists only for time that was searched.

Commands:
  python parquet_store.py build  [--archive-dir data/history] [--out data/parquet] [--run-id ID]
  python parquet_store.py verify [--archive-dir data/history] [--out data/parquet]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import history
from collect import PROVIDER, SLOT_SECONDS, UK_BOUNDS, inside_uk_view, parse_utc, product_slot

ROOT = Path(__file__).resolve().parent
SCHEMA_FLASHES = "uk-lightning-parquet-flashes.v1"
SCHEMA_DAYS = "uk-lightning-parquet-coverage-days.v1"
SCHEMA_WINDOWS = "uk-lightning-parquet-scan-windows.v1"
SCHEMA_SLOTS = "uk-lightning-parquet-coverage-slots.v1"
SCHEMA_MANIFEST = "uk-lightning-parquet-manifest.v1"
ROW_GROUP_ROWS = 32_768          # ~0.5-1 MB compressed per row group: one HTTP range read
DATA_PAGE_BYTES = 64 * 1024
ZSTD_LEVEL = 9
SLOT_PHASE = timedelta(seconds=history.SLOT_PHASE_SECONDS)
SLOT = timedelta(seconds=SLOT_SECONDS)
# A genuine flash may be timed slightly outside its product's nominal sensing
# slot (observed: 0.361 s before the start of slot _OPE_20261023195009_).
SLOT_TOLERANCE = timedelta(seconds=60)
GENUINE_ID = re.compile(r"^W_XX-EUMETSAT-Darmstadt,IMG\+SAT,MTI\d\+LI-2-LFL--.*_OPE_\d{14}_\d{14}_.*:\d+$")
SOURCE_PRODUCT = "MTG LI L2-LFL"
OBSERVATION_KIND = "optical_lightning_flash"
OBSERVATION_NOTE = "satellite optical lightning flashes; not ground strikes, not damage, not causality"
SYNTHETIC_FLAG = "SYNTHETIC-NOT-PUBLISHED"

FLASH_SCHEMA = pa.schema([
    pa.field("id", pa.string(), nullable=False),
    pa.field("time_utc", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("lon", pa.float64(), nullable=False),
    pa.field("lat", pa.float64(), nullable=False),
    pa.field("source_product", pa.string(), nullable=False),
    pa.field("observation_kind", pa.string(), nullable=False),
    pa.field("ingest_run_id", pa.string(), nullable=False),
])
DAYS_SCHEMA = pa.schema([
    pa.field("date", pa.date32(), nullable=False),
    pa.field("coverage", pa.string(), nullable=False),  # none | partial | full | shard_lost
    pa.field("count_state", pa.string(), nullable=False),  # observed | UNKNOWN
    pa.field("hours_observed", pa.float64()),       # scanned_seconds / 3600; NULL iff UNKNOWN
    pa.field("hours_expected", pa.float64(), nullable=False),  # 24
    pa.field("scanned_seconds", pa.int32()),        # NULL iff coverage == none
    pa.field("flash_count", pa.int64()),            # NULL iff coverage == none; 0 only when searched
    pa.field("slots_searched", pa.int16()),         # NULL iff coverage == none
    pa.field("sealed", pa.bool_(), nullable=False),
    pa.field("first_observation_utc", pa.timestamp("us", tz="UTC")),
    pa.field("last_observation_utc", pa.timestamp("us", tz="UTC")),
])
WINDOWS_SCHEMA = pa.schema([
    pa.field("start_utc", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("end_utc", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("successful_runs", pa.int32(), nullable=False),
])
SLOTS_SCHEMA = pa.schema([
    pa.field("slot_start_utc", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("date", pa.date32(), nullable=False),      # UTC date of slot start
    pa.field("coverage", pa.string(), nullable=False),  # none | partial | full
    pa.field("scanned_seconds", pa.int16()),            # NULL iff never searched
    pa.field("basis", pa.string()),                     # scan_window | flash_proven | NULL
    pa.field("flash_count", pa.int32()),                # NULL iff never searched
])


# ---------------------------------------------------------------- helpers
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return history.format_utc(value) if value.microsecond % 1000 == 0 else \
        value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parquet_bytes(table: pa.Table, *, dictionary: list[str] | None = None) -> bytes:
    """Deterministic Parquet bytes: same table + same library versions -> same bytes."""
    buffer = io.BytesIO()
    # Monotonic time/integer columns delta-encode to almost nothing; strings use
    # dictionaries where repeated (or plain + zstd for unique ids).
    delta = [f.name for f in table.schema
             if pa.types.is_timestamp(f.type) or pa.types.is_date(f.type) or pa.types.is_integer(f.type)]
    strings = [f.name for f in table.schema if pa.types.is_string(f.type) or pa.types.is_boolean(f.type)]
    pq.write_table(
        table, buffer, compression="zstd", compression_level=ZSTD_LEVEL, row_group_size=ROW_GROUP_ROWS,
        data_page_size=DATA_PAGE_BYTES,
        use_dictionary=dictionary if dictionary is not None else [n for n in strings],
        column_encoding={name: "DELTA_BINARY_PACKED" for name in delta} or None,
        write_statistics=True, write_page_index=True, store_schema=True,
    )
    return buffer.getvalue()


def write_if_changed(path: Path, blob: bytes) -> bool:
    if path.exists() and path.read_bytes() == blob:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(blob)
    os.replace(temporary, path)
    return True


def with_metadata(schema: pa.Schema, name: str, extra: dict | None = None) -> pa.Schema:
    meta = {"schema": name, "observation": OBSERVATION_NOTE, "source": PROVIDER}
    meta.update(extra or {})
    return schema.with_metadata({k: json.dumps(v) if not isinstance(v, str) else v for k, v in meta.items()})


def slot_start_of(moment: datetime) -> datetime:
    """Start of the 10-minute LI slot (phase HH:M0:09) that contains ``moment``."""
    day0 = datetime.combine(moment.date(), time.min, tzinfo=timezone.utc) + SLOT_PHASE
    k = math.floor((moment - day0).total_seconds() / SLOT_SECONDS)
    return day0 + k * SLOT


def flash_slot(identifier: str, moment: datetime) -> datetime:
    """The examined slot a flash proves: its product's slot if the id names one, else by time."""
    product = product_slot(identifier.rsplit(":", 1)[0])
    if product and product[0] - SLOT_TOLERANCE <= moment < product[1] + SLOT_TOLERANCE:
        return slot_start_of(product[0])
    return slot_start_of(moment)


def is_synthetic(path: Path) -> bool:
    manifest = Path(path) / "manifest.json"
    if not manifest.exists():
        return False
    return json.loads(manifest.read_text(encoding="utf-8")).get("synthetic") == SYNTHETIC_FLAG


# ---------------------------------------------------------------- read the archive
class ArchiveError(ValueError):
    """The JSON archive is missing, inconsistent or invalid; nothing is written."""


def read_archive(archive_dir: Path) -> tuple[dict, list[dict]]:
    """Return (index, records) after verifying every shard and every record.

    records: dicts {id, time (datetime UTC), lon, lat, day (iso date)} sorted by (time, id).
    """
    archive_dir = Path(archive_dir)
    index_path = archive_dir / "index.json"
    if not index_path.exists():
        raise ArchiveError(f"{index_path}: no archive index (the archive step has not run yet)")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("schema") != history.SCHEMA_INDEX:
        raise ArchiveError(f"{index_path}: expected {history.SCHEMA_INDEX}, got {index.get('schema')}")
    for row in index["days"]:  # bytes + sha256 of every listed shard (history.verify_archive)
        if (row.get("path") or row.get("parts")) and not row.get("shard_lost"):
            try:
                history._verify_unchanged(archive_dir, row)
            except ValueError as error:
                raise ArchiveError(f"{error} (a lost shard must be flagged shard_lost, never read as 0)") from error
    now = parse_utc(index["generated_utc"])
    cutoff = date.fromisoformat(index["cutoff_date_utc"])
    disk, expired = history._discover_shards(archive_dir, cutoff, now.date())
    if expired:
        raise ArchiveError(f"shards older than the cutoff remain on disk: {expired[:3]}")
    listed: set[str] = set()
    lost_paths: set[str] = set()
    records: list[dict] = []
    seen: set[str] = set()
    for row in index["days"]:
        if row.get("shard_lost"):
            # A day whose shard is known to be lost reads UNKNOWN; its records are not
            # reconstructed and its count is never reported (not even the index's).
            lost_paths.update([row["path"]] if row.get("path") else [p["path"] for p in row.get("parts") or []])
            continue
        paths = [row["path"]] if row.get("path") else [p["path"] for p in row.get("parts") or []]
        listed.update(paths)
        day_records = 0
        for rel in paths:
            for feature in history._read_features(archive_dir / rel, row["date"]):
                valid = history.validate_feature(feature, now, cutoff)
                if valid is None or valid[0] != row["date"]:
                    raise ArchiveError(f"{rel}: record {feature.get('id')!r} fails validation")
                identifier = feature["id"]
                if identifier in seen:
                    raise ArchiveError(f"{rel}: duplicate id {identifier}")
                seen.add(identifier)
                lon, lat = (float(c) for c in feature["geometry"]["coordinates"][:2])
                records.append({"id": identifier, "time": parse_utc(feature["properties"]["time_utc"]),
                                "lon": lon, "lat": lat, "day": row["date"]})
                day_records += 1
        expected = row["count"] or 0
        if day_records != expected:
            raise ArchiveError(f"{row['date']}: {day_records} records in shards but index count {row['count']}")
    on_disk = set()
    for entry in disk.values():
        if entry["file"]:
            on_disk.add(entry["file"].relative_to(archive_dir).as_posix())
        on_disk.update(p.relative_to(archive_dir).as_posix() for p in entry["parts"].values())
    on_disk -= lost_paths  # a lost day's leftovers are never read
    if on_disk != listed:
        raise ArchiveError(f"shards on disk and in the index differ: {sorted(on_disk ^ listed)[:5]}")
    lost = sum((r["count"] or 0) for r in index["days"] if r.get("shard_lost"))
    if len(records) + lost != index["archived_observations"]:
        raise ArchiveError("archived_observations disagrees with the shards")
    records.sort(key=lambda r: (r["time"], r["id"]))
    return index, records


# ---------------------------------------------------------------- build tables
def previous_run_ids(out_dir: Path) -> dict[str, str]:
    """id -> ingest_run_id already recorded in the store (carried forward, never rewritten)."""
    carried: dict[str, str] = {}
    for file in sorted((Path(out_dir) / "flashes").glob("year=*/month=*/*.parquet")):
        table = pq.read_table(file, columns=["id", "ingest_run_id"])
        for identifier, run_id in zip(table.column("id").to_pylist(), table.column("ingest_run_id").to_pylist()):
            carried[identifier] = run_id
    return carried


def flash_tables(records: list[dict], run_id: str, carried: dict[str, str]) -> dict[tuple[int, int], pa.Table]:
    by_month: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for record in records:
        by_month[(record["time"].year, record["time"].month)].append(record)
    tables = {}
    schema = with_metadata(FLASH_SCHEMA, SCHEMA_FLASHES)
    for key, rows in sorted(by_month.items()):
        tables[key] = pa.table({
            "id": [r["id"] for r in rows],
            "time_utc": [r["time"] for r in rows],
            "lon": [r["lon"] for r in rows],
            "lat": [r["lat"] for r in rows],
            "source_product": [SOURCE_PRODUCT] * len(rows),
            "observation_kind": [OBSERVATION_KIND] * len(rows),
            "ingest_run_id": [carried.get(r["id"], run_id) for r in rows],
        }, schema=schema)
    return tables


def window_overlap(windows: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> float:
    total = 0.0
    for w_start, w_end in windows:
        if w_end <= start:
            continue
        if w_start >= end:
            break
        total += (min(w_end, end) - max(w_start, start)).total_seconds()
    return total


def coverage_tables(index: dict, records: list[dict]) -> tuple[pa.Table, pa.Table, dict[tuple[int, int], pa.Table]]:
    windows = sorted((parse_utc(w["start_utc"]), parse_utc(w["end_utc"]), int(w["successful_runs"]))
                     for w in index["scan_windows"])
    spans = [(s, e) for s, e, _ in windows]
    windows_table = pa.table({
        "start_utc": [s for s, _, _ in windows], "end_utc": [e for _, e, _ in windows],
        "successful_runs": [n for _, _, n in windows],
    }, schema=with_metadata(WINDOWS_SCHEMA, SCHEMA_WINDOWS, {"note": history.WINDOW_NOTE}))

    flashes_per_slot: dict[datetime, int] = defaultdict(int)
    for record in records:
        flashes_per_slot[flash_slot(record["id"], record["time"])] += 1

    cutoff = date.fromisoformat(index["cutoff_date_utc"])
    today = parse_utc(index["generated_utc"]).date()
    first = slot_start_of(datetime.combine(cutoff, time.min, tzinfo=timezone.utc))
    last = slot_start_of(datetime.combine(today, time.max, tzinfo=timezone.utc))
    slot_rows: dict[tuple[int, int], dict[str, list]] = defaultdict(lambda: defaultdict(list))
    searched_slots_per_day: dict[date, int] = defaultdict(int)
    stray = [s for s in flashes_per_slot if not first <= s <= last]
    if stray:
        raise ArchiveError(f"flashes fall outside the slot grid: {stray[:3]}")
    lost_days = {date.fromisoformat(r["date"]) for r in index["days"] if r.get("shard_lost")}
    slot = first
    while slot <= last:
        if slot.date() in lost_days:
            cols = slot_rows[(slot.year, slot.month)]
            for column, value in (("slot_start_utc", slot), ("date", slot.date()), ("coverage", "shard_lost"),
                                  ("scanned_seconds", None), ("basis", None), ("flash_count", None)):
                cols[column].append(value)
            slot += SLOT
            continue
        scanned = int(round(window_overlap(spans, slot, slot + SLOT)))
        flashes = flashes_per_slot.get(slot, 0)
        basis = "scan_window" if scanned > 0 else None
        if scanned <= 0 and flashes:
            scanned, basis = SLOT_SECONDS, "flash_proven"  # the product was examined (lower bound)
        searched = scanned > 0
        if searched:
            searched_slots_per_day[slot.date()] += 1
        cols = slot_rows[(slot.year, slot.month)]
        cols["slot_start_utc"].append(slot)
        cols["date"].append(slot.date())
        cols["coverage"].append("none" if not searched else ("full" if scanned >= SLOT_SECONDS else "partial"))
        cols["scanned_seconds"].append(scanned if searched else None)
        cols["basis"].append(basis)
        cols["flash_count"].append(flashes if searched else None)
        slot += SLOT
    slot_schema = with_metadata(SLOTS_SCHEMA, SCHEMA_SLOTS, {
        "slot_rule": "10-minute LI slots start at HH:M0:09 UTC; a flash is counted in its product's slot",
        "null_rule": "scanned_seconds and flash_count are NULL for never-searched slots, never 0"})
    slot_tables = {key: pa.table(dict(cols), schema=slot_schema) for key, cols in sorted(slot_rows.items())}

    days = defaultdict(list)
    for row in index["days"]:
        day = date.fromisoformat(row["date"])
        if row.get("shard_lost"):
            days["date"].append(day)
            days["coverage"].append("shard_lost")
            days["count_state"].append("UNKNOWN")
            days["hours_observed"].append(None)
            days["hours_expected"].append(24.0)
            for column in ("scanned_seconds", "flash_count", "slots_searched",
                           "first_observation_utc", "last_observation_utc"):
                days[column].append(None)
            days["sealed"].append(bool(row["sealed"]))
            continue
        none = row["coverage"] == "none"
        if none != (row["count"] is None):
            raise ArchiveError(f"{row['date']}: index count/coverage disagree")
        if not none and row["scanned_seconds"] <= 0:
            raise ArchiveError(f"{row['date']}: searched day with no searched seconds")
        days["date"].append(day)
        days["coverage"].append(row["coverage"])
        days["count_state"].append("UNKNOWN" if none else "observed")
        days["hours_observed"].append(None if none else round(row["scanned_seconds"] / 3600, 4))
        days["hours_expected"].append(24.0)
        days["scanned_seconds"].append(None if none else int(row["scanned_seconds"]))
        days["flash_count"].append(None if none else int(row["count"]))
        days["slots_searched"].append(None if none else searched_slots_per_day.get(day, 0))
        days["sealed"].append(bool(row["sealed"]))
        days["first_observation_utc"].append(parse_utc(row["first_observation_utc"]) if row.get("first_observation_utc") else None)
        days["last_observation_utc"].append(parse_utc(row["last_observation_utc"]) if row.get("last_observation_utc") else None)
    days_table = pa.table(dict(days), schema=with_metadata(DAYS_SCHEMA, SCHEMA_DAYS, {
        "null_rule": ("scanned_seconds, hours_observed, flash_count and slots_searched are NULL iff "
                      "coverage is none or shard_lost (count_state UNKNOWN); never 0"),
        "seed_note": history.SEED_NOTE}))
    return days_table, windows_table, slot_tables


# ---------------------------------------------------------------- manifest
def describe_parquet(path: Path, rel: str) -> dict:
    meta = pq.ParquetFile(path)
    schema_meta = {k.decode(): v.decode() for k, v in (meta.schema_arrow.metadata or {}).items()}
    entry = {
        "path": rel, "bytes": path.stat().st_size, "sha256": sha256_file(path),
        "rows": meta.metadata.num_rows, "row_groups": meta.metadata.num_row_groups,
        "schema_version": schema_meta.get("schema"), "min_time_utc": None, "max_time_utc": None,
    }
    time_column = next((c for c in ("time_utc", "slot_start_utc", "start_utc", "hour_utc", "date")
                        if c in meta.schema_arrow.names), None)
    if time_column and meta.metadata.num_rows:
        column = pq.read_table(path, columns=[time_column]).column(time_column)
        low, high = pa.compute.min(column).as_py(), pa.compute.max(column).as_py()
        if time_column == "start_utc" and "end_utc" in meta.schema_arrow.names:
            high = pa.compute.max(pq.read_table(path, columns=["end_utc"]).column("end_utc")).as_py()
        as_text = (lambda v: v.isoformat()) if isinstance(low, date) and not isinstance(low, datetime) else utc_iso
        entry["min_time_utc"], entry["max_time_utc"] = as_text(low), as_text(high)
        entry["time_column"] = time_column
    return entry


def build_manifest(out_dir: Path, index: dict, substations_pin: dict | None, *, synthetic: bool) -> dict:
    out_dir = Path(out_dir)
    files = []
    for path in sorted(p for p in out_dir.rglob("*") if p.is_file() and p.name != "manifest.json"):
        rel = path.relative_to(out_dir).as_posix()
        if path.suffix == ".parquet":
            files.append(describe_parquet(path, rel))
        elif path.suffix == ".json":
            obj = json.loads(path.read_text(encoding="utf-8"))
            files.append({"path": rel, "bytes": path.stat().st_size, "sha256": sha256_file(path),
                          "rows": None, "row_groups": None, "schema_version": obj.get("schema"),
                          "min_time_utc": None, "max_time_utc": None})
        else:
            raise ArchiveError(f"unexpected file in the store: {rel}")
    flashes = [f for f in files if f["path"].startswith("flashes/")]
    manifest = {
        "schema": SCHEMA_MANIFEST,
        "what_this_is": ("Parquet mirror of the one-year UTC lightning archive (data/history), plus coverage and "
                         "DuckDB rollups. Every file is listed with bytes, sha256, rows, time range and schema."),
        "observation": OBSERVATION_NOTE,
        "source": PROVIDER,
        "derived_from": {
            "archive_index_schema": index["schema"],
            "archived_observations": index["archived_observations"],
            "cutoff_date_utc": index["cutoff_date_utc"],
            "coverage_start_utc": index.get("coverage_start_utc"),
            "last_successful_scan_end_utc": index.get("last_successful_scan_end_utc"),
            "first_observation_utc": index.get("first_observation_utc"),
            "last_observation_utc": index.get("last_observation_utc"),
            "substations": substations_pin,
        },
        "writer": {"pyarrow": pa.__version__, "compression": f"zstd level {ZSTD_LEVEL}",
                   "row_group_rows": ROW_GROUP_ROWS, "data_page_bytes": DATA_PAGE_BYTES,
                   "partitioning": "flashes and slots: year=YYYY/month=MM (UTC)"},
        "totals": {"files": len(files), "bytes": sum(f["bytes"] for f in files),
                   "flash_rows": sum(f["rows"] for f in flashes), "flash_files": len(flashes),
                   "flash_bytes": sum(f["bytes"] for f in flashes)},
        "files": files,
    }
    if synthetic:
        manifest["synthetic"] = SYNTHETIC_FLAG
    return manifest


# ---------------------------------------------------------------- build
def build(archive_dir: Path, out_dir: Path, *, run_id: str, synthetic: bool = False,
          substations_path: Path | None = None, rollups: bool = True) -> dict:
    out_dir = Path(out_dir)
    if not synthetic and is_synthetic(out_dir):
        raise ArchiveError(f"{out_dir} holds a synthetic store; refusing to mix")
    index, records = read_archive(archive_dir)
    if not synthetic:
        fakes = [r["id"] for r in records if not GENUINE_ID.match(r["id"])]
        if fakes:
            raise ArchiveError(f"{len(fakes)} records lack a genuine EUMETSAT product id (e.g. {fakes[0]!r}); "
                               "only genuine observations may be published")
    carried = previous_run_ids(out_dir)
    changed = []
    wanted: set[Path] = set()

    def put(rel: str, blob: bytes):
        target = out_dir / rel
        wanted.add(target)
        if write_if_changed(target, blob):
            changed.append(rel)

    for (year, month), table in flash_tables(records, run_id, carried).items():
        put(f"flashes/year={year:04d}/month={month:02d}/part-0.parquet",
            parquet_bytes(table, dictionary=["source_product", "observation_kind", "ingest_run_id"]))
    days, windows, slots = coverage_tables(index, records)
    put("coverage/days.parquet", parquet_bytes(days))
    put("coverage/scan_windows.parquet", parquet_bytes(windows))
    for (year, month), table in slots.items():
        put(f"coverage/slots/year={year:04d}/month={month:02d}/part-0.parquet", parquet_bytes(table))

    substations_pin = None
    if rollups:
        import rollups as rollup_module
        produced, substations_pin = rollup_module.build_rollups(out_dir, substations_path=substations_path,
                                                                synthetic=synthetic, index=index)
        for rel, blob in produced.items():
            put(rel, blob)
    # Remove files no longer produced (expired months), never anything outside the store.
    for path in sorted(out_dir.rglob("*")):
        if path.is_file() and path.name != "manifest.json" and path not in wanted:
            path.unlink()
            changed.append(path.relative_to(out_dir).as_posix() + " (removed)")
    for directory in sorted((p for p in out_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    manifest = build_manifest(out_dir, index, substations_pin, synthetic=synthetic)
    if write_if_changed(out_dir / "manifest.json", (json.dumps(manifest, indent=1) + "\n").encode("utf-8")):
        changed.append("manifest.json")
    return {"changed": changed, "manifest": manifest, "records": len(records)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    b.add_argument("--out", type=Path, default=Path("data/parquet"))
    b.add_argument("--run-id", default=os.environ.get("LIGHTNING_RUN_ID") or "local",
                   help="recorded as ingest_run_id for flashes not yet in the store")
    b.add_argument("--substations", type=Path, default=None)
    b.add_argument("--if-archive", action="store_true",
                   help="exit 0 without writing when no archive index exists yet")
    v = sub.add_parser("verify")
    v.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    v.add_argument("--out", type=Path, default=Path("data/parquet"))
    v.add_argument("--substations", type=Path, default=None)
    v.add_argument("--allow-synthetic", action="store_true", help="tests only: accept a synthetic temp store")
    args = parser.parse_args(argv)
    if args.command == "build":
        if args.if_archive and not (args.archive_dir / "index.json").exists():
            print("no archive index yet; nothing to build")
            return 0
        result = build(args.archive_dir, args.out, run_id=args.run_id, substations_path=args.substations)
        totals = result["manifest"]["totals"]
        print(f"Parquet store: {result['records']} flashes, {totals['files']} files, {totals['bytes']} bytes; "
              f"changed: {result['changed'] or 'nothing'}")
        return 0
    import verify_parquet
    report = verify_parquet.verify(args.archive_dir, args.out, substations_path=args.substations,
                                   allow_synthetic=args.allow_synthetic)
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(f"PARQUET FAILURE: {error}", file=sys.stderr)
        raise SystemExit(2)

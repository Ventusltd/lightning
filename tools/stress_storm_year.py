#!/usr/bin/env python3
"""SYNTHETIC storm-year stress test: JSON shards vs Parquet, and DuckDB query times.

*** SYNTHETIC. NEVER PUBLISHED. *** Every flash this tool makes is invented
(ids start with "SYNTHETIC-"), lives only in a fresh temporary directory, and
the resulting store is stamped SYNTHETIC-NOT-PUBLISHED so verify_parquet.py
refuses it outside a test. The tool refuses to write anywhere inside the
repository. It measures storage and query cost; it says nothing about real
lightning.

Model: a year of storm days weighted towards summer; each day has 1-4 cells
moving north-east across GB at 30-60 km/h for 2-8 h with ~15 km spread.
The whole year is recorded as searched (one scan window), the best case for
coverage, so every day is 'full'.

Usage: python tools/stress_storm_year.py --flashes 1000000 [--keep] [--json-out report.json]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import sys
import tempfile
import time as clock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history  # noqa: E402
import parquet_store  # noqa: E402
from collect import build_feature  # noqa: E402

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
SLOT = timedelta(seconds=600)


def product_name(slot_start: datetime) -> str:
    end = slot_start + SLOT
    published = end + timedelta(seconds=15)
    slot_no = (slot_start.hour * 6 + slot_start.minute // 10) % 144
    return ("SYNTHETIC-W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_"
            f"{published:%Y%m%d%H%M%S}_L2PF_OPE_{slot_start:%Y%m%d%H%M%S}_{end:%Y%m%d%H%M%S}_N__O_{slot_no:04d}_0000")


def storm_year(total: int, seed: int = 20261026):
    """Yield synthetic features, month by month (memory-bounded)."""
    rng = random.Random(seed)
    first = (NOW - timedelta(days=364)).date()
    days = [first + timedelta(days=i) for i in range(365)]
    weights = [0.15 + math.exp(-((d.timetuple().tm_yday - 196) / 45.0) ** 2) for d in days]
    storm_days = sorted(set(rng.choices(days, weights=weights, k=110)))
    shares = [rng.paretovariate(1.6) for _ in storm_days]
    scale = total / sum(shares)
    counts = [int(s * scale) for s in shares]
    counts[-1] += total - sum(counts)
    by_month: dict[tuple[int, int], list] = {}
    for day, n in zip(storm_days, counts):
        by_month.setdefault((day.year, day.month), []).append((day, n))
    for _, items in sorted(by_month.items()):
        features = []
        for day, n in items:
            cells = rng.randint(1, 4)
            per_cell = [n // cells] * cells
            per_cell[-1] += n - sum(per_cell)
            for m in per_cell:
                start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=rng.uniform(9, 15))
                hours = rng.uniform(2, 8)
                lon0, lat0 = rng.uniform(-5.5, 0.5), rng.uniform(50.3, 56.5)
                speed = rng.uniform(30, 60) / 111.0  # degrees of latitude per hour (approx)
                for _ in range(m):
                    t = start + timedelta(seconds=rng.uniform(0, hours * 3600))
                    if t >= NOW:
                        t = NOW - timedelta(seconds=rng.uniform(1, 3600))
                    dt = (t - start).total_seconds() / 3600
                    lat = lat0 + speed * dt * 0.7 + rng.gauss(0, 0.13)
                    lon = lon0 + speed * dt * 0.7 / math.cos(math.radians(lat0)) + rng.gauss(0, 0.2)
                    lon, lat = min(max(lon, -10.99), 2.99), min(max(lat, 49.51), 61.49)
                    features.append((t, lon, lat))
        features.sort()
        out, last_slot, k = [], None, 0
        for t, lon, lat in features:
            slot = parquet_store.slot_start_of(t)
            k = k + 1 if slot == last_slot else 0
            last_slot = slot
            out.append(build_feature(lon, lat, t, product_name(slot), k))
        yield out


def measure(total: int, workdir: Path) -> dict:
    archive, store = workdir / "history", workdir / "parquet"
    window = history.successful_scan((NOW - timedelta(days=366)).isoformat(), NOW.isoformat(), 52_000)
    t0 = clock.perf_counter()
    made = 0
    for features in storm_year(total):
        made += len(features)
        history.archive_observations(archive, features, now=NOW, scanned_window=window)
    t_json = clock.perf_counter() - t0
    index = json.loads((archive / "index.json").read_text())
    shard_bytes = sum(r["bytes"] or 0 for r in index["days"])
    t0 = clock.perf_counter()
    result = parquet_store.build(archive, store, run_id="SYNTHETIC-stress", synthetic=True)
    t_parquet = clock.perf_counter() - t0
    manifest = result["manifest"]
    flash_files = [f for f in manifest["files"] if f["path"].startswith("flashes/")]
    flash_bytes = sum(f["bytes"] for f in flash_files)

    import pyarrow.parquet as pq
    import rollups
    con, _ = rollups.connect(store)
    busiest = max((r for r in index["days"] if r["count"]), key=lambda r: r["count"])
    timings = {}
    queries = {
        "count_all": "SELECT count(*) FROM flashes",
        "daily_rollup": "SELECT count(*), sum(flash_count) FROM daily_optical_flashes",
        "hourly_rollup": "SELECT count(*), sum(flash_count) FROM hourly_optical_flashes",
        "one_day_points": (f"SELECT count(*) FROM flashes WHERE time_utc >= TIMESTAMPTZ '{busiest['date']} 00:00:00+00' "
                           f"AND time_utc < TIMESTAMPTZ '{busiest['date']} 00:00:00+00' + INTERVAL 1 DAY"),
        "substation_proximity": "SELECT sum(optical_flash_proximity_10km) FROM substation_optical_flash_proximity",
        "storm_episodes": "SELECT count(*), max(flash_count) FROM storm_episodes",
    }
    for name, sql in queries.items():
        t0 = clock.perf_counter()
        value = con.execute(sql).fetchall()
        timings[name] = {"seconds": round(clock.perf_counter() - t0, 3), "result": [list(map(str, r)) for r in value]}
    con.close()
    groups, touched = [], 0
    day_start = datetime.fromisoformat(busiest["date"]).replace(tzinfo=timezone.utc)
    for f in flash_files:
        meta = pq.ParquetFile(store / f["path"]).metadata
        for i in range(meta.num_row_groups):
            rg = meta.row_group(i)
            groups.append(rg.total_compressed_size if hasattr(rg, "total_compressed_size") else
                          sum(rg.column(c).total_compressed_size for c in range(rg.num_columns)))
            stats = rg.column(1).statistics
            if stats and stats.min < day_start + timedelta(days=1) and stats.max >= day_start:
                touched += 1
    import verify_parquet
    t0 = clock.perf_counter()
    verdict = verify_parquet.verify(archive, store, allow_synthetic=True)["outcome"]
    t_verify = clock.perf_counter() - t0
    return {
        "label": "SYNTHETIC storm year - generated in a temp dir, never published",
        "flashes": made, "storm_days": sum(1 for r in index["days"] if r["count"]),
        "busiest_day": {"date": busiest["date"], "flashes": busiest["count"], "split_into_hourly_parts": bool(busiest.get("parts"))},
        "json_shard_bytes": shard_bytes, "json_bytes_per_flash": round(shard_bytes / made, 1),
        "parquet_flash_bytes": flash_bytes, "parquet_bytes_per_flash": round(flash_bytes / made, 2),
        "parquet_store_bytes_all_tables": manifest["totals"]["bytes"],
        "ratio_json_to_parquet": round(shard_bytes / flash_bytes, 1),
        "flash_files": len(flash_files), "row_groups": len(groups),
        "row_group_compressed_bytes": {"min": min(groups), "max": max(groups), "mean": round(sum(groups) / len(groups))},
        "row_groups_touched_by_busiest_day_query": touched,
        "seconds": {"json_archive_build": round(t_json, 1), "parquet_and_rollups_build": round(t_parquet, 1),
                    "verify_fail_closed": round(t_verify, 1)},
        "verify_outcome": verdict,
        "duckdb_queries": timings,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flashes", type=int, default=1_000_000)
    parser.add_argument("--keep", action="store_true", help="keep the temp dir (printed) for inspection")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)
    workdir = Path(tempfile.mkdtemp(prefix="SYNTHETIC-lightning-stress-"))
    if ROOT.resolve() in workdir.resolve().parents:
        raise SystemExit("refusing to write synthetic data inside the repository")
    try:
        report = measure(args.flashes, workdir)
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
    report["temp_dir"] = str(workdir) + ("" if args.keep else " (deleted)")
    text = json.dumps(report, indent=1)
    if args.json_out:
        args.json_out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

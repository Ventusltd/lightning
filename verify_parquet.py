#!/usr/bin/env python3
"""Fail-closed verification of the lightning Parquet store.

Re-reads the JSON archive and the Parquet and recomputes every published
number independently in Python (not with the SQL that built it): a builder and
a product that drifted together must still fail. Raises VerifyError on:

- a file missing, unlisted, or differing from the manifest (bytes, sha256,
  rows, row groups, schema version, min/max time);
- a flash outside the UK box, non-finite, duplicated, in the wrong partition,
  without a genuine EUMETSAT product id, or disagreeing with the archive;
- a count of 0 (or any number) claimed for never-searched or shard_lost time,
  or a NULL count for searched time;
- any rollup or headline number that disagrees with the recomputation;
- a headline over 4096 bytes, or a synthetic store outside a test.

Usage: python verify_parquet.py [--archive-dir data/history] [--out data/parquet]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq

import parquet_store as ps
import rollups
from collect import SLOT_SECONDS, inside_uk_view, parse_utc, product_slot

UNKNOWN_COVERAGE = {"none", "shard_lost"}
EARTH_RADIUS_KM = 6371.0088


class VerifyError(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise VerifyError(message)


def haversine_km(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def overlap_seconds(spans, start, end) -> float:
    return sum((min(e, end) - max(s, start)).total_seconds() for s, e in spans if s < end and e > start)


def round_half_up(x: float) -> int:  # DuckDB round(): half away from zero (x >= 0 here)
    return int(math.floor(x + 0.5))


def read_rows(store: Path, pattern: str) -> list[dict]:
    rows = []
    for file in sorted(store.glob(pattern)):
        rows.extend(pq.read_table(file).to_pylist())
    return rows


def null_rule(row: dict, where: str, count_field: str = "flash_count") -> bool:
    """True when the row is searched. Enforces NULL-for-UNKNOWN and never-0-for-UNKNOWN."""
    unknown = row["coverage"] in UNKNOWN_COVERAGE
    if unknown:
        check(row["count_state"] == "UNKNOWN", f"{where}: unsearched/lost row must be UNKNOWN")
        check(row[count_field] is None, f"{where}: count {row[count_field]!r} claimed for unsearched/lost time")
        check(row["hours_observed"] is None, f"{where}: hours_observed claimed for unsearched/lost time")
        check(row.get("scanned_seconds") is None, f"{where}: scanned_seconds claimed for unsearched/lost time")
    else:
        check(row["count_state"] == "observed", f"{where}: searched row must be observed")
        check(row[count_field] is not None and row[count_field] >= 0, f"{where}: searched row lacks a count")
        check(row["hours_observed"] is not None and row["hours_observed"] > 0, f"{where}: searched row lacks hours")
        check(row["hours_observed"] <= row["hours_expected"] + 1e-9, f"{where}: more hours observed than expected")
    return not unknown


def verify(archive_dir: Path, out_dir: Path, *, substations_path: Path | None = None,
           allow_synthetic: bool = False) -> dict:
    store = Path(out_dir)
    manifest_path = store / "manifest.json"
    check(manifest_path.exists(), f"{manifest_path} missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    check(manifest.get("schema") == ps.SCHEMA_MANIFEST, "manifest schema mismatch")
    synthetic = manifest.get("synthetic") == ps.SYNTHETIC_FLAG
    check(allow_synthetic or not synthetic, "synthetic store: never published")

    # 1. Files: exactly the manifest, byte for byte.
    listed = {f["path"]: f for f in manifest["files"]}
    on_disk = {p.relative_to(store).as_posix() for p in store.rglob("*") if p.is_file() and p.name != "manifest.json"}
    check(on_disk == set(listed), f"files and manifest differ: {sorted(on_disk ^ set(listed))[:5]}")
    for rel, entry in listed.items():
        path = store / rel
        check(path.stat().st_size == entry["bytes"], f"{rel}: bytes differ from the manifest")
        check(ps.sha256_file(path) == entry["sha256"], f"{rel}: sha256 differs from the manifest")
        if path.suffix == ".parquet":
            fresh = ps.describe_parquet(path, rel)
            for key in ("rows", "row_groups", "schema_version", "min_time_utc", "max_time_utc"):
                check(fresh[key] == entry[key], f"{rel}: manifest {key} {entry[key]!r} != file {fresh[key]!r}")
    check(manifest["totals"]["files"] == len(listed), "manifest file total wrong")
    check(manifest["totals"]["bytes"] == sum(f["bytes"] for f in listed.values()), "manifest byte total wrong")

    # 2. Flashes: genuine, in the box, in the right partition, equal to the archive.
    index, records = ps.read_archive(archive_dir)
    flashes = []
    for file in sorted(store.glob("flashes/year=*/month=*/*.parquet")):
        year = int(file.parent.parent.name.split("=")[1])
        month = int(file.parent.name.split("=")[1])
        check(pq.read_schema(file).metadata[b"schema"].decode() == ps.SCHEMA_FLASHES, f"{file}: schema")
        for row in pq.read_table(file).to_pylist():
            check((row["time_utc"].year, row["time_utc"].month) == (year, month), f"{row['id']}: wrong partition")
            flashes.append(row)
    check(len(flashes) == manifest["totals"]["flash_rows"], "flash total differs from the manifest")
    ids = Counter(row["id"] for row in flashes)
    check(not [i for i, n in ids.items() if n > 1], "duplicate flash ids")
    for row in flashes:
        check(math.isfinite(row["lon"]) and math.isfinite(row["lat"]) and inside_uk_view(row["lon"], row["lat"]),
              f"{row['id']}: outside the UK box {ps.UK_BOUNDS}")
        check(row["source_product"] == ps.SOURCE_PRODUCT and row["observation_kind"] == ps.OBSERVATION_KIND,
              f"{row['id']}: not an MTG LI L2-LFL optical flash")
        check(bool(row["ingest_run_id"]), f"{row['id']}: no ingest run id")
        if not synthetic:
            check(bool(ps.GENUINE_ID.match(row["id"])), f"{row['id']}: not a genuine EUMETSAT product id")
            slot = product_slot(row["id"].rsplit(":", 1)[0])
            check(slot is not None and slot[0] - ps.SLOT_TOLERANCE <= row["time_utc"] < slot[1] + ps.SLOT_TOLERANCE,
                  f"{row['id']}: time outside its product's sensing slot")
    by_id = {r["id"]: r for r in records}
    check(set(by_id) == set(ids), f"Parquet and archive ids differ ({len(ids)} vs {len(by_id)})")
    for row in flashes:
        source = by_id[row["id"]]
        check((row["time_utc"], row["lon"], row["lat"]) == (source["time"], source["lon"], source["lat"]),
              f"{row['id']}: differs from its archive record")
    derived = manifest["derived_from"]
    check(derived["archived_observations"] == index["archived_observations"], "manifest/archive totals differ")
    for key in ("cutoff_date_utc", "coverage_start_utc", "last_successful_scan_end_utc"):
        check(derived[key] == index.get(key), f"manifest {key} differs from the archive index")

    spans = sorted((parse_utc(w["start_utc"]), parse_utc(w["end_utc"]), int(w["successful_runs"]))
                   for w in index["scan_windows"])
    stored = [(w["start_utc"], w["end_utc"], w["successful_runs"])
              for w in pq.read_table(store / "coverage/scan_windows.parquet").to_pylist()]
    check(stored == spans, "coverage/scan_windows differs from the archive index windows")
    spans = [(s, e) for s, e, _ in spans]
    lost_days = {date.fromisoformat(r["date"]) for r in index["days"] if r.get("shard_lost")}
    lost = [(datetime.combine(d, time.min, tzinfo=timezone.utc),
             datetime.combine(d, time.min, tzinfo=timezone.utc) + timedelta(days=1)) for d in sorted(lost_days)]

    per_day = Counter(row["time_utc"].date() for row in flashes)
    per_hour = Counter(row["time_utc"].replace(minute=0, second=0, microsecond=0) for row in flashes)

    # 3. Coverage days: one row per index date; NULL for unknown; counts equal.
    days = pq.read_table(store / "coverage/days.parquet").to_pylist()
    index_rows = {date.fromisoformat(r["date"]): r for r in index["days"]}
    check([d["date"] for d in days] == sorted(index_rows), "coverage days differ from the index dates")
    searched_day = {}
    for row in days:
        where = f"coverage/days {row['date']}"
        source = index_rows[row["date"]]
        if source.get("shard_lost"):
            check(row["coverage"] == "shard_lost", f"{where}: lost shard not flagged")
        else:
            check(row["coverage"] == source["coverage"], f"{where}: coverage differs from the index")
        searched = null_rule(row, where)
        searched_day[row["date"]] = searched
        if searched:
            check(row["flash_count"] == source["count"] == per_day.get(row["date"], 0),
                  f"{where}: count {row['flash_count']} vs index {source['count']} vs Parquet {per_day.get(row['date'], 0)}")
            check(row["scanned_seconds"] == source["scanned_seconds"] > 0, f"{where}: scanned seconds differ")
            check(row["hours_observed"] == round(source["scanned_seconds"] / 3600, 4), f"{where}: hours_observed differs")
        else:
            check(per_day.get(row["date"], 0) == 0, f"{where}: flashes exist on an UNKNOWN day")
        check(row["hours_expected"] == 24.0 and row["sealed"] == bool(source["sealed"]), f"{where}: hours_expected/sealed")

    # 4. Slots: NULL rule; every flash is in a searched slot.
    slot_total = 0
    per_slot = Counter(ps.flash_slot(row["id"], row["time_utc"]) for row in flashes)
    slot_rows = read_rows(store, "coverage/slots/year=*/month=*/*.parquet")
    phase = timedelta(seconds=9)
    first_slot = datetime.combine(min(index_rows), time.min, tzinfo=timezone.utc) + phase
    last_day_end = datetime.combine(max(index_rows), time.max, tzinfo=timezone.utc)
    grid = []
    slot = first_slot - timedelta(seconds=SLOT_SECONDS)  # the slot that contains 00:00:00 of the first day
    while slot <= last_day_end:
        grid.append(slot)
        slot += timedelta(seconds=SLOT_SECONDS)
    check([r["slot_start_utc"] for r in slot_rows] == grid, "coverage/slots is not the 10-minute grid of the index")
    searched_slots = Counter()
    for row in slot_rows:
        unknown = row["coverage"] in UNKNOWN_COVERAGE
        check(unknown == (row["scanned_seconds"] is None) == (row["flash_count"] is None),
              f"slot {row['slot_start_utc']}: NULL rule broken")
        start = row["slot_start_utc"]
        if start.date() in lost_days:
            want = ("shard_lost", None, None, None)
        else:
            scanned = round(overlap_seconds(spans, start, start + timedelta(seconds=SLOT_SECONDS)))
            n = per_slot.get(start, 0)
            basis = "scan_window" if scanned > 0 else None
            if scanned <= 0 and n:
                scanned, basis = SLOT_SECONDS, "flash_proven"
            if scanned > 0:
                searched_slots[start.date()] += 1
                want = ("full" if scanned >= SLOT_SECONDS else "partial", scanned, basis, n)
            else:
                want = ("none", None, None, None)
        got = (row["coverage"], row["scanned_seconds"], row["basis"], row["flash_count"])
        check(got == want, f"slot {start}: {got} != recomputed {want}")
        slot_total += row["flash_count"] or 0
    check(slot_total == len(flashes), f"slot counts {slot_total} != flashes {len(flashes)}")
    for row in days:
        if searched_day[row["date"]]:
            check(row["slots_searched"] == searched_slots.get(row["date"], 0), f"coverage/days {row['date']}: slots_searched")

    # 5. Daily and hourly rollups.
    daily = pq.read_table(store / "rollups/daily.parquet").to_pylist()
    check(len(daily) == len(days), "daily rollup rows differ from coverage days")
    for row, cov in zip(daily, days):
        where = f"rollups/daily {row['date']}"
        check(row["date"] == cov["date"] and row["coverage"] == cov["coverage"], f"{where}: coverage differs")
        for key in ("count_state", "hours_observed", "hours_expected", "scanned_seconds", "sealed"):
            check(row[key] == cov[key], f"{where}: {key} {row[key]!r} differs from coverage/days {cov[key]!r}")
        if null_rule(row, where):
            check(row["flash_count"] == per_day.get(row["date"], 0), f"{where}: count differs from the Parquet")
    hourly = read_rows(store, "rollups/hourly/year=*/month=*/*.parquet")
    check(len(hourly) == 24 * len(days), "hourly rollup is not 24 rows per day")
    hourly_total = 0
    hour_slots = defaultdict(set)
    for row in flashes:
        hour_slots[row["time_utc"].replace(minute=0, second=0, microsecond=0)].add(
            math.floor((row["time_utc"].timestamp() - 9) / 600))
    check([h["hour_utc"] for h in hourly] ==
          [datetime.combine(d, time.min, tzinfo=timezone.utc) + timedelta(hours=k) for d in sorted(index_rows)
           for k in range(24)], "rollups/hourly hours differ from the index days")
    for row in hourly:
        where = f"rollups/hourly {row['hour_utc']}"
        hour = row["hour_utc"]
        check(row["date"] == hour.date(), f"{where}: date")
        if hour.date() in lost_days:
            want = ("shard_lost", None, None)
        else:
            window_s = round_half_up(overlap_seconds(spans, hour, hour + timedelta(hours=1)))
            if window_s > 0:
                want_s, basis = min(window_s, 3600), "scan_window"
            elif per_hour.get(hour, 0) > 0:
                want_s, basis = min(600 * len(hour_slots[hour]), 3600), "flash_proven"
            else:
                want_s, basis = None, None
            cov = "none" if want_s is None else ("full" if want_s >= 3600 else "partial")
            want = (cov, want_s, basis)
        got = (row["coverage"], row["scanned_seconds"], row["basis"])
        # +-1 s: DuckDB sums float epochs, Python sums timedeltas; a .5 s tie may round apart.
        check(got[0] == want[0] and got[2] == want[2] and (got[1] is None) == (want[1] is None)
              and (got[1] is None or abs(got[1] - want[1]) <= 1), f"{where}: {got} != recomputed {want}")
        if got[1] is not None:
            check(row["hours_observed"] == round(got[1] / 3600, 4) and row["hours_expected"] == 1.0,
                  f"{where}: hours_observed")
        if null_rule(row, where):
            check(row["flash_count"] == per_hour.get(row["hour_utc"], 0), f"{where}: count differs")
            check(searched_day[row["date"]], f"{where}: searched hour inside an UNKNOWN day")
            hourly_total += row["flash_count"]
        else:
            check(per_hour.get(row["hour_utc"], 0) == 0, f"{where}: flashes in an UNKNOWN hour")
    check(hourly_total == len(flashes), "hourly counts do not add up to the flashes")

    # 6. Optical-flash proximity, recomputed independently.
    table, pin = rollups.load_substations(substations_path)
    sites = table.to_pylist()
    expected = {}
    expected_daily: dict[tuple[str, date], list[int]] = {}
    buckets = defaultdict(list)  # 0.25 deg cells (>= 27 km N-S, >= 13 km E-W at 61.5 N)
    for row in flashes:
        buckets[(math.floor(row["lat"] / 0.25), math.floor(row["lon"] / 0.25))].append(row)
    for site in sites:
        counts = [0, 0, 0]
        cy, cx = math.floor(site["lat"] / 0.25), math.floor(site["lon"] / 0.25)
        nearby = [row for dy in (-1, 0, 1) for dx in (-2, -1, 0, 1, 2) for row in buckets.get((cy + dy, cx + dx), ())]
        for row in nearby:
            distance = haversine_km(site["lon"], site["lat"], row["lon"], row["lat"])
            for i, radius in enumerate((5, 10, 20)):
                counts[i] += distance <= radius
            if distance <= 20:
                per_site_day = expected_daily.setdefault((site["site_code"], row["time_utc"].date()), [0, 0, 0])
                for i, radius in enumerate((5, 10, 20)):
                    per_site_day[i] += distance <= radius
        expected[site["site_code"]] = counts
    hours_observed = round(sum(d["hours_observed"] or 0 for d in days), 4)
    complete = all(d["coverage"] == "full" for d in days)
    nothing_searched = not hours_observed > 0
    state = "UNKNOWN" if nothing_searched else ("complete" if complete else "lower_bound_partial_coverage")
    proximity = pq.read_table(store / "rollups/substation_optical_flash_proximity.parquet").to_pylist()
    check(len(proximity) == len(sites), "proximity rollup does not list every located substation")
    for row in proximity:
        got = [row["optical_flash_proximity_5km"], row["optical_flash_proximity_10km"], row["optical_flash_proximity_20km"]]
        want = [None, None, None] if nothing_searched else expected[row["site_code"]]
        check(got == want, f"proximity {row['site_code']}: {got} != recomputed {want}")
        check(abs(row["hours_observed"] - hours_observed) < 1e-3 and row["hours_expected"] == 24.0 * len(days),
              f"proximity {row['site_code']}: coverage hours differ")
        check(row["count_state"] == state, f"proximity {row['site_code']}: count_state wrong")
        cov_counts = Counter(d["coverage"] for d in days)
        check((row["days_full"], row["days_partial"], row["days_never_searched"], row["days_shard_lost"]) ==
              (cov_counts["full"], cov_counts["partial"], cov_counts["none"], cov_counts["shard_lost"]),
              f"proximity {row['site_code']}: day coverage counts differ")
    check(manifest["derived_from"]["substations"]["upstream_sha256"] == pin["upstream_sha256"],
          "manifest substation pin differs from the extract")
    for name in ("substation_optical_flash_proximity", "substation_optical_flash_proximity_daily"):
        file = store / f"rollups/{name}.parquet"
        for field in pq.read_schema(file):
            if field.name.startswith("optical_flash_proximity"):
                text = field.metadata[b"description"].decode()
                check("OPTICAL-FLASH PROXIMITY" in text and "not strikes or faults" in text,
                      f"{name}.{field.name}: missing the optical-flash proximity label")
    days_by_date = {d["date"]: d for d in days}
    seen_daily = {}
    for row in pq.read_table(store / "rollups/substation_optical_flash_proximity_daily.parquet").to_pylist():
        key = (row["site_code"], row["date"])
        check(key not in seen_daily, f"daily proximity {key}: duplicate row")
        cov = days_by_date.get(row["date"])
        check(cov is not None and searched_day[row["date"]], f"daily proximity {key}: on an UNKNOWN day")
        for field in ("coverage", "count_state", "hours_observed", "hours_expected"):
            check(row[field] == cov[field], f"daily proximity {key}: {field} {row[field]!r} != coverage/days {cov[field]!r}")
        seen_daily[key] = [row["optical_flash_proximity_5km"], row["optical_flash_proximity_10km"],
                           row["optical_flash_proximity_20km"]]
    check(seen_daily == expected_daily, "daily proximity differs from the recomputation")

    # 7. Storm episodes partition the flashes; coverage flags are consistent.
    episodes = pq.read_table(store / "rollups/storm_episodes.parquet").to_pylist()
    check(sum(e["flash_count"] for e in episodes) == len(flashes), "episodes do not partition the flashes")

    def searched(a, b) -> bool:  # [a, b] inside one window and touching no shard_lost day
        return any(s <= a and e >= b for s, e in spans) and not any(s < b and e > a for s, e in lost)

    gap = timedelta(minutes=30)
    groups = []
    for row in sorted(flashes, key=lambda r: (r["time_utc"], r["id"])):
        t = row["time_utc"]
        if groups and t - groups[-1][-1] <= gap and searched(groups[-1][-1], t):
            groups[-1].append(t)
        else:
            groups.append([t])
    want = []
    for g in groups:
        before, after = searched(g[0] - gap, g[0]), searched(g[-1], g[-1] + gap)
        want.append((g[0], g[-1], len(g), before, after,
                     "complete" if before and after else "edge_unsearched_may_extend"))
    got = [(e["start_utc"], e["end_utc"], e["flash_count"], e["searched_30min_before"], e["searched_30min_after"],
            e["coverage"]) for e in episodes]
    if got != want:
        first = next((g, w) for g, w in zip(got + [None] * len(want), want + [None] * len(got)) if g != w)
        raise VerifyError(f"storm episodes differ from the recomputation (stored, recomputed) = {first}")
    for e in episodes:
        check((e["coverage"] == "complete") == (e["searched_30min_before"] and e["searched_30min_after"]),
              f"episode {e['episode_id']}: coverage flag inconsistent")
        check(e["end_utc"] - e["start_utc"] >= timedelta(0), f"episode {e['episode_id']}: negative duration")

    # 8. Headline: tiny, and every number equal to the recomputation.
    blob = (store / "rollups/headline.json").read_bytes()
    check(len(blob) <= rollups.HEADLINE_MAX_BYTES, f"headline.json is {len(blob)} bytes > 4096")
    head = json.loads(blob)
    check(head["schema"] == rollups.SCHEMA_HEADLINE and "not ground strikes" in head["not"], "headline caveats missing")
    check(synthetic == (head.get("synthetic") == ps.SYNTHETIC_FLAG), "headline synthetic flag disagrees")
    arch = head["archive"]
    check(arch["flashes"] == (None if nothing_searched else len(flashes)), "headline flash total differs")
    check(abs(arch["hours_observed"] - hours_observed) < 1e-3 and arch["hours_expected"] == 24.0 * len(days),
          "headline hours differ")
    check(arch["count_state"] == state and head["substations"]["count_state"] == state,
          f"headline count_state {arch['count_state']!r}/{head['substations']['count_state']!r} != recomputed {state!r}")
    check(head["episodes"]["complete"] == sum(1 for e in want if e[5] == "complete"), "headline complete episodes differ")
    cov_counts = Counter(d["coverage"] for d in days)
    check(arch["days"] == {"in_range": len(days), "full": cov_counts["full"], "partial": cov_counts["partial"],
                           "never_searched": cov_counts["none"], "shard_lost": cov_counts["shard_lost"]},
          "headline day coverage differs")
    by_date = {d["date"].strftime("%m-%d"): d for d in days[-7:]}
    for item in head["last_7_days"]:
        cov = by_date[item["d"]]
        check(item["cov"] == cov["coverage"] and item["n"] == cov["flash_count"] and item["h_obs"] == cov["hours_observed"],
              f"headline last_7_days {item['d']} differs")
        check(item["cov"] not in UNKNOWN_COVERAGE or item["n"] is None, "headline claims a count for UNKNOWN day")
    ranked = sorted(((-c[1], code) for code, c in expected.items() if c[1] > 0))[:3]
    check([t["site"] for t in head["substations"]["top"]] == [code for _, code in ranked], "headline top sites differ")
    check([t["n10"] for t in head["substations"]["top"]] == [-n for n, _ in ranked], "headline top counts differ")
    check(head["substations"]["with_any_within_10km"] ==
          (None if nothing_searched else sum(1 for c in expected.values() if c[1] > 0)),
          "headline substation total differs")
    check("optical-flash proximity" in head["substations"]["label"], "headline proximity label missing")
    check(head["episodes"]["count"] == len(episodes), "headline episode count differs")
    return {"outcome": "PASS", "files": len(listed), "bytes": manifest["totals"]["bytes"], "flashes": len(flashes),
            "days": len(days), "days_searched": sum(searched_day.values()), "substations": len(sites),
            "episodes": len(episodes), "headline_bytes": len(blob), "synthetic": synthetic}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    parser.add_argument("--out", type=Path, default=Path("data/parquet"))
    parser.add_argument("--substations", type=Path, default=None)
    parser.add_argument("--if-archive", action="store_true", help="pass when neither archive nor store exists yet")
    args = parser.parse_args(argv)
    if args.if_archive and not (args.archive_dir / "index.json").exists() and not (args.out / "manifest.json").exists():
        print(json.dumps({"outcome": "SKIPPED", "reason": "no archive index and no store yet"}))
        return 0
    try:
        report = verify(args.archive_dir, args.out, substations_path=args.substations)
    except (VerifyError, ValueError, OSError, KeyError) as error:
        print(f"PARQUET VERIFY FAILURE: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""DuckDB SQL layer over the lightning Parquet store (sql/*.sql).

``build_rollups`` opens an in-memory DuckDB, creates the views in sql/ over
the Parquet written by parquet_store.py, and returns deterministic Parquet
bytes for each rollup plus the browser-sized ``rollups/headline.json``
(<= 4096 bytes). ``connect`` gives the same views to an analyst, and
``python rollups.py duckdb --out lightning.duckdb`` writes a small DuckDB file
holding only views over the Parquet (gitignored, never committed).

Every number carries its coverage: never-searched and shard_lost time reads
UNKNOWN (NULL), never 0. Substation metrics are OPTICAL-FLASH PROXIMITY, not
ground strikes, confirmed strikes or electrical faults.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb
import pyarrow as pa

ROOT = Path(__file__).resolve().parent
SQL_DIR = ROOT / "sql"
SUBSTATIONS = ROOT / "sources" / "gb-transmission-substations.v1.json"
SUBSTATIONS_PIN = ROOT / "sources" / "gb-transmission-substations.source.json"
HEADLINE_MAX_BYTES = 4096
SCHEMA_HEADLINE = "uk-lightning-headline.v1"
PROXIMITY_LABEL = ("optical-flash proximity: satellite optical flashes within the radius of a transmission "
                   "connection point's map location; not ground strikes, not confirmed strikes, not electrical faults")


def _field(name, typ, description, nullable=True):
    return pa.field(name, typ, nullable=nullable, metadata={"description": description})


TS = pa.timestamp("us", tz="UTC")
COVERAGE_FIELDS = [
    _field("coverage", pa.string(), "none | partial | full | shard_lost (none and shard_lost read UNKNOWN)"),
    _field("count_state", pa.string(), "observed | UNKNOWN; UNKNOWN counts are NULL, never 0"),
    _field("hours_observed", pa.float64(), "searched hours in the row's period; NULL when UNKNOWN"),
    _field("hours_expected", pa.float64(), "hours in the row's period"),
]
SCHEMAS = {
    "daily_optical_flashes": ("uk-lightning-rollup-daily.v1", pa.schema([
        _field("date", pa.date32(), "UTC date"), *COVERAGE_FIELDS,
        _field("scanned_seconds", pa.int32(), "searched seconds of the date; NULL when UNKNOWN"),
        _field("flash_count", pa.int64(), "optical flashes archived on the date; NULL when UNKNOWN; 0 only when searched"),
        _field("sealed", pa.bool_(), "the date can no longer change"),
    ])),
    "hourly_optical_flashes": ("uk-lightning-rollup-hourly.v1", pa.schema([
        _field("hour_utc", TS, "UTC hour start"), _field("date", pa.date32(), "UTC date"), *COVERAGE_FIELDS,
        _field("scanned_seconds", pa.int32(), "searched seconds of the hour; NULL when UNKNOWN"),
        _field("basis", pa.string(), "scan_window | flash_proven (lower bound) | NULL"),
        _field("flash_count", pa.int64(), "optical flashes in the hour; NULL when UNKNOWN; 0 only when searched"),
    ])),
    "substation_optical_flash_proximity": ("uk-lightning-rollup-substation-optical-flash-proximity.v1", pa.schema([
        _field("site_code", pa.string(), "NESO ETYS site code (via data-grid-gb)"),
        _field("name", pa.string(), "NESO ETYS site name"),
        _field("transmission_owner", pa.string(), "transmission owner"),
        _field("max_kv", pa.int32(), "highest published voltage, kV"),
        _field("lon", pa.float64(), "joined map longitude (c) OpenStreetMap contributors, ODbL"),
        _field("lat", pa.float64(), "joined map latitude (c) OpenStreetMap contributors, ODbL"),
        _field("matched_by", pa.string(), "how data-grid-gb joined the coordinates"),
        _field("optical_flash_proximity_5km", pa.int64(), "OPTICAL-FLASH PROXIMITY within 5 km over searched hours; at or below LI location accuracy; not strikes or faults"),
        _field("optical_flash_proximity_10km", pa.int64(), "OPTICAL-FLASH PROXIMITY within 10 km over searched hours; not strikes or faults"),
        _field("optical_flash_proximity_20km", pa.int64(), "OPTICAL-FLASH PROXIMITY within 20 km over searched hours; not strikes or faults"),
        _field("first_flash_within_20km_utc", TS, "first optical flash within 20 km"),
        _field("last_flash_within_20km_utc", TS, "last optical flash within 20 km"),
        _field("hours_observed", pa.float64(), "searched hours over the archive range"),
        _field("hours_expected", pa.float64(), "hours in the archive range"),
        _field("days_full", pa.int64(), "fully searched UTC days"),
        _field("days_partial", pa.int64(), "partly searched UTC days"),
        _field("days_never_searched", pa.int64(), "never-searched UTC days (UNKNOWN)"),
        _field("days_shard_lost", pa.int64(), "days whose shard is lost (UNKNOWN)"),
        _field("count_state", pa.string(), "complete | lower_bound_partial_coverage"),
    ])),
    "substation_optical_flash_proximity_daily": ("uk-lightning-rollup-substation-optical-flash-proximity-daily.v1", pa.schema([
        _field("site_code", pa.string(), "NESO ETYS site code"), _field("date", pa.date32(), "UTC date"),
        *COVERAGE_FIELDS,
        _field("optical_flash_proximity_5km", pa.int64(), "OPTICAL-FLASH PROXIMITY within 5 km; not strikes or faults"),
        _field("optical_flash_proximity_10km", pa.int64(), "OPTICAL-FLASH PROXIMITY within 10 km; not strikes or faults"),
        _field("optical_flash_proximity_20km", pa.int64(), "OPTICAL-FLASH PROXIMITY within 20 km; not strikes or faults"),
    ])),
    "storm_episodes": ("uk-lightning-rollup-storm-episodes.v1", pa.schema([
        _field("episode_id", pa.string(), "E<start>-<n>"),
        _field("start_utc", TS, "first optical flash"), _field("end_utc", TS, "last optical flash"),
        _field("duration_seconds", pa.float64(), "end - start"),
        _field("flash_count", pa.int64(), "optical flashes in the episode (searched time only)"),
        _field("min_lon", pa.float64(), ""), _field("max_lon", pa.float64(), ""),
        _field("min_lat", pa.float64(), ""), _field("max_lat", pa.float64(), ""),
        _field("mean_lon", pa.float64(), ""), _field("mean_lat", pa.float64(), ""),
        _field("start_reason", pa.string(), "first | quiet_gap (> 30 min searched and empty) | unsearched_gap"),
        _field("searched_30min_before", pa.bool_(), "the 30 min before the first flash were searched"),
        _field("searched_30min_after", pa.bool_(), "the 30 min after the last flash were searched"),
        _field("coverage", pa.string(), "complete | edge_unsearched_may_extend"),
    ])),
}
OUTPUTS = {
    "daily_optical_flashes": "rollups/daily.parquet",
    "substation_optical_flash_proximity": "rollups/substation_optical_flash_proximity.parquet",
    "substation_optical_flash_proximity_daily": "rollups/substation_optical_flash_proximity_daily.parquet",
    "storm_episodes": "rollups/storm_episodes.parquet",
    # hourly is partitioned by month: rollups/hourly/year=YYYY/month=MM/part-0.parquet
}


def _quote(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/").replace("'", "''")


def load_substations(path: Path | None = None) -> tuple[pa.Table, dict]:
    """The pinned substation extract, checked against its pin before use."""
    path = Path(path or SUBSTATIONS)
    extract = json.loads(path.read_text(encoding="utf-8"))
    pin = json.loads(SUBSTATIONS_PIN.read_text(encoding="utf-8"))
    derived = extract["derived_from"]
    if (derived["sha256"], derived["commit"]) != (pin["sha256"], pin["commit"]):
        raise ValueError("substation extract does not match its pinned upstream")
    rows = extract["substations"]
    table = pa.table({
        "site_code": [r["site_code"] for r in rows], "name": [r["name"] for r in rows],
        "transmission_owner": [r["transmission_owner"] for r in rows],
        "max_kv": pa.array([r["max_kv"] for r in rows], pa.int32()),
        "lon": [float(r["lon"]) for r in rows], "lat": [float(r["lat"]) for r in rows],
        "matched_by": [r["matched_by"] for r in rows],
    })
    summary = {"repository": derived["repository"], "commit": derived["commit"], "path": derived["path"],
               "upstream_sha256": derived["sha256"],
               "extract": path.name, "extract_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "located": len(rows), "unlocated_not_included": extract["counts"]["unlocated_not_included"],
               "attribution": extract["attribution"]}
    return table, summary


def connect(store: Path, *, substations_path: Path | None = None, database: str = ":memory:"):
    """A DuckDB connection with every view in sql/ defined over ``store``."""
    store = Path(store)
    con = duckdb.connect(database)
    con.execute("SET threads = 1")  # deterministic ordering of ties and floating-point sums
    flash_files = sorted(store.glob("flashes/year=*/month=*/*.parquet"))
    if flash_files:
        flashes_source = f"read_parquet('{_quote(store)}/flashes/year=*/month=*/*.parquet', hive_partitioning = false)"
    else:
        flashes_source = ("(SELECT NULL::VARCHAR AS id, NULL::TIMESTAMPTZ AS time_utc, NULL::DOUBLE AS lon, "
                          "NULL::DOUBLE AS lat, NULL::VARCHAR AS source_product, NULL::VARCHAR AS observation_kind, "
                          "NULL::VARCHAR AS ingest_run_id WHERE false)")
    substations, summary = load_substations(substations_path)
    if database == ":memory:":
        con.register("substations_arrow", substations)
        con.execute("CREATE OR REPLACE TEMP TABLE substations AS SELECT * FROM substations_arrow")
        con.unregister("substations_arrow")
    else:
        con.register("substations_arrow", substations)
        con.execute("CREATE OR REPLACE TABLE substations AS SELECT * FROM substations_arrow")
        con.unregister("substations_arrow")
        con.execute("COMMENT ON TABLE substations IS "
                    "'GB transmission connection points located by data-grid-gb; coordinates (c) OpenStreetMap contributors, ODbL'")
    for file in sorted(SQL_DIR.glob("*.sql")):
        sql = file.read_text(encoding="utf-8").replace("{root}", _quote(store)).replace("{flashes_source}", flashes_source)
        con.execute(sql)
    return con, summary


def _arrow(con, view: str) -> pa.Table:
    name, schema = SCHEMAS[view]
    table = con.sql(f"SELECT * FROM {view}").to_arrow_table()
    table = table.select(schema.names).cast(schema)
    return table.replace_schema_metadata({
        "schema": name, "observation": "satellite optical lightning flashes; not ground strikes, not damage, not causality",
        "coverage_rule": "none and shard_lost read UNKNOWN (NULL); 0 only for searched time"})


_ISO = "strftime({}, '%Y-%m-%dT%H:%M:%SZ')"


def headline(con, index: dict, substations: dict, *, synthetic: bool) -> dict:
    cov = con.execute("SELECT * FROM archive_coverage").fetchone()
    days_in_range, days_full, days_partial, days_none, days_lost, hours_observed, hours_expected = cov
    # Timestamps are formatted in SQL (UTC session) so no Python timezone module is needed.
    total = con.execute(f"SELECT count(*), {_ISO.format('min(time_utc)')}, {_ISO.format('max(time_utc)')} "
                        "FROM flashes").fetchone()
    last7 = con.execute("""SELECT strftime(date, '%m-%d'), coverage, hours_observed, flash_count
                           FROM daily_optical_flashes ORDER BY date DESC LIMIT 7""").fetchall()
    busiest = con.execute("""SELECT strftime(date, '%Y-%m-%d'), flash_count, coverage, hours_observed
                             FROM daily_optical_flashes WHERE count_state = 'observed'
                             ORDER BY flash_count DESC, date LIMIT 1""").fetchone()
    top = con.execute("""SELECT site_code, name, optical_flash_proximity_10km FROM substation_optical_flash_proximity
                         WHERE optical_flash_proximity_10km > 0
                         ORDER BY optical_flash_proximity_10km DESC, site_code LIMIT 3""").fetchall()
    near = con.execute("""SELECT count(*) FILTER (WHERE optical_flash_proximity_10km > 0), count(*)
                          FROM substation_optical_flash_proximity""").fetchone()
    episodes = con.execute("""SELECT count(*), count(*) FILTER (WHERE coverage = 'complete') FROM storm_episodes""").fetchone()
    largest = con.execute(f"""SELECT {_ISO.format('start_utc')}, {_ISO.format('end_utc')}, flash_count, coverage
                             FROM storm_episodes
                             ORDER BY flash_count DESC, start_utc LIMIT 1""").fetchone()
    count_state = "complete" if hours_observed >= hours_expected else "lower_bound_partial_coverage"
    product = {
        "schema": SCHEMA_HEADLINE,
        "what": "UK-box satellite optical lightning flashes (EUMETSAT MTG LI L2-LFL) from the one-year UTC archive.",
        "not": "Optical flashes, not ground strikes, not damage, not electrical faults, not causality.",
        "coverage_rule": "Counts cover searched hours only; never-searched or lost time is UNKNOWN, not 0.",
        "archive": {
            "flashes": total[0], "count_state": count_state,
            "hours_observed": hours_observed, "hours_expected": hours_expected,
            "days": {"in_range": days_in_range, "full": days_full, "partial": days_partial,
                     "never_searched": days_none, "shard_lost": days_lost},
            "coverage_start_utc": index.get("coverage_start_utc"),
            "last_scan_end_utc": index.get("last_successful_scan_end_utc"),
            "first_flash_utc": total[1], "last_flash_utc": total[2],
            "prospective": "starts when collection began; earlier seed days were only partly searched",
        },
        "last_7_days": [{"d": d, "cov": c, "h_obs": h, "n": n} for d, c, h, n in reversed(last7)],
        "busiest_day": ({"date": busiest[0], "flashes": busiest[1], "coverage": busiest[2], "hours_observed": busiest[3]}
                        if busiest else None),
        "substations": {
            "metric": "optical_flash_proximity_10km",
            "label": "optical-flash proximity, not strikes or faults",
            "located": near[1], "with_any_within_10km": near[0], "count_state": count_state,
            "top": [{"site": s, "name": n, "n10": c} for s, n, c in top],
        },
        "episodes": {
            "count": episodes[0], "complete": episodes[1], "gap_rule_minutes": 30,
            "largest": ({"start": largest[0], "end": largest[1], "flashes": largest[2], "coverage": largest[3]}
                        if largest else None),
        },
        "attribution": "EUMETSAT (LI L2-LFL, NoConditions); NESO ETYS 2025 via data-grid-gb; "
                       "substation map locations (c) OpenStreetMap contributors, ODbL.",
        "detail": "manifest.json",
    }
    if synthetic:
        product["synthetic"] = "SYNTHETIC-NOT-PUBLISHED"
    return product


def headline_bytes(product: dict) -> bytes:
    blob = (json.dumps(product, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    if len(blob) > HEADLINE_MAX_BYTES:
        raise ValueError(f"headline.json would be {len(blob)} bytes > {HEADLINE_MAX_BYTES}")
    return blob


def build_rollups(store: Path, *, substations_path: Path | None = None, synthetic: bool = False,
                  index: dict | None = None) -> tuple[dict[str, bytes], dict]:
    import parquet_store
    con, summary = connect(store, substations_path=substations_path)
    produced: dict[str, bytes] = {}
    try:
        for view, rel in OUTPUTS.items():
            produced[rel] = parquet_store.parquet_bytes(_arrow(con, view))
        hourly = _arrow(con, "hourly_optical_flashes")
        hours = hourly.column("hour_utc").to_pylist()
        months = sorted({(h.year, h.month) for h in hours})
        for year, month in months:
            mask = [h.year == year and h.month == month for h in hours]
            produced[f"rollups/hourly/year={year:04d}/month={month:02d}/part-0.parquet"] = \
                parquet_store.parquet_bytes(hourly.filter(pa.array(mask)))
        produced["rollups/headline.json"] = headline_bytes(headline(con, index or {}, summary, synthetic=synthetic))
    finally:
        con.close()
    return produced, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    db = sub.add_parser("duckdb", help="write a small DuckDB file of views over the Parquet store")
    db.add_argument("--store", type=Path, default=Path("data/parquet"))
    db.add_argument("--out", type=Path, default=Path("lightning.duckdb"))
    q = sub.add_parser("query", help="run one SQL statement against the views")
    q.add_argument("--store", type=Path, default=Path("data/parquet"))
    q.add_argument("sql")
    args = parser.parse_args(argv)
    if args.command == "duckdb":
        if args.out.exists():
            args.out.unlink()
        con, _ = connect(args.store, database=str(args.out))
        views = [r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal ORDER BY 1").fetchall()]
        con.close()
        print(f"wrote {args.out} ({args.out.stat().st_size} bytes): views {', '.join(views)}")
        return 0
    con, _ = connect(args.store)
    for row in con.execute(args.sql).fetchall():
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

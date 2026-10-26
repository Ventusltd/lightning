#!/usr/bin/env python3
"""Write data/status.json after every collector run, successful or not.

The status file is the collector's heartbeat and the source of the
self-healing lookback: ``last_successful_scan_end_utc`` is the end of the
last examined EUMETSAT product slot, so the next run searches from there
(plus a margin) instead of leaving a gap. A failed run keeps the previous
success values, records the error and increments ``consecutive_failures``;
nothing here ever claims zero lightning for an unsearched period.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from collect import RUN_RECORD_SCHEMA, iso_utc, parse_utc

SCHEMA_STATUS = "uk-lightning-status.v1"
STALE_WARN_HOURS = 6
STALE_HOURS = 12
STOPPED_HOURS = 24


def _load(path: Path | None) -> dict | None:
    if not path or not Path(path).exists():
        return None
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def health(now: datetime, last_success_end: datetime | None) -> str:
    """ok / warn (>6 h) / stale (>12 h) / stopped (>24 h) / never."""
    if last_success_end is None:
        return "never"
    age = (now - last_success_end).total_seconds() / 3600
    if age > STOPPED_HOURS:
        return "stopped"
    if age > STALE_HOURS:
        return "stale"
    if age > STALE_WARN_HOURS:
        return "warn"
    return "ok"


def build_status(
    *,
    now: datetime,
    previous: dict | None,
    record: dict | None,
    archive_result: dict | None,
    collect_outcome: str,
    archive_outcome: str,
) -> dict:
    previous = previous or {}
    record = record if record and record.get("schema") == RUN_RECORD_SCHEMA else None
    collected = collect_outcome == "success" and record is not None and record.get("outcome") == "success"
    error = None
    if not collected:
        error = (record or {}).get("error") or f"collect step outcome={collect_outcome}; no run record"
    archive_error = None
    if archive_outcome == "failure":
        archive_error = (archive_result or {}).get("error") or "archive step failed (see workflow log)"
    elif archive_result and archive_result.get("outcome") == "failure":
        archive_error = archive_result.get("error")

    last_success_end = previous.get("last_successful_scan_end_utc")
    last_success_start = previous.get("last_successful_scan_start_utc")
    last_success_run = previous.get("last_successful_run_utc")
    if collected:
        last_success_run = record["run_utc"]
        ranges = record.get("scanned_ranges") or []
        if ranges and record.get("slots_parsed") is not False:  # unparsed = requested window, not examined slots
            end = max(parse_utc(r["end_utc"]) for r in ranges)
            start = min(parse_utc(r["start_utc"]) for r in ranges)
            if not last_success_end or end > parse_utc(last_success_end):
                last_success_end = iso_utc(end)
            last_success_start = iso_utc(start)
    failures = 0 if collected else int(previous.get("consecutive_failures") or 0) + 1
    last_end_dt = parse_utc(last_success_end) if last_success_end else None
    return {
        "schema": SCHEMA_STATUS,
        "written_utc": iso_utc(now),
        "health": health(now, last_end_dt),
        "last_attempt_utc": (record or {}).get("run_utc") or iso_utc(now),
        "last_attempt_outcome": "success" if collected else "failure",
        "last_error": error,
        "consecutive_failures": failures,
        "last_successful_run_utc": last_success_run,
        "last_successful_scan_start_utc": last_success_start,
        "last_successful_scan_end_utc": last_success_end,
        "last_query_start_utc": (record or {}).get("query_start_utc"),
        "last_query_end_utc": (record or {}).get("query_end_utc"),
        "last_lookback_hours": (record or {}).get("lookback_hours"),
        "last_lookback_source": (record or {}).get("lookback_source"),
        "last_lookback_capped": (record or {}).get("lookback_capped"),
        "last_products_examined": (record or {}).get("products_examined"),
        "last_slots_examined": (record or {}).get("slots_examined"),
        "last_slots_parsed": (record or {}).get("slots_parsed"),
        "last_slots_examined_utc": (record or {}).get("slots_examined_utc") if collected else None,
        "last_scanned_ranges": (record or {}).get("scanned_ranges") if collected else None,
        "last_unscanned_ranges": (record or {}).get("unscanned_ranges") if collected else None,
        "last_tail_unobserved_seconds": (record or {}).get("tail_unobserved_seconds") if collected else None,
        "feed": {
            "features": (record or {}).get("feed_features"),
            "total_before_display_cap": (record or {}).get("feed_total_before_display_cap"),
            "latest_detection_utc": (record or {}).get("feed_latest_detection_utc"),
            "incoming_features": (record or {}).get("incoming_features"),
        } if collected else previous.get("feed"),
        "archive": {
            "outcome": archive_outcome,
            "error": archive_error,
            "archived_observations": (archive_result or {}).get("archived_observations"),
            "files_changed": (archive_result or {}).get("files_changed"),
            "dropped_before_cutoff": (archive_result or {}).get("dropped_before_cutoff"),
            "split_days": (archive_result or {}).get("split_days"),
            "lost_days": (archive_result or {}).get("lost_days"),
            "dropped_lost_day": (archive_result or {}).get("dropped_lost_day"),
            "index_generated_utc": (archive_result or {}).get("index_generated_utc"),
        },
        "thresholds_hours": {"warn": STALE_WARN_HOURS, "stale": STALE_HOURS, "stopped": STOPPED_HOURS},
        "note": ("A failed run leaves the feed and archive unchanged. Time not covered by a successful "
                 "scan is unknown, not zero. Flashes are optical satellite detections, not confirmed ground strikes."),
    }


def write_status(path: Path, status: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=1) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write")
    write.add_argument("--status", type=Path, default=Path("data/status.json"))
    write.add_argument("--run-record", type=Path, default=None)
    write.add_argument("--archive-result", type=Path, default=None)
    write.add_argument("--collect-outcome", default="failure")
    write.add_argument("--archive-outcome", default="skipped")
    write.add_argument("--now", default=None)
    args = parser.parse_args(argv)
    now = parse_utc(args.now) if args.now else datetime.now(timezone.utc)
    status = build_status(
        now=now, previous=_load(args.status), record=_load(args.run_record),
        archive_result=_load(args.archive_result), collect_outcome=args.collect_outcome,
        archive_outcome=args.archive_outcome,
    )
    write_status(args.status, status)
    print(f"status: {status['last_attempt_outcome']} health={status['health']} "
          f"last_successful_scan_end={status['last_successful_scan_end_utc']} "
          f"consecutive_failures={status['consecutive_failures']} archive={status['archive']['outcome']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

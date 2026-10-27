#!/usr/bin/env python3
"""Write data/status.json after every collector run, successful or not.

The status file is the collector's heartbeat and the source of the
self-healing lookback: ``last_successful_scan_end_utc`` is the end of the
last examined EUMETSAT product slot, so the next run searches from there
(plus a margin) instead of leaving a gap. A failed run keeps the previous
success values, records the error and increments ``consecutive_failures``;
nothing here ever claims zero lightning for an unsearched period.

``freshness`` is the age-based value (ok/warn/stale/stopped/never). ``health``
is the same value except that an archive with any ``shard_lost`` date is never
reported ``ok``: a fresh run with a lost shard is ``degraded``. ``integrity``
carries the explicit alert; ``status.py alert`` exits 1 while it is raised.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collect import RUN_RECORD_SCHEMA, iso_utc, parse_utc

SCHEMA_STATUS = "uk-lightning-status.v1"
STALE_WARN_HOURS = 6
STALE_HOURS = 12
STOPPED_HOURS = 24

# The hourly collector's schedule (update-lightning.yml). Two cron offsets: the
# :47 run is a fallback that skips itself (``status.py due``) when the :17 run
# already succeeded, because GitHub's scheduler delays or drops runs (27 Oct
# 2026: 2 of 13 hourly slots between 03:17 and 15:17 UTC produced a run).
CRON_UTC = ("17 * * * *", "47 * * * *")
CRON_MINUTES = (17, 47)
EXPECTED_EVERY_MINUTES = 60
LATE_AFTER_MINUTES = 90      # a heartbeat older than this is late, whatever the cause
DUE_MIN_GAP_MINUTES = 40     # a scheduled fallback run skips if a run succeeded this recently
QUEUED_MINUTES = 20          # queue time above this is reported as "waited behind another run"


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


def _minutes(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() / 60, 1)


def _maybe_utc(value) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc(str(value))
    except ValueError:
        return None


def next_cron_slot(now: datetime) -> datetime:
    """The next scheduled slot (minute 17 or 47 of any hour) strictly after ``now``."""
    base = now.replace(second=0, microsecond=0)
    for hour_offset in (0, 1):
        for minute in CRON_MINUTES:
            slot = base.replace(minute=minute) + timedelta(hours=hour_offset)
            if slot > now:
                return slot
    raise AssertionError("unreachable")


def _fmt_minutes(minutes: float) -> str:
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    return f"{hours} h {mins:02d} min" if hours else f"{mins} min"


def heartbeat_block(
    *,
    now: datetime,
    previous: dict,
    attempt_utc: str,
    outcome: str,
    trigger: str | None,
    run_id: str | None,
    run_created_utc: str | None,
    job_started_utc: str | None,
) -> dict:
    """Explain when the collector ran, what triggered it and why the gap before it was long.

    The gap is measured from the previous heartbeat's attempt to the moment
    GitHub created this run, and split into the scheduler's share (no run was
    created) and the queue's share (the run existed but waited for the
    concurrency group). A collector cannot report a run that never started, so
    ``if_late`` tells a reader what a heartbeat older than ``late_after_utc``
    means.
    """
    attempt = parse_utc(attempt_utc)
    created = _maybe_utc(run_created_utc)
    started = _maybe_utc(job_started_utc)
    prev_attempt = _maybe_utc(previous.get("last_attempt_utc"))
    queued = _minutes(created, started) if created and started and started >= created else None
    this_run = {
        "trigger": trigger or None,
        "run_id": str(run_id) if run_id else None,
        "run_created_utc": iso_utc(created) if created else None,
        "job_started_utc": iso_utc(started) if started else None,
        "queued_minutes": queued,
        "attempt_utc": attempt_utc,
        "outcome": outcome,
    }
    arrived = created or started or attempt
    gap = None
    if prev_attempt is not None and arrived >= prev_attempt:
        total = _minutes(prev_attempt, attempt)
        to_created = _minutes(prev_attempt, arrived)
        scheduler_late = to_created > LATE_AFTER_MINUTES
        long_queue = queued is not None and queued > QUEUED_MINUTES
        if total <= LATE_AFTER_MINUTES:
            cause = "on_time"
        elif scheduler_late and long_queue:
            cause = "github_schedule_late_then_queued"
        elif long_queue:
            cause = "queued_behind_another_run"
        elif scheduler_late and trigger == "schedule" and created is not None:
            cause = "github_schedule_late_or_dropped"
        elif scheduler_late and trigger and trigger != "schedule":
            cause = "no_scheduled_run_until_manual_start"
        else:
            cause = "unknown"
        # :17 slots after the previous attempt that produced no run (a scheduled run serves one of them).
        missed = max(0, _hourly_slots(prev_attempt, arrived) - (1 if trigger == "schedule" else 0))
        gap = {
            "previous_attempt_utc": iso_utc(prev_attempt),
            "previous_attempt_outcome": previous.get("last_attempt_outcome"),
            "minutes": total,
            "minutes_until_run_created": to_created if created else None,
            "hourly_slots_without_a_run": missed,
            "cause": cause,
        }
    explanation = _explain(attempt, outcome, this_run, gap)
    late_after = attempt + timedelta(minutes=LATE_AFTER_MINUTES)
    return {
        "expected_every_minutes": EXPECTED_EVERY_MINUTES,
        "cron_utc": list(CRON_UTC),
        "next_expected_run_utc": iso_utc(next_cron_slot(max(now, attempt))),
        "late_after_utc": iso_utc(late_after),
        "this_run": this_run,
        "gap_before_this_run": gap,
        "explanation": explanation,
        "if_late": ("If now is past late_after_utc, no collector run has written a heartbeat since "
                    f"{_hhmm(attempt)} UTC. GitHub's scheduler does not guarantee scheduled runs; the next run "
                    "records the cause (scheduler delay or queue). Flashes in the gap are unknown, not zero."),
    }


def _hhmm(value: datetime) -> str:
    return value.strftime("%d %b %H:%M")


def _slots_between(start: datetime, end: datetime):
    slot = next_cron_slot(start)
    while slot <= end:
        yield slot
        slot = next_cron_slot(slot)


def _hourly_slots(start: datetime, end: datetime) -> int:
    """Number of :17 slots in (start, end]: the hourly cadence the job promises."""
    return sum(1 for slot in _slots_between(start, end) if slot.minute == CRON_MINUTES[0])


def _explain(attempt: datetime, outcome: str, this_run: dict, gap: dict | None) -> str:
    trigger = this_run.get("trigger") or "unknown trigger"
    text = f"Last collector run {_hhmm(attempt)} UTC ({trigger}, {outcome})."
    if gap is None:
        return text + " No earlier heartbeat to compare with."
    span = _fmt_minutes(gap["minutes"])
    prev = _hhmm(parse_utc(gap["previous_attempt_utc"]))
    queued = this_run.get("queued_minutes")
    cause = gap["cause"]
    if cause == "on_time":
        return text + f" Previous run {prev} UTC, {span} earlier: on schedule."
    missed = gap["hourly_slots_without_a_run"]
    slots = f"{missed} hourly slot{'s' if missed != 1 else ''} produced no run"
    if cause == "github_schedule_late_or_dropped":
        return text + (f" Before it, no run for {span} (since {prev} UTC): GitHub's scheduler started no run "
                       f"on time ({slots}). It is not a collector or EUMETSAT failure.")
    if cause == "queued_behind_another_run":
        return text + (f" Before it, no run for {span} (since {prev} UTC): this run waited "
                       f"{_fmt_minutes(queued)} in the queue behind another run in its concurrency group.")
    if cause == "github_schedule_late_then_queued":
        return text + (f" Before it, no run for {span} (since {prev} UTC): GitHub's scheduler started no run "
                       f"on time ({slots}), then this run waited {_fmt_minutes(queued)} in the queue behind another run.")
    if cause == "no_scheduled_run_until_manual_start":
        return text + f" Before it, no run for {span} (since {prev} UTC): {slots}; this run was started manually."
    return text + f" Before it, no run for {span} (since {prev} UTC); cause unknown (run creation time not recorded)."


def is_due(previous: dict | None, *, now: datetime, event: str, min_gap_minutes: int = DUE_MIN_GAP_MINUTES) -> tuple[bool, str]:
    """Should this run collect? Only a scheduled run may skip, and only after a recent success.

    The fallback cron slot (:47) makes a dropped :17 slot cost at most ~30 min.
    When the :17 run already succeeded, the :47 run skips instead of searching
    the same 6 h window again.
    """
    if event != "schedule":
        return True, f"{event or 'unknown'} trigger always collects"
    previous = previous or {}
    attempt = _maybe_utc(previous.get("last_attempt_utc"))
    if attempt is None:
        return True, "no previous heartbeat"
    if previous.get("last_attempt_outcome") != "success":
        return True, "previous attempt did not succeed"
    age = (now - attempt).total_seconds() / 60
    if 0 <= age < min_gap_minutes:
        return False, f"last successful attempt {age:.0f} min ago (< {min_gap_minutes} min); skipping this slot"
    return True, f"last successful attempt {age:.0f} min ago"


def integrity_status(previous: dict, archive_result: dict | None, archive_outcome: str) -> dict:
    """The archive integrity block. A lost shard is never forgotten by a failed run.

    Only a successful archive step (ingest + verify, then committed) can change the
    state; otherwise the previous status is carried, and any loss this run detected
    is added to it (an alert is never hidden by a failure).
    """
    prev = previous.get("integrity") if isinstance(previous.get("integrity"), dict) else None
    this = (archive_result or {}).get("integrity") if isinstance((archive_result or {}).get("integrity"), dict) else None
    if archive_outcome == "success" and this and this.get("state") in ("ok", "shard_lost"):
        lost, source = sorted(this.get("lost_days") or []), "this_run"
        alert = this.get("alert")
        quarantined = this.get("quarantined_observations")
    elif prev or (this and this.get("state") == "shard_lost"):
        lost = sorted(set((prev or {}).get("lost_days") or []) | set((this or {}).get("lost_days") or []))
        source = "previous_status" if prev else "this_run_unverified"
        alert = (this or {}).get("alert") if (this or {}).get("state") == "shard_lost" else (prev or {}).get("alert")
        quarantined = (prev or {}).get("quarantined_observations")
    else:
        return {"state": "unknown", "alert": None, "lost_days": [], "quarantined_observations": None, "source": None}
    state = "shard_lost" if lost else "ok"
    if state == "shard_lost" and not alert:
        alert = f"INTEGRITY ALERT: archive shard lost for {', '.join(lost)}; counts unknown, not zero"
    return {"state": state, "alert": alert if state == "shard_lost" else None, "lost_days": lost,
            "quarantined_observations": quarantined, "source": source}


def build_status(
    *,
    now: datetime,
    previous: dict | None,
    record: dict | None,
    archive_result: dict | None,
    collect_outcome: str,
    archive_outcome: str,
    trigger: str | None = None,
    run_id: str | None = None,
    run_created_utc: str | None = None,
    job_started_utc: str | None = None,
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
    freshness = health(now, last_end_dt)
    integrity = integrity_status(previous, archive_result, archive_outcome)
    overall = "degraded" if integrity["state"] == "shard_lost" and freshness in ("ok", "warn") else freshness
    attempt_utc = (record or {}).get("run_utc") or iso_utc(now)
    heartbeat = heartbeat_block(now=now, previous=previous, attempt_utc=attempt_utc,
                                outcome="success" if collected else "failure", trigger=trigger, run_id=run_id,
                                run_created_utc=run_created_utc, job_started_utc=job_started_utc)
    return {
        "schema": SCHEMA_STATUS,
        "written_utc": iso_utc(now),
        "health": overall,
        "freshness": freshness,
        "integrity": integrity,
        "last_attempt_utc": attempt_utc,
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
        "heartbeat": heartbeat,
        "backfill": previous.get("backfill"),
        "note": ("A failed run leaves the feed and archive unchanged. Time not covered by a successful "
                 "scan is unknown, not zero. Flashes are optical satellite detections, not confirmed ground strikes."),
    }


BACKFILL_OK = ("completed", "time_budget")


def backfill_status(
    previous: dict | None,
    *,
    now: datetime,
    planned: list[str],
    filled: list[str],
    stopped: str,
    error: str | None,
    index: dict | None,
) -> dict:
    """Record a backfill run WITHOUT touching the hourly heartbeat.

    The heartbeat fields (last attempt, failures, last successful scan end,
    freshness) belong to the hourly collector: a backfill searches past dates
    and must not move the lookback anchor. Only the ``backfill`` block, the
    archive totals and the integrity state are refreshed from the index the
    backfill committed, so status.json and index.json stay consistent.
    """
    status = dict(previous or {"schema": SCHEMA_STATUS})
    filled_set = set(filled)
    status["backfill"] = {
        "last_run_utc": iso_utc(now),
        "outcome": stopped if stopped in BACKFILL_OK else "stopped",
        "stopped_reason": stopped,
        "error": error or None,
        "planned_days": list(planned),
        "filled_days": list(filled),
        "remaining_unknown_days": [d for d in planned if d not in filled_set],
        "note": ("Backfilled dates carry the examined 10-minute slots of a fresh whole-day EUMETSAT search; "
                 "dates not filled stay never searched (unknown, not zero)."),
    }
    if index:
        archive = dict(status.get("archive") or {})
        archive["archived_observations"] = index.get("archived_observations")
        archive["index_generated_utc"] = index.get("generated_utc")
        status["archive"] = archive
        integ = index.get("integrity") or {}
        lost = sorted(str(e.get("date")) for e in integ.get("lost_days") or [] if isinstance(e, dict))
        state = "shard_lost" if lost else "ok"
        status["integrity"] = {
            "state": state, "alert": integ.get("alert") if lost else None, "lost_days": lost,
            "quarantined_observations": sum(int(e.get("quarantined_observations") or 0)
                                            for e in integ.get("lost_days") or [] if isinstance(e, dict)),
            "source": "backfill",
        }
        freshness = status.get("freshness")
        if freshness is not None:
            status["health"] = "degraded" if state == "shard_lost" and freshness in ("ok", "warn") else freshness
    return status


def _lines(path: Path | None) -> list[str]:
    if not path or not Path(path).exists():
        return []
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


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
    write.add_argument("--trigger", default=None, help="github.event_name (schedule | workflow_dispatch)")
    write.add_argument("--run-id", default=None)
    write.add_argument("--run-created", default=None, help="When GitHub created this run (UTC ISO); empty = unknown")
    write.add_argument("--job-started", default=None, help="When the collect job started (UTC ISO)")
    due = sub.add_parser("due", help="Print due=true|false: may a scheduled fallback run skip?")
    due.add_argument("--status", type=Path, default=Path("data/status.json"))
    due.add_argument("--event", required=True)
    due.add_argument("--min-gap-minutes", type=int, default=DUE_MIN_GAP_MINUTES)
    due.add_argument("--github-output", type=Path, default=None)
    due.add_argument("--now", default=None)
    alert = sub.add_parser("alert", help="Print the integrity alert and exit 1 while any shard_lost date exists")
    alert.add_argument("--status", type=Path, default=Path("data/status.json"))
    backfill = sub.add_parser("backfill", help="Record a backfill run; the hourly heartbeat is left untouched")
    backfill.add_argument("--status", type=Path, default=Path("data/status.json"))
    backfill.add_argument("--plan", type=Path, required=True, help="Planned dates, one per line (newest first)")
    backfill.add_argument("--filled", type=Path, required=True, help="Dates actually backfilled, one per line")
    backfill.add_argument("--stopped", required=True, help="completed | time_budget | eumetsat_error | archive_refused")
    backfill.add_argument("--error", default="")
    backfill.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    backfill.add_argument("--now", default=None)
    args = parser.parse_args(argv)
    if args.command == "backfill":
        now = parse_utc(args.now) if args.now else datetime.now(timezone.utc)
        status = backfill_status(_load(args.status), now=now, planned=_lines(args.plan), filled=_lines(args.filled),
                                 stopped=args.stopped, error=args.error, index=_load(args.archive_dir / "index.json"))
        write_status(args.status, status)
        block = status["backfill"]
        print(f"backfill: {block['outcome']} filled={len(block['filled_days'])} "
              f"remaining_unknown={len(block['remaining_unknown_days'])} integrity={status.get('integrity', {}).get('state')}")
        return 0
    if args.command == "due":
        now = parse_utc(args.now) if args.now else datetime.now(timezone.utc)
        ok, reason = is_due(_load(args.status), now=now, event=args.event, min_gap_minutes=args.min_gap_minutes)
        print(f"due={'true' if ok else 'false'}: {reason}")
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as fh:
                fh.write(f"due={'true' if ok else 'false'}\n")
        return 0
    if args.command == "alert":
        current = _load(args.status) or {}
        integrity = current.get("integrity") or {}
        if integrity.get("state") == "shard_lost":
            print(f"::error::{integrity.get('alert') or 'INTEGRITY ALERT: shard_lost'}")
            return 1
        print(f"integrity={integrity.get('state', 'unknown')}")
        return 0
    now = parse_utc(args.now) if args.now else datetime.now(timezone.utc)
    status = build_status(
        now=now, previous=_load(args.status), record=_load(args.run_record),
        archive_result=_load(args.archive_result), collect_outcome=args.collect_outcome,
        archive_outcome=args.archive_outcome, trigger=args.trigger or None, run_id=args.run_id or None,
        run_created_utc=args.run_created or None, job_started_utc=args.job_started or None,
    )
    write_status(args.status, status)
    print(f"status: {status['last_attempt_outcome']} health={status['health']} "
          f"last_successful_scan_end={status['last_successful_scan_end_utc']} "
          f"consecutive_failures={status['consecutive_failures']} archive={status['archive']['outcome']} "
          f"integrity={status['integrity']['state']}")
    print(status["heartbeat"]["explanation"])
    if status["integrity"]["state"] == "shard_lost":
        print(status["integrity"]["alert"], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

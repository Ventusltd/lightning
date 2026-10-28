#!/usr/bin/env python3
"""Parallel backfill of never-searched UTC dates (.github/workflows/backfill-parallel-lightning.yml).

The sequential backfill searches about 10 dates per run (one whole-day
EUMETSAT search takes about 18.5 minutes). This splits the work:

1. ``plan``      never-searched dates of the committed index, newest first
                 (``history.backfill_plan``), and a GitHub Actions matrix of at
                 most ``MAX_SHARDS`` round-robin date shards (EUMETSAT fair use).
2. ``shard``     one matrix job: ``collect.py --rebuild-day D`` for each of its
                 dates, newest first. It writes ONLY run records (never the feed,
                 status or archive) plus a ``shard-K.json`` summary, both uploaded
                 as an artifact. The first EUMETSAT error (rate limit, outage, no
                 products, a date with too many unreadable products) stops
                 the shard; later dates are never attempted. One unreadable
                 product only leaves its 10-minute slot unknown.
3. ``finalize``  one job: ingests every record sequentially with the gated
                 ``history.backfill_day`` (staging copy, verify), writes the
                 ``backfill`` status block and leaves one commit to the workflow.
                 Dates without a verified record stay never searched: unknown,
                 not zero.
4. ``wait-hourly`` best-effort wait while the hourly collector is running, so
                 the short finalize commit does not race its archive push.

Nothing here talks to EUMETSAT directly; ``shard`` runs collect.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import history
import status as status_mod

MAX_SHARDS = 6  # EUMETSAT fair use: never more than six concurrent whole-day searches
SHARD_SCHEMA = "uk-lightning-backfill-shard.v1"
# Severity of a stop, worst last. Only completed / time_budget are "green".
STOP_ORDER = ("completed", "time_budget", "cancelled", "shard_missing", "eumetsat_error", "archive_refused")
HOURLY_ACTIVE = ("in_progress", "queued", "requested", "waiting", "pending")
# No new date starts after SHARD_BUDGET_MINUTES (about 18.5 min per whole-day
# search, so a date started just before the budget ends near budget + 20).
DEFAULT_SHARD_BUDGET_MINUTES = 170
# A date still running this many minutes after the budget is killed (left
# unknown) so the shard always uploads its records inside the job timeout (200).
HARD_STOP_GRACE_MINUTES = 20
# No single whole-day search may run longer than this (2.4x the usual 18.5 min).
DATE_TIMEOUT_MINUTES = 45


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json_atomic(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_lines(path: Path) -> list[str]:
    if not Path(path).exists():
        return []
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def worst(stops) -> str:
    stops = [s for s in stops if s] or ["completed"]
    return max(stops, key=lambda s: STOP_ORDER.index(s) if s in STOP_ORDER else len(STOP_ORDER))


# --- 1. plan ---------------------------------------------------------------

def split_shards(dates: list[str], shards: int) -> list[dict]:
    """Round-robin: date i goes to shard (i mod n) + 1, each shard newest first.

    Round-robin (not contiguous blocks) keeps every shard working on similar
    dates, so a time-budget stop leaves the newest dates filled and a stop at an
    EUMETSAT retention edge is reached by all shards at about the same time.
    Empty shards are dropped. ``shards`` is capped at MAX_SHARDS.
    """
    if not 1 <= shards <= MAX_SHARDS:
        raise ValueError(f"shards must be 1..{MAX_SHARDS} (EUMETSAT fair use)")
    groups = [dates[k::shards] for k in range(shards)]
    return [{"shard": k + 1, "dates": " ".join(g)} for k, g in enumerate(groups) if g]


def plan(archive_dir: Path, *, end_date: str, days: int, shards: int, now: datetime) -> tuple[list[str], list[dict]]:
    today = now.date()
    end = date.fromisoformat(end_date.strip()) if end_date.strip() else today - timedelta(days=1)
    dates = history.backfill_plan(archive_dir, end_date=end, days=days, today=today,
                                  max_days=history.BACKFILL_PARALLEL_MAX_DAYS)
    return dates, split_shards(dates, shards)


# --- 2. shard --------------------------------------------------------------

class Cancelled(BaseException):  # not caught by the per-date "except Exception"
    pass


def _on_term(signum, frame):  # GitHub cancels a job with SIGINT, then SIGTERM
    raise Cancelled(f"signal {signum}")


def collect_command(day: str, record: Path) -> list[str]:
    return [sys.executable, "collect.py", "--rebuild-day", day, "--run-record", str(record)]


def collect_subprocess(day: str, record: Path, log: Path, timeout: float | None = None) -> None:
    """Run the real ``collect.py --rebuild-day``; raise RuntimeError on any failure.

    ``timeout`` (seconds) is a wall-clock limit: the child is killed and
    ``subprocess.TimeoutExpired`` propagates (the date stays unknown).
    """
    with open(log, "w", encoding="utf-8") as err:
        code = subprocess.call(collect_command(day, record), stdin=subprocess.DEVNULL, stderr=err, timeout=timeout)
    text = Path(log).read_text(encoding="utf-8", errors="replace")
    sys.stderr.write(text)
    if code != 0:
        last = next((line for line in reversed(text.splitlines()) if line.strip()), f"exit {code}")
        raise RuntimeError(last.strip())


def run_shard(shard: int, dates: list[str], out_dir: Path, *, budget_minutes: float, stagger_seconds: float = 0,
              collector=None, clock=time.monotonic, sleep=time.sleep, now=utc_now,
              hard_stop_minutes: float | None = None, date_timeout_minutes: float = DATE_TIMEOUT_MINUTES) -> dict:
    """Search each date of one shard, newest first, writing only run records.

    A record is renamed into ``out_dir`` only after collect.py succeeded, so a
    killed or failed search never leaves a partial ``rb-D.json``. The summary is
    rewritten after every date, so even a cancelled job uploads an accurate one.

    Wall clock: no new date starts after ``budget_minutes``; a date in flight is
    killed after ``date_timeout_minutes`` or at ``hard_stop_minutes`` (default
    budget + HARD_STOP_GRACE_MINUTES) since the shard started, whichever comes
    first. A killed date stops the shard as ``time_budget`` (it stays unknown);
    the records already collected are kept and uploaded.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    collector = collector or collect_subprocess
    summary_path = out_dir / f"shard-{shard}.json"
    summary = {"schema": SHARD_SCHEMA, "shard": shard, "dates": list(dates), "collected": [],
               "stopped": "running", "error": None, "started_utc": iso(now()), "finished_utc": None,
               "budget_minutes": budget_minutes,
               "note": "Only dates listed in 'collected' have a record; every other date stays never searched."}
    write_json_atomic(summary_path, summary)
    if hard_stop_minutes is None:
        hard_stop_minutes = budget_minutes + HARD_STOP_GRACE_MINUTES
    summary["hard_stop_minutes"] = hard_stop_minutes
    summary["date_timeout_minutes"] = date_timeout_minutes
    started = clock()
    stopped, error = "completed", None
    try:
        if stagger_seconds > 0:  # spread the shards' first token/search requests (fair use)
            sleep(stagger_seconds)
        for day in dates:
            if clock() - started >= budget_minutes * 60:
                stopped = "time_budget"
                break
            tmp = out_dir / f".partial-rb-{day}.json"
            final = out_dir / f"rb-{day}.json"
            began = clock()
            limit = max(1.0, min(date_timeout_minutes * 60, hard_stop_minutes * 60 - (began - started)))
            try:
                collector(day, tmp, out_dir / f"rb-{day}.log", timeout=limit)
                record = json.loads(tmp.read_text(encoding="utf-8"))
                if record.get("rebuild_day_utc") != day or record.get("outcome") != "success":
                    raise RuntimeError(f"collect.py left no successful rebuild record for {day}")
            except subprocess.TimeoutExpired:
                tmp.unlink(missing_ok=True)
                stopped = "time_budget"
                error = (f"{day}: whole-day search killed after {limit / 60:.1f} min (wall-clock limit; "
                         f"date left unknown, not zero)")
                print(f"shard {shard}: {error}", file=sys.stderr)
                break
            except Exception as exc:  # noqa: BLE001 - every EUMETSAT failure stops the shard
                tmp.unlink(missing_ok=True)
                stopped, error = "eumetsat_error", f"{day}: {exc}"
                break
            os.replace(tmp, final)
            summary["collected"].append({"date": day, "record": final.name, "sha256": sha256_file(final),
                                         "bytes": final.stat().st_size, "seconds": round(clock() - began, 1)})
            write_json_atomic(summary_path, summary)
    except (Cancelled, KeyboardInterrupt) as exc:
        for leftover in out_dir.glob(".partial-rb-*.json"):
            leftover.unlink(missing_ok=True)
        stopped, error = "cancelled", f"shard {shard} cancelled ({exc or 'interrupt'})"
    summary.update(stopped=stopped, error=error, finished_utc=iso(now()))
    write_json_atomic(summary_path, summary)
    return summary


# --- 3. finalize -----------------------------------------------------------

def finalize(*, plan_dates: list[str], shards: int, records_dir: Path, archive_dir: Path, status_path: Path,
             now: datetime) -> dict:
    """Ingest all shard records into the archive, one date at a time, newest first.

    Gates beyond ``history.backfill_day``'s own: the record must be listed in its
    shard's summary with the same sha256 (an artifact that changed on the way is
    refused). A date the index no longer marks never searched (filled by another
    backfill, or out of retention) is skipped, not refused, so a re-ingest on a
    newer main after a rejected push is idempotent. A refused date (a gate of
    ``history.backfill_day``, a record that changed in transit, or any other
    error while ingesting it) is skipped ONLY: it stays never searched (unknown,
    not zero), its reason is recorded in ``refused_days`` and the stop becomes
    ``archive_refused``, while every other date is still ingested and committed.
    ``backfill_day`` stages its writes, so a refused date changes nothing.
    """
    records_dir, archive_dir = Path(records_dir), Path(archive_dir)
    expected = split_shards(plan_dates, shards) if plan_dates else []
    listed: dict[str, dict] = {}
    stops, errors = [], []
    for entry in expected:
        path = records_dir / f"shard-{entry['shard']}.json"
        if not path.exists():
            stops.append("shard_missing")
            errors.append(f"shard {entry['shard']}: no summary artifact ({entry['dates'].split()[0]}...)")
            continue
        summary = json.loads(path.read_text(encoding="utf-8"))
        if summary.get("schema") != SHARD_SCHEMA or summary.get("dates") != entry["dates"].split():
            stops.append("archive_refused")
            errors.append(f"shard {entry['shard']}: summary does not match the plan")
            continue
        stop = summary.get("stopped")
        stops.append("cancelled" if stop == "running" else stop)
        if summary.get("error"):
            errors.append(f"shard {entry['shard']} {summary['error']}")
        for item in summary.get("collected") or []:
            listed[item["date"]] = item

    filled, skipped = [], []
    unparsed_by_day: dict[str, int] = {}
    retries_by_day: dict[str, dict] = {}
    outside_by_day: dict[str, int] = {}
    refused: dict[str, str] = {}
    for day in plan_dates:
        item = listed.get(day)
        if item is None:
            continue
        record = records_dir / f"rb-{day}.json"
        if not record.exists() or sha256_file(record) != item.get("sha256"):
            refused[day] = "record missing or sha256 differs from its shard summary"
            print(f"REFUSED {day}: {refused[day]} (date left unknown; continuing)", file=sys.stderr)
            continue
        index = json.loads((archive_dir / "index.json").read_text(encoding="utf-8"))
        row = next((r for r in index.get("days") or [] if r.get("date") == day), None)
        lost = {str(e.get("date")) for e in (index.get("integrity") or {}).get("lost_days") or [] if isinstance(e, dict)}
        if not history._never_searched(row, lost):
            skipped.append(day)
            continue
        try:
            result = history.backfill_day(archive_dir, day, now=now, rebuild_record=record)
        except Exception as exc:  # noqa: BLE001 - one date must never cost the whole run
            refused[day] = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
            print(f"REFUSED {day}: {refused[day]} (date left unknown; continuing)", file=sys.stderr)
            continue
        row = result["row"]
        record_obj = json.loads(record.read_text(encoding="utf-8"))
        unparsed = int(record_obj.get("products_unparsed") or 0)
        if unparsed:
            unparsed_by_day[day] = unparsed
        retry = status_mod.retry_counts(record_obj.get("download_retries"))
        if retry and (retry.get("retries") or retry.get("products_failed")):
            retries_by_day[day] = retry
        if result.get("outside_day"):
            outside_by_day[day] = int(result["outside_day"])
        print(f"Backfilled {day}: coverage {row['coverage']} ({row['scanned_seconds']} s searched), count {row['count']}, "
              f"{len(result['unscanned_ranges'])} unsearched gap(s) left unknown, {unparsed} unparsed product(s).",
              file=sys.stderr)
        filled.append(day)
    if refused:
        stops.append("archive_refused")
        errors[:0] = [f"{day}: {reason}" for day, reason in refused.items()]
    stopped = worst(stops)
    error = "; ".join(errors)[:2000]
    checked = history.verify_archive(archive_dir)
    index = json.loads((archive_dir / "index.json").read_text(encoding="utf-8"))
    new_status = status_mod.backfill_status(status_mod._load(status_path), now=now, planned=plan_dates,
                                            filled=filled, stopped=stopped, error=error, index=index,
                                            unparsed_by_day=unparsed_by_day, retries_by_day=retries_by_day)
    new_status["backfill"].update(mode="parallel", shards=len(expected), skipped_already_searched_days=skipped,
                                  refused_days=dict(refused), outside_day_by_day=outside_by_day)
    status_mod.write_status(status_path, new_status)
    return {"stopped": stopped, "error": error, "filled": filled, "skipped": skipped, "refused": dict(refused),
            "remaining_unknown": new_status["backfill"]["remaining_unknown_days"], "verify": checked}


# --- 4. wait-hourly ----------------------------------------------------------

def hourly_active(gh: str, repo: str, workflow: str) -> int | None:
    """Number of active runs of the hourly workflow, or None when gh cannot tell."""
    try:
        out = subprocess.run([gh, "run", "list", "-R", repo, "--workflow", workflow, "-L", "5", "--json", "status"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None
        return sum(1 for run in json.loads(out.stdout or "[]") if run.get("status") in HOURLY_ACTIVE)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def wait_for_hourly(gh: str, repo: str, workflow: str, *, max_minutes: float, poll_seconds: float = 30,
                    clock=time.monotonic, sleep=time.sleep) -> str:
    """Wait (bounded) while the hourly collector runs. Never blocks it: it only delays us."""
    started = clock()
    while True:
        active = hourly_active(gh, repo, workflow)
        if active is None:
            return "unknown"
        if active == 0:
            return "idle"
        if clock() - started >= max_minutes * 60:
            return "timeout"
        print(f"Hourly collector active ({active} run(s)); waiting {poll_seconds:.0f} s", file=sys.stderr)
        sleep(poll_seconds)


# --- CLI ---------------------------------------------------------------------

def _github_output(pairs: dict) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    lines = []
    for key, value in pairs.items():
        value = str(value)
        if "\n" in value:
            lines += [f"{key}<<BACKFILL_EOF", value, "BACKFILL_EOF"]
        else:
            lines.append(f"{key}={value}")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="Plan never-searched dates and the shard matrix")
    p.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    p.add_argument("--end-date", default="")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--shards", type=int, default=MAX_SHARDS)
    p.add_argument("--plan-out", type=Path, required=True)
    p.add_argument("--now", default=None)
    s = sub.add_parser("shard", help="Search one shard's dates (run records only)")
    s.add_argument("--shard", type=int, required=True)
    s.add_argument("--dates", required=True, help="Space-separated UTC dates, newest first")
    s.add_argument("--out-dir", type=Path, required=True)
    s.add_argument("--budget-minutes", type=float, default=DEFAULT_SHARD_BUDGET_MINUTES,
                   help="No new date starts after this many minutes")
    s.add_argument("--hard-stop-minutes", type=float, default=None,
                   help="Kill a date still running at this many minutes (default budget + 20)")
    s.add_argument("--date-timeout-minutes", type=float, default=DATE_TIMEOUT_MINUTES,
                   help="Kill a single whole-day search after this many minutes")
    s.add_argument("--stagger-step-seconds", type=float, default=20,
                   help="Shard K pauses step x (K - 1) seconds before its first search (fair use)")
    f = sub.add_parser("finalize", help="Ingest all shard records, write status, verify")
    f.add_argument("--plan", type=Path, required=True)
    f.add_argument("--shards", type=int, required=True)
    f.add_argument("--records-dir", type=Path, required=True)
    f.add_argument("--archive-dir", type=Path, default=Path("data/history"))
    f.add_argument("--status", type=Path, default=Path("data/status.json"))
    f.add_argument("--filled-out", type=Path, default=None)
    f.add_argument("--now", default=None)
    w = sub.add_parser("wait-hourly", help="Bounded wait while the hourly collector workflow is active")
    w.add_argument("--repo", required=True)
    w.add_argument("--workflow", default="update-lightning.yml")
    w.add_argument("--max-minutes", type=float, default=20)
    w.add_argument("--poll-seconds", type=float, default=30)
    w.add_argument("--gh", default="gh")
    args = parser.parse_args(argv)
    now = history.parse_utc(args.now) if getattr(args, "now", None) else utc_now()

    if args.command == "plan":
        dates, matrix = plan(args.archive_dir, end_date=args.end_date, days=args.days, shards=args.shards, now=now)
        args.plan_out.parent.mkdir(parents=True, exist_ok=True)
        args.plan_out.write_text("".join(d + "\n" for d in dates), encoding="utf-8")
        print(f"{len(dates)} never-searched UTC date(s) in {len(matrix)} shard(s):", file=sys.stderr)
        for entry in matrix:
            print(f"  shard {entry['shard']}: {entry['dates']}", file=sys.stderr)
        # GitHub rejects an empty matrix even on a job its ``if`` skips: give it a
        # placeholder that the shard job's ``count != '0'`` condition never runs.
        include = matrix or [{"shard": 0, "dates": ""}]
        _github_output({"count": len(dates), "shards": len(matrix),
                        "matrix": json.dumps({"include": include}, separators=(",", ":"))})
        return 0
    if args.command == "shard":
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
        stagger = max(0.0, args.stagger_step_seconds) * (args.shard - 1)
        summary = run_shard(args.shard, args.dates.split(), args.out_dir, budget_minutes=args.budget_minutes,
                            stagger_seconds=stagger, hard_stop_minutes=args.hard_stop_minutes,
                            date_timeout_minutes=args.date_timeout_minutes)
        print(f"shard {args.shard}: {summary['stopped']}, {len(summary['collected'])}/{len(summary['dates'])} "
              f"date(s) searched{'; ' + summary['error'] if summary['error'] else ''}", file=sys.stderr)
        _github_output({"stopped": summary["stopped"], "collected": len(summary["collected"]),
                        "error": summary["error"] or ""})
        return 0
    if args.command == "finalize":
        result = finalize(plan_dates=read_lines(args.plan), shards=args.shards, records_dir=args.records_dir,
                          archive_dir=args.archive_dir, status_path=args.status, now=now)
        if args.filled_out:
            args.filled_out.write_text("".join(d + "\n" for d in result["filled"]), encoding="utf-8")
        print(f"finalize: {result['stopped']} filled={len(result['filled'])} skipped={len(result['skipped'])} "
              f"refused={len(result['refused'])} "
              f"remaining_unknown={len(result['remaining_unknown'])}", file=sys.stderr)
        _github_output({"stopped": result["stopped"], "filled": len(result["filled"]),
                        "range": ",".join(result["filled"]), "error": result["error"]})
        return 0
    state = wait_for_hourly(args.gh, args.repo, args.workflow, max_minutes=args.max_minutes,
                            poll_seconds=args.poll_seconds)
    print(f"hourly collector: {state}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(f"BACKFILL FAILURE: {error}", file=sys.stderr)
        raise SystemExit(2)

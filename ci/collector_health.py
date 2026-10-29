#!/usr/bin/env python3
"""Collector health: open, update or close the "collector-stale" GitHub issue.

Reads the heartbeat that ``status.py write`` puts in ``data/status.json`` after
every hourly run and decides one of:

  ok        the heartbeat is on time and the collector is not failing
  late      now is past ``heartbeat.late_after_utc`` (no run for 90 min) but
            within the grace period: reported, no issue (GitHub's scheduler
            misses single slots every day; see docs/OPERATIONS.md)
  stale     now is past ``late_after_utc`` + grace (default 90 min, so no
            heartbeat for 3 h), or status.json is missing or unreadable:
            neither the cron nor the hourly nudge has started a run
  failing   runs happen but do not succeed: ``consecutive_failures`` >= 3, or
            ``freshness`` is stale/stopped/never (for example rejected
            EUMETSAT credentials)

``stale`` and ``failing`` open (or update) ONE open issue labelled
``collector-stale``; ``ok`` and ``late`` close it with a recovery comment.

``--raw`` adds the delivery probe used by the scheduled watchdog: the public
map reads raw.githubusercontent.com/Ventusltd/lightning/main/data/..., which
has no SLA. When that path fails, or serves a status.json more than an hour
older than the committed one, ONE issue labelled ``feed-delivery`` is opened;
it is closed when the probe passes again.

Standard library only (the watchdog runs it without setup-python or pip).
Exit 0 unless ``--fail-when-unhealthy`` is given and a check is unhealthy, or
the GitHub API cannot be reached with ``--issue`` (exit 2).

  python ci/collector_health.py --status data/status.json                 # report only
  python ci/collector_health.py --status data/status.json --issue         # + sync the issue
  python ci/collector_health.py --status data/status.json --issue --raw --fail-when-unhealthy
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STALE_LABEL = "collector-stale"
DELIVERY_LABEL = "feed-delivery"
GRACE_MINUTES = 90          # late_after_utc (attempt + 90 min) + 90 min = no heartbeat for 3 h
LATE_AFTER_FALLBACK_MINUTES = 90  # status.py LATE_AFTER_MINUTES, for a heartbeat block that lacks it
MAX_FAILURES = 3
RAW_BASE = "https://raw.githubusercontent.com/Ventusltd/lightning/main/"
RAW_LAG_MINUTES = 60
MARKER = "<!-- collector-health:{label}:state={state} -->"
UNHEALTHY = ("stale", "failing")
RUNBOOK = "docs/OPERATIONS.md"


def parse_utc(value) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def load_status(path: Path) -> dict | None:
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def evaluate(status: dict | None, *, now: datetime, grace_minutes: int = GRACE_MINUTES,
             max_failures: int = MAX_FAILURES) -> dict:
    """The collector verdict from status.json alone (pure: no clock, no network)."""
    if not status:
        return {"state": "stale", "reasons": ["data/status.json is missing or unreadable"],
                "late_after_utc": None, "stale_after_utc": None, "last_attempt_utc": None,
                "last_attempt_outcome": None, "consecutive_failures": None, "freshness": None,
                "last_successful_scan_end_utc": None, "last_error": None, "minutes_since_attempt": None}
    heartbeat = status.get("heartbeat") if isinstance(status.get("heartbeat"), dict) else {}
    attempt = parse_utc(status.get("last_attempt_utc"))
    late_after = parse_utc(heartbeat.get("late_after_utc"))
    if late_after is None and attempt is not None:
        late_after = attempt + timedelta(minutes=LATE_AFTER_FALLBACK_MINUTES)
    stale_after = late_after + timedelta(minutes=grace_minutes) if late_after else None
    failures = status.get("consecutive_failures")
    try:
        failures = int(failures or 0)
    except (TypeError, ValueError):
        failures = 0
    freshness = status.get("freshness")
    last_end = status.get("last_successful_scan_end_utc")
    error = status.get("last_error")
    reasons: list[str] = []
    state = "ok"
    if late_after is None:
        state = "stale"
        reasons.append("status.json has no last_attempt_utc or heartbeat.late_after_utc")
    elif now > stale_after:
        state = "stale"
        reasons.append(f"no collector heartbeat since {iso(attempt) or 'unknown'}: now is past "
                       f"late_after_utc {iso(late_after)} + {grace_minutes} min grace. Neither the GitHub cron "
                       "nor the hourly nudge has started a run")
    if failures >= max_failures or freshness in ("stale", "stopped", "never"):
        if state == "ok":
            state = "failing"
        if failures >= max_failures:
            reasons.append(f"{failures} consecutive failed collector runs (threshold {max_failures})"
                           + (f"; last error: {str(error)[:300]}" if error else ""))
        if freshness in ("stale", "stopped", "never"):
            reasons.append(f"freshness={freshness}: last successful scan end {last_end or 'never'}")
    if state == "ok" and now > late_after:
        state = "late"
        reasons.append(f"heartbeat late: now is past late_after_utc {iso(late_after)} but within the "
                       f"{grace_minutes} min grace (a single dropped cron slot)")
    return {
        "state": state,
        "reasons": reasons,
        "late_after_utc": iso(late_after),
        "stale_after_utc": iso(stale_after),
        "last_attempt_utc": iso(attempt),
        "last_attempt_outcome": status.get("last_attempt_outcome"),
        "consecutive_failures": failures,
        "freshness": freshness,
        "last_successful_scan_end_utc": last_end,
        "last_error": error,
        "minutes_since_attempt": round((now - attempt).total_seconds() / 60, 1) if attempt else None,
    }


# --- delivery probe (raw.githubusercontent.com) --------------------------------

def _fetch(url: str, *, method: str = "GET", timeout: int = 20, attempts: int = 3) -> tuple[int | None, bytes, str]:
    last = ""
    for attempt in range(attempts):
        req = urllib.request.Request(url, method=method, headers={"Cache-Control": "no-cache",
                                                                  "User-Agent": "lightning-collector-health"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read() if method == "GET" else b"", ""
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code < 500 and exc.code != 429:
                return exc.code, b"", last
        except (urllib.error.URLError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < attempts:
            time.sleep(5 * (attempt + 1))
    return None, b"", last


def probe_raw(committed: dict | None, *, base: str = RAW_BASE, fetch=_fetch,
              lag_minutes: int = RAW_LAG_MINUTES) -> dict:
    """Is the public raw path serving the data the map reads, and not far behind main?"""
    reasons: list[str] = []
    stamp = int(time.time())
    code, body, err = fetch(f"{base}data/status.json?health={stamp}")
    served = None
    if code != 200:
        reasons.append(f"data/status.json: {err or f'HTTP {code}'}")
    else:
        try:
            served = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            reasons.append("data/status.json: served bytes are not JSON")
    for rel in ("data/uk_lightning.geojson", "data/history/index.json"):
        code, _, err = fetch(f"{base}{rel}?health={stamp}", method="HEAD")
        if code != 200:
            reasons.append(f"{rel}: {err or f'HTTP {code}'}")
    if isinstance(served, dict) and committed:
        mine, theirs = parse_utc(committed.get("last_attempt_utc")), parse_utc(served.get("last_attempt_utc"))
        if mine and theirs and (mine - theirs) > timedelta(minutes=lag_minutes):
            reasons.append(f"raw serves a status.json from {iso(theirs)}, {round((mine - theirs).total_seconds() / 60)} "
                           f"min behind main ({iso(mine)})")
    return {"state": "failing" if reasons else "ok", "reasons": reasons, "base": base}


# --- GitHub issues ------------------------------------------------------------

class GitHub:
    """The four REST calls the issue sync needs (urllib, GITHUB_TOKEN/GH_TOKEN)."""

    def __init__(self, repo: str, token: str, api: str = "https://api.github.com"):
        self.repo, self.token, self.api = repo, token, api.rstrip("/")

    def call(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"{self.api}/repos/{self.repo}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "lightning-collector-health"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def open_issues(self, label: str) -> list[dict]:
        rows = self.call("GET", f"/issues?state=open&labels={label}&per_page=100&sort=created&direction=asc") or []
        return [row for row in rows if "pull_request" not in row]

    def ensure_label(self, label: str, color: str, description: str) -> None:
        try:
            self.call("POST", "/labels", {"name": label, "color": color, "description": description})
        except urllib.error.HTTPError as exc:
            if exc.code != 422:  # 422 = already exists
                raise

    def create_issue(self, title: str, body: str, label: str) -> dict:
        return self.call("POST", "/issues", {"title": title, "body": body, "labels": [label]})

    def update_issue(self, number: int, **fields) -> dict:
        return self.call("PATCH", f"/issues/{number}", fields)

    def comment(self, number: int, body: str) -> None:
        self.call("POST", f"/issues/{number}/comments", {"body": body})


LABELS = {
    STALE_LABEL: ("b60205", "Lightning collector: no heartbeat past late_after_utc + grace, or failing runs"),
    DELIVERY_LABEL: ("d93f0b", "Lightning data: raw.githubusercontent.com path failing or far behind main"),
}


def sync_issue(client, *, label: str, unhealthy: bool, state: str, title: str, body: str,
               recovery: str) -> str:
    """One open issue per label: open or refresh it while unhealthy, close it on recovery.

    The body is replaced on every unhealthy check (no notification); a comment
    is added only when the state changes (for example stale -> failing), so a
    long outage does not post one comment per check. Duplicates left by two
    checks racing are closed onto the oldest issue.
    """
    marker = MARKER.format(label=label, state=state)
    issues = client.open_issues(label)
    if not unhealthy:
        for issue in issues:
            client.comment(issue["number"], recovery)
            client.update_issue(issue["number"], state="closed", state_reason="completed")
        return f"closed #{', #'.join(str(i['number']) for i in issues)}" if issues else "none open; nothing to do"
    full = f"{marker}\n{body}"
    if not issues:
        color, description = LABELS.get(label, ("ededed", ""))
        client.ensure_label(label, color, description)
        created = client.create_issue(title, full, label)
        return f"opened #{created['number']}"
    keep, extras = issues[0], issues[1:]
    previous = keep.get("body") or ""
    client.update_issue(keep["number"], title=title, body=full)
    action = f"updated #{keep['number']}"
    if marker not in previous:
        client.comment(keep["number"], f"State is now **{state}**.\n\n{body}")
        action += " (state changed; commented)"
    for extra in extras:
        client.comment(extra["number"], f"Duplicate of #{keep['number']} (two health checks raced).")
        client.update_issue(extra["number"], state="closed", state_reason="not_planned")
    return action


def _bullets(reasons: list[str]) -> str:
    return "\n".join(f"- {r}" for r in reasons) or "- (none)"


def stale_issue(verdict: dict, *, source: str, now: datetime) -> tuple[str, str]:
    if verdict["state"] == "stale":
        title = f"Lightning collector stale: no heartbeat since {verdict['last_attempt_utc'] or 'unknown'}"
    elif (verdict["consecutive_failures"] or 0) > 0:
        title = f"Lightning collector failing: {verdict['consecutive_failures']} consecutive failed runs"
    else:
        title = (f"Lightning collector failing: no successful scan since "
                 f"{verdict['last_successful_scan_end_utc'] or 'ever'}")
    rows = [("state", verdict["state"]), ("last attempt", verdict["last_attempt_utc"]),
            ("last attempt outcome", verdict["last_attempt_outcome"]),
            ("late_after_utc", verdict["late_after_utc"]), ("stale after (late + grace)", verdict["stale_after_utc"]),
            ("consecutive failures", verdict["consecutive_failures"]), ("freshness", verdict["freshness"]),
            ("last successful scan end", verdict["last_successful_scan_end_utc"])]
    table = "\n".join(f"| {k} | `{v}` |" for k, v in rows)
    body = (f"**{verdict['state'].upper()}**: the UK lightning archive is not being kept current.\n\n"
            f"{_bullets(verdict['reasons'])}\n\n| field | value |\n|---|---|\n{table}\n\n"
            f"Flashes in the gap are unknown, not zero. What to do: see `{RUNBOOK}` (\"collector-stale\"). "
            "The next successful hourly run closes this issue automatically.\n\n"
            f"_Checked {iso(now)} by {source}._")
    return title, body


def delivery_issue(probe: dict, *, source: str, now: datetime) -> tuple[str, str]:
    title = "Lightning data delivery failing: raw.githubusercontent.com"
    body = (f"The public map reads `{probe['base']}data/...`. The delivery probe failed:\n\n"
            f"{_bullets(probe['reasons'])}\n\nThe committed data on main may be fine; this is the public path. "
            f"What to do: see `{RUNBOOK}` (\"feed-delivery\"). Closed automatically when the probe passes.\n\n"
            f"_Checked {iso(now)} by {source}._")
    return title, body


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", type=Path, default=Path("data/status.json"))
    parser.add_argument("--now", default=None, help="UTC ISO time to evaluate at (tests); default: the clock")
    parser.add_argument("--grace-minutes", type=int, default=GRACE_MINUTES)
    parser.add_argument("--max-failures", type=int, default=MAX_FAILURES)
    parser.add_argument("--issue", action="store_true", help="Open/update/close the GitHub issue(s)")
    parser.add_argument("--raw", action="store_true", help="Also probe raw.githubusercontent.com (feed-delivery issue)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "Ventusltd/lightning"))
    parser.add_argument("--source", default="a manual check")
    parser.add_argument("--summary", type=Path, default=None, help="Append a Markdown line (GITHUB_STEP_SUMMARY)")
    parser.add_argument("--fail-when-unhealthy", action="store_true")
    args = parser.parse_args(argv)

    now = parse_utc(args.now) if args.now else datetime.now(timezone.utc)
    status = load_status(args.status)
    verdict = evaluate(status, now=now, grace_minutes=args.grace_minutes, max_failures=args.max_failures)
    unhealthy = verdict["state"] in UNHEALTHY
    print(f"collector: {verdict['state']} last_attempt={verdict['last_attempt_utc']} "
          f"late_after={verdict['late_after_utc']} stale_after={verdict['stale_after_utc']} "
          f"failures={verdict['consecutive_failures']} freshness={verdict['freshness']}")
    for reason in verdict["reasons"]:
        level = "error" if unhealthy else "warning"
        print(f"::{level} title=collector {verdict['state']}::{reason}")
    probe = probe_raw(status) if args.raw else None
    if probe:
        print(f"delivery: {probe['state']}")
        for reason in probe["reasons"]:
            print(f"::error title=feed-delivery::{reason}")

    actions = []
    if args.issue:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            print("::error::--issue needs GITHUB_TOKEN or GH_TOKEN", file=sys.stderr)
            return 2
        client = GitHub(args.repo, token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        try:
            title, body = stale_issue(verdict, source=args.source, now=now)
            recovery = (f"Recovered: collector is **{verdict['state']}** at {iso(now)} (last attempt "
                        f"{verdict['last_attempt_utc']}, outcome {verdict['last_attempt_outcome']}). Closed by {args.source}.")
            actions.append(f"{STALE_LABEL}: " + sync_issue(client, label=STALE_LABEL, unhealthy=unhealthy,
                                                           state=verdict["state"], title=title, body=body,
                                                           recovery=recovery))
            if probe:
                title, body = delivery_issue(probe, source=args.source, now=now)
                actions.append(f"{DELIVERY_LABEL}: " + sync_issue(
                    client, label=DELIVERY_LABEL, unhealthy=probe["state"] != "ok", state=probe["state"],
                    title=title, body=body, recovery=f"Recovered: the raw.githubusercontent.com probe passed at {iso(now)}."))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"::error title=collector-health issue sync::{type(exc).__name__}: {exc}")
            return 2
        for line in actions:
            print(line)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Collector health: {verdict['state']}\n\n{_bullets(verdict['reasons'])}\n\n")
            if probe:
                fh.write(f"Delivery (raw): {probe['state']}\n\n{_bullets(probe['reasons'])}\n\n")
            for line in actions:
                fh.write(f"- issue {line}\n")
    if args.fail_when_unhealthy and (unhealthy or (probe and probe["state"] != "ok")):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

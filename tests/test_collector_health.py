"""ci/collector_health.py: the collector-stale verdict, the issue sync and the workflow wiring.

No network and no GitHub: the issue sync runs against an in-memory fake of the
four REST calls, and the raw probe against a fake fetch.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))

import collector_health as ch  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def status(attempt="2026-10-29T11:31:00Z", late_after="2026-10-29T13:01:00Z", outcome="success",
           failures=0, freshness="ok", error=None):
    return {"last_attempt_utc": attempt, "last_attempt_outcome": outcome, "consecutive_failures": failures,
            "freshness": freshness, "last_error": error, "last_successful_scan_end_utc": "2026-10-29T11:20:05Z",
            "heartbeat": {"late_after_utc": late_after}}


class FakeGitHub:
    def __init__(self, issues=None):
        self.issues = {i["number"]: dict(i) for i in (issues or [])}
        self.comments, self.labels, self.next = [], [], 100

    def open_issues(self, label):
        return [dict(i) for n, i in sorted(self.issues.items())
                if i.get("state", "open") == "open" and label in i.get("labels", [])]

    def ensure_label(self, label, color, description):
        self.labels.append(label)

    def create_issue(self, title, body, label):
        self.next += 1
        self.issues[self.next] = {"number": self.next, "title": title, "body": body, "labels": [label], "state": "open"}
        return self.issues[self.next]

    def update_issue(self, number, **fields):
        self.issues[number].update(fields)
        return self.issues[number]

    def comment(self, number, body):
        self.comments.append((number, body))


class EvaluateTests(unittest.TestCase):
    def test_on_time_heartbeat_is_ok(self):
        self.assertEqual(ch.evaluate(status(), now=NOW)["state"], "ok")

    def test_past_late_after_but_within_grace_is_late_not_an_issue(self):
        v = ch.evaluate(status(), now=datetime(2026, 10, 29, 13, 30, tzinfo=timezone.utc))
        self.assertEqual(v["state"], "late")
        self.assertNotIn(v["state"], ch.UNHEALTHY)

    def test_past_late_after_plus_grace_is_stale(self):
        v = ch.evaluate(status(), now=datetime(2026, 10, 29, 14, 32, tzinfo=timezone.utc))
        self.assertEqual(v["state"], "stale")
        self.assertEqual(v["stale_after_utc"], "2026-10-29T14:31:00Z")
        self.assertIn("late_after_utc 2026-10-29T13:01:00Z", v["reasons"][0])

    def test_grace_zero_alerts_exactly_at_late_after(self):
        v = ch.evaluate(status(), now=datetime(2026, 10, 29, 13, 2, tzinfo=timezone.utc), grace_minutes=0)
        self.assertEqual(v["state"], "stale")

    def test_missing_or_unreadable_status_is_stale(self):
        self.assertEqual(ch.evaluate(None, now=NOW)["state"], "stale")
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "status.json"
            bad.write_text("{not json")
            self.assertIsNone(ch.load_status(bad))

    def test_heartbeat_block_missing_falls_back_to_attempt_plus_90_min(self):
        s = status()
        del s["heartbeat"]
        self.assertEqual(ch.evaluate(s, now=NOW)["late_after_utc"], "2026-10-29T13:01:00Z")

    def test_repeated_failures_are_failing_even_with_a_fresh_heartbeat(self):
        v = ch.evaluate(status(outcome="failure", failures=3, error="401 invalid_client"), now=NOW)
        self.assertEqual(v["state"], "failing")
        self.assertIn("401 invalid_client", v["reasons"][0])
        self.assertEqual(ch.evaluate(status(outcome="failure", failures=2), now=NOW)["state"], "ok")

    def test_stale_freshness_is_failing(self):
        self.assertEqual(ch.evaluate(status(freshness="stopped"), now=NOW)["state"], "failing")

    def test_stale_wins_over_failing_and_keeps_both_reasons(self):
        v = ch.evaluate(status(failures=5), now=datetime(2026, 10, 30, tzinfo=timezone.utc))
        self.assertEqual(v["state"], "stale")
        self.assertEqual(len(v["reasons"]), 2)

    def test_the_committed_status_json_is_readable(self):
        committed = ch.load_status(ROOT / "data" / "status.json")
        if committed is None:
            self.skipTest("no data/status.json in this checkout")
        v = ch.evaluate(committed, now=NOW)
        self.assertIsNotNone(v["late_after_utc"])


class SyncIssueTests(unittest.TestCase):
    def sync(self, gh, unhealthy, state="stale", body="b"):
        return ch.sync_issue(gh, label=ch.STALE_LABEL, unhealthy=unhealthy, state=state, title="t", body=body,
                             recovery="recovered")

    def test_opens_one_issue_with_the_label(self):
        gh = FakeGitHub()
        self.assertEqual(self.sync(gh, True), "opened #101")
        self.assertEqual(gh.labels, [ch.STALE_LABEL])
        self.assertEqual(gh.issues[101]["labels"], ["collector-stale"])
        self.assertIn("state=stale", gh.issues[101]["body"])

    def test_second_unhealthy_check_updates_without_a_comment(self):
        gh = FakeGitHub()
        self.sync(gh, True, body="first")
        self.assertEqual(self.sync(gh, True, body="second"), "updated #101")
        self.assertEqual(len(gh.issues), 1)
        self.assertTrue(gh.issues[101]["body"].endswith("second"))
        self.assertEqual(gh.comments, [])

    def test_a_state_change_comments_once(self):
        gh = FakeGitHub()
        self.sync(gh, True, state="failing")
        self.assertIn("commented", self.sync(gh, True, state="stale"))
        self.sync(gh, True, state="stale")
        self.assertEqual(len(gh.comments), 1)

    def test_recovery_closes_with_a_comment(self):
        gh = FakeGitHub()
        self.sync(gh, True)
        self.assertEqual(self.sync(gh, False, state="ok"), "closed #101")
        self.assertEqual(gh.issues[101]["state"], "closed")
        self.assertEqual(gh.comments, [(101, "recovered")])
        self.assertEqual(self.sync(gh, False, state="ok"), "none open; nothing to do")

    def test_raced_duplicates_are_closed_onto_the_oldest(self):
        gh = FakeGitHub([{"number": 7, "labels": ["collector-stale"], "body": ""},
                         {"number": 9, "labels": ["collector-stale"], "body": ""},
                         {"number": 8, "labels": ["bug"], "body": ""}])
        self.sync(gh, True)
        self.assertEqual(gh.issues[7].get("state", "open"), "open")
        self.assertEqual(gh.issues[9]["state"], "closed")
        self.assertEqual(gh.issues[8].get("state", "open"), "open")  # other labels untouched

    def test_issue_text_names_the_threshold_and_runbook(self):
        v = ch.evaluate(status(), now=datetime(2026, 10, 29, 15, tzinfo=timezone.utc))
        title, body = ch.stale_issue(v, source="test", now=NOW)
        self.assertIn("no heartbeat since 2026-10-29T11:31:00Z", title)
        self.assertIn("late_after_utc", body)
        self.assertIn("docs/OPERATIONS.md", body)


class RawProbeTests(unittest.TestCase):
    def fetcher(self, served_status, codes=None):
        codes = codes or {}

        def fetch(url, method="GET", **_):
            rel = url.split(ch.RAW_BASE, 1)[1].split("?", 1)[0]
            code = codes.get(rel, 200)
            body = json.dumps(served_status).encode() if rel == "data/status.json" and code == 200 else b""
            return code, body, "" if code == 200 else f"HTTP {code}"
        return fetch

    def test_ok_when_raw_serves_current_bytes(self):
        s = status()
        self.assertEqual(ch.probe_raw(s, fetch=self.fetcher(s))["state"], "ok")

    def test_failing_on_http_errors(self):
        s = status()
        p = ch.probe_raw(s, fetch=self.fetcher(s, {"data/history/index.json": 429}))
        self.assertEqual(p["state"], "failing")
        self.assertIn("data/history/index.json: HTTP 429", p["reasons"])

    def test_failing_when_raw_is_far_behind_main(self):
        p = ch.probe_raw(status(), fetch=self.fetcher(status(attempt="2026-10-29T08:00:00Z")))
        self.assertEqual(p["state"], "failing")
        self.assertIn("min behind main", p["reasons"][0])


class CliTests(unittest.TestCase):
    def test_report_only_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(json.dumps(status()))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(ch.main(["--status", str(path), "--now", "2026-10-29T12:00:00Z",
                                          "--fail-when-unhealthy"]), 0)
                self.assertEqual(ch.main(["--status", str(path), "--now", "2026-10-29T18:00:00Z",
                                          "--fail-when-unhealthy"]), 1)
                self.assertEqual(ch.main(["--status", str(path), "--now", "2026-10-29T18:00:00Z"]), 0)
            self.assertIn("::error title=collector stale::", out.getvalue())


class WorkflowWiringTests(unittest.TestCase):
    """The check runs inside the existing workflows and the watchdog stays tiny."""

    def test_hourly_collector_syncs_the_issue_after_its_commit_without_changing_the_outcome(self):
        text = (WORKFLOWS / "update-lightning.yml").read_text()
        self.assertIn("issues: write", text.split("\nconcurrency:", 1)[0])
        step = text.split("- name: Collector health issue (collector-stale)", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("if: always()", step)
        self.assertIn("continue-on-error: true", step)
        self.assertIn("python ci/collector_health.py", step)
        self.assertIn("--issue", step)
        order = [text.index(n) for n in ("- name: Commit archive and status (archive commit)",
                                         "- name: Collector health issue (collector-stale)",
                                         "- name: Surface collection, archive or integrity failure")]
        self.assertEqual(order, sorted(order))

    def test_data_verifier_has_a_health_job_with_issue_write_only_there(self):
        text = (WORKFLOWS / "verify-lightning-data.yml").read_text()
        top = text.split("\njobs:", 1)[0]
        self.assertNotIn("issues: write", top)
        health = text.split("\n  health:\n", 1)[1]
        self.assertIn("issues: write", health)
        self.assertIn("ci/collector_health.py", health)
        self.assertIn("--issue", health)

    def test_watchdog_is_read_only_scheduled_and_never_in_a_writer_group(self):
        text = (WORKFLOWS / "collector-watchdog.yml").read_text()
        self.assertIn("schedule:", text)
        self.assertIn("contents: read", text)
        self.assertIn("issues: write", text)
        self.assertNotIn("contents: write", text)
        for group in ("uk-lightning-update", "uk-lightning-backfill"):
            self.assertNotIn(group, text)
        self.assertIn("--fail-when-unhealthy", text)
        self.assertIn("--raw", text)
        self.assertIn("timeout-minutes: 5", text)
        self.assertNotIn("pip install", text)


if __name__ == "__main__":
    unittest.main()

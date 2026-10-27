"""status.json explains staleness: when the collector last ran, what started it, and why the gap was long.

Timings are the real 27 Oct 2026 runs of Ventusltd/lightning (gh api .../actions/runs):
00:26 scheduled run; 03:14-07:01 and 07:02-10:26 backfills in the shared group;
run 40476905747 created 08:55:15, job started 10:26:22 (91 min queued);
no run created again until 15:19:46 although the group was free from 10:43.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import status as status_module
from status import build_status, is_due, next_cron_slot

UTC = timezone.utc


def record(run_utc: str, end_utc: str) -> dict:
    return {"schema": "uk-lightning-run-record.v1", "run_utc": run_utc, "outcome": "success", "error": None,
            "products_examined": 36, "slots_parsed": True,
            "scanned_ranges": [{"start_utc": "2026-10-27T00:00:00.000Z", "end_utc": end_utc}]}


def write(previous, rec, *, now, trigger="schedule", created=None, started=None, collect="success"):
    return build_status(now=now, previous=previous, record=rec, archive_result=None, collect_outcome=collect,
                        archive_outcome="success" if collect == "success" else "skipped", trigger=trigger,
                        run_id="1", run_created_utc=created, job_started_utc=started)


PREV = {"last_attempt_utc": "2026-10-27T00:27:10.000Z", "last_attempt_outcome": "success",
        "last_successful_scan_end_utc": "2026-10-27T00:20:09.000Z"}


class HeartbeatTests(unittest.TestCase):
    def test_on_time_run(self):
        prev = {**PREV, "last_attempt_utc": "2026-10-27T14:18:00.000Z"}
        hb = write(prev, record("2026-10-27T15:20:03.957Z", "2026-10-27T15:10:09.000Z"),
                   now=datetime(2026, 10, 27, 15, 24, tzinfo=UTC), created="2026-10-27T15:19:46Z",
                   started="2026-10-27T15:19:49Z")["heartbeat"]
        self.assertEqual(hb["gap_before_this_run"]["cause"], "on_time")
        self.assertEqual(hb["this_run"]["queued_minutes"], 0.1)
        self.assertIn("on schedule", hb["explanation"])
        self.assertEqual(hb["next_expected_run_utc"], "2026-10-27T15:47:00.000Z")
        self.assertEqual(hb["late_after_utc"], "2026-10-27T16:50:03.957Z")

    def test_the_08_55_run_names_both_the_scheduler_and_the_queue(self):
        hb = write(PREV, record("2026-10-27T10:26:40.000Z", "2026-10-27T10:20:09.000Z"),
                   now=datetime(2026, 10, 27, 10, 43, tzinfo=UTC), created="2026-10-27T08:55:15Z",
                   started="2026-10-27T10:26:22Z")["heartbeat"]
        gap = hb["gap_before_this_run"]
        self.assertEqual(gap["cause"], "github_schedule_late_then_queued")
        self.assertEqual(hb["this_run"]["queued_minutes"], 91.1)
        self.assertEqual(gap["hourly_slots_without_a_run"], 7)  # 01:17..08:17 minus the one this run served
        self.assertIn("GitHub's scheduler started no run on time", hb["explanation"])
        self.assertIn("waited 1 h 31 min in the queue behind another run", hb["explanation"])

    def test_the_15_19_run_blames_the_scheduler_not_the_collector(self):
        prev = {**PREV, "last_attempt_utc": "2026-10-27T10:26:40.000Z"}
        hb = write(prev, record("2026-10-27T15:20:03.957Z", "2026-10-27T15:10:09.000Z"),
                   now=datetime(2026, 10, 27, 15, 24, tzinfo=UTC), created="2026-10-27T15:19:46Z",
                   started="2026-10-27T15:19:49Z")["heartbeat"]
        self.assertEqual(hb["gap_before_this_run"]["cause"], "github_schedule_late_or_dropped")
        self.assertEqual(hb["gap_before_this_run"]["hourly_slots_without_a_run"], 4)  # 11:17..14:17
        self.assertIn("no run for 4 h 53 min (since 27 Oct 10:26 UTC)", hb["explanation"])
        self.assertIn("not a collector or EUMETSAT failure", hb["explanation"])

    def test_queue_only(self):
        prev = {**PREV, "last_attempt_utc": "2026-10-27T07:20:00.000Z"}
        hb = write(prev, record("2026-10-27T10:26:40.000Z", "2026-10-27T10:20:09.000Z"),
                   now=datetime(2026, 10, 27, 10, 43, tzinfo=UTC), created="2026-10-27T08:17:30Z",
                   started="2026-10-27T10:26:22Z")["heartbeat"]
        self.assertEqual(hb["gap_before_this_run"]["cause"], "queued_behind_another_run")

    def test_manual_start_and_unknown_creation_time(self):
        hb = write(PREV, record("2026-10-27T02:38:30.000Z", "2026-10-27T02:30:09.000Z"),
                   now=datetime(2026, 10, 27, 3, 13, tzinfo=UTC), trigger="workflow_dispatch",
                   created="2026-10-27T02:38:11Z", started="2026-10-27T02:38:14Z")["heartbeat"]
        self.assertEqual(hb["gap_before_this_run"]["cause"], "no_scheduled_run_until_manual_start")
        self.assertIn("started manually", hb["explanation"])
        hb = write(PREV, record("2026-10-27T10:26:40.000Z", "2026-10-27T10:20:09.000Z"),
                   now=datetime(2026, 10, 27, 10, 43, tzinfo=UTC))["heartbeat"]
        self.assertEqual(hb["gap_before_this_run"]["cause"], "unknown")
        self.assertIsNone(hb["this_run"]["queued_minutes"])
        self.assertIn("cause unknown", hb["explanation"])

    def test_first_heartbeat_and_failed_run(self):
        hb = write(None, record("2026-10-27T10:26:40.000Z", "2026-10-27T10:20:09.000Z"),
                   now=datetime(2026, 10, 27, 10, 43, tzinfo=UTC))["heartbeat"]
        self.assertIsNone(hb["gap_before_this_run"])
        failed = write(PREV, None, now=datetime(2026, 10, 27, 1, 20, tzinfo=UTC), collect="failure",
                       created="2026-10-27T01:17:40Z", started="2026-10-27T01:17:44Z")
        self.assertEqual(failed["heartbeat"]["this_run"]["outcome"], "failure")
        self.assertIn("(schedule, failure)", failed["heartbeat"]["explanation"])

    def test_if_late_never_claims_zero(self):
        hb = write(PREV, record("2026-10-27T10:26:40.000Z", "2026-10-27T10:20:09.000Z"),
                   now=datetime(2026, 10, 27, 10, 43, tzinfo=UTC))["heartbeat"]
        self.assertIn("unknown, not zero", hb["if_late"])
        self.assertIn("since 27 Oct 10:26 UTC", hb["if_late"])

    def test_heartbeat_survives_a_backfill_status_write(self):
        st = write(PREV, record("2026-10-27T10:26:40.000Z", "2026-10-27T10:20:09.000Z"),
                   now=datetime(2026, 10, 27, 10, 43, tzinfo=UTC))
        after = status_module.backfill_status(st, now=datetime(2026, 10, 27, 11, tzinfo=UTC), planned=["2026-10-01"],
                                              filled=["2026-10-01"], stopped="completed", error=None, index=None)
        self.assertEqual(after["heartbeat"], st["heartbeat"])

    def test_next_cron_slot(self):
        self.assertEqual(next_cron_slot(datetime(2026, 10, 27, 10, 17, tzinfo=UTC)), datetime(2026, 10, 27, 10, 47, tzinfo=UTC))
        self.assertEqual(next_cron_slot(datetime(2026, 10, 27, 10, 50, tzinfo=UTC)), datetime(2026, 10, 27, 11, 17, tzinfo=UTC))
        self.assertEqual(next_cron_slot(datetime(2026, 10, 27, 23, 59, tzinfo=UTC)), datetime(2026, 10, 28, 0, 17, tzinfo=UTC))


class DueTests(unittest.TestCase):
    NOW = datetime(2026, 10, 27, 15, 47, 30, tzinfo=UTC)

    def test_only_a_scheduled_run_after_a_recent_success_skips(self):
        recent = {"last_attempt_utc": "2026-10-27T15:20:03.957Z", "last_attempt_outcome": "success"}
        self.assertEqual(is_due(recent, now=self.NOW, event="schedule")[0], False)
        self.assertEqual(is_due(recent, now=self.NOW, event="workflow_dispatch")[0], True)
        self.assertEqual(is_due({**recent, "last_attempt_outcome": "failure"}, now=self.NOW, event="schedule")[0], True)
        old = {**recent, "last_attempt_utc": "2026-10-27T14:20:00.000Z"}
        self.assertEqual(is_due(old, now=self.NOW, event="schedule")[0], True)
        self.assertEqual(is_due(None, now=self.NOW, event="schedule")[0], True)
        self.assertEqual(is_due({"last_attempt_utc": "garbage"}, now=self.NOW, event="schedule")[0], True)

    def test_cli_writes_the_github_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            st, out = Path(tmp) / "status.json", Path(tmp) / "out"
            st.write_text(json.dumps({"last_attempt_utc": "2026-10-27T15:20:03.957Z", "last_attempt_outcome": "success"}))
            self.assertEqual(status_module.main(["due", "--status", str(st), "--event", "schedule",
                                                 "--github-output", str(out), "--now", self.NOW.isoformat()]), 0)
            self.assertEqual(out.read_text(), "due=false\n")
            status_module.main(["due", "--status", str(Path(tmp) / "missing.json"), "--event", "schedule",
                                "--github-output", str(out), "--now", self.NOW.isoformat()])
            self.assertEqual(out.read_text(), "due=false\ndue=true\n")

    def test_cli_write_accepts_the_timing_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            st, rec = Path(tmp) / "status.json", Path(tmp) / "rec.json"
            st.write_text(json.dumps(PREV))
            rec.write_text(json.dumps(record("2026-10-27T10:26:40.000Z", "2026-10-27T10:20:09.000Z")))
            code = status_module.main(["write", "--status", str(st), "--run-record", str(rec), "--collect-outcome",
                                       "success", "--archive-outcome", "skipped", "--trigger", "schedule",
                                       "--run-id", "40476905747", "--run-created", "2026-10-27T08:55:15Z",
                                       "--job-started", "2026-10-27T10:26:22Z", "--now", "2026-10-27T10:43:00Z"])
            self.assertEqual(code, 0)
            hb = json.loads(st.read_text())["heartbeat"]
            self.assertEqual(hb["this_run"]["run_id"], "40476905747")
            self.assertEqual(hb["gap_before_this_run"]["cause"], "github_schedule_late_then_queued")
            # Empty flags (gh api unavailable) are unknown, never an error.
            code = status_module.main(["write", "--status", str(st), "--run-record", str(rec), "--collect-outcome",
                                       "success", "--run-created", "", "--job-started", "", "--now", "2026-10-27T10:43:00Z"])
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()

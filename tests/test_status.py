"""data/status.json is written after every run; failures leave a trace, never a zero."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import status as status_module
from status import build_status, health

NOW = datetime(2026, 10, 26, 14, 20, tzinfo=timezone.utc)
RECORD = {
    "schema": "uk-lightning-run-record.v1", "run_utc": "2026-10-26T14:17:00.000Z", "outcome": "success", "error": None,
    "query_start_utc": "2026-10-26T08:17:00.000Z", "query_end_utc": "2026-10-26T14:17:00.000Z", "lookback_hours": 6.0,
    "lookback_source": "status", "lookback_capped": False, "products_examined": 36, "slots_examined": 36,
    "slots_examined_utc": ["2026-10-26T08:10:09.000Z"], "scanned_ranges": [
        {"start_utc": "2026-10-26T08:17:00.000Z", "end_utc": "2026-10-26T14:10:09.000Z"}],
    "unscanned_ranges": [], "tail_unobserved_seconds": 411, "incoming_features": 3, "feed_features": 411,
    "feed_total_before_display_cap": 411, "feed_latest_detection_utc": "2026-10-26T13:58:00.000Z", "features": [],
}


class StatusTests(unittest.TestCase):
    def test_success_records_the_examined_scan_end_not_now(self):
        result = build_status(now=NOW, previous=None, record=RECORD, archive_result={
            "outcome": "success", "archived_observations": 411, "files_changed": 2, "index_generated_utc": "2026-10-26T14:18:00.000Z"},
            collect_outcome="success", archive_outcome="success")
        self.assertEqual(result["schema"], "uk-lightning-status.v1")
        self.assertEqual(result["last_attempt_outcome"], "success")
        self.assertEqual(result["consecutive_failures"], 0)
        self.assertEqual(result["last_successful_scan_end_utc"], "2026-10-26T14:10:09.000Z")
        self.assertEqual(result["last_successful_scan_start_utc"], "2026-10-26T08:17:00.000Z")
        self.assertEqual(result["health"], "ok")
        self.assertEqual(result["last_tail_unobserved_seconds"], 411)
        self.assertEqual(result["archive"]["archived_observations"], 411)
        self.assertIsNone(result["last_error"])
        self.assertIn("unknown, not zero", result["note"])

    def test_failure_keeps_previous_success_and_counts_consecutive_failures(self):
        previous = build_status(now=NOW - timedelta(hours=1), previous=None, record=RECORD, archive_result=None,
                                collect_outcome="success", archive_outcome="success")
        failed_record = {**RECORD, "run_utc": "2026-10-26T15:17:00.000Z", "outcome": "failure",
                         "error": "RuntimeError: No EUMETSAT product files returned", "scanned_ranges": [], "products_examined": 0}
        first = build_status(now=NOW + timedelta(hours=1), previous=previous, record=failed_record, archive_result=None,
                             collect_outcome="failure", archive_outcome="skipped")
        self.assertEqual(first["last_attempt_outcome"], "failure")
        self.assertEqual(first["consecutive_failures"], 1)
        self.assertIn("No EUMETSAT product files", first["last_error"])
        self.assertEqual(first["last_successful_scan_end_utc"], "2026-10-26T14:10:09.000Z")
        self.assertEqual(first["last_successful_run_utc"], "2026-10-26T14:17:00.000Z")
        self.assertIsNone(first["last_scanned_ranges"])
        second = build_status(now=NOW + timedelta(hours=2), previous=first, record=None, archive_result=None,
                              collect_outcome="failure", archive_outcome="skipped")
        self.assertEqual(second["consecutive_failures"], 2)
        self.assertIn("no run record", second["last_error"])
        self.assertEqual(second["last_successful_scan_end_utc"], "2026-10-26T14:10:09.000Z")
        recovered = build_status(now=NOW + timedelta(hours=3), previous=second,
                                 record={**RECORD, "run_utc": "2026-10-26T17:17:00.000Z", "scanned_ranges": [
                                     {"start_utc": "2026-10-26T13:40:09.000Z", "end_utc": "2026-10-26T17:10:09.000Z"}]},
                                 archive_result=None, collect_outcome="success", archive_outcome="success")
        self.assertEqual(recovered["consecutive_failures"], 0)
        self.assertEqual(recovered["last_successful_scan_end_utc"], "2026-10-26T17:10:09.000Z")

    def test_archive_failure_is_recorded_without_touching_feed_success(self):
        result = build_status(now=NOW, previous=None, record=RECORD, archive_result=None,
                              collect_outcome="success", archive_outcome="failure")
        self.assertEqual(result["last_attempt_outcome"], "success")
        self.assertEqual(result["archive"]["outcome"], "failure")
        self.assertIn("archive step failed", result["archive"]["error"])
        self.assertEqual(result["feed"]["features"], 411)

    def test_unparsed_slots_do_not_advance_the_scan_end(self):
        previous = build_status(now=NOW - timedelta(hours=1), previous=None, record=RECORD, archive_result=None,
                                collect_outcome="success", archive_outcome="success")
        opaque = {**RECORD, "run_utc": "2026-10-26T15:17:00.000Z", "slots_parsed": False, "scanned_ranges": [
            {"start_utc": "2026-10-26T09:17:00.000Z", "end_utc": "2026-10-26T15:17:00.000Z"}]}
        result = build_status(now=NOW + timedelta(hours=1), previous=previous, record=opaque, archive_result=None,
                              collect_outcome="success", archive_outcome="success")
        self.assertEqual(result["last_successful_scan_end_utc"], "2026-10-26T14:10:09.000Z")  # not the query end
        self.assertIs(result["last_slots_parsed"], False)
        self.assertEqual(result["consecutive_failures"], 0)  # the feed run itself succeeded

    def test_lost_days_reach_the_status_file(self):
        result = build_status(now=NOW, previous=None, record=RECORD, archive_result={
            "outcome": "success", "lost_days": ["2026-10-24"], "dropped_lost_day": 1},
            collect_outcome="success", archive_outcome="success")
        self.assertEqual(result["archive"]["lost_days"], ["2026-10-24"])
        self.assertEqual(result["archive"]["dropped_lost_day"], 1)

    # --- shard_lost: explicit integrity alert, never reported entirely healthy ---------
    LOST_RESULT = {"outcome": "success", "lost_days": ["2026-10-24"], "dropped_lost_day": 0,
                   "integrity": {"state": "shard_lost", "lost_days": ["2026-10-24"], "quarantined_observations": 1,
                                 "alert": "INTEGRITY ALERT: 1 UTC date lost its listed shard (2026-10-24); unknown, not zero"}}

    def test_shard_lost_degrades_health_and_raises_an_integrity_alert(self):
        result = build_status(now=NOW, previous=None, record=RECORD, archive_result=self.LOST_RESULT,
                              collect_outcome="success", archive_outcome="success")
        self.assertEqual(result["freshness"], "ok")
        self.assertEqual(result["health"], "degraded")  # fresh, but not entirely healthy
        self.assertEqual(result["integrity"]["state"], "shard_lost")
        self.assertEqual(result["integrity"]["lost_days"], ["2026-10-24"])
        self.assertIn("INTEGRITY ALERT", result["integrity"]["alert"])
        self.assertEqual(result["integrity"]["source"], "this_run")

    def test_integrity_alert_is_carried_when_the_archive_step_fails_or_is_skipped(self):
        lost = build_status(now=NOW, previous=None, record=RECORD, archive_result=self.LOST_RESULT,
                            collect_outcome="success", archive_outcome="success")
        for outcome in ("failure", "skipped"):
            later = build_status(now=NOW + timedelta(hours=1), previous=lost, record=RECORD, archive_result=None,
                                 collect_outcome="success", archive_outcome=outcome)
            self.assertEqual(later["integrity"]["state"], "shard_lost", outcome)
            self.assertEqual(later["integrity"]["source"], "previous_status", outcome)
            self.assertEqual(later["health"], "degraded", outcome)

    def test_recovered_archive_reports_ok_again(self):
        lost = build_status(now=NOW, previous=None, record=RECORD, archive_result=self.LOST_RESULT,
                            collect_outcome="success", archive_outcome="success")
        ok = build_status(now=NOW, previous=lost, record=RECORD, archive_result={
            "outcome": "success", "integrity": {"state": "ok", "lost_days": [], "alert": None}},
            collect_outcome="success", archive_outcome="success")
        self.assertEqual((ok["health"], ok["integrity"]["state"], ok["integrity"]["alert"]), ("ok", "ok", None))

    def test_alert_command_exits_nonzero_while_a_shard_is_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "status.json"
            status_module.write_status(target, build_status(
                now=NOW, previous=None, record=RECORD, archive_result=self.LOST_RESULT,
                collect_outcome="success", archive_outcome="success"))
            self.assertEqual(status_module.main(["alert", "--status", str(target)]), 1)
            status_module.write_status(target, build_status(
                now=NOW, previous=None, record=RECORD, archive_result={"outcome": "success", "integrity": {"state": "ok"}},
                collect_outcome="success", archive_outcome="success"))
            self.assertEqual(status_module.main(["alert", "--status", str(target)]), 0)

    def test_health_thresholds(self):
        self.assertEqual(health(NOW, None), "never")
        self.assertEqual(health(NOW, NOW - timedelta(hours=5)), "ok")
        self.assertEqual(health(NOW, NOW - timedelta(hours=7)), "warn")
        self.assertEqual(health(NOW, NOW - timedelta(hours=13)), "stale")
        self.assertEqual(health(NOW, NOW - timedelta(hours=25)), "stopped")

    def test_cli_writes_the_file_even_without_a_run_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "data" / "status.json"
            code = status_module.main(["write", "--status", str(target), "--run-record", str(Path(tmp) / "missing.json"),
                                       "--collect-outcome", "failure", "--archive-outcome", "skipped", "--now", NOW.isoformat()])
            self.assertEqual(code, 0)
            written = json.loads(target.read_text())
            self.assertEqual(written["last_attempt_outcome"], "failure")
            self.assertEqual(written["consecutive_failures"], 1)
            self.assertEqual(written["health"], "never")
            record = Path(tmp) / "run.json"
            record.write_text(json.dumps(RECORD))
            status_module.main(["write", "--status", str(target), "--run-record", str(record), "--collect-outcome", "success",
                                "--archive-outcome", "success", "--now", NOW.isoformat()])
            written = json.loads(target.read_text())
            self.assertEqual(written["consecutive_failures"], 0)
            self.assertEqual(written["last_successful_scan_end_utc"], "2026-10-26T14:10:09.000Z")


if __name__ == "__main__":
    unittest.main()

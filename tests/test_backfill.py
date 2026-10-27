"""Backfill of never-searched UTC dates from a fresh, whole-day EUMETSAT search.

``collect.py --rebuild-day D`` (here with a fake downloader, never the network)
writes a run record; ``history.py backfill --day D`` ingests it only for a date
the index marks never searched (coverage none / count null, not shard_lost), with
the record's real examined slots as coverage: a gap stays unknown, a zero is
only written for searched time, and any refusal or failure changes nothing.

Synthetic features exist ONLY inside temporary directories created by these
tests (ids start with "TEST-"); nothing synthetic is ever published.
"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect
import history
import status as status_module
from collect import build_feature
from history import archive_observations, day_path, successful_scan, verify_archive

NOW = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
TARGET = "2026-10-20"          # never searched in the fixture archive
SEARCHED = "2026-10-26"        # searched by an "hourly" run in the fixture archive
DAY0 = datetime(2026, 10, 20, tzinfo=timezone.utc)


def slot_name(start: datetime) -> str:
    end = start + timedelta(minutes=10)
    return ("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_"
            f"{end:%Y%m%d%H%M%S}_L2PF_OPE_{start:%Y%m%d%H%M%S}_{end:%Y%m%d%H%M%S}_N__O_0001_0000")


def flash(moment: datetime, tag: str, index: int = 0) -> dict:
    return build_feature(-2.12, 51.11, moment, "TEST-" + tag, index)


class FakeDownloader:
    """Stands in for collect.download: 10-minute slots of [start, end) minus ``skip``."""

    def __init__(self, flashes=(), skip_slots=(), error=None):
        self.flashes, self.skip, self.error, self.calls = list(flashes), set(skip_slots), error, []

    def __call__(self, start, end):
        self.calls.append((start, end))
        if self.error:
            raise self.error
        names, cursor, i = [], start, 0
        while cursor < end:
            if i not in self.skip:
                names.append(slot_name(cursor))
            cursor += timedelta(minutes=10)
            i += 1
        return [f for f in self.flashes if start <= collect.parse_utc(f["properties"]["time_utc"]) < end], len(names), names


def rows(index):
    return {r["date"]: r for r in index["days"]}


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "history"
        hourly = datetime(2026, 10, 26, tzinfo=timezone.utc)
        archive_observations(
            self.root, [flash(hourly + timedelta(hours=3), "hourly")], now=NOW - timedelta(hours=1),
            scanned_windows=[successful_scan((hourly + timedelta(hours=1)).isoformat(),
                                             (hourly + timedelta(hours=7)).isoformat(), 36)],
            lookback_hours=6.0)
        self.before = json.loads((self.root / "index.json").read_text())
        self.record = self.base / "rb.json"

    def tearDown(self):
        self.temp.cleanup()

    def index(self):
        return json.loads((self.root / "index.json").read_text())

    def snapshot(self):
        return {p.relative_to(self.base).as_posix(): p.read_bytes() for p in sorted(self.base.rglob("*")) if p.is_file()
                and p != self.record}

    def rebuild(self, day=TARGET, **fake):
        fake_dl = FakeDownloader(**fake)
        collect.rebuild_day_record(day, self.record, now=NOW, downloader=fake_dl)
        return fake_dl

    def backfill(self, day=TARGET, **kwargs):
        return history.backfill_day(self.root, day, now=NOW, rebuild_record=self.record, **kwargs)

    # --- the fixture: TARGET is never searched, SEARCHED is ---------------------
    def test_fixture_marks_the_target_date_never_searched(self):
        r = rows(self.before)
        self.assertEqual((r[TARGET]["coverage"], r[TARGET]["count"]), ("none", None))
        self.assertEqual(r[SEARCHED]["coverage"], "partial")

    # --- happy path: real coverage, real count, gaps stay unknown ---------------
    def test_backfill_ingests_a_rebuild_with_its_real_coverage_and_count(self):
        flashes = [flash(DAY0 + timedelta(hours=h, minutes=3), f"bf{h}") for h in (2, 9, 17)]
        fake = self.rebuild(flashes=flashes, skip_slots={30, 31})  # 05:00-05:20 missing at EUMETSAT
        self.assertEqual(fake.calls, [(DAY0, DAY0 + timedelta(days=1))])
        result = self.backfill()
        row = rows(self.index())[TARGET]
        self.assertEqual(row["coverage"], "partial")
        self.assertEqual(row["scanned_seconds"], 86400 - 1200)
        self.assertEqual(row["count"], 3)
        self.assertEqual(row["path"], day_path(date(2026, 10, 20)))
        self.assertEqual(verify_archive(self.root)["integrity"], "ok")
        self.assertEqual(result["row"]["count"], 3)
        self.assertEqual(result["unscanned_ranges"],
                         [{"start_utc": "2026-10-20T05:00:00.000Z", "end_utc": "2026-10-20T05:20:00.000Z"}])

    def test_backfill_keeps_the_hourly_heartbeat_fields_of_the_index(self):
        self.rebuild(flashes=[flash(DAY0 + timedelta(hours=4), "x")])
        self.backfill()
        after = self.index()
        for key in ("last_successful_scan_end_utc", "last_run_lookback_hours", "archive_first_enabled_utc"):
            self.assertEqual(after[key], self.before[key], key)
        self.assertEqual(after["coverage_start_utc"], "2026-10-20T00:00:00.000Z")
        self.assertEqual(rows(after)[SEARCHED], {**rows(self.before)[SEARCHED], "sealed": rows(after)[SEARCHED]["sealed"]})

    def test_a_fully_searched_day_without_flashes_is_zero_with_full_coverage(self):
        self.rebuild(flashes=[])
        self.backfill()
        row = rows(self.index())[TARGET]
        self.assertEqual((row["coverage"], row["scanned_seconds"], row["count"], row["path"]), ("full", 86400, 0, None))

    def test_heavy_day_respects_the_byte_caps_and_splits_into_hourly_parts(self):
        flashes = [flash(DAY0 + timedelta(hours=h, seconds=s), f"heavy{h}", s) for h in (1, 2) for s in range(20)]
        self.rebuild(flashes=flashes)
        self.backfill(target_shard_bytes=4000, max_shard_bytes=25_000)
        row = rows(self.index())[TARGET]
        self.assertIsNone(row["path"])
        self.assertEqual([p["hour"] for p in row["parts"]], ["01", "02"])
        self.assertEqual(row["count"], 40)
        self.assertEqual(verify_archive(self.root)["shards_checked"], 2)

    # --- refusals change nothing -------------------------------------------------
    def assert_refused(self, message, **kwargs):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, message):
            self.backfill(**kwargs)
        self.assertEqual(self.snapshot(), before, "a refused backfill must not change any file")
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), sorted(["history", "rb.json"]))

    def test_shard_too_large_changes_nothing_and_the_day_stays_unknown(self):
        self.rebuild(flashes=[flash(DAY0 + timedelta(hours=1, seconds=s), "big", s) for s in range(20)])
        self.assert_refused("max_shard_bytes", max_shard_bytes=3000, target_shard_bytes=2000)
        self.assertIsNone(rows(self.index())[TARGET]["count"])

    def test_refuses_a_date_that_was_already_searched(self):
        self.rebuild(day=SEARCHED)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "already searched"):
            history.backfill_day(self.root, SEARCHED, now=NOW, rebuild_record=self.record)
        self.assertEqual(self.snapshot(), before)

    def test_refuses_a_shard_lost_date_and_points_to_recover(self):
        lost_day = datetime(2026, 10, 26, tzinfo=timezone.utc)
        (self.root / day_path(lost_day.date())).unlink()
        archive_observations(self.root, [], now=NOW - timedelta(minutes=30))
        self.rebuild(day=SEARCHED)
        with self.assertRaisesRegex(ValueError, "shard_lost.*recover"):
            history.backfill_day(self.root, SEARCHED, now=NOW, rebuild_record=self.record)

    def test_refuses_a_record_for_another_date(self):
        self.rebuild(day="2026-10-19")
        self.assert_refused("2026-10-19")

    def test_refuses_a_record_without_parsed_slots(self):
        self.rebuild()
        rec = json.loads(self.record.read_text())
        rec["slots_parsed"] = False
        self.record.write_text(json.dumps(rec))
        self.assert_refused("slots_parsed")

    def test_refuses_a_failed_record(self):
        self.rebuild()
        rec = json.loads(self.record.read_text())
        rec.update(outcome="failure", error="HTTPError: 429 Too Many Requests")
        self.record.write_text(json.dumps(rec))
        self.assert_refused("did not succeed")

    def test_refuses_a_search_made_before_the_date_ended(self):
        collect.rebuild_day_record(TARGET, self.record, now=DAY0 + timedelta(hours=20), downloader=FakeDownloader())
        self.assert_refused("whole UTC date")

    def test_refuses_a_flash_outside_the_examined_slots(self):
        self.rebuild(skip_slots={12})
        rec = json.loads(self.record.read_text())
        rec["features"] = [flash(DAY0 + timedelta(hours=2, minutes=5), "ghost")]
        self.record.write_text(json.dumps(rec))
        self.assert_refused("outside the examined slots")

    def test_refuses_without_an_index(self):
        self.rebuild()
        with self.assertRaisesRegex(ValueError, "no index.json"):
            history.backfill_day(self.base / "empty", TARGET, now=NOW, rebuild_record=self.record)
        self.assertFalse((self.base / "empty").exists())

    # --- EUMETSAT errors: no record, nothing to ingest --------------------------
    def test_eumetsat_error_or_rate_limit_writes_no_record(self):
        with self.assertRaisesRegex(RuntimeError, "429"):
            collect.rebuild_day_record(TARGET, self.record, now=NOW,
                                       downloader=FakeDownloader(error=RuntimeError("HTTP 429 Too Many Requests")))
        self.assertFalse(self.record.exists())

    # --- CLI ------------------------------------------------------------------
    def test_cli_backfill_writes_a_result_and_runs_verify(self):
        self.rebuild(flashes=[flash(DAY0 + timedelta(hours=4), "cli")])
        out = self.base / "result.json"
        code = history.main(["backfill", "--archive-dir", str(self.root), "--day", TARGET,
                             "--rebuild-record", str(self.record), "--result-out", str(out), "--now", NOW.isoformat()])
        self.assertEqual(code, 0)
        result = json.loads(out.read_text())
        self.assertEqual((result["outcome"], result["date"], result["row"]["count"]), ("success", TARGET, 1))
        self.assertEqual(result["verify"]["integrity"], "ok")


class BackfillPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "history"
        d = datetime(2026, 10, 24, tzinfo=timezone.utc)
        archive_observations(self.root, [flash(d + timedelta(hours=1), "p")], now=NOW,
                             scanned_windows=[successful_scan((d + timedelta(hours=1)).isoformat(),
                                                              (d + timedelta(hours=2)).isoformat(), 6)])

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_lists_never_searched_dates_newest_first(self):
        plan = history.backfill_plan(self.root, end_date=date(2026, 10, 26), days=5, today=NOW.date())
        self.assertEqual(plan, ["2026-10-26", "2026-10-25", "2026-10-23", "2026-10-22"])

    def test_plan_skips_shard_lost_dates(self):
        (self.root / day_path(date(2026, 10, 24))).unlink()
        archive_observations(self.root, [], now=NOW)
        plan = history.backfill_plan(self.root, end_date=date(2026, 10, 25), days=3, today=NOW.date())
        self.assertEqual(plan, ["2026-10-25", "2026-10-23"])

    def test_plan_stops_at_the_retention_cutoff(self):
        plan = history.backfill_plan(self.root, end_date=date(2025, 10, 28), days=5, today=NOW.date())
        self.assertEqual(plan, ["2025-10-28", "2025-10-27"])

    def test_plan_limits(self):
        for end, days in ((date(2026, 10, 26), 32), (date(2026, 10, 26), 0), (NOW.date(), 1)):
            with self.assertRaises(ValueError):
                history.backfill_plan(self.root, end_date=end, days=days, today=NOW.date())
        with self.assertRaisesRegex(ValueError, "no index.json"):
            history.backfill_plan(Path(self.temp.name) / "none", end_date=date(2026, 10, 26), days=1, today=NOW.date())

    def test_cli_plan_defaults_to_yesterday(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = history.main(["backfill-plan", "--archive-dir", str(self.root), "--end-date", "", "--days", "3",
                                 "--now", NOW.isoformat()])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().split(), ["2026-10-26", "2026-10-25"])


class BackfillStatusTests(unittest.TestCase):
    PREVIOUS = {
        "schema": "uk-lightning-status.v1", "written_utc": "2026-10-27T05:18:00.000Z", "health": "ok", "freshness": "ok",
        "integrity": {"state": "ok", "alert": None, "lost_days": [], "quarantined_observations": 0, "source": "this_run"},
        "last_attempt_utc": "2026-10-27T05:17:00.000Z", "last_attempt_outcome": "success", "last_error": None,
        "consecutive_failures": 0, "last_successful_scan_end_utc": "2026-10-27T05:10:09.000Z",
        "archive": {"outcome": "success", "archived_observations": 1, "index_generated_utc": "2026-10-27T05:18:00.000Z"},
    }

    def test_backfill_block_leaves_the_hourly_heartbeat_untouched(self):
        index = {"generated_utc": "2026-10-27T07:00:00.000Z", "archived_observations": 4,
                 "integrity": {"state": "ok", "alert": None, "lost_days": []}}
        result = status_module.backfill_status(
            self.PREVIOUS, now=NOW, planned=["2026-10-26", "2026-10-25", "2026-10-24"], filled=["2026-10-26"],
            stopped="eumetsat_error", error="2026-10-25: RuntimeError: HTTP 429", index=index)
        for key in ("written_utc", "last_attempt_utc", "last_attempt_outcome", "last_error", "consecutive_failures",
                    "last_successful_scan_end_utc", "freshness", "health"):
            self.assertEqual(result[key], self.PREVIOUS[key], key)
        block = result["backfill"]
        self.assertEqual(block["outcome"], "stopped")
        self.assertEqual(block["filled_days"], ["2026-10-26"])
        self.assertEqual(block["remaining_unknown_days"], ["2026-10-25", "2026-10-24"])
        self.assertIn("429", block["error"])
        self.assertEqual(result["archive"]["archived_observations"], 4)
        self.assertEqual(result["archive"]["index_generated_utc"], "2026-10-27T07:00:00.000Z")
        self.assertEqual(result["archive"]["outcome"], "success")

    def test_hourly_status_carries_the_last_backfill_forward(self):
        previous = {**self.PREVIOUS, "backfill": {"last_run_utc": "2026-10-27T06:00:00.000Z", "outcome": "completed"}}
        result = status_module.build_status(now=NOW, previous=previous, record=None, archive_result=None,
                                            collect_outcome="failure", archive_outcome="skipped")
        self.assertEqual(result["backfill"], previous["backfill"])

    def test_cli_backfill_reads_plan_and_filled_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "status.json").write_text(json.dumps(self.PREVIOUS))
            (tmp / "plan.txt").write_text("2026-10-26\n2026-10-25\n")
            (tmp / "filled.txt").write_text("2026-10-26\n2026-10-25\n\n")
            (tmp / "history").mkdir()
            (tmp / "history" / "index.json").write_text(json.dumps(
                {"generated_utc": "2026-10-27T07:00:00.000Z", "archived_observations": 9,
                 "integrity": {"state": "ok", "alert": None, "lost_days": []}}))
            code = status_module.main(["backfill", "--status", str(tmp / "status.json"), "--plan", str(tmp / "plan.txt"),
                                       "--filled", str(tmp / "filled.txt"), "--stopped", "completed", "--error", "",
                                       "--archive-dir", str(tmp / "history"), "--now", NOW.isoformat()])
            self.assertEqual(code, 0)
            written = json.loads((tmp / "status.json").read_text())
            self.assertEqual(written["backfill"]["outcome"], "completed")
            self.assertEqual(written["backfill"]["remaining_unknown_days"], [])
            self.assertIsNone(written["backfill"]["error"])
            self.assertEqual(written["archive"]["archived_observations"], 9)


if __name__ == "__main__":
    unittest.main()

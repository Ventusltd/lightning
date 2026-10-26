"""Offline contracts for one-year lightning history; no network or synthetic public points."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collect import build_feature
from history import archive_observations, day_path, successful_scan, year_ago


class LightningHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "history"
        self.now = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def flash(self, timestamp, product, index=0):
        return build_feature(-2.12, 51.11, timestamp, product, index)

    def test_history_one_year_archive_and_utc_index(self):
        oldest = self.now - timedelta(days=100)
        newest = self.now - timedelta(hours=1)
        a, b = self.flash(oldest, "a"), self.flash(newest, "b")
        scanned = successful_scan(
            (self.now - timedelta(hours=3)).isoformat(),
            self.now.isoformat(), 18
        )
        result = archive_observations(self.root, [b, a], now=self.now, scanned_window=scanned)
        index = result["index"]
        self.assertEqual(index["archived_observations"], 2)
        self.assertEqual(index["archived_days_with_flashes"], 2)
        self.assertEqual(index["first_observation_utc"], a["properties"]["time_utc"])
        self.assertEqual(index["last_observation_utc"], b["properties"]["time_utc"])
        self.assertEqual(index["scan_windows"][0]["successful_runs"], 1)
        self.assertEqual(len(index["days"]), 2)
        for day in index["days"]:
            self.assertTrue(day["path"].endswith(".json"))
            self.assertTrue(day["path"].startswith("days/"))
            content = json.loads((self.root / day["path"]).read_text())
            self.assertEqual(content["type"], "FeatureCollection")
            self.assertEqual(content["schema"], "uk-lightning-history-day.v1")
            self.assertEqual(len(content["features"]), 1)
            self.assertEqual(content["features"][0]["properties"]["observation_kind"], "optical_lightning_flash")
            self.assertNotIn("strike", day["path"])

    def test_duplicate_reingestion_and_rerun_are_idempotent(self):
        flash = self.flash(self.now - timedelta(hours=1), "same")
        first = archive_observations(self.root, [flash], now=self.now)
        second = archive_observations(self.root, [flash, flash], now=self.now)
        self.assertEqual(second["index"]["archived_observations"], 1)
        self.assertEqual(second["files_changed"], 0)
        self.assertEqual(first["index"]["archive_first_enabled_utc"], second["index"]["archive_first_enabled_utc"])

    def test_outside_location_non_source_and_future_are_rejected(self):
        valid = self.flash(self.now - timedelta(hours=2), "valid")
        future = self.flash(self.now + timedelta(hours=1), "future")
        wrong_kind = self.flash(self.now - timedelta(hours=1), "tamper")
        wrong_kind["properties"]["observation_kind"] = "ground_strike"
        outside = self.flash(self.now - timedelta(hours=2), "outside")
        outside["geometry"]["coordinates"] = [20, 51]
        result = archive_observations(self.root, [valid, future, wrong_kind, outside], now=self.now)
        self.assertEqual(result["index"]["archived_observations"], 1)
        self.assertFalse(result["index"]["days"][0]["path"].endswith(".geojson"))

    def test_retention_prunes_day_files_but_not_current(self):
        old_time = self.now - timedelta(days=367)
        fresh_time = self.now - timedelta(days=10)
        old = self.flash(old_time, "old")
        fresh = self.flash(fresh_time, "fresh")
        earlier = self.now - timedelta(days=360)
        archive_observations(self.root, [old, fresh], now=earlier)
        self.assertTrue((self.root / day_path(old_time.date())).exists())
        second = archive_observations(self.root, [fresh], now=self.now)
        self.assertEqual(second["days_expired"], 1)
        self.assertFalse((self.root / day_path(old_time.date())).exists())
        self.assertTrue((self.root / day_path(fresh_time.date())).exists())
        self.assertEqual(second["index"]["archived_observations"], 1)

    def test_damaged_day_archive_fails_without_erasure(self):
        flash = self.flash(self.now - timedelta(hours=1), "good")
        archive_observations(self.root, [flash], now=self.now)
        path = self.root / day_path(self.now.date())
        original = path.read_text()
        payload = json.loads(original)
        payload["schema"] = "wrong-schema"
        path.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):
            archive_observations(self.root, [], now=self.now)
        self.assertEqual(json.loads(path.read_text())["schema"], "wrong-schema")

    def test_missing_days_never_claim_zero_lightning(self):
        result = archive_observations(self.root, [], now=self.now)
        self.assertEqual(result["index"]["days"], [])
        self.assertEqual(result["index"]["archived_observations"], 0)
        self.assertIn("unknown historical availability", result["index"]["availability_note"].lower())
        self.assertIn("partial", result["index"]["status"])

    def test_calendar_year_cutoff_handles_leap_day(self):
        self.assertEqual(str(year_ago(datetime(2024, 2, 29, tzinfo=timezone.utc).date())), "2023-02-28")

    def test_overlap_scan_windows_merge_without_false_complete_coverage_claim(self):
        s1 = successful_scan("2026-10-26T10:00:00Z", "2026-10-26T12:00:00Z", 12)
        s2 = successful_scan("2026-10-26T11:30:00Z", "2026-10-26T13:00:00Z", 12)
        archive_observations(self.root, [], now=self.now, scanned_window=s1)
        result = archive_observations(self.root, [], now=self.now, scanned_window=s2)
        windows = result["index"]["scan_windows"]
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["start_utc"], "2026-10-26T10:00:00.000Z")
        self.assertEqual(windows[0]["end_utc"], "2026-10-26T13:00:00.000Z")
        self.assertEqual(windows[0]["successful_runs"], 2)
        self.assertIn("not a guarantee", result["index"]["scan_window_note"])


if __name__ == "__main__":
    unittest.main()

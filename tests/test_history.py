"""Offline contracts for the one-year lightning archive (index v2); no network.

Synthetic features exist ONLY inside temporary directories created by these
tests (ids start with "TEST-"); nothing synthetic is ever published.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import history
from collect import build_feature
from history import (
    MAX_SHARD_BYTES, SCHEMA_INDEX, TARGET_SHARD_BYTES, ShardTooLarge, archive_observations, coverage_for, day_path,
    is_sealed, load_seed_windows, part_path, scanned_seconds_for_day, slots_proved_by_flashes, successful_scan,
    verify_archive, windows_from_run_record, year_ago,
)

F8_ROW_KEYS = ["date", "coverage", "scanned_seconds", "count", "bytes", "path", "sealed",
               "first_observation_utc", "last_observation_utc", "sha256"]
F8_TOP_KEYS = ["schema", "generated_utc", "collection_interval_hours", "collection_lookback_hours", "next_update_due_utc",
               "cutoff_date_utc", "retention", "source", "source_url", "observation", "bounds_wsen", "max_shard_bytes",
               "archived_observations", "first_observation_utc", "last_observation_utc", "scan_windows", "days", "status"]


def rows_with_flashes(index):
    return [row for row in index["days"] if row["count"]]


class LightningHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "history"
        self.now = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def flash(self, timestamp, product, index=0, lon=-2.12, lat=51.11):
        return build_feature(lon, lat, timestamp, "TEST-" + product, index)

    def scan(self, start, end):
        return successful_scan(start.isoformat(), end.isoformat(), 18)

    # --- F8 shape --------------------------------------------------------
    def test_index_v2_has_the_f8_fields_and_one_row_per_utc_date(self):
        result = archive_observations(self.root, [self.flash(self.now - timedelta(hours=1), "a")], now=self.now,
                                      scanned_window=self.scan(self.now - timedelta(hours=6), self.now), lookback_hours=6)
        index = result["index"]
        self.assertEqual(index["schema"], SCHEMA_INDEX)
        for key in F8_TOP_KEYS:
            self.assertIn(key, index)
        self.assertEqual(index["cutoff_date_utc"], "2025-10-26")
        self.assertEqual(len(index["days"]), 366)
        self.assertEqual(index["days"][0]["date"], "2025-10-26")
        self.assertEqual(index["days"][-1]["date"], "2026-10-26")
        for row in index["days"]:
            self.assertEqual([k for k in row if k in F8_ROW_KEYS], F8_ROW_KEYS)
        self.assertEqual(index["max_shard_bytes"], 25_000_000)
        self.assertEqual(index["collection_interval_hours"], 1)
        self.assertEqual(index["next_update_due_utc"], "2026-10-26T17:00:00.000Z")
        self.assertEqual(index["status"], "archive_partial_until_history_is_genuinely_collected_or_backfilled")
        self.assertEqual(index["observation"], "satellite optical lightning flashes, not ground strikes")
        self.assertEqual(index["last_successful_scan_end_utc"], "2026-10-26T14:00:00.000Z")
        self.assertEqual(index["coverage_start_utc"], "2026-10-26T08:00:00.000Z")
        self.assertEqual(json.loads((self.root / "index.json").read_text())["schema"], SCHEMA_INDEX)

    def test_never_scanned_and_scanned_zero_are_different_rows(self):
        scanned_day = datetime(2026, 10, 24, tzinfo=timezone.utc)
        result = archive_observations(self.root, [], now=self.now,
                                      scanned_window=self.scan(scanned_day, scanned_day + timedelta(days=1)))
        rows = {row["date"]: row for row in result["index"]["days"]}
        full = rows["2026-10-24"]
        self.assertEqual((full["coverage"], full["scanned_seconds"], full["count"], full["path"], full["sealed"]),
                         ("full", 86400, 0, None, True))
        never = rows["2026-10-23"]
        self.assertEqual((never["coverage"], never["scanned_seconds"], never["count"], never["path"], never["bytes"]),
                         ("none", 0, None, None, None))
        self.assertIn("unknown, not zero", result["index"]["availability_note"])
        for row in result["index"]["days"]:
            self.assertEqual(row["count"] is None, row["coverage"] == "none")
            if row["count"] == 0:
                self.assertGreater(row["scanned_seconds"], 0)

    def test_partial_coverage_counts_only_scanned_seconds(self):
        day = datetime(2026, 10, 24, tzinfo=timezone.utc)
        windows = [self.scan(day + timedelta(hours=1, minutes=52), day + timedelta(hours=4, minutes=50)),
                   self.scan(day + timedelta(hours=8), day + timedelta(hours=11))]
        result = archive_observations(self.root, [self.flash(day + timedelta(hours=2), "p")], now=self.now,
                                      scanned_windows=windows)
        row = {r["date"]: r for r in result["index"]["days"]}["2026-10-24"]
        self.assertEqual(row["coverage"], "partial")
        self.assertEqual(row["scanned_seconds"], (2 * 3600 + 58 * 60) + 3 * 3600)
        self.assertEqual(row["count"], 1)
        self.assertEqual(scanned_seconds_for_day(result["index"]["scan_windows"], day.date()), row["scanned_seconds"])
        self.assertEqual(coverage_for(0), "none")
        self.assertEqual(coverage_for(86400), "full")

    def test_day_with_flashes_but_no_recorded_window_gets_a_proven_lower_bound(self):
        day = datetime(2026, 10, 24, 10, 15, tzinfo=timezone.utc)
        flashes = [self.flash(day, "s1"), self.flash(day + timedelta(minutes=3), "s2"), self.flash(day + timedelta(minutes=30), "s3")]
        result = archive_observations(self.root, flashes, now=self.now)
        row = {r["date"]: r for r in result["index"]["days"]}["2026-10-24"]
        self.assertEqual(row["coverage"], "partial")
        self.assertEqual(row["scanned_seconds"], 1200)  # two distinct 10-minute slots proven by three flashes
        self.assertEqual(row["count"], 3)
        self.assertEqual(slots_proved_by_flashes(flashes), 1200)

    # --- shard shape, bytes, sha256, dedupe ----------------------------------
    def test_day_shard_v1_shape_bytes_and_sha256_are_indexed(self):
        import hashlib
        oldest = self.now - timedelta(days=100)
        newest = self.now - timedelta(hours=1)
        result = archive_observations(self.root, [self.flash(newest, "b"), self.flash(oldest, "a")], now=self.now,
                                      scanned_window=self.scan(self.now - timedelta(hours=3), self.now))
        index = result["index"]
        self.assertEqual(index["archived_observations"], 2)
        self.assertEqual(index["archived_days_with_flashes"], 2)
        self.assertEqual(len(rows_with_flashes(index)), 2)
        for row in rows_with_flashes(index):
            self.assertTrue(row["path"].startswith("days/") and row["path"].endswith(".json"))
            self.assertNotIn("strike", row["path"])
            file = self.root / row["path"]
            raw = file.read_bytes()
            self.assertEqual(row["bytes"], len(raw))
            self.assertEqual(row["sha256"], hashlib.sha256(raw).hexdigest())
            content = json.loads(raw)
            self.assertEqual(content["schema"], "uk-lightning-history-day.v1")
            self.assertEqual(content["type"], "FeatureCollection")
            self.assertEqual(content["metadata"]["date_utc"], row["date"])
            self.assertEqual(content["metadata"]["count"], 1)
            feature = content["features"][0]
            self.assertEqual(sorted(feature), ["geometry", "id", "properties", "type"])
            self.assertEqual(sorted(feature["properties"]),
                             ["name", "observation_kind", "raw_tech", "source", "source_product", "status", "time_utc"])
            self.assertEqual(feature["properties"]["observation_kind"], "optical_lightning_flash")

    def test_duplicate_reingestion_and_rerun_are_idempotent_and_existing_bytes_win(self):
        flash = self.flash(self.now - timedelta(hours=1), "same")
        first = archive_observations(self.root, [flash], now=self.now)
        reordered = json.loads(json.dumps(flash))
        reordered["properties"] = dict(reversed(list(reordered["properties"].items())))
        second = archive_observations(self.root, [reordered, flash], now=self.now)
        self.assertEqual(second["index"]["archived_observations"], 1)
        self.assertEqual(second["files_changed"], 0)
        self.assertEqual(first["index"]["archive_first_enabled_utc"], second["index"]["archive_first_enabled_utc"])
        self.assertEqual(rows_with_flashes(first["index"])[0]["sha256"], rows_with_flashes(second["index"])[0]["sha256"])

    def test_outside_location_non_source_and_future_are_rejected(self):
        valid = self.flash(self.now - timedelta(hours=2), "valid")
        future = self.flash(self.now + timedelta(hours=1), "future")
        wrong_kind = self.flash(self.now - timedelta(hours=1), "tamper")
        wrong_kind["properties"]["observation_kind"] = "ground_strike"
        outside = self.flash(self.now - timedelta(hours=2), "outside")
        outside["geometry"]["coordinates"] = [20, 51]
        result = archive_observations(self.root, [valid, future, wrong_kind, outside], now=self.now)
        self.assertEqual(result["index"]["archived_observations"], 1)
        self.assertEqual(result["dropped_invalid"], 3)
        self.assertFalse(rows_with_flashes(result["index"])[0]["path"].endswith(".geojson"))

    # --- retention, clamp-and-flag ----------------------------------------
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
        self.assertEqual(second["index"]["days"][0]["date"], "2025-10-26")

    def test_observations_before_the_cutoff_are_clamped_and_flagged_not_raised(self):
        too_old = self.flash(self.now - timedelta(days=400), "ancient")
        result = archive_observations(self.root, [too_old, self.flash(self.now - timedelta(hours=1), "ok")], now=self.now,
                                      scanned_window=self.scan(self.now - timedelta(days=400), self.now - timedelta(days=399)))
        self.assertEqual(result["dropped_before_cutoff"], 1)
        self.assertEqual(result["index"]["archived_observations"], 1)
        self.assertEqual(result["index"]["scan_windows"], [])  # a window entirely before the cutoff is clipped away

    def test_calendar_year_cutoff_handles_leap_day(self):
        self.assertEqual(str(year_ago(datetime(2024, 2, 29, tzinfo=timezone.utc).date())), "2023-02-28")

    # --- sealed ---------------------------------------------------------------
    def test_sealed_rule(self):
        today = self.now.date()
        self.assertTrue(is_sealed(today - timedelta(days=5), "partial", self.now))
        self.assertFalse(is_sealed(today - timedelta(days=1), "partial", self.now))
        self.assertTrue(is_sealed(today - timedelta(days=1), "full", self.now))
        self.assertFalse(is_sealed(today, "partial", self.now))
        # date + 1 day + 48 h boundary: 2026-10-23 closes at 2026-10-26T00:00Z
        self.assertTrue(is_sealed(datetime(2026, 10, 23).date(), "partial", datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)))
        self.assertFalse(is_sealed(datetime(2026, 10, 23).date(), "partial", datetime(2026, 10, 25, 23, 59, tzinfo=timezone.utc)))

    # --- storm day: split and hard cap --------------------------------------
    def test_storm_day_splits_into_hourly_parts_above_the_target(self):
        day = datetime(2026, 10, 24, tzinfo=timezone.utc)
        flashes = [self.flash(day + timedelta(hours=h, minutes=m), f"storm{h}-{m}", m) for h in (3, 4, 17) for m in range(4)]
        result = archive_observations(self.root, flashes, now=self.now, scanned_window=self.scan(day, day + timedelta(days=1)),
                                      target_shard_bytes=2000, max_shard_bytes=25_000_000)
        self.assertEqual(result["split_days"], ["2026-10-24"])
        row = {r["date"]: r for r in result["index"]["days"]}["2026-10-24"]
        self.assertIsNone(row["path"])
        self.assertIsNone(row["sha256"])
        self.assertEqual(row["count"], 12)
        self.assertEqual([p["hour"] for p in row["parts"]], ["03", "04", "17"])
        self.assertEqual([p["path"] for p in row["parts"]], [part_path(day.date(), h) for h in (3, 4, 17)])
        self.assertEqual(row["bytes"], sum(p["bytes"] for p in row["parts"]))
        for part in row["parts"]:
            file = self.root / part["path"]
            self.assertEqual(part["bytes"], len(file.read_bytes()))
            content = json.loads(file.read_text())
            self.assertEqual(content["schema"], "uk-lightning-history-day.v1")
            self.assertEqual(content["metadata"]["date_utc"], "2026-10-24")
            self.assertEqual(content["metadata"]["hour_utc"], part["hour"])
            self.assertEqual(len(content["features"]), 4)
        self.assertFalse((self.root / day_path(day.date())).exists())
        # A second run with one more flash in hour 04 rewrites only that part.
        again = archive_observations(self.root, [self.flash(day + timedelta(hours=4, minutes=30), "late", 9)], now=self.now,
                                     target_shard_bytes=2000)
        self.assertEqual(again["files_changed"], 2)  # the 04 part and the index
        self.assertEqual({r["date"]: r for r in again["index"]["days"]}["2026-10-24"]["count"], 13)
        self.assertEqual(verify_archive(self.root)["shards_checked"], 1)

    def test_real_constants_split_a_genuine_shape_storm_day(self):
        # 20,000 features of the real 526-byte shape are ~10.5 MB: above the 8 MiB target, below the 25 MB cap.
        self.assertEqual(TARGET_SHARD_BYTES, 8 * 1024 * 1024)
        self.assertEqual(MAX_SHARD_BYTES, 25_000_000)
        day = datetime(2026, 10, 24, tzinfo=timezone.utc)
        product = ("TEST-W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_20261024010026_L2PF_OPE_"
                   "20261024000009_20261024001009_N__O_0001_0000")
        flashes = [build_feature(-2.0 + (i % 100) / 1000, 51.0 + (i // 100) / 1000, day + timedelta(seconds=i * 4), product, i)
                   for i in range(20000)]
        result = archive_observations(self.root, flashes, now=self.now, scanned_window=self.scan(day, day + timedelta(days=1)))
        row = {r["date"]: r for r in result["index"]["days"]}["2026-10-24"]
        self.assertEqual(result["split_days"], ["2026-10-24"])
        self.assertGreater(row["bytes"], TARGET_SHARD_BYTES)
        self.assertEqual(sum(p["count"] for p in row["parts"]), 20000)
        self.assertTrue(all(p["bytes"] <= MAX_SHARD_BYTES for p in row["parts"]))
        self.assertEqual(len(row["parts"]), 23)  # 20000 x 4 s = 22.2 hours

    def test_shard_above_max_bytes_fails_loudly_and_writes_nothing(self):
        day = datetime(2026, 10, 24, tzinfo=timezone.utc)
        flashes = [self.flash(day + timedelta(hours=3, minutes=m), f"cap{m}", m) for m in range(5)]
        with self.assertRaises(ShardTooLarge):
            archive_observations(self.root, flashes, now=self.now, target_shard_bytes=600, max_shard_bytes=900)
        self.assertFalse((self.root / "index.json").exists())
        self.assertFalse(any((self.root / "days").rglob("*.json")) if (self.root / "days").exists() else False)
        self.assertTrue(issubclass(ShardTooLarge, ValueError))

    # --- integrity --------------------------------------------------------
    def test_damaged_day_archive_fails_without_erasure(self):
        flash = self.flash(self.now - timedelta(hours=1), "good")
        archive_observations(self.root, [flash], now=self.now)
        path = self.root / day_path(self.now.date())
        payload = json.loads(path.read_text())
        payload["schema"] = "wrong-schema"
        path.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):
            archive_observations(self.root, [flash], now=self.now)  # touched day: full re-validation
        with self.assertRaises(ValueError):
            archive_observations(self.root, [], now=self.now)  # untouched day: sha256 verification
        self.assertEqual(json.loads(path.read_text())["schema"], "wrong-schema")

    def test_untouched_days_are_verified_by_sha256_not_reparsed(self):
        archive_observations(self.root, [self.flash(self.now - timedelta(days=3), "a"), self.flash(self.now - timedelta(hours=1), "b")], now=self.now)
        parsed = []
        original = history._read_features

        def spy(file, expected_day):
            parsed.append(expected_day)
            return original(file, expected_day)
        history._read_features = spy
        try:
            archive_observations(self.root, [self.flash(self.now - timedelta(hours=2), "c")], now=self.now)
        finally:
            history._read_features = original
        self.assertEqual(parsed, ["2026-10-26"])  # only the touched day was parsed

    def test_missing_days_never_claim_zero_lightning(self):
        result = archive_observations(self.root, [], now=self.now)
        self.assertEqual(rows_with_flashes(result["index"]), [])
        self.assertTrue(all(row["coverage"] == "none" and row["count"] is None for row in result["index"]["days"]))
        self.assertEqual(result["index"]["archived_observations"], 0)
        self.assertIn("unknown, not zero", result["index"]["availability_note"])
        self.assertIn("partial", result["index"]["status"])

    # --- scan windows ---------------------------------------------------------
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
        today = {r["date"]: r for r in result["index"]["days"]}["2026-10-26"]
        self.assertEqual((today["coverage"], today["scanned_seconds"], today["count"]), ("partial", 3 * 3600, 0))

    def test_zero_product_search_cannot_be_recorded_as_coverage(self):
        with self.assertRaises(ValueError):
            successful_scan("2026-10-26T10:00:00Z", "2026-10-26T12:00:00Z", 0)
        with self.assertRaises(ValueError):
            successful_scan("2026-10-26T12:00:00Z", "2026-10-26T10:00:00Z", 5)

    def test_windows_from_run_record_use_examined_ranges_only(self):
        record = {"outcome": "success", "products_examined": 18,
                  "scanned_ranges": [{"start_utc": "2026-10-26T07:05:58.000Z", "end_utc": "2026-10-26T13:00:09.000Z"}]}
        self.assertEqual(windows_from_run_record(record),
                         [{"start_utc": "2026-10-26T07:05:58.000Z", "end_utc": "2026-10-26T13:00:09.000Z", "successful_runs": 1}])
        self.assertEqual(windows_from_run_record({"outcome": "failure", "products_examined": 0, "scanned_ranges": []}), [])

    def test_seed_windows_are_used_only_when_no_index_exists(self):
        seed_file = Path(self.temp.name) / "seed.json"
        seed_file.write_text(json.dumps({"scan_windows": [
            {"start_utc": "2026-10-20T22:41:54.847Z", "end_utc": "2026-10-22T22:40:09.000Z", "products_examined": 288}]}))
        seeds = load_seed_windows(seed_file)
        first = archive_observations(self.root, [], now=self.now, seed_scan_windows=seeds)
        self.assertEqual(first["index"]["scan_windows"][0]["successful_runs"], 1)
        self.assertEqual({r["date"]: r for r in first["index"]["days"]}["2026-10-21"]["coverage"], "full")
        second = archive_observations(self.root, [], now=self.now, seed_scan_windows=seeds)
        self.assertEqual(second["index"]["scan_windows"][0]["successful_runs"], 1)  # not double counted
        self.assertEqual(load_seed_windows(Path(self.temp.name) / "absent.json"), [])

    # --- v1 migration -----------------------------------------------------------
    def test_v1_index_from_pr1_is_accepted_and_upgraded(self):
        flash = self.flash(datetime(2026, 10, 24, 10, tzinfo=timezone.utc), "v1")
        self.root.mkdir(parents=True)
        shard = {"schema": "uk-lightning-history-day.v1", "type": "FeatureCollection",
                 "metadata": {"date_utc": "2026-10-24", "count": 1}, "features": [flash]}
        (self.root / "days/2026/10").mkdir(parents=True)
        (self.root / "days/2026/10/24.json").write_text(json.dumps(shard))
        (self.root / "index.json").write_text(json.dumps({
            "schema": "uk-lightning-history-index.v1", "generated_utc": "2026-10-25T00:00:00.000Z",
            "archive_first_enabled_utc": "2026-10-23T00:00:00.000Z", "cutoff_date_utc": "2025-10-25",
            "days": [{"date": "2026-10-24", "path": "days/2026/10/24.json", "count": 1,
                      "first_observation_utc": flash["properties"]["time_utc"], "last_observation_utc": flash["properties"]["time_utc"]}],
            "scan_windows": [{"start_utc": "2026-10-24T09:00:00.000Z", "end_utc": "2026-10-24T12:00:00.000Z", "successful_runs": 1}]}))
        result = archive_observations(self.root, [], now=self.now)
        index = result["index"]
        self.assertEqual(index["schema"], SCHEMA_INDEX)
        self.assertEqual(index["archive_first_enabled_utc"], "2026-10-23T00:00:00.000Z")
        row = {r["date"]: r for r in index["days"]}["2026-10-24"]
        self.assertEqual((row["coverage"], row["scanned_seconds"], row["count"]), ("partial", 3 * 3600, 1))
        self.assertTrue(row["sha256"])
        (self.root / "index.json").write_text(json.dumps({"schema": "something-else", "days": []}))
        with self.assertRaises(ValueError):
            archive_observations(self.root, [], now=self.now)

    # --- D-1: a shard listed in the index but missing on disk -------------------
    def test_listed_shard_missing_on_disk_becomes_unknown_not_zero(self):
        day = datetime(2026, 10, 24, tzinfo=timezone.utc)
        first = archive_observations(self.root, [self.flash(day + timedelta(hours=10), "a"),
                                                 self.flash(day + timedelta(hours=11), "b")],
                                     now=self.now, scanned_window=self.scan(day, day + timedelta(days=1)))
        self.assertEqual({r["date"]: r for r in first["index"]["days"]}["2026-10-24"]["count"], 2)
        (self.root / day_path(day.date())).unlink()  # lost between runs (bad merge, manual edit, ...)
        second = archive_observations(self.root, [], now=self.now)
        row = {r["date"]: r for r in second["index"]["days"]}["2026-10-24"]
        self.assertEqual((row["coverage"], row["scanned_seconds"], row["count"], row["path"], row["bytes"], row["sha256"]),
                         ("none", 0, None, None, None, None))  # unknown, never a searched zero
        self.assertTrue(row["shard_lost"])
        self.assertEqual(second["lost_days"], ["2026-10-24"])
        self.assertEqual(second["index"]["archived_observations"], 0)
        # Sticky: a later run carrying one flash of that day must not turn it into a partial "count 1".
        third = archive_observations(self.root, [self.flash(day + timedelta(hours=12), "c")], now=self.now)
        row = {r["date"]: r for r in third["index"]["days"]}["2026-10-24"]
        self.assertEqual((row["coverage"], row["count"], row["path"]), ("none", None, None))
        self.assertEqual(third["dropped_lost_day"], 1)
        self.assertFalse((self.root / day_path(day.date())).exists())
        self.assertEqual(verify_archive(self.root)["shards_checked"], 0)

    def test_missing_hourly_part_marks_the_whole_day_unknown(self):
        day = datetime(2026, 10, 24, tzinfo=timezone.utc)
        flashes = [self.flash(day + timedelta(hours=h, minutes=m), f"p{h}-{m}", m) for h in (3, 4) for m in range(4)]
        archive_observations(self.root, flashes, now=self.now, scanned_window=self.scan(day, day + timedelta(days=1)),
                             target_shard_bytes=2000)
        (self.root / part_path(day.date(), 4)).unlink()
        result = archive_observations(self.root, [], now=self.now, target_shard_bytes=2000)
        row = {r["date"]: r for r in result["index"]["days"]}["2026-10-24"]
        self.assertEqual((row["coverage"], row["count"], row.get("parts")), ("none", None, None))
        self.assertEqual(result["lost_days"], ["2026-10-24"])

    # --- D-3: unparsed product names are not examined slots ----------------------
    def test_unparsed_slot_names_record_no_coverage(self):
        record = {"outcome": "success", "products_examined": 2, "slots_parsed": False,
                  "scanned_ranges": [{"start_utc": "2026-10-26T08:00:00.000Z", "end_utc": "2026-10-26T14:00:00.000Z"}]}
        self.assertEqual(windows_from_run_record(record), [])
        result = archive_observations(self.root, [], now=self.now, scanned_windows=windows_from_run_record(record))
        today = {r["date"]: r for r in result["index"]["days"]}["2026-10-26"]
        self.assertEqual((today["coverage"], today["scanned_seconds"], today["count"]), ("none", 0, None))
        self.assertEqual(len(windows_from_run_record({**record, "slots_parsed": True})), 1)

    # --- CLI --------------------------------------------------------------------
    def test_cli_ingest_and_verify(self):
        record_path = Path(self.temp.name) / "run.json"
        result_path = Path(self.temp.name) / "result.json"
        record_path.write_text(json.dumps({
            "schema": "uk-lightning-run-record.v1", "outcome": "success", "run_utc": "2026-10-26T13:05:58.000Z",
            "products_examined": 18, "lookback_hours": 6.0,
            "scanned_ranges": [{"start_utc": "2026-10-26T07:05:58.000Z", "end_utc": "2026-10-26T13:00:09.000Z"}],
            "features": [self.flash(self.now - timedelta(hours=2), "cli")]}))
        code = history.main(["ingest", "--archive-dir", str(self.root), "--run-record", str(record_path),
                             "--result-out", str(result_path), "--now", self.now.isoformat(),
                             "--seed-scan-windows", str(Path(self.temp.name) / "none.json")])
        self.assertEqual(code, 0)
        summary = json.loads(result_path.read_text())
        self.assertEqual(summary["archived_observations"], 1)
        self.assertEqual(summary["last_successful_scan_end_utc"], "2026-10-26T13:00:09.000Z")
        self.assertEqual(json.loads((self.root / "index.json").read_text())["last_run_lookback_hours"], 6.0)
        self.assertEqual(history.main(["verify", "--archive-dir", str(self.root)]), 0)
        record_path.write_text(json.dumps({"schema": "uk-lightning-run-record.v1", "outcome": "failure", "error": "x"}))
        with self.assertRaises(SystemExit):
            history.main(["ingest", "--archive-dir", str(self.root), "--run-record", str(record_path)])


if __name__ == "__main__":
    unittest.main()

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect
from collect import (
    LOOKBACK_MAX_HOURS, LOOKBACK_MIN_HOURS, assemble, build_feature, build_run_record, inside_uk_view, iso_utc,
    lookback_window, merge_ranges, parse_utc, product_slot, read_last_success, scanned_ranges, unscanned_ranges,
)

# A genuine product name from data/uk_lightning.geojson (sensing slot 12:30:09-12:40:09 UTC, 21 Oct 2026).
REAL_PRODUCT = ("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_20261021124026_L2PF_OPE_"
                "20261021123009_20261021124009_N__O_0076_0000")


def product_for(slot_start: datetime) -> str:
    """Build an LFL-style product name for a 10-minute slot starting at slot_start."""
    end = slot_start + timedelta(minutes=10)
    return ("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_"
            f"{end:%Y%m%d%H%M%S}_L2PF_OPE_{slot_start:%Y%m%d%H%M%S}_{end:%Y%m%d%H%M%S}_N__O_0001_0000")


class UKLightningTests(unittest.TestCase):
    def test_uk_extent_and_neighbouring_waters(self):
        self.assertTrue(inside_uk_view(-3.2, 55.9))
        self.assertTrue(inside_uk_view(-6.5, 54.5))
        self.assertFalse(inside_uk_view(20, 55.9))
        self.assertFalse(inside_uk_view(-3, float("nan")))

    def test_utc_normalisation(self):
        self.assertEqual(parse_utc("2026-10-22T15:00:00+01:00").isoformat(), "2026-10-22T14:00:00+00:00")

    def test_feature_timestamp_and_provenance(self):
        point = build_feature(-1.3, 52.1, datetime(2026, 10, 22, 12, tzinfo=timezone.utc), "product1", 1)
        self.assertEqual(point["geometry"]["coordinates"], [-1.3, 52.1])
        self.assertIn("not a confirmed ground strike", point["properties"]["raw_tech"])
        self.assertEqual(point["properties"]["source_product"], "MTG LI L2-LFL")

    def test_no_outside_points(self):
        self.assertIsNone(build_feature(15, 52, datetime.now(timezone.utc), "x", 1))

    def test_deduplicate_and_retention(self):
        now = datetime(2026, 10, 22, 17, tzinfo=timezone.utc)
        recent = build_feature(-1, 51, now - timedelta(hours=1), "recent", 1)
        old = build_feature(-1, 51, now - timedelta(days=9), "old", 1)
        result = assemble([old, recent], [recent], now, retention_days=7)
        self.assertEqual(len(result["features"]), 1)
        self.assertEqual(result["features"][0]["id"], recent["id"])

    def test_empty_collection_has_no_synthetic_strikes(self):
        output = assemble([], [], datetime.now(timezone.utc))
        self.assertEqual(output["features"], [])
        self.assertEqual(output["metadata"]["total_before_display_cap"], 0)

    def test_iso_utc_keeps_whole_seconds_and_strips_only_fractional_zeros(self):
        # Latent bug found by the archive audit: "12:32:20" used to become "12:32:2Z".
        self.assertEqual(iso_utc("2026-10-21T12:32:20"), "2026-10-21T12:32:20Z")
        self.assertEqual(iso_utc("2026-10-21T12:32:19.282000"), "2026-10-21T12:32:19.282Z")
        self.assertEqual(iso_utc("2026-10-21T12:32:19.000000"), "2026-10-21T12:32:19Z")
        self.assertEqual(iso_utc("2026-10-21T12:32:19.282Z"), "2026-10-21T12:32:19.282Z")
        self.assertEqual(iso_utc(datetime(2026, 10, 21, 12, 32, 19, 282000, tzinfo=timezone.utc)), "2026-10-21T12:32:19.282Z")


class LookbackWindowTests(unittest.TestCase):
    """window = max(6 h, now - last successful scan end + 30 min) capped at 48 h."""

    def setUp(self):
        self.now = datetime(2026, 10, 26, 14, 17, tzinfo=timezone.utc)

    def test_defaults_to_six_hours_without_history(self):
        window, capped = lookback_window(self.now, None)
        self.assertEqual(window, timedelta(hours=LOOKBACK_MIN_HOURS))
        self.assertFalse(capped)

    def test_recent_success_keeps_the_six_hour_minimum(self):
        window, capped = lookback_window(self.now, self.now - timedelta(hours=1, minutes=17))
        self.assertEqual(window, timedelta(hours=6))
        self.assertFalse(capped)

    def test_gap_since_last_success_plus_margin_wins_over_minimum(self):
        window, capped = lookback_window(self.now, self.now - timedelta(hours=10))
        self.assertEqual(window, timedelta(hours=10, minutes=30))
        self.assertFalse(capped)

    def test_gap_is_capped_at_48_hours_and_flagged(self):
        window, capped = lookback_window(self.now, self.now - timedelta(days=3))
        self.assertEqual(window, timedelta(hours=LOOKBACK_MAX_HOURS))
        self.assertTrue(capped)

    def test_exactly_at_the_cap_is_not_flagged(self):
        window, capped = lookback_window(self.now, self.now - timedelta(hours=47, minutes=30))
        self.assertEqual(window, timedelta(hours=48))
        self.assertFalse(capped)

    def test_last_success_comes_from_status_then_index_then_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(read_last_success(root / "status.json", root / "history"), (None, "default"))
            (root / "history").mkdir()
            (root / "history" / "index.json").write_text(json.dumps({
                "scan_windows": [{"start_utc": "2026-10-26T01:00:00Z", "end_utc": "2026-10-26T04:40:09Z"},
                                 {"start_utc": "2026-10-25T01:00:00Z", "end_utc": "2026-10-25T04:00:00Z"}]}))
            value, source = read_last_success(root / "status.json", root / "history")
            self.assertEqual((value.isoformat(), source), ("2026-10-26T04:40:09+00:00", "index"))
            (root / "status.json").write_text(json.dumps({"last_successful_scan_end_utc": "2026-10-26T13:00:09Z"}))
            value, source = read_last_success(root / "status.json", root / "history")
            self.assertEqual((value.isoformat(), source), ("2026-10-26T13:00:09+00:00", "status"))


class ProductSlotTests(unittest.TestCase):
    """Only examined 10-minute slots count as searched; the tail is unobserved."""

    def test_real_product_name_yields_its_sensing_slot(self):
        start, end = product_slot(REAL_PRODUCT)
        self.assertEqual(start.isoformat(), "2026-10-21T12:30:09+00:00")
        self.assertEqual(end.isoformat(), "2026-10-21T12:40:09+00:00")
        self.assertIsNone(product_slot("not-a-product"))
        self.assertIsNone(product_slot(""))

    def test_adjacent_slots_merge_and_clip_to_the_query_window(self):
        beginning = datetime(2026, 10, 26, 10, 5, 58, tzinfo=timezone.utc)
        now = datetime(2026, 10, 26, 13, 5, 58, tzinfo=timezone.utc)
        first = datetime(2026, 10, 26, 10, 0, 9, tzinfo=timezone.utc)
        names = [product_for(first + timedelta(minutes=10 * i)) for i in range(18)]  # 10:00:09 .. 13:00:09
        ranges, slots, parsed = scanned_ranges(names, beginning, now)
        self.assertTrue(parsed)
        self.assertEqual(len(slots), 18)
        self.assertEqual(ranges, [(beginning, datetime(2026, 10, 26, 13, 0, 9, tzinfo=timezone.utc))])
        gaps, tail = unscanned_ranges(ranges, beginning, now)
        self.assertEqual(gaps, [])
        self.assertEqual(tail, 349)  # 13:00:09 -> 13:05:58 not yet published: unknown, not zero

    def test_missing_middle_slot_is_a_source_gap(self):
        beginning = datetime(2026, 10, 26, 10, 0, 9, tzinfo=timezone.utc)
        now = datetime(2026, 10, 26, 11, 0, 9, tzinfo=timezone.utc)
        names = [product_for(beginning + timedelta(minutes=10 * i)) for i in (0, 1, 3, 4, 5)]  # slot 2 missing
        ranges, slots, parsed = scanned_ranges(names, beginning, now)
        self.assertEqual(len(ranges), 2)
        gaps, tail = unscanned_ranges(ranges, beginning, now)
        self.assertEqual(gaps, [(datetime(2026, 10, 26, 10, 20, 9, tzinfo=timezone.utc),
                                 datetime(2026, 10, 26, 10, 30, 9, tzinfo=timezone.utc))])
        self.assertEqual(tail, 0)

    def test_long_tail_is_also_reported_as_a_gap(self):
        beginning = datetime(2026, 10, 26, 10, 0, 9, tzinfo=timezone.utc)
        now = datetime(2026, 10, 26, 12, 0, 9, tzinfo=timezone.utc)
        names = [product_for(beginning)]
        ranges, _, _ = scanned_ranges(names, beginning, now)
        gaps, tail = unscanned_ranges(ranges, beginning, now)
        self.assertEqual(tail, 6600)
        self.assertEqual(gaps, [(datetime(2026, 10, 26, 10, 10, 9, tzinfo=timezone.utc), now)])

    def test_unparseable_names_fall_back_to_the_query_window_and_say_so(self):
        beginning = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
        now = datetime(2026, 10, 26, 13, tzinfo=timezone.utc)
        ranges, slots, parsed = scanned_ranges(["opaque-product"], beginning, now)
        self.assertFalse(parsed)
        self.assertEqual(ranges, [(beginning, now)])
        self.assertEqual(slots, [])

    def test_merge_ranges_tolerates_one_second_seams(self):
        a = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        merged = merge_ranges([(a, a + timedelta(minutes=10)), (a + timedelta(minutes=10, seconds=1), a + timedelta(minutes=20))])
        self.assertEqual(merged, [(a, a + timedelta(minutes=20))])


class RunRecordTests(unittest.TestCase):
    def test_run_record_carries_window_slots_and_uncapped_features(self):
        now = datetime(2026, 10, 26, 13, 5, 58, tzinfo=timezone.utc)
        beginning = now - timedelta(hours=6)
        names = [product_for(datetime(2026, 10, 26, 7, 0, 9, tzinfo=timezone.utc) + timedelta(minutes=10 * i)) for i in range(36)]
        incoming = [build_feature(-2, 52, now - timedelta(hours=1), names[0], 7)]
        existing = [build_feature(-2, 52, now - timedelta(days=2), "older", 1)]
        feed = assemble(existing, incoming, now)
        record = build_run_record(now=now, beginning=beginning, lookback_source="status", lookback_capped=False,
                                  last_success_before=now - timedelta(hours=1), product_names=names,
                                  products_examined=36, incoming=incoming, existing=existing, feed=feed)
        self.assertEqual(record["schema"], "uk-lightning-run-record.v1")
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["lookback_hours"], 6)
        self.assertEqual(record["slots_examined"], 36)
        self.assertEqual(record["scanned_ranges"], [{"start_utc": "2026-10-26T07:05:58.000Z", "end_utc": "2026-10-26T13:00:09.000Z"}])
        self.assertEqual(record["scanned_end_utc"], "2026-10-26T13:00:09.000Z")
        self.assertEqual(record["tail_unobserved_seconds"], 349)
        self.assertEqual(record["incoming_features"], 1)
        self.assertEqual(len(record["features"]), 2)
        self.assertEqual(record["feed_features"], 2)

    def test_main_without_credentials_writes_a_failure_record_and_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {k: v for k, v in os.environ.items() if not k.startswith("EUMETSAT_")}
            saved = dict(os.environ)
            os.environ.clear()
            os.environ.update(env)
            try:
                with self.assertRaises(RuntimeError):
                    collect.main(["--auto-window", "--out", str(root / "feed.geojson"), "--status", str(root / "status.json"),
                                  "--history-dir", str(root / "history"), "--run-record", str(root / "run.json")])
            finally:
                os.environ.clear()
                os.environ.update(saved)
            record = json.loads((root / "run.json").read_text())
            self.assertEqual(record["outcome"], "failure")
            self.assertIn("EUMETSAT_CONSUMER_KEY", record["error"])
            self.assertEqual(record["lookback_hours"], 6)
            self.assertEqual(record["lookback_source"], "default")
            self.assertEqual(record["features"], [])
            self.assertFalse((root / "feed.geojson").exists(), "a failed run must not write the feed")


if __name__ == "__main__":
    unittest.main()

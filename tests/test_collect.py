import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collect import assemble, build_feature, inside_uk_view, parse_utc


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


if __name__ == "__main__":
    unittest.main()

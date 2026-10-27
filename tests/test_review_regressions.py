"""Regressions found in review of PR #3 (SYNTHETIC TEST- ids, temp dirs only)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pyarrow.parquet as pq  # noqa: E402

import history  # noqa: E402
import parquet_store as ps  # noqa: E402
import verify_parquet as vp  # noqa: E402
from collect import build_feature  # noqa: E402
from test_parquet_store import NOW  # noqa: E402


class ZeroSearchedHours(unittest.TestCase):
    def test_no_searched_time_publishes_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            archive, store = Path(temp) / "history", Path(temp) / "parquet"
            history.archive_observations(archive, [], now=NOW)
            ps.build(archive, store, run_id="r", synthetic=True)
            vp.verify(archive, store, allow_synthetic=True)
            for row in pq.read_table(store / "rollups/substation_optical_flash_proximity.parquet").to_pylist():
                self.assertEqual(row["hours_observed"], 0)
                self.assertEqual(row["count_state"], "UNKNOWN")
                self.assertIsNone(row["optical_flash_proximity_10km"])
            head = json.loads((store / "rollups/headline.json").read_text())
            self.assertEqual(head["archive"]["count_state"], "UNKNOWN")
            self.assertIsNone(head["archive"]["flashes"])
            self.assertIsNone(head["substations"]["with_any_within_10km"])


class EpisodeNextToLostShard(unittest.TestCase):
    def test_edge_over_shard_lost_day_is_not_searched(self):
        lost_day = (NOW - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        times = [lost_day - timedelta(minutes=4, seconds=30),     # 23:55:30, 4.5 min before the lost day
                 lost_day + timedelta(minutes=10, seconds=30),    # inside the day that will be lost
                 lost_day + timedelta(days=1, minutes=3)]         # 00:03 the day after: gap crosses the lost day
        flashes = [build_feature(-1.5, 52.5, t, "TEST-product", i) for i, t in enumerate(times, 1)]
        with tempfile.TemporaryDirectory() as temp:
            archive, store = Path(temp) / "history", Path(temp) / "parquet"
            history.archive_observations(archive, flashes, now=NOW, scanned_window=history.successful_scan(
                (NOW - timedelta(days=4)).isoformat(), NOW.isoformat(), 18))
            index = json.loads((archive / "index.json").read_text())
            row = next(r for r in index["days"] if r["date"] == lost_day.date().isoformat())
            (archive / row["path"]).unlink()
            row["shard_lost"] = True
            (archive / "index.json").write_text(json.dumps(index))
            ps.build(archive, store, run_id="r", synthetic=True)
            vp.verify(archive, store, allow_synthetic=True)
            first, second = pq.read_table(store / "rollups/storm_episodes.parquet").to_pylist()
            self.assertTrue(first["searched_30min_before"])
            self.assertFalse(first["searched_30min_after"])
            self.assertEqual(first["coverage"], "edge_unsearched_may_extend")
            self.assertFalse(second["searched_30min_before"])
            self.assertEqual(second["coverage"], "edge_unsearched_may_extend")


if __name__ == "__main__":
    unittest.main()

"""Regressions found in review of PR #3 (SYNTHETIC TEST- ids, temp dirs only)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pyarrow.parquet as pq  # noqa: E402

import history  # noqa: E402
import parquet_store as ps  # noqa: E402
import verify_parquet as vp  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()

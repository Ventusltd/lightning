"""Month density grids (grid/YYYY-MM.json, uk-lightning-history-grid.v1); no network.

The grid is derived from the day shards only: each day's cells sum exactly to
its index count, never-searched days stay unknown (count null, no cells), a
grid day that differs from the index is stale, and the bytes are a pure
function of the archive (deterministic, rewritten only when a month changes).

Synthetic features exist ONLY inside temporary directories created by these
tests (ids start with "TEST-"); nothing synthetic is ever published.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import collect
import grid
import history
from collect import build_feature
from history import archive_observations, successful_scan
from test_backfill import FakeDownloader

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)


def flash(moment, tag, index=0, lon=-2.12, lat=51.11):
    return build_feature(lon, lat, moment, "TEST-" + tag, index)


def scan(start, end):
    return successful_scan(start.isoformat(), end.isoformat(), 18)


def load(root, month):
    return json.loads((root / "grid" / f"{month}.json").read_text())


def summed(grid_obj, day):
    entries, cells = grid.parse_grid(json.dumps(grid_obj), grid_obj["month"])
    return cells.get(day, {})


class GridTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "history"

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, flashes, *, now=NOW, windows=(), **kwargs):
        return archive_observations(self.root, flashes, now=now, scanned_windows=list(windows), **kwargs)

    def seed(self):
        d1 = datetime(2026, 10, 24, tzinfo=timezone.utc)
        d2 = datetime(2026, 9, 30, tzinfo=timezone.utc)
        flashes = [flash(d1 + timedelta(hours=3), "a", 0, -2.12, 51.11), flash(d1 + timedelta(hours=4), "b", 1, -2.11, 51.13),
                   flash(d1 + timedelta(hours=5), "c", 2, 0.5, 52.0), flash(d2 + timedelta(hours=9), "d", 0, -4.0, 55.0)]
        return self.archive(flashes, windows=[scan(d1, d1 + timedelta(days=1)), scan(d2, d2 + timedelta(days=1)),
                                              scan(datetime(2026, 10, 25, tzinfo=timezone.utc),
                                                   datetime(2026, 10, 25, 12, tzinfo=timezone.utc))])

    # --- cell rule -------------------------------------------------------
    def test_cell_rule_is_a_clamped_0_1_degree_grid_over_the_view_bounds(self):
        self.assertEqual((grid.COLS, grid.ROWS, grid.CELL_DEG), (140, 120, 0.1))
        self.assertEqual(grid.cell_of(-11.0, 49.5), 0)
        self.assertEqual(grid.cell_of(3.0, 61.5), grid.COLS * grid.ROWS - 1)  # the closed east/north edges clamp
        self.assertEqual(grid.cell_of(-10.9, 49.5), 1)  # exact 0.1 steps never fall into the cell below
        self.assertEqual(grid.cell_of(-11.0, 49.6), grid.COLS)
        self.assertEqual(grid.cell_of(-10.95, 49.55), 0)
        self.assertEqual(grid.cell_of(-2.12, 51.11), 16 * 140 + 88)  # row floor(16.1), col floor(88.8)

    # --- contents --------------------------------------------------------
    def test_ingest_writes_one_grid_per_retention_month_summing_exactly_to_the_index(self):
        result = self.seed()
        self.assertIsNone(result["grid"]["error"])
        index = result["index"]
        months = sorted({r["date"][:7] for r in index["days"]})
        self.assertEqual(len(months), 13)  # 2025-10 .. 2026-10: any window sums from at most 13 files
        self.assertEqual(sorted(p.name for p in (self.root / "grid").iterdir()), [f"{m}.json" for m in months])
        total = 0
        for m in months:
            g = load(self.root, m)
            self.assertEqual(g["schema"], grid.SCHEMA_GRID)
            self.assertIn("not ground strikes", g["observation"])
            entries, cells = grid.parse_grid(json.dumps(g), m)
            for e in entries:
                self.assertEqual(sum(cells.get(e["date"], {}).values()), e["count"] or 0)
            total += g["total"]
        self.assertEqual(total, index["archived_observations"])
        oct_ = load(self.root, "2026-10")
        self.assertEqual(summed(oct_, "2026-10-24"), {grid.cell_of(-2.12, 51.11): 2, grid.cell_of(0.5, 52.0): 1})
        self.assertEqual(grid.verify_grids(self.root, deep=True), {"months": 13})

    def test_unknown_days_are_null_not_zero_and_a_searched_empty_day_is_zero(self):
        self.seed()
        days = {e["date"]: e for e in load(self.root, "2026-10")["days"]}
        self.assertEqual((days["2026-10-20"]["coverage"], days["2026-10-20"]["count"]), ("none", None))
        self.assertEqual(days["2026-10-20"]["sha256"], [])
        self.assertEqual((days["2026-10-25"]["coverage"], days["2026-10-25"]["count"]), ("partial", 0))
        self.assertNotIn("2026-10-27", days)  # the future is not listed at all
        first = load(self.root, "2025-10")["days"]
        self.assertEqual(first[0]["date"], "2025-10-26")  # days before the cutoff are not listed
        self.assertEqual(load(self.root, "2025-10")["cells"], [])

    def test_days_carry_the_shard_sha256_the_client_compares_with_the_index(self):
        index = self.seed()["index"]
        row = {r["date"]: r for r in index["days"]}["2026-10-24"]
        entry = {e["date"]: e for e in load(self.root, "2026-10")["days"]}["2026-10-24"]
        self.assertEqual(entry, {"date": "2026-10-24", "coverage": "full", "count": 3, "sha256": [row["sha256"]]})

    def test_split_days_sum_their_hourly_parts(self):
        day = datetime(2026, 10, 24, tzinfo=timezone.utc)
        flashes = [flash(day + timedelta(hours=h, minutes=m), f"s{h}-{m}", m) for h in (3, 4, 17) for m in range(4)]
        result = self.archive(flashes, windows=[scan(day, day + timedelta(days=1))], target_shard_bytes=2000)
        row = {r["date"]: r for r in result["index"]["days"]}["2026-10-24"]
        entry = {e["date"]: e for e in load(self.root, "2026-10")["days"]}["2026-10-24"]
        self.assertEqual(entry["sha256"], [p["sha256"] for p in row["parts"]])
        self.assertEqual(sum(summed(load(self.root, "2026-10"), "2026-10-24").values()), 12)

    # --- determinism and incremental refresh ----------------------------------
    def test_rerun_is_byte_identical_and_a_rebuild_from_scratch_gives_the_same_bytes(self):
        self.seed()
        before = {p.name: p.read_bytes() for p in (self.root / "grid").iterdir()}
        again = self.archive([], windows=[])
        self.assertEqual((again["grid"]["written"], again["grid"]["days_read"]), ([], 0))
        for p in (self.root / "grid").iterdir():
            p.unlink()
        index = json.loads((self.root / "index.json").read_text())
        grid.refresh_grids(self.root, index)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "grid").iterdir()}, before)

    def test_a_new_flash_rewrites_only_its_month_and_rereads_only_its_day(self):
        self.seed()
        sept = (self.root / "grid" / "2026-09.json").read_bytes()
        result = self.archive([flash(datetime(2026, 10, 24, 20, tzinfo=timezone.utc), "late", 7, 1.0, 53.0)])
        self.assertEqual(result["grid"]["written"], ["grid/2026-10.json"])
        self.assertEqual(result["grid"]["days_read"], 1)
        self.assertEqual((self.root / "grid" / "2026-09.json").read_bytes(), sept)
        self.assertEqual(sum(summed(load(self.root, "2026-10"), "2026-10-24").values()), 4)
        grid.verify_grids(self.root, deep=True)

    def test_months_leaving_retention_are_removed(self):
        self.seed()
        later = NOW + timedelta(days=40)
        result = self.archive([], now=later)
        self.assertIn("grid/2025-10.json", result["grid"]["removed"])
        self.assertIn("grid/2025-11.json", result["grid"]["removed"])
        self.assertTrue((self.root / "grid" / "2026-12.json").exists())
        first = load(self.root, "2025-12")["days"][0]["date"]
        self.assertEqual(first, "2025-12-05")
        grid.verify_grids(self.root, deep=True)

    # --- staleness and failure handling ---------------------------------------
    def test_a_grid_that_differs_from_the_index_is_stale_and_verify_refuses_it(self):
        self.seed()
        path = self.root / "grid" / "2026-10.json"
        g = json.loads(path.read_text())
        for e in g["days"]:
            if e["date"] == "2026-10-24":
                e["sha256"] = ["0" * 64]
        path.write_text(json.dumps(g))
        with self.assertRaisesRegex(ValueError, "stale"):
            grid.verify_grids(self.root)
        # The next archive run repairs it from the shards.
        result = self.archive([])
        self.assertEqual(result["grid"]["written"], ["grid/2026-10.json"])
        grid.verify_grids(self.root, deep=True)

    def test_a_corrupt_grid_is_rebuilt_and_bad_cell_rows_are_refused(self):
        self.seed()
        (self.root / "grid" / "2026-09.json").write_text("{not json")
        self.assertEqual(self.archive([])["grid"]["written"], ["grid/2026-09.json"])
        g = load(self.root, "2026-10")
        for bad in ([[999999, 24, 1]], [[1, 32, 1]], [[1, 24, 0]], [[1, 24]], ["x"]):
            with self.assertRaises(ValueError):
                grid.parse_grid(json.dumps({**g, "cells": bad}), "2026-10")
        with self.assertRaisesRegex(ValueError, "sum"):
            grid.parse_grid(json.dumps({**g, "cells": g["cells"][:-1]}), "2026-10")

    def test_a_grid_failure_never_blocks_the_archive(self):
        with mock.patch.object(grid, "refresh_grids", side_effect=ValueError("boom")):
            result = self.seed()
        self.assertEqual(result["grid"]["error"], "ValueError: boom")
        self.assertEqual(result["index"]["archived_observations"], 4)
        history.verify_archive(self.root)

    def test_backfill_regenerates_the_month_of_the_filled_date(self):
        self.seed()
        target = datetime(2026, 10, 20, tzinfo=timezone.utc)
        record = Path(self.temp.name) / "rb.json"
        flashes = [flash(target + timedelta(hours=h, minutes=3), f"bf{h}") for h in (2, 9)]
        collect.rebuild_day_record("2026-10-20", record, now=NOW, downloader=FakeDownloader(flashes=flashes))
        result = history.backfill_day(self.root, "2026-10-20", now=NOW, rebuild_record=record)
        self.assertEqual(result["grid"]["written"], ["grid/2026-10.json"])
        entry = {e["date"]: e for e in load(self.root, "2026-10")["days"]}["2026-10-20"]
        self.assertEqual((entry["coverage"], entry["count"]), ("full", 2))
        grid.verify_grids(self.root, deep=True)

    def test_cli_build_is_the_one_off_generator_and_is_idempotent(self):
        self.seed()
        for p in (self.root / "grid").iterdir():
            p.unlink()
        run = subprocess.run([sys.executable, str(ROOT / "grid.py"), "build", "--archive-dir", str(self.root)],
                             capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(json.loads(run.stdout)["written"]), 13)
        run = subprocess.run([sys.executable, str(ROOT / "grid.py"), "verify", "--deep", "--archive-dir", str(self.root)],
                             capture_output=True, text=True, cwd=ROOT)
        self.assertEqual((run.returncode, json.loads(run.stdout)), (0, {"months": 13}))


if __name__ == "__main__":
    unittest.main()

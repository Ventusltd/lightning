"""reproduce.py: independent recomputation of the archive from its committed files; no network.

Every fixture archive is written by the production writer (history.archive_observations)
inside a temporary directory, then checked by reproduce.py, which shares no code with
the writer. Tampering with any shard, index value or grid cell must be reported, and
unknown (never searched) must never be counted as zero.

Synthetic features exist ONLY inside temporary directories created by these tests
(ids start with "TEST-" or carry a TEST product name); nothing synthetic is ever published.
"""
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import grid  # noqa: E402
import history  # noqa: E402
import reproduce  # noqa: E402
from collect import build_feature  # noqa: E402
from history import archive_observations, successful_scan  # noqa: E402

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
D24 = datetime(2026, 10, 24, tzinfo=timezone.utc)
D23 = datetime(2026, 10, 23, tzinfo=timezone.utc)
D20 = datetime(2026, 10, 20, tzinfo=timezone.utc)
D30 = datetime(2026, 9, 30, tzinfo=timezone.utc)
PRODUCT = ("TEST-LI-2-LFL_C_EUMT_20261024100009_L2PF_OPE_20261024100009_20261024101009_N__O_0061_0000")


def flash(moment, tag, index=0, lon=-2.12, lat=51.11):
    return build_feature(lon, lat, moment, "TEST-" + tag, index)


def scan(start, end):
    return successful_scan(start.isoformat(), end.isoformat(), 18)


class ReproduceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        self.root = self.data / "history"

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, **kwargs):
        """24 Oct full (3 flashes), 23 Oct searched with no flash, 25 Oct half searched,
        30 Sep full (1 flash), every other retained date never searched."""
        flashes = [flash(D24 + timedelta(hours=3), "a", 0), flash(D24 + timedelta(hours=4), "b", 1, -2.11, 51.13),
                   flash(D24 + timedelta(hours=5), "c", 2, 0.5, 52.0), flash(D30 + timedelta(hours=9), "d", 0, -4.0, 55.0)]
        windows = [scan(D23, D24 + timedelta(days=1)), scan(D30, D30 + timedelta(days=1)),
                   scan(D24 + timedelta(days=1), D24 + timedelta(days=1, hours=12))]
        return archive_observations(self.root, flashes, now=NOW, scanned_windows=windows, **kwargs)

    def check(self):
        return reproduce.check(self.data)

    def index(self):
        return json.loads((self.root / "index.json").read_text())

    def write_index(self, index):
        (self.root / "index.json").write_text(reproduce.canonical(index))

    def rewrite_json(self, rel, change):
        path = self.root / rel
        obj = json.loads(path.read_text())
        change(obj)
        path.write_text(reproduce.canonical(obj))

    # --- a faithful archive reproduces --------------------------------------------------
    def test_writer_output_reproduces_with_no_mismatch_and_agrees_with_the_writers_own_verify(self):
        self.seed()
        history.verify_archive(self.root)
        grid.verify_grids(self.root, deep=True)
        summary = self.check()
        self.assertEqual(summary["verdict"], "reproduced", summary["mismatches"])
        self.assertEqual(summary["mismatch_count"], 0)
        h = summary["headline"]
        self.assertEqual(h["days_in_window"], 366)  # 2025-10-26 .. 2026-10-26
        self.assertEqual(h["optical_flashes"], 4)
        self.assertEqual((h["days_full"], h["days_partial"]), (3, 1))  # 23, 24 Oct, 30 Sep full; 25 Oct partial
        self.assertEqual(h["days_searched"], 4)
        self.assertEqual(h["days_never_searched"], 362)
        self.assertEqual(h["days_with_flashes"], 2)
        self.assertEqual(h["days_searched_without_flashes"], 2)  # 23 Oct and 25 Oct: searched, count 0
        self.assertEqual(h["searched_seconds"], 3 * 86400 + 12 * 3600)
        self.assertEqual(h["busiest_day"], {"date": "2026-10-24", "count": 3})
        self.assertEqual(h["monthly"]["2026-10"]["flashes"], 3)
        self.assertEqual(summary["grids"]["2026-09"]["total"], 1)
        self.assertEqual(summary["inputs"]["history_files"], len([p for p in self.root.rglob("*") if p.is_file()]))

    def test_summary_digest_is_deterministic_and_moves_with_any_byte(self):
        self.seed()
        first, second = self.check(), self.check()
        self.assertEqual(first["summary_sha256"], second["summary_sha256"])
        self.assertEqual(first["summary_sha256"], reproduce.summary_digest(first))
        self.assertRegex(first["summary_sha256"], r"^[0-9a-f]{64}$")
        archive_observations(self.root, [flash(D24 + timedelta(hours=6), "e", 3)], now=NOW)
        third = self.check()
        self.assertEqual(third["verdict"], "reproduced", third["mismatches"])
        self.assertNotEqual(third["inputs"]["manifest_sha256"], first["inputs"]["manifest_sha256"])
        self.assertNotEqual(third["summary_sha256"], first["summary_sha256"])

    def test_unknown_is_never_counted_as_zero(self):
        self.seed()
        rows = {r["date"]: r for r in self.index()["days"]}
        self.assertEqual((rows["2026-10-22"]["coverage"], rows["2026-10-22"]["count"]), ("none", None))
        h = self.check()["headline"]
        self.assertEqual(h["days_never_searched"], 362)
        self.assertEqual(h["days_searched_without_flashes"], 2)
        self.assertEqual(h["unsearched_seconds"], 366 * 86400 - h["searched_seconds"])

    def test_split_day_parts_reproduce(self):
        self.seed(target_shard_bytes=900)
        index = self.index()
        row = next(r for r in index["days"] if r["date"] == "2026-10-24")
        self.assertIsNone(row["path"])
        self.assertEqual(len(row["parts"]), 3)
        summary = self.check()
        self.assertEqual(summary["verdict"], "reproduced", summary["mismatches"])
        self.assertEqual(summary["headline"]["split_days"], ["2026-10-24"])

    def test_flash_proof_fallback_for_a_day_with_flashes_but_no_window(self):
        moment = datetime(2026, 10, 24, 10, 3, tzinfo=timezone.utc)
        archive_observations(self.root, [build_feature(-2.0, 52.0, moment, PRODUCT, 7),
                                         flash(D20 + timedelta(hours=1), "x", 0)], now=NOW)
        rows = {r["date"]: r for r in self.index()["days"]}
        self.assertEqual((rows["2026-10-24"]["coverage"], rows["2026-10-24"]["scanned_seconds"]), ("partial", 600))
        summary = self.check()
        self.assertEqual(summary["verdict"], "reproduced", summary["mismatches"])

    def test_shard_lost_day_reproduces_as_unknown(self):
        self.seed()
        (self.root / "days/2026/09/30.json").unlink()
        archive_observations(self.root, [], now=NOW)
        rows = {r["date"]: r for r in self.index()["days"]}
        self.assertTrue(rows["2026-09-30"]["shard_lost"])
        summary = self.check()
        self.assertEqual(summary["verdict"], "reproduced", summary["mismatches"])
        self.assertEqual(summary["headline"]["days_shard_lost"], 1)
        self.assertEqual(summary["headline"]["optical_flashes"], 3)

    # --- tampering is reported -------------------------------------------------------------
    def assert_mismatch(self, pattern):
        summary = self.check()
        self.assertEqual(summary["verdict"], "mismatch")
        self.assertTrue(any(re.search(pattern, m) for m in summary["mismatches"]),
                        summary["mismatches"])
        return summary

    def test_moved_flash_breaks_sha256_and_grid(self):
        self.seed()
        self.rewrite_json("days/2026/10/24.json",
                          lambda o: o["features"][0]["geometry"].__setitem__("coordinates", [-3.5, 53.0]))
        self.assert_mismatch(r"index row 2026-10-24")
        self.assert_mismatch(r"grid/2026-10\.json: cells differ")

    def test_index_total_and_row_count_edits_are_reported(self):
        self.seed()
        index = self.index()
        index["archived_observations"] += 1
        self.write_index(index)
        self.assert_mismatch(r"index\.archived_observations")
        index["archived_observations"] -= 1
        next(r for r in index["days"] if r["date"] == "2026-10-24")["count"] = 2
        self.write_index(index)
        self.assert_mismatch(r"index row 2026-10-24")

    def test_never_searched_row_turned_into_zero_is_reported(self):
        self.seed()
        index = self.index()
        row = next(r for r in index["days"] if r["date"] == "2026-10-22")
        row["count"] = 0
        self.write_index(index)
        self.assert_mismatch(r"count must be null iff coverage is none")

    def test_coverage_upgrade_without_a_scan_window_is_reported(self):
        self.seed()
        index = self.index()
        row = next(r for r in index["days"] if r["date"] == "2026-10-25")
        row.update(coverage="full", scanned_seconds=86400, sealed=True)
        self.write_index(index)
        self.assert_mismatch(r"index row 2026-10-25")

    def test_grid_cell_edit_is_reported(self):
        self.seed()
        self.rewrite_json("grid/2026-09.json", lambda o: o["cells"][0].__setitem__(2, 5))
        self.assert_mismatch(r"grid/2026-09\.json: cells differ")

    def test_missing_shard_unexpected_file_and_non_canonical_bytes_are_reported(self):
        self.seed()
        path = self.root / "days/2026/10/24.json"
        path.write_text(json.dumps(json.loads(path.read_text()), indent=1))
        self.assert_mismatch(r"not the canonical serialisation")
        (self.root / "notes.txt").write_text("x")
        self.assert_mismatch(r"notes\.txt: unexpected file")
        (self.root / "days/2026/09/30.json").unlink()
        self.assert_mismatch(r"index row 2026-09-30")

    def test_flash_outside_bounds_or_on_another_date_is_reported(self):
        self.seed()
        self.rewrite_json("days/2026/09/30.json",
                          lambda o: o["features"][0]["geometry"].__setitem__("coordinates", [5.0, 55.0]))
        self.assert_mismatch(r"outside the UK view rectangle")
        self.rewrite_json("days/2026/09/30.json",
                          lambda o: o["features"][0]["properties"].__setitem__("time_utc", "2026-10-01T09:00:00.000Z"))
        self.assert_mismatch(r"not on 2026-09-30 UTC")

    # --- compare-day ------------------------------------------------------------------------
    def record(self, features, *, start=D24, seconds=86400, **extra):
        rec = {"schema": "uk-lightning-run-record.v1", "outcome": "success", "run_utc": "2026-10-29T00:00:00.000Z",
               "rebuild_day_utc": start.date().isoformat(), "products_examined": 144, "slots_examined": 144,
               "products_unparsed": 0, "unparsed_slots_utc": [], "outside_day": 0,
               "scanned_ranges": [{"start_utc": start.isoformat(), "end_utc": (start + timedelta(seconds=seconds)).isoformat()}],
               "features": features, **extra}
        path = Path(self.temp.name) / "record.json"
        path.write_text(json.dumps(rec))
        return path

    def test_compare_day_identical_differs_and_unknown(self):
        self.seed()
        same = [flash(D24 + timedelta(hours=3), "a", 0), flash(D24 + timedelta(hours=4), "b", 1, -2.11, 51.13),
                flash(D24 + timedelta(hours=5), "c", 2, 0.5, 52.0)]
        result = reproduce.compare_day(self.data, "2026-10-24", self.record(same))
        self.assertEqual(result["verdict"], "identical")
        self.assertEqual(result["matched_time_position"], 3)
        extra = same + [flash(D24 + timedelta(hours=7), "z", 9, 1.0, 52.0),
                        flash(D24 - timedelta(minutes=1), "prev", 0)]  # previous date: ignored
        result = reproduce.compare_day(self.data, "2026-10-24", self.record(extra))
        self.assertEqual((result["verdict"], result["only_in_record"], result["only_in_archive"]), ("differs", 1, 0))
        result = reproduce.compare_day(self.data, "2026-10-24", self.record(same, seconds=86000))
        self.assertEqual(result["verdict"], "same_flashes_different_coverage")
        result = reproduce.compare_day(self.data, "2026-10-22", self.record([], start=D24 - timedelta(days=2)))
        self.assertEqual(result["verdict"], "archive_unknown")

    def test_compare_day_refuses_a_failed_or_wrong_day_record(self):
        self.seed()
        with self.assertRaises(ValueError):
            reproduce.compare_day(self.data, "2026-10-24", self.record([], outcome="failure"))
        with self.assertRaises(ValueError):
            reproduce.compare_day(self.data, "2026-10-23", self.record([]))

    # --- CLI --------------------------------------------------------------------------------
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = reproduce.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_exit_codes_json_and_expect(self):
        self.seed()
        target = Path(self.temp.name) / "out" / "summary.json"
        code, out, _ = self.run_cli("check", "--data-dir", str(self.data), "--json", str(target))
        self.assertEqual(code, 0, out)
        self.assertIn("verdict: REPRODUCED", out)
        written = json.loads(target.read_text())
        self.assertEqual(written["summary_sha256"], reproduce.summary_digest(written))
        self.assertIn(written["summary_sha256"], out)
        code, _, _ = self.run_cli("check", "--data-dir", str(self.data), "--expect", written["summary_sha256"])
        self.assertEqual(code, 0)
        code, _, err = self.run_cli("check", "--data-dir", str(self.data), "--expect", "0" * 64)
        self.assertEqual(code, 1)
        self.assertIn("does not match", err)
        code, _, _ = self.run_cli("--data-dir", str(self.data))  # "check" is the default command
        self.assertEqual(code, 0)
        index = self.index()
        index["scanned_days"] += 1
        self.write_index(index)
        code, out, _ = self.run_cli("check", "--data-dir", str(self.data))
        self.assertEqual(code, 1)
        self.assertIn("index.scanned_days", out)
        code, _, err = self.run_cli("check", "--data-dir", str(Path(self.temp.name) / "nowhere"))
        self.assertEqual(code, 2)
        self.assertIn("REPRODUCE FAILURE", err)

    def test_ci_runs_the_reproduction_on_main_and_the_tests_on_changes(self):
        verify = (ROOT / ".github/workflows/verify-lightning-data.yml").read_text()
        self.assertIn("python reproduce.py check --json", verify)
        tests = (ROOT / ".github/workflows/test-lightning.yml").read_text()
        self.assertEqual(tests.count('- "reproduce.py"'), 2)  # push and pull_request

    def test_methods_doc_states_the_contract_reproduce_checks(self):
        text = (ROOT / "docs/METHODS.md").read_text()
        for needle in ("EO:EUM:DAT:0691", "(−11.0, 49.5, 3.0, 61.5)", "±60 s", "PR #13", "unknown, not zero",
                       "shard_lost", "0.1°", "reproduce.py check", "--rebuild-day", "How to cite",
                       "Only fully open data is used"):
            self.assertIn(needle, text)
        self.assertEqual(reproduce.UK_BOUNDS, (-11.0, 49.5, 3.0, 61.5))

    def test_reproduce_imports_no_writer_module(self):
        source = (ROOT / "reproduce.py").read_text()
        for name in ("collect", "history", "grid", "status"):
            self.assertNotRegex(source, rf"(?m)^\s*(import|from)\s+{name}\b")


if __name__ == "__main__":
    unittest.main()

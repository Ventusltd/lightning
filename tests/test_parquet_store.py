"""Parquet + DuckDB store: build, coverage, rollups and fail-closed verification.

The real-data tests rebuild the archive the first workflow run would write from
the committed feed (data/uk_lightning.geojson, 408 genuine flashes) and the
committed seed scan windows, inside a temporary directory. Synthetic features
(ids "TEST-" or "SYNTHETIC-") exist ONLY in temporary directories created here
and are built with synthetic=True, which stamps the store SYNTHETIC-NOT-PUBLISHED.
"""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import history  # noqa: E402
import parquet_store as ps  # noqa: E402
import rollups  # noqa: E402
import verify_parquet as vp  # noqa: E402
from collect import build_feature, parse_utc  # noqa: E402

NOW = parse_utc("2026-10-26T14:00:00Z")


def real_archive(directory: Path) -> Path:
    feed = json.loads((ROOT / "data/uk_lightning.geojson").read_text(encoding="utf-8"))
    archive = directory / "history"
    history.archive_observations(archive, feed["features"], now=NOW,
                                 seed_scan_windows=history.load_seed_windows(ROOT / "data/seed_scan_windows.json"))
    return archive


def refresh_manifest(store: Path) -> None:
    """Re-describe every file so ONLY the semantic checks can catch a tampered value."""
    manifest = json.loads((store / "manifest.json").read_text())
    entries = []
    for entry in manifest["files"]:
        path = store / entry["path"]
        if path.suffix == ".parquet":
            entries.append(ps.describe_parquet(path, entry["path"]))
        else:
            entries.append({**entry, "bytes": path.stat().st_size, "sha256": ps.sha256_file(path)})
    manifest["files"] = entries
    manifest["totals"]["bytes"] = sum(e["bytes"] for e in entries)
    (store / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")


def rewrite(path: Path, mutate) -> None:
    table = pq.read_table(path)
    rows = table.to_pylist()
    mutate(rows)
    path.write_bytes(ps.parquet_bytes(pa.Table.from_pylist(rows, schema=table.schema)))


class RealSeedTests(unittest.TestCase):
    """The 408 genuine flashes of the committed feed."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.temp.name)
        cls.archive = real_archive(cls.dir)
        cls.store = cls.dir / "parquet"
        cls.result = ps.build(cls.archive, cls.store, run_id="test-run-1")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def copy_store(self) -> Path:
        target = Path(tempfile.mkdtemp(dir=self.dir)) / "parquet"
        shutil.copytree(self.store, target)
        return target

    def assert_fails(self, store: Path, needle: str):
        with self.assertRaises((vp.VerifyError, ValueError)) as caught:
            vp.verify(self.archive, store)
        self.assertIn(needle, str(caught.exception))

    # --- build ----------------------------------------------------------
    def test_all_408_genuine_flashes_with_the_seven_columns(self):
        files = sorted(self.store.glob("flashes/year=*/month=*/*.parquet"))
        self.assertEqual([f.relative_to(self.store).as_posix() for f in files],
                         ["flashes/year=2026/month=10/part-0.parquet"])
        table = pq.read_table(files[0])
        self.assertEqual(table.num_rows, 408)
        self.assertEqual(table.schema.names, ["id", "time_utc", "lon", "lat", "source_product",
                                              "observation_kind", "ingest_run_id"])
        self.assertEqual(str(table.schema.field("time_utc").type), "timestamp[us, tz=UTC]")
        per_day = Counter(t.date().isoformat() for t in table.column("time_utc").to_pylist())
        self.assertEqual([per_day[d] for d in sorted(per_day)], [31, 67, 189, 118, 3])
        meta = pq.ParquetFile(files[0]).metadata
        self.assertEqual(meta.row_group(0).column(0).compression, "ZSTD")
        self.assertTrue(all(ps.GENUINE_ID.match(i) for i in table.column("id").to_pylist()))

    def test_verify_passes_on_the_real_store(self):
        report = vp.verify(self.archive, self.store)
        self.assertEqual(report["outcome"], "PASS")
        self.assertEqual(report["flashes"], 408)
        self.assertEqual(report["days"], 366)

    def test_parquet_is_much_smaller_than_the_json_shards(self):
        index = json.loads((self.archive / "index.json").read_text())
        json_bytes = sum(r["bytes"] or 0 for r in index["days"])
        parquet_bytes = self.result["manifest"]["totals"]["flash_bytes"]
        print(f"\n[size, real 408 flashes] JSON day shards {json_bytes} B ({json_bytes / 408:.1f} B/flash); "
              f"Parquet flashes {parquet_bytes} B ({parquet_bytes / 408:.1f} B/flash); "
              f"whole Parquet store {self.result['manifest']['totals']['bytes']} B")
        self.assertLess(parquet_bytes * 10, json_bytes)

    def test_rebuild_is_byte_stable_and_carries_ingest_run_ids(self):
        store = self.copy_store()
        again = ps.build(self.archive, store, run_id="test-run-2")
        self.assertEqual(again["changed"], [])
        ids = set(pq.read_table(next(store.glob("flashes/*/*/*.parquet"))).column("ingest_run_id").to_pylist())
        self.assertEqual(ids, {"test-run-1"})

    def test_new_flashes_get_the_new_run_id_and_old_keep_theirs(self):
        store = self.copy_store()
        archive = Path(tempfile.mkdtemp(dir=self.dir)) / "history"
        shutil.copytree(self.archive, archive)
        # a genuine-shaped id is required for a non-synthetic build; this one is
        # derived from a real feed id so it never leaves the temp dir
        feed = json.loads((ROOT / "data/uk_lightning.geojson").read_text())
        extra = copy.deepcopy(feed["features"][-1])
        extra["id"] = extra["id"].rsplit(":", 1)[0] + ":999999"
        history.archive_observations(archive, [extra], now=NOW)
        result = ps.build(archive, store, run_id="test-run-2")
        table = pq.read_table(next(store.glob("flashes/*/*/*.parquet")))
        runs = dict(zip(table.column("id").to_pylist(), table.column("ingest_run_id").to_pylist()))
        self.assertEqual(runs.pop(extra["id"]), "test-run-2")
        self.assertEqual(set(runs.values()), {"test-run-1"})
        self.assertIn("flashes/year=2026/month=10/part-0.parquet", result["changed"])

    # --- coverage: never searched is NULL, never zero --------------------
    def test_coverage_days_null_for_never_searched(self):
        days = pq.read_table(self.store / "coverage/days.parquet").to_pylist()
        self.assertEqual(len(days), 366)
        none = [d for d in days if d["coverage"] == "none"]
        self.assertGreater(len(none), 300)
        for d in none:
            self.assertIsNone(d["flash_count"])
            self.assertIsNone(d["scanned_seconds"])
            self.assertIsNone(d["hours_observed"])
            self.assertEqual(d["count_state"], "UNKNOWN")
        searched = {d["date"].isoformat(): d for d in days if d["coverage"] != "none"}
        self.assertEqual(searched["2026-10-23"]["flash_count"], 189)
        self.assertEqual(searched["2026-10-20"]["flash_count"], 0)  # searched, genuinely none archived

    def test_slots_and_hours_null_when_never_searched(self):
        slots = vp.read_rows(self.store, "coverage/slots/year=*/month=*/*.parquet")
        self.assertEqual(sum(s["flash_count"] or 0 for s in slots), 408)
        self.assertTrue(all((s["coverage"] == "none") == (s["flash_count"] is None) for s in slots))
        hourly = vp.read_rows(self.store, "rollups/hourly/year=*/month=*/*.parquet")
        self.assertEqual(len(hourly), 366 * 24)
        unknown = [h for h in hourly if h["count_state"] == "UNKNOWN"]
        self.assertTrue(unknown and all(h["flash_count"] is None and h["hours_observed"] is None for h in unknown))
        self.assertEqual(sum(h["flash_count"] or 0 for h in hourly), 408)

    # --- rollups ---------------------------------------------------------
    def test_headline_is_tiny_and_carries_coverage(self):
        blob = (self.store / "rollups/headline.json").read_bytes()
        self.assertLessEqual(len(blob), 4096)
        head = json.loads(blob)
        self.assertEqual(head["archive"]["flashes"], 408)
        self.assertEqual(head["archive"]["count_state"], "lower_bound_partial_coverage")
        self.assertIn("not ground strikes", head["not"])
        self.assertIn("optical-flash proximity", head["substations"]["label"])
        for day in head["last_7_days"]:
            self.assertIn("h_obs", day)

    def test_substation_metrics_are_labelled_optical_flash_proximity(self):
        schema = pq.read_schema(self.store / "rollups/substation_optical_flash_proximity.parquet")
        names = [n for n in schema.names if "km" in n]
        self.assertEqual(names, ["optical_flash_proximity_5km", "optical_flash_proximity_10km",
                                 "optical_flash_proximity_20km", "first_flash_within_20km_utc",
                                 "last_flash_within_20km_utc"])
        for name in names[:3]:
            text = schema.field(name).metadata[b"description"].decode()
            self.assertIn("OPTICAL-FLASH PROXIMITY", text)
            self.assertIn("not strikes or faults", text)
        con, _ = rollups.connect(self.store)
        comment = con.execute("SELECT comment FROM duckdb_views() WHERE view_name = "
                              "'substation_optical_flash_proximity'").fetchone()[0]
        self.assertIn("not ground strikes", comment.lower())
        self.assertIn("not electrical faults", comment.lower())
        con.close()

    def test_duckdb_file_of_views_over_the_parquet(self):
        out = Path(tempfile.mkdtemp(dir=self.dir)) / "lightning.duckdb"
        rollups.main(["duckdb", "--store", str(self.store), "--out", str(out)])
        import duckdb
        con = duckdb.connect(str(out), read_only=True)
        views = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
        self.assertTrue({"flashes", "coverage_days", "daily_optical_flashes", "hourly_optical_flashes",
                         "substation_optical_flash_proximity", "storm_episodes"} <= views)
        self.assertEqual(con.execute("SELECT count(*) FROM flashes").fetchone()[0], 408)
        con.close()
        self.assertLess(out.stat().st_size, 2_000_000)

    # --- fail closed -----------------------------------------------------
    def test_fails_when_a_byte_changes(self):
        store = self.copy_store()
        path = store / "coverage/scan_windows.parquet"
        blob = bytearray(path.read_bytes())
        blob[100] ^= 0xFF
        path.write_bytes(bytes(blob))
        self.assert_fails(store, "differs from the manifest")

    def test_fails_when_a_file_is_missing(self):
        store = self.copy_store()
        (store / "rollups/storm_episodes.parquet").unlink()
        self.assert_fails(store, "files and manifest differ")

    def test_fails_on_a_row_outside_the_uk_box(self):
        store = self.copy_store()
        rewrite(next(store.glob("flashes/*/*/*.parquet")), lambda rows: rows[0].update(lon=5.5))
        refresh_manifest(store)
        self.assert_fails(store, "outside the UK box")

    def test_fails_on_a_zero_claimed_for_an_unsearched_day(self):
        store = self.copy_store()

        def zero(rows):
            row = next(r for r in rows if r["coverage"] == "none")
            row["flash_count"] = 0
        rewrite(store / "rollups/daily.parquet", zero)
        refresh_manifest(store)
        self.assert_fails(store, "claimed for unsearched/lost time")

    def test_fails_on_a_zero_claimed_for_an_unsearched_coverage_day(self):
        store = self.copy_store()
        rewrite(store / "coverage/days.parquet",
                lambda rows: next(r for r in rows if r["coverage"] == "none").update(flash_count=0))
        refresh_manifest(store)
        self.assert_fails(store, "claimed for unsearched/lost time")

    def test_fails_when_a_proximity_count_disagrees(self):
        store = self.copy_store()
        rewrite(store / "rollups/substation_optical_flash_proximity.parquet",
                lambda rows: rows[0].update(optical_flash_proximity_20km=rows[0]["optical_flash_proximity_20km"] + 1))
        refresh_manifest(store)
        self.assert_fails(store, "recomputed")

    def test_fails_when_the_headline_disagrees(self):
        store = self.copy_store()
        path = store / "rollups/headline.json"
        head = json.loads(path.read_text())
        head["archive"]["flashes"] += 1
        path.write_text(json.dumps(head, separators=(",", ":")) + "\n")
        refresh_manifest(store)
        self.assert_fails(store, "headline flash total differs")

    def test_fails_when_the_manifest_row_count_lies(self):
        store = self.copy_store()
        manifest = json.loads((store / "manifest.json").read_text())
        next(f for f in manifest["files"] if f["path"].startswith("flashes/"))["rows"] = 407
        (store / "manifest.json").write_text(json.dumps(manifest))
        self.assert_fails(store, "manifest rows")

    def test_fails_when_parquet_and_archive_disagree(self):
        store = self.copy_store()
        rewrite(next(store.glob("flashes/*/*/*.parquet")), lambda rows: rows[5].update(lat=rows[5]["lat"] + 0.01))
        refresh_manifest(store)
        self.assert_fails(store, "differs from its archive record")


class ShardLostAndEpisodeTests(unittest.TestCase):
    """Small synthetic archives (TEST- ids) in temp dirs only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.archive = self.dir / "history"

    def tearDown(self):
        self.temp.cleanup()

    def flash(self, when, index, lon=-1.5, lat=52.5):
        return build_feature(lon, lat, when, "TEST-product", index)

    def scan(self, start, end):
        return history.successful_scan(start.isoformat(), end.isoformat(), 18)

    def test_refuses_non_genuine_ids_unless_synthetic(self):
        t = NOW - timedelta(hours=2)
        history.archive_observations(self.archive, [self.flash(t, 1)], now=NOW,
                                     scanned_window=self.scan(NOW - timedelta(hours=6), NOW))
        with self.assertRaises(ps.ArchiveError):
            ps.build(self.archive, self.dir / "parquet", run_id="r")
        ps.build(self.archive, self.dir / "parquet", run_id="r", synthetic=True)
        with self.assertRaises(vp.VerifyError):
            vp.verify(self.archive, self.dir / "parquet")  # synthetic store is never published
        self.assertEqual(vp.verify(self.archive, self.dir / "parquet", allow_synthetic=True)["outcome"], "PASS")
        head = json.loads((self.dir / "parquet/rollups/headline.json").read_text())
        self.assertEqual(head["synthetic"], "SYNTHETIC-NOT-PUBLISHED")

    def test_unsearched_gap_splits_an_episode_and_marks_edges(self):
        d = NOW - timedelta(days=1)
        base = d.replace(hour=12, minute=0, second=30, microsecond=0)
        flashes = [self.flash(base, 1), self.flash(base + timedelta(minutes=10), 2),
                   self.flash(base + timedelta(minutes=20), 3), self.flash(base + timedelta(minutes=70), 4)]
        windows = [self.scan(base - timedelta(hours=1), base + timedelta(minutes=12)),   # gap 12->18 min
                   self.scan(base + timedelta(minutes=18), base + timedelta(hours=3))]
        history.archive_observations(self.archive, flashes, now=NOW, scanned_windows=windows)
        store = self.dir / "parquet"
        ps.build(self.archive, store, run_id="r", synthetic=True)
        episodes = pq.read_table(store / "rollups/storm_episodes.parquet").to_pylist()
        self.assertEqual([e["flash_count"] for e in episodes], [2, 1, 1])
        self.assertEqual([e["start_reason"] for e in episodes], ["first", "unsearched_gap", "quiet_gap"])
        self.assertEqual([e["coverage"] for e in episodes], ["edge_unsearched_may_extend"] * 2 + ["complete"])
        vp.verify(self.archive, store, allow_synthetic=True)

    def test_shard_lost_day_reads_unknown_everywhere(self):
        day1, day2 = NOW - timedelta(days=2), NOW - timedelta(days=1)
        flashes = [self.flash(day1.replace(hour=10), 1), self.flash(day2.replace(hour=10), 2)]
        history.archive_observations(self.archive, flashes, now=NOW,
                                     scanned_window=self.scan(NOW - timedelta(days=3), NOW))
        index = json.loads((self.archive / "index.json").read_text())
        lost = next(r for r in index["days"] if r["date"] == day1.date().isoformat())
        (self.archive / lost["path"]).unlink()
        store = self.dir / "parquet"
        with self.assertRaises(ps.ArchiveError) as caught:  # unflagged loss: fail closed
            ps.build(self.archive, store, run_id="r", synthetic=True)
        self.assertIn("shard_lost", str(caught.exception))
        lost["shard_lost"] = True
        (self.archive / "index.json").write_text(json.dumps(index))
        ps.build(self.archive, store, run_id="r", synthetic=True)
        days = {d["date"]: d for d in pq.read_table(store / "coverage/days.parquet").to_pylist()}
        row = days[day1.date()]
        self.assertEqual((row["coverage"], row["count_state"], row["flash_count"], row["hours_observed"]),
                         ("shard_lost", "UNKNOWN", None, None))
        daily = {d["date"]: d for d in pq.read_table(store / "rollups/daily.parquet").to_pylist()}
        self.assertIsNone(daily[day1.date()]["flash_count"])
        self.assertEqual(daily[day2.date()]["flash_count"], 1)
        hours = [h for h in vp.read_rows(store, "rollups/hourly/year=*/month=*/*.parquet") if h["date"] == day1.date()]
        self.assertTrue(all(h["coverage"] == "shard_lost" and h["flash_count"] is None for h in hours))
        head = json.loads((store / "rollups/headline.json").read_text())
        self.assertEqual(head["archive"]["days"]["shard_lost"], 1)
        self.assertEqual(vp.verify(self.archive, store, allow_synthetic=True)["outcome"], "PASS")

    def test_no_archive_yet_is_a_clean_no_op(self):
        out = self.dir / "parquet"
        self.assertEqual(ps.main(["build", "--archive-dir", str(self.archive), "--out", str(out), "--if-archive"]), 0)
        self.assertFalse(out.exists())
        self.assertEqual(vp.main(["--archive-dir", str(self.archive), "--out", str(out), "--if-archive"]), 0)
        self.assertEqual(vp.main(["--archive-dir", str(self.archive), "--out", str(out)]), 1)


class SyntheticStormYearTests(unittest.TestCase):
    """SYNTHETIC storm-year stress test (small); the full-size run is tools/stress_storm_year.py."""

    def test_storm_year_bytes_per_flash_and_query_times(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import stress_storm_year
        with tempfile.TemporaryDirectory(prefix="SYNTHETIC-") as temp:
            report = stress_storm_year.measure(5000, Path(temp))
        print(f"\n[SYNTHETIC storm year, 5000 flashes] JSON {report['json_bytes_per_flash']} B/flash, "
              f"Parquet {report['parquet_bytes_per_flash']} B/flash, ratio {report['ratio_json_to_parquet']}x; "
              + ", ".join(f"{k} {v['seconds']} s" for k, v in report["duckdb_queries"].items()))
        self.assertEqual(report["flashes"], 5000)
        self.assertEqual(report["verify_outcome"], "PASS")
        self.assertGreater(report["ratio_json_to_parquet"], 10)
        self.assertEqual(report["duckdb_queries"]["count_all"]["result"], [["5000"]])
        self.assertIn("SYNTHETIC", report["label"])

    def test_synthetic_data_never_lands_in_the_repository(self):
        self.assertFalse(list((ROOT / "data").rglob("*SYNTHETIC*")))
        manifest = ROOT / "data/parquet/manifest.json"
        if manifest.exists():
            self.assertNotIn("synthetic", json.loads(manifest.read_text()))


class SubstationPinTests(unittest.TestCase):
    def test_extract_matches_its_sha256_pin(self):
        pin = json.loads((ROOT / "sources/gb-transmission-substations.source.json").read_text())
        extract = json.loads((ROOT / "sources/gb-transmission-substations.v1.json").read_text())
        self.assertEqual(extract["derived_from"]["sha256"], pin["sha256"])
        self.assertEqual(extract["derived_from"]["commit"], pin["commit"])
        self.assertIn(pin["commit"], pin["raw_url"])
        self.assertEqual(extract["counts"]["located"], len(extract["substations"]))
        self.assertIn("OpenStreetMap", extract["attribution"]["coordinates"])
        self.assertIn("ODbL", extract["attribution"]["coordinates"])

    def test_tampered_extract_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "s.json"
            extract = json.loads((ROOT / "sources/gb-transmission-substations.v1.json").read_text())
            extract["derived_from"]["sha256"] = "0" * 64
            path.write_text(json.dumps(extract))
            with self.assertRaises(ValueError):
                rollups.load_substations(path)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github/workflows/update-lightning.yml").read_text()

    def test_cron_and_feed_first_ordering_are_unchanged(self):
        self.assertIn('- cron: "17 * * * *"', self.text)
        order = ["name: EUMETSAT collection (feed first)", "name: Commit latest public GeoJSON (feed commit)",
                 "name: Append to the one-year UTC archive", "name: Commit archive and status (archive commit)",
                 "name: Build Parquet, coverage, rollups and manifest", "name: Verify the Parquet store (fail closed)",
                 "name: Commit the Parquet store (parquet commit)", "name: Surface collection or archive failure"]
        positions = [self.text.index(step) for step in order]
        self.assertEqual(positions, sorted(positions))

    def test_parquet_commit_is_separate_and_only_on_change(self):
        block = self.text[self.text.index("name: Commit the Parquet store (parquet commit)"):]
        block = block[:block.index("- name: Surface")]
        self.assertIn("git add -A data/parquet", block)
        self.assertIn("git diff --cached --quiet", block)
        self.assertNotIn("data/history", block)
        self.assertNotIn("uk_lightning.geojson", block)
        self.assertIn("steps.verify_parquet.outcome == 'success'", block)


if __name__ == "__main__":
    unittest.main()

"""A listed shard that vanished: unknown (never zero), flagged in index and status,
healthy days keep flowing, and recovery only from verified evidence.

Synthetic features exist ONLY inside temporary directories created by these
tests (ids start with "TEST-"); nothing synthetic is ever published.
"""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import history
from collect import build_feature
from history import archive_observations, day_path, part_path, successful_scan, verify_archive

LOST = "2026-10-24"
HEALTHY = "2026-10-23"


def rows(result_or_index):
    index = result_or_index.get("index", result_or_index)
    return {r["date"]: r for r in index["days"]}


class ShardLostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "history"
        self.saved = self.base / "from-git-history"  # outside the archive: an earlier committed copy
        self.saved.mkdir()
        self.now = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)
        self.day = datetime(2026, 10, 24, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def flash(self, timestamp, product, index=0):
        return build_feature(-2.12, 51.11, timestamp, "TEST-" + product, index)

    def scan(self, start, end):
        return successful_scan(start.isoformat(), end.isoformat(), 18)

    def lose_whole_day(self):
        """Archive two flashes on LOST and one on HEALTHY, save LOST's shard, delete it, re-run."""
        healthy = datetime(2026, 10, 23, tzinfo=timezone.utc)
        first = archive_observations(
            self.root, [self.flash(self.day + timedelta(hours=10), "a"), self.flash(self.day + timedelta(hours=11), "b"),
                        self.flash(healthy + timedelta(hours=9), "h")],
            now=self.now, scanned_windows=[self.scan(healthy, self.day + timedelta(days=1))])
        self.recorded = rows(first)[LOST]
        self.first_index = json.loads((self.root / "index.json").read_text())
        shutil.copy(self.root / day_path(self.day.date()), self.saved / "24.json")
        (self.root / day_path(self.day.date())).unlink()
        return archive_observations(self.root, [], now=self.now)

    def index_bytes(self):
        return (self.root / "index.json").read_bytes()

    # --- condition 1: the affected day is UNKNOWN, never zero --------------------
    def test_lost_day_is_unknown_and_its_last_recorded_entry_is_kept_as_evidence(self):
        result = self.lose_whole_day()
        row = rows(result)[LOST]
        self.assertEqual((row["coverage"], row["scanned_seconds"], row["count"], row["path"]), ("none", 0, None, None))
        self.assertTrue(row["shard_lost"])
        lost = result["index"]["integrity"]["lost_days"]
        self.assertEqual([e["date"] for e in lost], [LOST])
        evidence = lost[0]["last_recorded"]
        self.assertEqual((evidence["count"], evidence["sha256"], evidence["bytes"], evidence["path"]),
                         (2, self.recorded["sha256"], self.recorded["bytes"], self.recorded["path"]))
        self.assertEqual(lost[0]["missing_paths"], [self.recorded["path"]])
        self.assertEqual(lost[0]["detected_utc"], "2026-10-26T14:00:00.000Z")

    def test_later_flashes_for_a_lost_day_are_quarantined_never_counted_or_discarded(self):
        self.lose_whole_day()
        late = archive_observations(self.root, [self.flash(self.day + timedelta(hours=23), "late")], now=self.now)
        self.assertEqual(rows(late)[LOST]["count"], None)
        self.assertEqual((late["quarantined_lost_day"], late["dropped_lost_day"]), (1, 0))
        quarantine = json.loads((self.root / "quarantine" / f"{LOST}.json").read_text())
        self.assertEqual([f["id"] for f in quarantine["features"]], [self.flash(self.day + timedelta(hours=23), "late")["id"]])
        again = archive_observations(self.root, [self.flash(self.day + timedelta(hours=23), "late")], now=self.now)
        self.assertEqual(again["quarantined_lost_day"], 0)  # idempotent by stable id
        self.assertEqual(again["index"]["integrity"]["lost_days"][0]["quarantined_observations"], 1)
        verify_archive(self.root)

    # --- condition 2: index integrity block with an explicit alert ---------------
    def test_index_carries_an_explicit_integrity_alert(self):
        result = self.lose_whole_day()
        integrity = result["index"]["integrity"]
        self.assertEqual(integrity["state"], "shard_lost")
        self.assertIn("INTEGRITY ALERT", integrity["alert"])
        self.assertIn(LOST, integrity["alert"])
        self.assertIn("unknown", integrity["alert"])
        self.assertEqual(result["integrity"]["state"], "shard_lost")  # in the ingest result -> status.json
        self.assertEqual(result["integrity"]["lost_days"], [LOST])

    def test_healthy_archive_reports_integrity_ok(self):
        result = archive_observations(self.root, [self.flash(self.day + timedelta(hours=1), "ok")], now=self.now)
        self.assertEqual(result["index"]["integrity"], {"state": "ok", "alert": None, "lost_days": [], "recoveries": []})

    def test_cli_ingest_prints_the_alert_and_still_succeeds(self):
        self.lose_whole_day()
        record = self.base / "run.json"
        out = self.base / "result.json"
        record.write_text(json.dumps({"schema": "uk-lightning-run-record.v1", "outcome": "success",
                                      "run_utc": "2026-10-26T13:05:58.000Z", "products_examined": 1, "scanned_ranges": [],
                                      "features": []}))
        code = history.main(["ingest", "--archive-dir", str(self.root), "--run-record", str(record), "--result-out", str(out),
                             "--now", self.now.isoformat(), "--seed-scan-windows", str(self.base / "none.json")])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.read_text())["integrity"]["state"], "shard_lost")

    # --- condition 3: healthy days keep flowing, never "entirely healthy" ---------
    def test_healthy_days_continue_while_the_archive_stays_flagged(self):
        self.lose_whole_day()
        today = datetime(2026, 10, 26, tzinfo=timezone.utc)
        later = archive_observations(self.root, [self.flash(today + timedelta(hours=12), "new")], now=self.now,
                                     scanned_window=self.scan(today + timedelta(hours=8), today + timedelta(hours=13)))
        self.assertEqual(rows(later)["2026-10-26"]["count"], 1)
        self.assertEqual(rows(later)[HEALTHY]["count"], 1)
        self.assertEqual(later["index"]["archived_observations"], 2)
        self.assertEqual(later["index"]["integrity"]["state"], "shard_lost")
        self.assertEqual(verify_archive(self.root)["integrity"], "shard_lost")

    def test_verify_rejects_a_silently_cleared_flag_or_a_lost_row_with_a_count(self):
        self.lose_whole_day()
        good = json.loads((self.root / "index.json").read_text())
        cleared = json.loads(json.dumps(good))
        cleared["integrity"] = {"state": "ok", "alert": None, "lost_days": [], "recoveries": []}
        (self.root / "index.json").write_text(json.dumps(cleared))
        with self.assertRaisesRegex(ValueError, "shard_lost"):
            verify_archive(self.root)
        counted = json.loads(json.dumps(good))
        next(r for r in counted["days"] if r["date"] == LOST).update({"coverage": "full", "scanned_seconds": 86400, "count": 0})
        (self.root / "index.json").write_text(json.dumps(counted))
        with self.assertRaisesRegex(ValueError, "shard_lost"):
            verify_archive(self.root)

    # --- condition 4: recovery only from verified evidence -------------------------
    def test_recover_from_a_verified_earlier_shard_restores_the_row_and_audits_it(self):
        self.lose_whole_day()
        archive_observations(self.root, [self.flash(self.day + timedelta(hours=23), "late")], now=self.now)
        result = history.recover_day(self.root, LOST, now=self.now + timedelta(hours=1),
                                     from_shards=[self.saved / "24.json"])
        row = rows(result)[LOST]
        self.assertNotIn("shard_lost", row)
        self.assertEqual(row["count"], 3)  # 2 verified + 1 quarantined flash merged after verification
        self.assertEqual(row["coverage"], "full")
        integrity = result["index"]["integrity"]
        self.assertEqual((integrity["state"], integrity["alert"], integrity["lost_days"]), ("ok", None, []))
        audit = integrity["recoveries"][0]
        self.assertEqual((audit["date"], audit["method"]), (LOST, "from_shard"))
        self.assertEqual(audit["verified_against"]["sha256"], self.recorded["sha256"])
        self.assertEqual(audit["verified_against"]["count"], 2)
        self.assertEqual(audit["inputs"][0]["sha256"], self.recorded["sha256"])
        self.assertEqual(audit["quarantined_merged"], 1)
        self.assertEqual(audit["recovered_utc"], "2026-10-26T15:00:00.000Z")
        self.assertFalse((self.root / "quarantine" / f"{LOST}.json").exists())
        self.assertEqual(verify_archive(self.root)["integrity"], "ok")
        # The audit survives later ingests.
        later = archive_observations(self.root, [], now=self.now + timedelta(hours=2))
        self.assertEqual(len(later["index"]["integrity"]["recoveries"]), 1)

    def test_recover_refuses_a_partial_or_altered_shard_and_changes_nothing(self):
        self.lose_whole_day()
        partial = json.loads((self.saved / "24.json").read_text())
        partial["features"] = partial["features"][:1]
        partial["metadata"]["count"] = 1
        bad = self.saved / "partial.json"
        bad.write_text(history._serialise(partial))
        before = self.index_bytes()
        with self.assertRaisesRegex(ValueError, "sha256"):
            history.recover_day(self.root, LOST, now=self.now, from_shards=[bad])
        self.assertEqual(self.index_bytes(), before)
        self.assertFalse((self.root / day_path(self.day.date())).exists())

    def test_recover_refuses_without_a_recorded_entry_unless_an_earlier_index_is_given(self):
        self.lose_whole_day()
        index = json.loads((self.root / "index.json").read_text())
        index["integrity"]["lost_days"][0]["last_recorded"] = None  # e.g. lost before evidence was kept
        (self.root / "index.json").write_text(history._serialise(index))
        with self.assertRaisesRegex(ValueError, "previously recorded"):
            history.recover_day(self.root, LOST, now=self.now, from_shards=[self.saved / "24.json"])
        earlier = self.base / "earlier-index.json"
        earlier.write_text(json.dumps(self.first_index))
        result = history.recover_day(self.root, LOST, now=self.now, from_shards=[self.saved / "24.json"],
                                     recorded_index=earlier)
        self.assertEqual(rows(result)[LOST]["count"], 2)
        self.assertEqual(result["index"]["integrity"]["recoveries"][0]["verified_against"]["source"], "recorded_index")

    def test_recover_split_day_needs_every_missing_part_and_verifies_survivors(self):
        flashes = [self.flash(self.day + timedelta(hours=h, minutes=m), f"p{h}-{m}", m) for h in (3, 4) for m in range(4)]
        archive_observations(self.root, flashes, now=self.now, scanned_window=self.scan(self.day, self.day + timedelta(days=1)),
                             target_shard_bytes=2000)
        shutil.copy(self.root / part_path(self.day.date(), 4), self.saved / "04.json")
        shutil.copy(self.root / part_path(self.day.date(), 3), self.saved / "03.json")
        (self.root / part_path(self.day.date(), 4)).unlink()
        archive_observations(self.root, [], now=self.now, target_shard_bytes=2000)
        with self.assertRaisesRegex(ValueError, "missing"):
            history.recover_day(self.root, LOST, now=self.now, from_shards=[self.saved / "03.json"],
                                target_shard_bytes=2000)
        result = history.recover_day(self.root, LOST, now=self.now, from_shards=[self.saved / "04.json"],
                                     target_shard_bytes=2000)
        row = rows(result)[LOST]
        self.assertEqual((row["count"], len(row["parts"])), (8, 2))
        self.assertEqual(verify_archive(self.root)["integrity"], "ok")

    def test_recover_refuses_a_day_that_is_not_lost(self):
        archive_observations(self.root, [self.flash(self.day + timedelta(hours=1), "x")], now=self.now)
        with self.assertRaisesRegex(ValueError, "not marked shard_lost"):
            history.recover_day(self.root, LOST, now=self.now, from_shards=[self.root / day_path(self.day.date())])

    def rebuild_record(self, features, start, end, run_utc="2026-10-26T15:00:00.000Z", **over):
        record = {"schema": "uk-lightning-run-record.v1", "outcome": "success", "run_utc": run_utc,
                  "products_examined": 144, "slots_parsed": True, "slots_examined": 144,
                  "scanned_ranges": [{"start_utc": start.isoformat(), "end_utc": end.isoformat()}],
                  "unscanned_ranges": [], "features": features}
        record.update(over)
        path = self.base / "rebuild.json"
        path.write_text(json.dumps(record))
        return path

    def test_rebuild_must_cover_the_recorded_scan_and_match_the_recorded_count(self):
        self.lose_whole_day()
        both = [self.flash(self.day + timedelta(hours=10), "a"), self.flash(self.day + timedelta(hours=11), "b")]
        later = self.now + timedelta(hours=2)
        before = self.index_bytes()
        half = self.rebuild_record(both, self.day, self.day + timedelta(hours=12))
        with self.assertRaisesRegex(ValueError, "does not cover"):
            history.recover_day(self.root, LOST, now=later, rebuild_record=half)
        short = self.rebuild_record(both[:1], self.day, self.day + timedelta(days=1))
        with self.assertRaisesRegex(ValueError, "count"):
            history.recover_day(self.root, LOST, now=later, rebuild_record=short)
        stale = self.rebuild_record(both, self.day, self.day + timedelta(days=1), run_utc="2026-10-26T13:00:00.000Z")
        with self.assertRaisesRegex(ValueError, "independent"):
            history.recover_day(self.root, LOST, now=later, rebuild_record=stale)
        opaque = self.rebuild_record(both, self.day, self.day + timedelta(days=1), slots_parsed=False)
        with self.assertRaisesRegex(ValueError, "slots"):
            history.recover_day(self.root, LOST, now=later, rebuild_record=opaque)
        self.assertEqual(self.index_bytes(), before)
        good = self.rebuild_record(both, self.day, self.day + timedelta(days=1))
        result = history.recover_day(self.root, LOST, now=later, rebuild_record=good)
        self.assertEqual(rows(result)[LOST]["count"], 2)
        audit = result["index"]["integrity"]["recoveries"][0]
        self.assertEqual((audit["method"], audit["rebuild"]["anchored"], audit["byte_identical"]), ("rebuild", True, True))
        self.assertEqual(audit["rebuild"]["uncovered_seconds"], 0)
        self.assertEqual(verify_archive(self.root)["integrity"], "ok")

    def test_rebuild_must_contain_every_quarantined_flash(self):
        self.lose_whole_day()
        archive_observations(self.root, [self.flash(self.day + timedelta(hours=23), "late")], now=self.now)
        both = [self.flash(self.day + timedelta(hours=10), "a"), self.flash(self.day + timedelta(hours=11), "b")]
        record = self.rebuild_record(both, self.day, self.day + timedelta(days=1))
        with self.assertRaisesRegex(ValueError, "quarantined"):
            history.recover_day(self.root, LOST, now=self.now + timedelta(hours=2), rebuild_record=record)

    def test_unanchored_rebuild_needs_an_explicit_flag_and_is_audited_as_such(self):
        self.lose_whole_day()
        index = json.loads((self.root / "index.json").read_text())
        index["integrity"]["lost_days"][0]["last_recorded"] = None
        (self.root / "index.json").write_text(history._serialise(index))
        both = [self.flash(self.day + timedelta(hours=10), "a"), self.flash(self.day + timedelta(hours=11), "b")]
        record = self.rebuild_record(both, self.day, self.day + timedelta(days=1))
        with self.assertRaisesRegex(ValueError, "accept-unanchored"):
            history.recover_day(self.root, LOST, now=self.now + timedelta(hours=2), rebuild_record=record)
        result = history.recover_day(self.root, LOST, now=self.now + timedelta(hours=2), rebuild_record=record,
                                     accept_unanchored=True)
        self.assertIs(result["index"]["integrity"]["recoveries"][0]["rebuild"]["anchored"], False)

    def test_cli_recover(self):
        self.lose_whole_day()
        with self.assertRaises(SystemExit):  # exactly one of --from-shard / --rebuild
            history.main(["recover", "--archive-dir", str(self.root), "--day", LOST])
        bad = self.saved / "empty.json"
        bad.write_text("{}\n")
        with self.assertRaises(ValueError):
            history.main(["recover", "--archive-dir", str(self.root), "--day", LOST, "--from-shard", str(bad),
                          "--now", self.now.isoformat()])
        code = history.main(["recover", "--archive-dir", str(self.root), "--day", LOST,
                             "--from-shard", str(self.saved / "24.json"), "--now", self.now.isoformat()])
        self.assertEqual(code, 0)
        self.assertEqual(verify_archive(self.root)["integrity"], "ok")

    def test_collect_rebuild_day_feeds_recover_rebuild_end_to_end(self):
        """collect.py --rebuild-day writes only a run record for that UTC date (never the feed or
        status); history.py recover --rebuild accepts it only through the same gates."""
        import collect
        self.lose_whole_day()
        both = [self.flash(self.day + timedelta(hours=10), "a"), self.flash(self.day + timedelta(hours=11), "b")]
        names = [("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_"
                  f"{e:%Y%m%d%H%M%S}_L2PF_OPE_{s:%Y%m%d%H%M%S}_{e:%Y%m%d%H%M%S}_N__O_0001_0000")
                 for s, e in ((self.day + timedelta(minutes=10 * i), self.day + timedelta(minutes=10 * (i + 1)))
                              for i in range(144))]
        calls = []

        def fake_download(start, end):
            calls.append((start, end))
            return both, len(names), names

        feed = self.base / "feed.geojson"
        status = self.base / "status.json"
        record = self.base / "rebuild-record.json"
        run_now = self.now + timedelta(hours=2)
        code = collect.rebuild_day_record(LOST, record, now=run_now, downloader=fake_download)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [(self.day, self.day + timedelta(days=1))])
        self.assertFalse(feed.exists() or status.exists())
        rec = json.loads(record.read_text())
        self.assertEqual((rec["lookback_source"], rec["rebuild_day_utc"], rec["run_utc"]),
                         ("rebuild", LOST, "2026-10-26T16:00:00.000Z"))
        self.assertEqual(len(rec["features"]), 2)
        with self.assertRaises(SystemExit):  # --rebuild-day excludes --hours / --auto-window
            collect.main(["--rebuild-day", LOST, "--hours", "6", "--run-record", str(record)])
        code = history.main(["recover", "--archive-dir", str(self.root), "--day", LOST, "--rebuild",
                             "--rebuild-record", str(record), "--now", run_now.isoformat()])
        self.assertEqual(code, 0)
        index = json.loads((self.root / "index.json").read_text())
        self.assertEqual(rows(index)[LOST]["count"], 2)
        self.assertEqual(index["integrity"]["recoveries"][0]["method"], "rebuild")


if __name__ == "__main__":
    unittest.main()

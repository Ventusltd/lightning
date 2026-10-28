"""Slot-edge flashes: attribution to the product's nominal slot, outside-day drops,
and a finalize that never loses a whole run to one refused date.

Incident (parallel backfill run 40608108170): the rebuild of 2026-08-28 held
flash ``..._OPE_20260828000049_20260828001009_...:203``, timed just before its
product's nominal sensing start 00:00:49. The examined spans had a 40 s hole
(the previous product ends 00:00:09), so ``history.backfill_day`` refused the
date ("lies outside the examined slots") and finalize stopped, leaving six
other verified records un-ingested. Real LFL flashes sit up to about 0.4 s
before their product's nominal start (observed: 0.361 s); the genuineness
tolerance is +-60 s.

Synthetic features exist ONLY inside temporary directories (fake products,
fake decoder); nothing synthetic is ever published.
"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backfill_parallel as bp
import collect
import history
from collect import build_feature
from test_unreadable_product import FakeProduct, fake_body, good_zip

NOW = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
TARGET = "2026-10-20"
DAY0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
EDGE = timedelta(seconds=0.361)  # observed: a flash 0.361 s before its product's nominal start


def product_name(start: datetime, end: datetime) -> str:
    return ("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_"
            f"{end:%Y%m%d%H%M%S}_L2PF_OPE_{start:%Y%m%d%H%M%S}_{end:%Y%m%d%H%M%S}_N__O_0001_0000")


def iso(moment: datetime) -> str:
    return collect.iso_utc(moment)


class EdgeEumetsat:
    """A day laid out like the incident, through the REAL download/decode path.

    P0 = previous-day product 23:50:09-00:00:09, P1 = 00:00:49-00:10:09 (the
    incident's first product), then 10-minute products from 00:10:09 on. The
    examined spans therefore have a 40 s hole 00:00:09-00:00:49.
    ``flashes`` maps a product start to [(lon, lat, time)].
    """

    def __init__(self, day: datetime, flashes: dict, skip=()):
        self.day, self.flashes, self.skip = day, flashes, set(skip)

    def slots(self):
        p0 = self.day - timedelta(seconds=591)                     # 23:50:09 of the previous day
        yield p0, self.day + timedelta(seconds=9)                  # -> 00:00:09
        yield self.day + timedelta(seconds=49), self.day + timedelta(minutes=10, seconds=9)
        cursor = self.day + timedelta(minutes=10, seconds=9)
        while cursor < self.day + timedelta(days=1):
            yield cursor, cursor + timedelta(minutes=10)
            cursor += timedelta(minutes=10)

    def products(self):
        for start, end in self.slots():
            if start in self.skip:
                continue
            name = product_name(start, end)
            rows = [(lon, lat, iso(t)) for lon, lat, t in self.flashes.get(start, [])]
            yield FakeProduct(name, good_zip(name, rows))

    def __call__(self, start, end):
        return collect.download_products(self.products(), start, end, body_decoder=fake_body)


def incident_flashes(day: datetime) -> dict:
    p0 = day - timedelta(seconds=591)
    p1 = day + timedelta(seconds=49)
    p5 = day + timedelta(hours=5, seconds=9)
    return {
        p0: [(-2.0, 52.0, day - timedelta(seconds=30)),     # previous UTC day: outside_day
             (-2.1, 52.1, day + timedelta(seconds=5))],     # in the day, attributed to P0
        p1: [(-2.2, 52.2, p1 - EDGE),                       # the incident: 0.361 s before P1
             (-2.3, 52.3, p1 + timedelta(minutes=4))],
        p5: [(-2.4, 52.4, p5 + timedelta(minutes=3))],
    }


def make_archive(root: Path) -> None:
    hourly = datetime(2026, 10, 26, tzinfo=timezone.utc)
    history.archive_observations(
        root, [build_feature(-2.12, 51.11, hourly + timedelta(hours=3), "TEST-hourly", 0)],
        now=NOW - timedelta(hours=1),
        scanned_windows=[history.successful_scan((hourly + timedelta(hours=1)).isoformat(),
                                                 (hourly + timedelta(hours=7)).isoformat(), 36)],
        lookback_hours=6.0)


def rows(root: Path) -> dict:
    return {r["date"]: r for r in json.loads((root / "index.json").read_text())["days"]}


class SlotAttributionTests(unittest.TestCase):
    def test_flash_slot_is_its_products_nominal_slot(self):
        start = datetime(2026, 10, 23, 19, 50, 9, tzinfo=timezone.utc)
        feature = build_feature(-2.0, 52.0, start - EDGE, product_name(start, start + timedelta(minutes=10)), 322)
        self.assertEqual(collect.flash_slot(feature), (start, start + timedelta(minutes=10)))
        self.assertIsNone(collect.flash_slot(build_feature(-2.0, 52.0, start, "TEST-x", 0)))

    def test_flashes_prove_their_products_slot_not_the_slot_of_their_timestamp(self):
        start = datetime(2026, 10, 23, 19, 50, 9, tzinfo=timezone.utc)
        name = product_name(start, start + timedelta(minutes=10))
        edge = build_feature(-2.0, 52.0, start - EDGE, name, 322)          # 19:50:08.639
        inner = build_feature(-2.0, 52.0, start + timedelta(minutes=5), name, 1)
        self.assertEqual(history.slots_proved_by_flashes([edge, inner]), 600)

    def test_edge_tolerance_is_the_documented_sixty_seconds(self):
        self.assertEqual(collect.SLOT_EDGE_TOLERANCE_SECONDS, 60)


class RebuildOutsideDayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.record = Path(self.temp.name) / "rb.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_rebuild_drops_previous_day_flashes_and_counts_them_outside_day(self):
        collect.rebuild_day_record(TARGET, self.record, now=NOW, downloader=EdgeEumetsat(DAY0, incident_flashes(DAY0)))
        record = json.loads(self.record.read_text())
        times = sorted(f["properties"]["time_utc"] for f in record["features"])
        self.assertEqual(record["outside_day"], 1)
        self.assertTrue(all(t.startswith(TARGET) for t in times), times)
        self.assertEqual(len(times), 4)
        self.assertIn("2026-10-20T00:00:48.639Z", times)


class BackfillEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "history"
        make_archive(self.root)
        self.record = self.base / "rb.json"

    def tearDown(self):
        self.temp.cleanup()

    def rebuild(self, flashes=None, skip=()):
        fake = EdgeEumetsat(DAY0, incident_flashes(DAY0) if flashes is None else flashes, skip=skip)
        collect.rebuild_day_record(TARGET, self.record, now=NOW, downloader=fake)
        return json.loads(self.record.read_text())

    def write(self, record):
        self.record.write_text(json.dumps(record))

    def backfill(self):
        return history.backfill_day(self.root, TARGET, now=NOW, rebuild_record=self.record)

    def test_incident_edge_flash_before_its_products_nominal_start_is_accepted(self):
        self.rebuild()
        result = self.backfill()
        row = rows(self.root)[TARGET]
        self.assertEqual(row["count"], 4)
        self.assertEqual(row["coverage"], "partial")       # the 40 s hole stays unknown
        self.assertEqual(row["scanned_seconds"], 86400 - 40)
        self.assertEqual(result["outside_day"], 1)
        self.assertEqual(history.verify_archive(self.root)["integrity"], "ok")

    def test_previous_day_flash_inside_a_record_is_dropped_and_counted_not_refused(self):
        record = self.rebuild()
        p0 = DAY0 - timedelta(seconds=591)
        name = product_name(p0, DAY0 + timedelta(seconds=9))
        record["features"].append(build_feature(-2.5, 52.5, DAY0 - timedelta(seconds=0.2), name, 77))
        self.write(record)
        result = self.backfill()
        self.assertEqual(rows(self.root)[TARGET]["count"], 4)
        self.assertEqual(result["outside_day"], 2)          # 1 dropped by collect + 1 in the record
        self.assertEqual(result["outside_day_in_record"], 1)

    def test_next_day_flash_inside_a_record_is_dropped_and_counted(self):
        record = self.rebuild()
        last = DAY0 + timedelta(hours=23, minutes=50, seconds=9)
        name = product_name(last, last + timedelta(minutes=10))
        record["features"].append(build_feature(-2.5, 52.5, DAY0 + timedelta(days=1, seconds=3), name, 5))
        self.write(record)
        result = self.backfill()
        self.assertEqual(rows(self.root)[TARGET]["count"], 4)
        self.assertEqual(result["outside_day_in_record"], 1)

    def test_flash_two_minutes_outside_its_product_is_still_refused(self):
        record = self.rebuild()
        p5 = DAY0 + timedelta(hours=5, seconds=9)
        name = product_name(p5, p5 + timedelta(minutes=10))
        # inside the examined spans (the 04:50:09 product), but 120 s before its own product
        record["features"].append(build_feature(-2.5, 52.5, p5 - timedelta(seconds=120), name, 9))
        self.write(record)
        before = (self.root / "index.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "lies outside the examined slots"):
            self.backfill()
        self.assertEqual((self.root / "index.json").read_bytes(), before)

    def test_flash_of_a_product_that_was_not_examined_is_refused(self):
        p5 = DAY0 + timedelta(hours=5, seconds=9)
        record = self.rebuild(skip={p5})
        name = product_name(p5, p5 + timedelta(minutes=10))
        record["features"].append(build_feature(-2.5, 52.5, p5 + timedelta(minutes=2), name, 9))
        self.write(record)
        with self.assertRaisesRegex(ValueError, "lies outside the examined slots"):
            self.backfill()

    def test_flash_without_a_product_slot_still_needs_an_examined_span(self):
        record = self.rebuild()
        record["features"].append(build_feature(-2.5, 52.5, DAY0 + timedelta(seconds=20), "TEST-noslot", 0))
        self.write(record)                                  # 00:00:20 lies in the 40 s hole
        with self.assertRaisesRegex(ValueError, "lies outside the examined slots"):
            self.backfill()


class FinalizeContinuesTests(unittest.TestCase):
    """One refused date is skipped (left unknown, reason recorded); the others are ingested."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root, self.records, self.status = base / "history", base / "records", base / "status.json"
        make_archive(self.root)
        self.status.write_text(json.dumps({"schema": "uk-lightning-status.v1", "health": "ok", "freshness": "ok"}))
        self.plan = ["2026-10-22", "2026-10-21", "2026-10-20", "2026-10-19"]

    def tearDown(self):
        self.temp.cleanup()

    def collector(self):
        def run(day, record, log, timeout=None):
            start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc)
            collect.rebuild_day_record(day, record, now=NOW, downloader=EdgeEumetsat(start, incident_flashes(start)))
        return run

    def shards(self, n=2):
        for m in bp.split_shards(self.plan, n):
            bp.run_shard(m["shard"], m["dates"].split(), self.records, budget_minutes=290, collector=self.collector())

    def tamper(self, day, mutate, resign=True):
        record = self.records / f"rb-{day}.json"
        data = json.loads(record.read_text())
        mutate(data)
        record.write_text(json.dumps(data))
        if resign:  # the shard summary agrees: the record reaches history.backfill_day's own gates
            for summary_path in self.records.glob("shard-*.json"):
                summary = json.loads(summary_path.read_text())
                for item in summary["collected"]:
                    if item["date"] == day:
                        item["sha256"] = bp.sha256_file(record)
                summary_path.write_text(json.dumps(summary))

    def finalize(self, n=2):
        return bp.finalize(plan_dates=self.plan, shards=n, records_dir=self.records, archive_dir=self.root,
                           status_path=self.status, now=NOW)

    def test_incident_records_with_edge_flashes_are_all_ingested(self):
        self.shards()
        result = self.finalize()
        self.assertEqual(result["stopped"], "completed")
        self.assertEqual(result["filled"], self.plan)
        status = json.loads(self.status.read_text())["backfill"]
        self.assertEqual(status["refused_days"], {})
        self.assertEqual(status["outside_day_by_day"], {d: 1 for d in self.plan})

    def test_one_inconsistent_date_is_skipped_and_the_rest_are_ingested(self):
        self.shards()

        def bad(data):
            p5 = datetime.combine(date(2026, 10, 21), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=5, seconds=9)
            data["features"].append(build_feature(-2.5, 52.5, p5 - timedelta(seconds=120),
                                                  product_name(p5, p5 + timedelta(minutes=10)), 9))
        self.tamper("2026-10-21", bad)
        result = self.finalize()
        self.assertEqual(result["stopped"], "archive_refused")
        self.assertEqual(result["filled"], ["2026-10-22", "2026-10-20", "2026-10-19"])
        self.assertIn("2026-10-21", result["refused"])
        r = rows(self.root)
        self.assertEqual((r["2026-10-21"]["coverage"], r["2026-10-21"]["count"]), ("none", None))
        for d in ("2026-10-22", "2026-10-20", "2026-10-19"):
            self.assertEqual(r[d]["count"], 4, d)
        status = json.loads(self.status.read_text())["backfill"]
        self.assertEqual(status["remaining_unknown_days"], ["2026-10-21"])
        self.assertIn("lies outside the examined slots", status["refused_days"]["2026-10-21"])
        self.assertIn("2026-10-21", status["error"])
        self.assertEqual(history.verify_archive(self.root)["integrity"], "ok")

    def test_a_record_changed_in_transit_is_skipped_and_later_dates_still_ingested(self):
        self.shards()
        self.tamper("2026-10-22", lambda data: data.update(features=[]), resign=False)
        result = self.finalize()
        self.assertEqual(result["stopped"], "archive_refused")
        self.assertEqual(result["filled"], ["2026-10-21", "2026-10-20", "2026-10-19"])
        self.assertIn("sha256", json.loads(self.status.read_text())["backfill"]["refused_days"]["2026-10-22"])
        self.assertIsNone(rows(self.root)["2026-10-22"]["count"])

    def test_an_unexpected_ingest_error_is_contained_to_its_date(self):
        self.shards()
        real = history.backfill_day

        def flaky(archive_dir, day, **kw):
            if day == "2026-10-20":
                raise OSError("disk hiccup")
            return real(archive_dir, day, **kw)
        bp.history.backfill_day = flaky
        try:
            result = self.finalize()
        finally:
            bp.history.backfill_day = real
        self.assertEqual(result["filled"], ["2026-10-22", "2026-10-21", "2026-10-19"])
        self.assertIn("OSError", result["refused"]["2026-10-20"])
        self.assertIsNone(rows(self.root)["2026-10-20"]["count"])


if __name__ == "__main__":
    unittest.main()

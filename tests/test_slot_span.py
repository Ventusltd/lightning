"""Flashes timed outside their product's NAME span: the 2026-04-12 and 2026-08-28 refusals.

Parallel backfill runs 40620556193 (on c17b65d, PR #12) and 40657338115 refused
two whole UTC dates with "flash ... lies outside the examined slots (its
product's nominal slot +- 60 s)":

* 2026-04-12, ``..._OPE_20260412001255_20260412002005_N__O_0002_0000:2326``.
  Repeat cycle 0002 is 00:10-00:20, but the product's name span starts late,
  at 00:12:55, and ends 00:20:05, across the next slot edge.
* 2026-08-28, ``..._OPE_20260828000049_20260828001009_N__O_0001_0000:203``.
  A flash of the PREVIOUS date (27 Aug 23:5x) cannot be the cause: it is dropped
  as ``outside_day`` before the gate. Every 28 Aug moment before 00:00:49 is
  within 60 s of the product's start, so the flash lay after 00:11:09.

Root cause: the gate (PR #12) took a product's "nominal slot" from the sensing
start/end in its NAME and required every flash to lie within +-60 s of it. That
span does not bound the flashes the product holds. Both products lie at the start
of the day, where cycles start late. Their flashes lay more than 60 s outside the
span, so whole dates were refused although every flash came from an examined
product. collect.py also decoded only the product's span +- 60 s (or the query
window), which would drop such flashes silently in other paths.

The fix checks a flash's OWN time:
- it decides the UTC date;
- it is consistent if it lies within 60 s of its product's span OR inside ANY
  examined span;
- otherwise the flash (of an examined product) is kept and counted, and its
  slot stays unsearched (the date is partial there, never zero, never refused).

A flash of an unexamined product is still refused. The same flash listed by two
products is archived once.

The exact flash times are not recoverable here: run logs and artifacts are
behind GitHub storage this sandbox cannot reach, and the EUMETSAT credentials
exist only as workflow secrets. The fixtures therefore cover every place such a
flash can lie, through the REAL collect download/decode path (fake products,
fake BODY decoder) and the real backfill gate and finalize. Synthetic features
exist ONLY inside temporary directories.
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
from test_unreadable_product import FakeProduct, fake_body, good_zip, make_zip

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
APR12 = datetime(2026, 4, 12, tzinfo=timezone.utc)
AUG28 = datetime(2026, 8, 28, tzinfo=timezone.utc)
S = timedelta(seconds=1)
M = timedelta(minutes=1)


def lfl_name(start: datetime, end: datetime, created: datetime | None = None, cycle: int = 1) -> str:
    created = created or end + 15 * M
    return ("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_"
            f"{created:%Y%m%d%H%M%S}_L2PF_OPE_{start:%Y%m%d%H%M%S}_{end:%Y%m%d%H%M%S}_N__O_{cycle:04d}_0000")


def april_layout(day: datetime) -> list[tuple[datetime, datetime, int]]:
    """2026-04-12 as the error shows it. Cycle 0001 runs 00:00:09-00:10:09. Cycle 0002 is
    the incident product, 00:12:55-00:20:05, so 00:10:09-00:12:55 is a hole. Then
    regular products follow from 00:20:09 (a 4 s hole). The previous date's last
    product ends 00:00:09."""
    out = [(day - 591 * S, day + 9 * S, 144), (day + 9 * S, day + 10 * M + 9 * S, 1),
           (day + 12 * M + 55 * S, day + 20 * M + 5 * S, 2)]
    cursor, cycle = day + 20 * M + 9 * S, 3
    while cursor < day + timedelta(days=1):
        out.append((cursor, cursor + 10 * M, cycle))
        cursor, cycle = cursor + 10 * M, cycle + 1
    return out


def august_layout(day: datetime) -> list[tuple[datetime, datetime, int]]:
    """2026-08-28: the previous date's last product ends 00:00:09. Cycle 0001 is the
    incident product, 00:00:49-00:10:09 (a 40 s hole before it), then regular
    10-minute products follow."""
    out = [(day - 591 * S, day + 9 * S, 144), (day + 49 * S, day + 10 * M + 9 * S, 1)]
    cursor, cycle = day + 10 * M + 9 * S, 2
    while cursor < day + timedelta(days=1):
        out.append((cursor, cursor + 10 * M, cycle))
        cursor, cycle = cursor + 10 * M, cycle + 1
    return out


APRIL_INCIDENT = lfl_name(APR12 + 12 * M + 55 * S, APR12 + 20 * M + 5 * S,
                          created=datetime(2026, 4, 12, 0, 29, 15, tzinfo=timezone.utc), cycle=2)
AUGUST_INCIDENT = lfl_name(AUG28 + 49 * S, AUG28 + 10 * M + 9 * S,
                           created=datetime(2026, 8, 28, 0, 15, 13, tzinfo=timezone.utc), cycle=1)


class LayoutEumetsat:
    """Fake whole-day search over a product layout, through the REAL download/decode path.

    ``flashes`` maps a product start to [(index, lon, lat, time)]. The index is the
    flash's position in its BODY, so the incident ids (``:2326``, ``:203``) are
    reproduced. ``skip`` leaves products out of the search (e.g. not yet published).
    """

    def __init__(self, layout, flashes: dict, skip=()):
        self.layout, self.flashes, self.skip = layout, flashes, set(skip)

    def name(self, start):
        if start == APR12 + 12 * M + 55 * S:
            return APRIL_INCIDENT
        if start == AUG28 + 49 * S:
            return AUGUST_INCIDENT
        end, cycle = next((e, c) for s, e, c in self.layout if s == start)
        return lfl_name(start, end, cycle=cycle)

    def products(self):
        for start, _, _ in self.layout:
            if start in self.skip:
                continue
            body = {str(index): [lon, lat, collect.iso_utc(moment)]
                    for index, lon, lat, moment in self.flashes.get(start, [])}
            name = self.name(start)
            yield FakeProduct(name, make_zip({f"{name}/{name}_BODY_0001.nc": json.dumps(body).encode()}))

    def __call__(self, start, end):
        return collect.download_products(self.products(), start, end, body_decoder=indexed_body)


def indexed_body(content: bytes, product_name: str, start: datetime, end: datetime):
    """Stand-in for decode_body: a JSON {BODY index: [lon, lat, time]}, filtered like decode_body."""
    out = []
    for index, (lon, lat, t) in sorted(json.loads(content).items(), key=lambda kv: int(kv[0])):
        moment = collect.parse_utc(t)
        if start <= moment < end:
            feature = build_feature(lon, lat, moment, product_name, int(index))
            if feature:
                out.append(feature)
    return out


def make_archive(root: Path) -> None:
    hourly = datetime(2026, 10, 27, tzinfo=timezone.utc)
    history.archive_observations(
        root, [build_feature(-2.12, 51.11, hourly + timedelta(hours=3), "TEST-hourly", 0)],
        now=NOW - timedelta(hours=1),
        scanned_windows=[history.successful_scan((hourly + timedelta(hours=1)).isoformat(),
                                                 (hourly + timedelta(hours=7)).isoformat(), 36)],
        lookback_hours=6.0)


def rows(root: Path) -> dict:
    return {r["date"]: r for r in json.loads((root / "index.json").read_text())["days"]}


class Harness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "history"
        make_archive(self.root)
        self.record = self.base / "rb.json"

    def tearDown(self):
        self.temp.cleanup()

    def rebuild(self, day: datetime, fake) -> dict:
        collect.rebuild_day_record(day.date().isoformat(), self.record, now=NOW, downloader=fake)
        return json.loads(self.record.read_text())

    def backfill(self, day: datetime) -> dict:
        return history.backfill_day(self.root, day.date().isoformat(), now=NOW, rebuild_record=self.record)

    def ids(self, day: datetime) -> list[str]:
        key = day.date().isoformat()
        return [f["id"] for f in history._read_features(self.root / rows(self.root)[key]["path"], key)]


class April12Tests(Harness):
    """``..._O_0002_..._OPE_20260412001255_20260412002005_...:2326``."""

    P2 = APR12 + 12 * M + 55 * S

    def test_incident_flash_in_its_cycle_before_the_late_start_is_kept_and_its_slot_stays_unknown(self):
        # 00:10:40 lies in repeat cycle 0002 (00:10-00:20) but 135 s before the name's start,
        # inside the 00:10:09-00:12:55 hole that no product covers.
        flash = APR12 + 10 * M + 40 * S
        fake = LayoutEumetsat(april_layout(APR12), {self.P2: [(2326, -2.0, 52.0, flash), (5, -2.1, 52.1, self.P2 + M)]})
        record = self.rebuild(APR12, fake)
        incident = next(f for f in record["features"] if f["id"] == f"{APRIL_INCIDENT}:2326")
        examined = {collect.parse_utc(s) for s in record["slots_examined_utc"]}
        spans = [(collect.parse_utc(w["start_utc"]), collect.parse_utc(w["end_utc"])) for w in record["scanned_ranges"]]
        # The pre-fix gate (product name span +- 60 s) is exactly what refused the date.
        self.assertEqual(collect.classify_flash(incident, examined, spans), collect.FLASH_IN_UNEXAMINED_TIME)

        result = self.backfill(APR12)
        row = rows(self.root)["2026-04-12"]
        self.assertEqual(row["count"], 2)                         # kept: never silently dropped
        self.assertEqual(row["coverage"], "partial")              # never zero, never full
        self.assertEqual(row["scanned_seconds"], 86400 - 166 - 4)  # the 00:10:09-00:12:55 and 00:20:05-00:20:09 holes
        self.assertEqual(result["unexamined_time_flashes"], 1)
        self.assertEqual(result["unexamined_time_slots_utc"], ["2026-04-12T00:10:09.000Z"])
        self.assertIn(f"{APRIL_INCIDENT}:2326", self.ids(APR12))
        self.assertEqual(history.verify_archive(self.root)["integrity"], "ok")

    def test_incident_flash_after_the_span_inside_the_next_product_is_accepted_and_counted_once(self):
        # 00:21:30 lies 85 s after the name's end, inside the 00:20:09 product, which lists the same flash too.
        flash = APR12 + 21 * M + 30 * S
        nxt = APR12 + 20 * M + 9 * S
        fake = LayoutEumetsat(april_layout(APR12), {self.P2: [(2326, -2.0, 52.0, flash)],
                                                    nxt: [(40, -2.0, 52.0, flash), (41, -2.3, 52.3, nxt + 5 * M)]})
        self.rebuild(APR12, fake)
        result = self.backfill(APR12)
        row = rows(self.root)["2026-04-12"]
        self.assertEqual(row["count"], 2)                         # the one flash once, plus the other flash
        self.assertEqual(result["cross_product_duplicates"], 1)
        self.assertEqual(result["unexamined_time_flashes"], 0)
        ids = self.ids(APR12)
        self.assertIn(lfl_name(nxt, nxt + 10 * M, cycle=3) + ":40", ids)  # the copy inside its own product's span
        self.assertNotIn(f"{APRIL_INCIDENT}:2326", ids)

    def test_incident_flash_after_the_span_listed_only_by_its_own_product_is_accepted(self):
        flash = APR12 + 21 * M + 30 * S
        fake = LayoutEumetsat(april_layout(APR12), {self.P2: [(2326, -2.0, 52.0, flash)]})
        self.rebuild(APR12, fake)
        result = self.backfill(APR12)
        self.assertEqual(rows(self.root)["2026-04-12"]["count"], 1)
        self.assertEqual((result["unexamined_time_flashes"], result["cross_product_duplicates"]), (0, 0))


class August28Tests(Harness):
    """``..._O_0001_..._OPE_20260828000049_20260828001009_...:203``."""

    P1 = AUG28 + 49 * S

    def test_previous_date_flash_of_the_product_is_dropped_and_counted_never_refused(self):
        # 27 Aug 23:58:30: belongs to 27 Aug, so it can never refuse 28 Aug.
        fake = LayoutEumetsat(august_layout(AUG28), {self.P1: [(203, -2.0, 52.0, AUG28 - 90 * S),
                                                               (204, -2.1, 52.1, self.P1 + M)]})
        record = self.rebuild(AUG28, fake)
        self.assertEqual(record["outside_day"], 1)                # decoded, then dropped and counted
        result = self.backfill(AUG28)
        self.assertEqual(rows(self.root)["2026-08-28"]["count"], 1)
        self.assertEqual(result["outside_day"], 1)

    def test_previous_date_flash_left_in_the_record_is_dropped_and_counted(self):
        record = self.rebuild(AUG28, LayoutEumetsat(august_layout(AUG28), {self.P1: [(204, -2.1, 52.1, self.P1 + M)]}))
        record["features"].append(build_feature(-2.0, 52.0, AUG28 - 90 * S, AUGUST_INCIDENT, 203))
        self.record.write_text(json.dumps(record))
        result = self.backfill(AUG28)
        self.assertEqual(result["outside_day_in_record"], 1)
        self.assertEqual(rows(self.root)["2026-08-28"]["count"], 1)

    def test_incident_flash_after_the_span_in_the_next_examined_product_is_accepted(self):
        # 00:11:30: 81 s after the name's end 00:10:09 (the only 28 Aug times the old gate refused).
        flash = AUG28 + 11 * M + 30 * S
        fake = LayoutEumetsat(august_layout(AUG28), {self.P1: [(203, -2.0, 52.0, flash)]})
        record = self.rebuild(AUG28, fake)
        examined = {collect.parse_utc(s) for s in record["slots_examined_utc"]}
        spans = [(collect.parse_utc(w["start_utc"]), collect.parse_utc(w["end_utc"])) for w in record["scanned_ranges"]]
        feature = next(f for f in record["features"] if f["id"].endswith(":203"))
        self.assertEqual(collect.classify_flash(feature, examined, spans), collect.FLASH_IN_EXAMINED_SPAN)
        result = self.backfill(AUG28)
        row = rows(self.root)["2026-08-28"]
        self.assertEqual((row["count"], row["coverage"], row["scanned_seconds"]), (1, "partial", 86400 - 40))
        self.assertEqual(result["unexamined_time_flashes"], 0)

    def test_incident_flash_in_a_slot_whose_product_was_not_examined_keeps_that_slot_unknown(self):
        flash = AUG28 + 11 * M + 30 * S
        nxt = AUG28 + 10 * M + 9 * S
        fake = LayoutEumetsat(august_layout(AUG28), {self.P1: [(203, -2.0, 52.0, flash)]}, skip={nxt})
        self.rebuild(AUG28, fake)
        result = self.backfill(AUG28)
        row = rows(self.root)["2026-08-28"]
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["scanned_seconds"], 86400 - 40 - 600)   # the hole and the missing slot stay unknown
        self.assertEqual(result["unexamined_time_slots_utc"], ["2026-08-28T00:10:09.000Z"])

    def test_flash_of_an_unexamined_product_is_still_refused(self):
        nxt = AUG28 + 10 * M + 9 * S
        record = self.rebuild(AUG28, LayoutEumetsat(august_layout(AUG28), {self.P1: [(1, -2.0, 52.0, self.P1 + M)]},
                                                    skip={nxt}))
        record["features"].append(build_feature(-2.5, 52.5, nxt + M, lfl_name(nxt, nxt + 10 * M, cycle=2), 7))
        self.record.write_text(json.dumps(record))
        before = (self.root / "index.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "lies outside the examined slots"):
            self.backfill(AUG28)
        self.assertEqual((self.root / "index.json").read_bytes(), before)


class DecodeNeverDropsSilentlyTests(unittest.TestCase):
    def test_download_keeps_flashes_far_outside_the_products_span_for_callers_to_count(self):
        start = APR12 + 12 * M + 55 * S
        name = lfl_name(start, start + 7 * M + 10 * S, cycle=2)
        rows_ = [[-2.0, 52.0, collect.iso_utc(APR12 + 10 * M + 40 * S)], [-2.0, 52.0, collect.iso_utc(APR12 + 2 * M)]]
        window = (APR12 + 12 * M, APR12 + 30 * M)
        features, count, names, unparsed, _ = collect.download_products(
            [FakeProduct(name, good_zip(name, rows_))], *window, body_decoder=fake_body)
        self.assertEqual(len(features), 2)                  # before: the 00:02:00 flash was dropped uncounted
        inside, outside = collect.split_by_window(features, *window)
        self.assertEqual((len(inside), outside), (0, 2))

    def test_second_body_file_gets_distinct_ids(self):
        name = lfl_name(APR12 + 9 * S, APR12 + 10 * M + 9 * S)
        one = json.dumps([[-2.0, 52.0, collect.iso_utc(APR12 + M)]]).encode()
        two = json.dumps([[-2.1, 52.1, collect.iso_utc(APR12 + 2 * M)]]).encode()
        blob = make_zip({f"{name}/{name}_BODY_0001.nc": one, f"{name}/{name}_BODY_0002.nc": two})
        features = collect.decode_product(blob, name, *collect.DECODE_ALL_TIMES, body_decoder=fake_body)
        self.assertEqual([f["id"] for f in features], [f"{name}:0", f"{name}:b1.0"])
        self.assertEqual({collect.flash_slot(f) for f in features}, {(APR12 + 9 * S, APR12 + 10 * M + 9 * S)})


class FinalizeTests(unittest.TestCase):
    """Both incident dates are ingested by finalize; the run is not refused."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root, self.records, self.status = base / "history", base / "records", base / "status.json"
        make_archive(self.root)
        self.status.write_text(json.dumps({"schema": "uk-lightning-status.v1", "health": "ok", "freshness": "ok"}))
        self.plan = ["2026-08-28", "2026-04-12"]

    def tearDown(self):
        self.temp.cleanup()

    def collector(self, day, record, log, timeout=None):
        if day == "2026-04-12":
            fake = LayoutEumetsat(april_layout(APR12), {APR12 + 12 * M + 55 * S: [(2326, -2.0, 52.0, APR12 + 10 * M + 40 * S)]})
        else:
            fake = LayoutEumetsat(august_layout(AUG28), {AUG28 + 49 * S: [(203, -2.0, 52.0, AUG28 + 11 * M + 30 * S)]})
        collect.rebuild_day_record(day, record, now=NOW, downloader=fake)

    def test_both_incident_dates_are_filled(self):
        for m in bp.split_shards(self.plan, 2):
            bp.run_shard(m["shard"], m["dates"].split(), self.records, budget_minutes=290, collector=self.collector)
        result = bp.finalize(plan_dates=self.plan, shards=2, records_dir=self.records, archive_dir=self.root,
                             status_path=self.status, now=NOW)
        self.assertEqual(result["stopped"], "completed")
        self.assertEqual(result["filled"], self.plan)
        self.assertEqual(result["refused"], {})
        status = json.loads(self.status.read_text())["backfill"]
        self.assertEqual(status["unexamined_time_flashes_by_day"],
                         {"2026-04-12": {"flashes": 1, "slots_utc": ["2026-04-12T00:10:09.000Z"]}})
        r = rows(self.root)
        self.assertEqual((r["2026-04-12"]["count"], r["2026-04-12"]["coverage"]), (1, "partial"))
        self.assertEqual((r["2026-08-28"]["count"], r["2026-08-28"]["coverage"]), (1, "partial"))
        self.assertEqual(history.verify_archive(self.root)["integrity"], "ok")


if __name__ == "__main__":
    unittest.main()

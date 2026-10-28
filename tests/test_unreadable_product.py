"""One unreadable EUMETSAT product must not stop a whole search.

Incident (parallel backfill, shard 2, 2026-09-18): one product ZIP had no BODY
NetCDF, collect.py raised ValueError and the shard stopped, leaving 22 dates
unknown. A per-product CONTENT error (ZIP without BODY, corrupt ZIP, corrupt
NetCDF) now makes that product's 10-minute slot UNPARSED: it is not searched
time (D-3), so the day becomes partial with that slot unknown, never zero, and
the run carries on. Network/auth/rate-limit errors, a missing decoder library,
and a day with more than 20 % unparsed slots still stop the run.

The fake downloader builds real ZIP bytes and goes through the real
``collect.download_products`` / ``decode_product``; only the BODY NetCDF reader
is faked (a JSON stand-in) because CI has no netCDF4. Synthetic features
("TEST-") exist only in temporary directories.
"""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backfill_parallel as bp
import collect
import history
import status as status_mod
from collect import build_feature

NOW = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
TARGET = "2026-10-20"
DAY0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
INCIDENT = ("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD-PART-x-x--ARC-x_C_EUMT_20260918003140_L2PF_OPE_"
            "20260918002945_20260918003005_N__O_0003_0000")


def slot_name(start: datetime) -> str:
    end = start + timedelta(minutes=10)
    return ("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_"
            f"{end:%Y%m%d%H%M%S}_L2PF_OPE_{start:%Y%m%d%H%M%S}_{end:%Y%m%d%H%M%S}_N__O_0001_0000")


def make_zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def good_zip(name: str, flashes) -> bytes:
    body = json.dumps([[lon, lat, t] for lon, lat, t in flashes]).encode()
    return make_zip({f"{name}/EOPMetadata.xml": b"<x/>", f"{name}/{name}_BODY_0001.nc": body,
                     f"{name}/{name}_TRAIL.nc": b"trail"})


def no_body_zip(name: str) -> bytes:
    """The incident: a product ZIP with manifest and TRAIL but no BODY member."""
    return make_zip({f"{name}/EOPMetadata.xml": b"<x/>", f"{name}/{name}_TRAIL.nc": b"trail"})


def corrupt_body_zip(name: str) -> bytes:
    return make_zip({f"{name}/{name}_BODY_0001.nc": b"\x00garbage, not HDF5"})


def fake_body(content: bytes, product_name: str, start: datetime, end: datetime):
    """JSON stand-in for decode_body; garbage raises like netCDF4 does on a corrupt file."""
    try:
        rows = json.loads(content)
    except ValueError:
        raise OSError(-51, "NetCDF: Unknown file format") from None
    out = []
    for i, (lon, lat, t) in enumerate(rows):
        moment = collect.parse_utc(t)
        if start <= moment < end:
            out.append(build_feature(lon, lat, moment, product_name, i))
    return out


class FakeProduct:
    def __init__(self, name, blob=b"", error=None):
        self.name, self.blob, self.error = name, blob, error

    def __str__(self):
        return self.name

    def open(self):
        if self.error:
            raise self.error
        return io.BytesIO(self.blob)


class FakeEumetsat:
    """Fake downloader: one product ZIP per 10-minute slot through the REAL decode path.

    ``no_body``/``corrupt`` are slot indexes whose ZIP lacks a BODY member or has
    a corrupt BODY; ``net_error`` raises on opening that slot (network failure);
    ``extra`` adds whole extra products (name, blob).
    """

    def __init__(self, no_body=(), corrupt=(), net_error=None, extra=(), body_decoder=fake_body):
        self.no_body, self.corrupt, self.net_error = set(no_body), set(corrupt), net_error or {}
        self.extra, self.body_decoder, self.calls = list(extra), body_decoder, []

    def products(self, start, end):
        cursor, i = start, 0
        while cursor < end:
            name = slot_name(cursor)
            if i in self.net_error:
                yield FakeProduct(name, error=self.net_error[i])
            elif i in self.no_body:
                yield FakeProduct(name, no_body_zip(name))
            elif i in self.corrupt:
                yield FakeProduct(name, corrupt_body_zip(name))
            else:
                t = (cursor + timedelta(minutes=3)).isoformat()
                yield FakeProduct(name, good_zip(name, [(-2.0, 52.0, t)] if i % 6 == 0 else []))
            cursor += timedelta(minutes=10)
            i += 1
        for name, blob in self.extra:
            yield FakeProduct(name, blob)

    def __call__(self, start, end):
        self.calls.append((start, end))
        return collect.download_products(self.products(start, end), start, end, body_decoder=self.body_decoder)


def make_archive(root: Path, now: datetime, searched: date) -> None:
    start = datetime.combine(searched, datetime.min.time(), tzinfo=timezone.utc)
    history.archive_observations(
        root, [build_feature(-2.0, 52.0, start + timedelta(hours=2), "TEST-hourly", 0)], now=now,
        scanned_windows=[history.successful_scan((start + timedelta(hours=1)).isoformat(),
                                                 (start + timedelta(hours=7)).isoformat(), 36)],
        lookback_hours=6.0)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.record = self.base / "rb.json"
        self.sleeps = []  # download retries never really sleep in tests
        patcher = mock.patch.object(collect, "_sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def rebuild(self, fake, day=TARGET):
        return collect.rebuild_day_record(day, self.record, now=NOW, downloader=fake)

    def load(self):
        return json.loads(self.record.read_text())


class RebuildDayTests(Base):
    def test_zip_without_body_becomes_an_unparsed_slot_and_the_run_carries_on(self):
        self.assertEqual(self.rebuild(FakeEumetsat(no_body={3})), 0)
        rec = self.load()
        bad = slot_name(DAY0 + timedelta(minutes=30))
        self.assertEqual(rec["outcome"], "success")
        self.assertIs(rec["slots_parsed"], True)
        self.assertEqual(rec["products_unparsed"], 1)
        self.assertEqual(rec["products_examined"], 143)
        self.assertEqual(rec["slots_examined"], 143)
        [entry] = rec["unparsed_products"]
        self.assertEqual(entry["product"], bad)
        self.assertIn("ZIP contained no BODY NetCDF", entry["reason"])
        self.assertEqual((entry["slot_start_utc"], entry["slot_end_utc"]),
                         ("2026-10-20T00:30:00.000Z", "2026-10-20T00:40:00.000Z"))
        self.assertEqual(rec["unparsed_slots_utc"], ["2026-10-20T00:30:00.000Z"])
        # The unparsed slot is NOT searched time: it is a hole in scanned_ranges.
        self.assertEqual(rec["unscanned_ranges"], [{"start_utc": "2026-10-20T00:30:00.000Z",
                                                    "end_utc": "2026-10-20T00:40:00.000Z"}])
        self.assertEqual(len(rec["features"]), 24)  # flashes of the readable products are kept

    def test_the_incident_product_name_is_recorded_and_does_not_stop_the_day(self):
        fake = FakeEumetsat(extra=[(INCIDENT, no_body_zip(INCIDENT))])
        day = "2026-09-18"
        self.assertEqual(collect.rebuild_day_record(day, self.record, now=NOW, downloader=fake), 0)
        rec = self.load()
        self.assertEqual(rec["outcome"], "success")
        self.assertEqual([e["product"] for e in rec["unparsed_products"]], [INCIDENT])
        self.assertEqual(rec["unparsed_products"][0]["reason"],
                         f"ValueError: {INCIDENT}: ZIP contained no BODY NetCDF")
        # Its 20-second PART slot is covered by readable regular products: nothing left unknown.
        self.assertEqual(rec["unparsed_slots_utc"], [])
        self.assertEqual(rec["unscanned_ranges"], [])

    def test_corrupt_netcdf_and_corrupt_zip_are_unparsed_slots_too(self):
        bad_zip = slot_name(DAY0 + timedelta(hours=5))
        fake = FakeEumetsat(corrupt={10})
        fake.products_orig = fake.products

        def products(start, end):
            for p in fake.products_orig(start, end):
                if str(p) == bad_zip:
                    p.blob = b"PK\x03\x04 truncated"
                yield p
        fake.products = products
        self.rebuild(fake)
        rec = self.load()
        reasons = {e["product"]: e["reason"] for e in rec["unparsed_products"]}
        self.assertEqual(rec["products_unparsed"], 2)
        self.assertIn("NetCDF: Unknown file format", reasons[slot_name(DAY0 + timedelta(minutes=100))])
        self.assertIn("expected EUMETSAT product ZIP", reasons[bad_zip])
        self.assertEqual(len(rec["unscanned_ranges"]), 2)

    def test_backfill_ingests_the_day_as_partial_with_the_unparsed_slot_unknown(self):
        root = self.base / "history"
        make_archive(root, NOW - timedelta(hours=1), date(2026, 10, 26))
        self.rebuild(FakeEumetsat(no_body={3, 4}))
        result = history.backfill_day(root, TARGET, now=NOW, rebuild_record=self.record)
        row = result["row"]
        self.assertEqual(row["coverage"], "partial")
        self.assertEqual(row["scanned_seconds"], 86400 - 1200)
        self.assertEqual(row["count"], 24)
        self.assertEqual(result["unscanned_ranges"], [{"start_utc": "2026-10-20T00:30:00.000Z",
                                                       "end_utc": "2026-10-20T00:50:00.000Z"}])

    def test_up_to_twenty_percent_unparsed_slots_still_succeed(self):
        self.rebuild(FakeEumetsat(no_body=set(range(28))))  # 28/144 = 19.4 %
        rec = self.load()
        self.assertEqual((rec["outcome"], rec["products_unparsed"]), ("success", 28))

    def test_more_than_twenty_percent_unparsed_slots_stop_the_date(self):
        with self.assertRaises(collect.TooManyUnparsed) as caught:
            self.rebuild(FakeEumetsat(no_body=set(range(29))))  # 29/144 = 20.1 %
        self.assertIn("2026-10-20", str(caught.exception))
        self.assertIn("29 of 144", str(caught.exception))
        rec = self.load()
        self.assertEqual(rec["outcome"], "failure")
        self.assertEqual(rec["products_unparsed"], 29)
        self.assertEqual(len(rec["unparsed_products"]), 29)
        self.assertEqual(rec["features"], [])

    def test_non_retryable_http_and_auth_errors_still_stop(self):
        # 401 here: the fake product has no eumdac token to re-request, so it stops too.
        # A 404/410/400 on ONE product is now an unparsed slot (test_download_retries:
        # test_one_product_the_store_will_not_serve_becomes_an_unparsed_slot).
        for error in (RuntimeError("HTTP 401 Unauthorized"), RuntimeError("HTTP 403 Forbidden"),
                      PermissionError("denied")):
            with self.subTest(error=error):
                self.record.unlink(missing_ok=True)
                with self.assertRaises(type(error)):
                    self.rebuild(FakeEumetsat(net_error={7: error}))
                self.assertFalse(self.record.exists() and self.load().get("outcome") == "success")

    def test_persistent_transient_errors_leave_one_unparsed_slot_after_retries(self):
        # Since the download-retry fix: retried, then the slot is unknown and the day carries on.
        for error in (RuntimeError("HTTP 429 Too Many Requests"), RuntimeError("HTTP 503 Service Unavailable"),
                      TimeoutError("read timed out"), ConnectionResetError(104, "Connection reset by peer")):
            with self.subTest(error=error):
                self.assertEqual(self.rebuild(FakeEumetsat(net_error={7: error})), 0)
                rec = self.load()
                self.assertEqual((rec["outcome"], rec["products_unparsed"]), ("success", 1))
                self.assertEqual(rec["unparsed_products"][0]["kind"], "download")

    @unittest.skipIf(__import__("importlib").util.find_spec("netCDF4") is not None, "netCDF4 installed")
    def test_a_missing_netcdf_library_is_not_an_unparsed_product(self):
        with self.assertRaises(ImportError):
            self.rebuild(FakeEumetsat(body_decoder=None))

    @unittest.skipUnless(__import__("importlib").util.find_spec("netCDF4") is not None, "netCDF4 not installed")
    def test_real_netcdf_reader_on_corrupt_body_is_an_unparsed_slot(self):
        fake = FakeEumetsat(corrupt={1})
        fake.products_orig = fake.products
        fake.products = lambda s, e: (p for p in fake.products_orig(s, e) if str(p) == slot_name(DAY0 + timedelta(minutes=10)))
        fake.body_decoder = None
        with self.assertRaises(RuntimeError):  # the only product is unreadable: nothing was searched
            self.rebuild(fake)


class ShardTests(Base):
    def test_shard_and_finalize_fill_the_day_and_surface_the_count_in_status(self):
        root, out, status_path = self.base / "history", self.base / "records", self.base / "status.json"
        make_archive(root, NOW - timedelta(hours=1), date(2026, 10, 26))
        dates = ["2026-10-20", "2026-10-19"]
        fakes = {"2026-10-20": FakeEumetsat(no_body={3}), "2026-10-19": FakeEumetsat()}
        collector = lambda day, record, log, timeout=None: collect.rebuild_day_record(day, record, now=NOW, downloader=fakes[day])
        summary = bp.run_shard(1, dates, out, budget_minutes=290, collector=collector)
        self.assertEqual((summary["stopped"], len(summary["collected"])), ("completed", 2))
        result = bp.finalize(plan_dates=dates, shards=1, records_dir=out, archive_dir=root,
                             status_path=status_path, now=NOW)
        self.assertEqual(result["filled"], dates)
        block = json.loads(status_path.read_text())["backfill"]
        self.assertEqual(block["unparsed_products"], 1)
        self.assertEqual(block["unparsed_products_by_day"], {"2026-10-20": 1})
        rows = {r["date"]: r for r in json.loads((root / "index.json").read_text())["days"]}
        self.assertEqual(rows["2026-10-20"]["coverage"], "partial")
        self.assertEqual(rows["2026-10-19"]["coverage"], "full")

    def test_a_date_over_the_threshold_stops_the_shard_and_stays_unknown(self):
        out = self.base / "records"
        fake = FakeEumetsat(no_body=set(range(40)))
        collector = lambda day, record, log, timeout=None: collect.rebuild_day_record(day, record, now=NOW, downloader=fake)
        summary = bp.run_shard(1, ["2026-10-20", "2026-10-19"], out, budget_minutes=290, collector=collector)
        self.assertEqual(summary["stopped"], "eumetsat_error")
        self.assertEqual(summary["collected"], [])
        self.assertIn("unparsed", summary["error"])
        self.assertFalse((out / "rb-2026-10-20.json").exists())


class HourlyTests(Base):
    def run_main(self, fake, now):
        out, status_path = self.base / "feed.geojson", self.base / "status.json"
        with mock.patch.object(collect, "download", fake):
            code = collect.main(["--hours", "6", "--out", str(out), "--status", str(status_path),
                                 "--history-dir", str(self.base / "history"), "--run-record", str(self.record)],
                                now=now)
        return code, out

    def test_hourly_run_publishes_and_records_the_unparsed_slot(self):
        now = datetime(2026, 10, 20, 12, 5, tzinfo=timezone.utc)
        code, out = self.run_main(FakeEumetsat(no_body={5}), now)
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())
        rec = self.load()
        self.assertEqual((rec["outcome"], rec["products_unparsed"]), ("success", 1))
        self.assertIn("ZIP contained no BODY NetCDF", rec["unparsed_products"][0]["reason"])
        self.assertIn({"start_utc": "2026-10-20T06:55:00.000Z", "end_utc": "2026-10-20T07:05:00.000Z"},
                      rec["unscanned_ranges"])
        self.assertEqual(json.loads(out.read_text())["metadata"]["products_unparsed"], 1)
        status = status_mod.build_status(now=now, previous={}, record=rec, archive_result=None,
                                         collect_outcome="success", archive_outcome="skipped")
        self.assertEqual(status["last_products_unparsed"], 1)
        self.assertEqual(status["last_unparsed_products"][0]["product"], rec["unparsed_products"][0]["product"])

    def test_hourly_run_over_the_threshold_stops_and_leaves_the_feed_alone(self):
        now = datetime(2026, 10, 20, 12, 5, tzinfo=timezone.utc)
        with self.assertRaises(collect.TooManyUnparsed):
            self.run_main(FakeEumetsat(no_body=set(range(10))), now)  # 10 of 36 slots of 2026-10-20
        rec = self.load()
        self.assertEqual(rec["outcome"], "failure")
        self.assertEqual(rec["products_unparsed"], 10)
        self.assertFalse((self.base / "feed.geojson").exists())

    def test_hourly_non_retryable_error_still_stops(self):
        now = datetime(2026, 10, 20, 12, 5, tzinfo=timezone.utc)
        with self.assertRaises(RuntimeError):
            self.run_main(FakeEumetsat(net_error={2: RuntimeError("HTTP 403 Forbidden")}), now)
        self.assertEqual(self.load()["outcome"], "failure")


if __name__ == "__main__":
    unittest.main()

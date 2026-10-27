"""A single transient download error must not stop a whole search.

Incident (parallel backfill run 40553623176, shard 2, 2026-09-16): one product
download broke mid-stream with
``urllib3.exceptions.ProtocolError: ('Connection broken: IncompleteRead(4955
bytes read, 233290 more expected)', ...)``; collect.py raised, the shard stopped
and only 3 dates were filled. The product download now retries transient
errors (IncompleteRead, ProtocolError, ConnectionError, ChunkedEncodingError,
timeouts, HTTP 500/502/503/504) 4 times with exponential backoff and jitter
(2, 4, 8, 16 s, capped at 30 s), honours Retry-After on 429 (at most 3 waits of
at most 60 s) and re-requests the access token once on 401. A product that
still fails becomes an UNPARSED slot (unknown, never zero, with its reason) and
the day carries on. The search stops only when a date exceeds the unparsed-slot
threshold or 5 consecutive products fail (a likely outage). Retry counts and
reasons are in the run record and status.json.

The fakes below go through the REAL ``collect.download_products`` retry loop
and ZIP decode; only the network (``product.open``), the sleep and the BODY
NetCDF reader are faked. No real sleeping happens.
"""
import http.client
import io
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import backfill_parallel as bp
import collect
import status as status_mod
from test_unreadable_product import DAY0, NOW, TARGET, fake_body, good_zip, make_archive, slot_name


# --- stand-ins for the exceptions urllib3 / requests / eumdac raise ---------
# CI installs neither urllib3 nor requests, so classes with the same NAMES
# stand in; collect.py classifies by class name, so the real ones match too.

class ProtocolError(Exception):  # urllib3.exceptions.ProtocolError
    pass


class ChunkedEncodingError(Exception):  # requests.exceptions.ChunkedEncodingError
    pass


class RequestsConnectionError(OSError):  # requests.exceptions.ConnectionError
    pass


RequestsConnectionError.__name__ = "ConnectionError"


class ReadTimeout(Exception):  # requests.exceptions.ReadTimeout -> Timeout
    pass


class Timeout(Exception):
    pass


class ReadTimeoutSub(Timeout):
    pass


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code, self.headers = status_code, dict(headers or {})


class HTTPError(Exception):  # requests.HTTPError
    def __init__(self, status, headers=None):
        super().__init__(f"{status} Client Error")
        self.response = FakeResponse(status, headers)


class ProductError(Exception):  # eumdac.product.ProductError (raised "from" the HTTPError)
    def __init__(self, status, headers=None):
        super().__init__(f"Could not download Product x of Collection {collect.COLLECTION}")
        self.extra_info = {"status": status}
        self.__cause__ = HTTPError(status, headers)


def incident_error():
    """The exact shape of the run 40553623176 failure."""
    inner = http.client.IncompleteRead(b"x" * 4955, 233290)
    return ProtocolError(f"Connection broken: {inner!r}", inner)


class FakeProduct:
    """Each open() pops the next scripted outcome: an exception, or success (None)."""

    def __init__(self, name, blob, script, opened):
        self.name, self.blob, self.script, self.opened = name, blob, list(script), opened

    def __str__(self):
        return self.name

    def open(self):
        self.opened.append(self.name)
        outcome = self.script.pop(0) if self.script else None
        if callable(outcome):
            outcome = outcome()
        if outcome is not None:
            raise outcome
        return io.BytesIO(self.blob)


class FlakyEumetsat:
    """Fake downloader: one product per 10-minute slot; ``scripts[i]`` lists the
    outcomes of successive open() calls of slot i (exception or None = success).
    ``always[i]`` is an exception raised on every open() of slot i."""

    def __init__(self, scripts=None, always=None, token_refresher=None):
        self.scripts, self.always = dict(scripts or {}), dict(always or {})
        self.sleeps, self.opened, self.calls = [], [], []
        self.token_refresher = token_refresher

    def products(self, start, end):
        cursor, i = start, 0
        while cursor < end:
            name = slot_name(cursor)
            if i in self.always:
                script = [self.always[i]] * 100
            else:
                script = self.scripts.get(i, [])
            t = (cursor + timedelta(minutes=3)).isoformat()
            yield FakeProduct(name, good_zip(name, [(-2.0, 52.0, t)] if i % 6 == 0 else []), script, self.opened)
            cursor += timedelta(minutes=10)
            i += 1

    def __call__(self, start, end):
        self.calls.append((start, end))
        return collect.download_products(self.products(start, end), start, end, body_decoder=fake_body,
                                         sleep=self.sleeps.append, token_refresher=self.token_refresher)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.record = self.base / "rb.json"

    def tearDown(self):
        self.temp.cleanup()

    def rebuild(self, fake, day=TARGET):
        return collect.rebuild_day_record(day, self.record, now=NOW, downloader=fake)

    def load(self):
        return json.loads(self.record.read_text())

    def assertBackoff(self, sleeps, bases):
        """Exponential backoff with jitter: each wait is in [base, min(30, 1.5 * base)]."""
        self.assertEqual(len(sleeps), len(bases), sleeps)
        for wait, base in zip(sleeps, bases):
            self.assertGreaterEqual(wait, base)
            self.assertLessEqual(wait, min(30, base * 1.5))


class TransientRetryTests(Base):
    def test_incomplete_read_twice_then_success_fills_the_day_with_no_unparsed_slot(self):
        fake = FlakyEumetsat(scripts={4: [incident_error(), http.client.IncompleteRead(b"", 10), None]})
        self.assertEqual(self.rebuild(fake), 0)
        rec = self.load()
        self.assertEqual(rec["outcome"], "success")
        self.assertEqual((rec["products_unparsed"], rec["products_examined"], rec["slots_examined"]), (0, 144, 144))
        self.assertEqual(rec["unscanned_ranges"], [])
        self.assertEqual(fake.opened.count(slot_name(DAY0 + timedelta(minutes=40))), 3)
        self.assertBackoff(fake.sleeps, [2, 4])
        retries = rec["download_retries"]
        self.assertEqual(retries["retries"], 2)
        self.assertEqual(retries["products_retried"], 1)
        self.assertEqual(retries["products_failed"], 0)
        self.assertEqual(retries["reasons"], {"ProtocolError": 1, "IncompleteRead": 1})
        self.assertEqual([e["product"] for e in retries["events"]], [slot_name(DAY0 + timedelta(minutes=40))] * 2)
        self.assertIn("Connection broken: IncompleteRead(4955 bytes read, 233290 more expected)",
                      retries["events"][0]["error"])

    def test_every_transient_kind_is_retried(self):
        kinds = [incident_error(), http.client.IncompleteRead(b"", 5), RequestsConnectionError("reset"),
                 ConnectionResetError(104, "Connection reset by peer"), ChunkedEncodingError("broken chunk"),
                 TimeoutError("read timed out"), ReadTimeoutSub("read timeout"),
                 ProductError(500), ProductError(502), ProductError(503), ProductError(504), HTTPError(503)]
        for error in kinds:
            with self.subTest(error=repr(error)):
                fake = FlakyEumetsat(scripts={2: [error, None]})
                self.assertEqual(self.rebuild(fake), 0)
                rec = self.load()
                self.assertEqual((rec["outcome"], rec["products_unparsed"]), ("success", 0))
                self.assertEqual(rec["download_retries"]["retries"], 1)
                self.assertBackoff(fake.sleeps, [2])

    def test_a_product_that_always_fails_becomes_an_unparsed_slot_and_the_day_carries_on(self):
        fake = FlakyEumetsat(always={7: incident_error()})
        self.assertEqual(self.rebuild(fake), 0)
        rec = self.load()
        bad = slot_name(DAY0 + timedelta(minutes=70))
        self.assertEqual(rec["outcome"], "success")
        self.assertEqual(fake.opened.count(bad), 5)  # 1 try + 4 retries
        self.assertBackoff(fake.sleeps, [2, 4, 8, 16])
        self.assertEqual((rec["products_unparsed"], rec["products_examined"]), (1, 143))
        [entry] = rec["unparsed_products"]
        self.assertEqual(entry["product"], bad)
        self.assertEqual(entry["kind"], "download")
        self.assertEqual(entry["attempts"], 5)
        self.assertIn("download failed after 5 attempts", entry["reason"])
        self.assertIn("ProtocolError", entry["reason"])
        self.assertEqual(rec["unparsed_slots_utc"], ["2026-10-20T01:10:00.000Z"])
        # Unknown, not zero: the failed slot is a hole in the searched coverage.
        self.assertEqual(rec["unscanned_ranges"], [{"start_utc": "2026-10-20T01:10:00.000Z",
                                                    "end_utc": "2026-10-20T01:20:00.000Z"}])
        retries = rec["download_retries"]
        self.assertEqual((retries["retries"], retries["products_failed"]), (4, 1))
        self.assertEqual(retries["failed_products"], [bad])

    def test_backoff_never_exceeds_thirty_seconds(self):
        with mock.patch.object(collect, "RETRY_BACKOFF_SECONDS", (2, 4, 8, 16, 32, 64)), \
             mock.patch.object(collect, "TRANSIENT_RETRIES", 6):
            fake = FlakyEumetsat(always={1: incident_error()})
            self.rebuild(fake)
        self.assertEqual(len(fake.sleeps), 6)
        self.assertTrue(all(w <= 30 for w in fake.sleeps), fake.sleeps)
        self.assertEqual(fake.sleeps[-2:], [30, 30])

    def test_four_consecutive_failures_then_a_good_product_carry_on(self):
        fake = FlakyEumetsat(always={i: incident_error() for i in range(10, 14)})
        self.assertEqual(self.rebuild(fake), 0)
        rec = self.load()
        self.assertEqual((rec["outcome"], rec["products_unparsed"]), ("success", 4))
        self.assertEqual(rec["download_retries"]["max_consecutive_failed_products"], 4)

    def test_five_consecutive_failed_products_stop_the_search_as_a_likely_outage(self):
        fake = FlakyEumetsat(always={i: incident_error() for i in range(20, 30)})
        with self.assertRaises(collect.DownloadOutage) as caught:
            self.rebuild(fake)
        self.assertIn("5 consecutive products failed", str(caught.exception))
        self.assertEqual(len(set(fake.opened)), 25)  # 20 good products, then stopped at the 5th failure
        rec = self.load()
        self.assertEqual(rec["outcome"], "failure")
        self.assertIn("DownloadOutage", rec["error"])
        self.assertEqual(rec["products_unparsed"], 5)
        self.assertEqual(rec["download_retries"]["products_failed"], 5)
        self.assertEqual(rec["features"], [])

    def test_scattered_failures_over_the_day_threshold_still_stop_the_date(self):
        fake = FlakyEumetsat(always={i: incident_error() for i in range(0, 58, 2)})  # 29 of 144, never 5 in a row
        with self.assertRaises(collect.TooManyUnparsed) as caught:
            self.rebuild(fake)
        self.assertIn("29 of 144", str(caught.exception))
        rec = self.load()
        self.assertEqual(rec["outcome"], "failure")
        self.assertEqual(rec["download_retries"]["products_failed"], 29)

    def test_non_transient_errors_stop_at_once_without_retry(self):
        for error in (ProductError(403), ProductError(404), HTTPError(400), PermissionError("denied"),
                      RuntimeError("HTTP 403 Forbidden")):
            with self.subTest(error=repr(error)):
                self.record.unlink(missing_ok=True)
                fake = FlakyEumetsat(always={3: error})
                with self.assertRaises(type(error)):
                    self.rebuild(fake)
                self.assertEqual(fake.opened.count(slot_name(DAY0 + timedelta(minutes=30))), 1)
                self.assertEqual(fake.sleeps, [])


class RateLimitTests(Base):
    def test_429_with_retry_after_waits_that_long_then_succeeds(self):
        fake = FlakyEumetsat(scripts={5: [ProductError(429, {"Retry-After": "7"}), None]})
        self.assertEqual(self.rebuild(fake), 0)
        rec = self.load()
        self.assertEqual(fake.sleeps, [7])
        self.assertEqual((rec["outcome"], rec["products_unparsed"]), ("success", 0))
        retries = rec["download_retries"]
        self.assertEqual((retries["rate_limit_waits"], retries["rate_limit_wait_seconds"]), (1, 7))
        self.assertEqual(retries["reasons"], {"HTTP 429": 1})

    def test_retry_after_is_capped_at_sixty_seconds_and_http_dates_are_understood(self):
        later = "Wed, 21 Oct 2099 07:28:00 GMT"
        fake = FlakyEumetsat(scripts={5: [HTTPError(429, {"Retry-After": "600"}),
                                          HTTPError(429, {"retry-after": later}), None]})
        self.rebuild(fake)
        self.assertEqual(fake.sleeps, [60, 60])

    def test_at_most_three_rate_limit_waits_then_the_slot_is_unparsed(self):
        fake = FlakyEumetsat(always={5: ProductError(429, {"Retry-After": "1"})})
        self.assertEqual(self.rebuild(fake), 0)
        rec = self.load()
        self.assertEqual(fake.sleeps, [1, 1, 1])
        self.assertEqual(fake.opened.count(slot_name(DAY0 + timedelta(minutes=50))), 4)
        [entry] = rec["unparsed_products"]
        self.assertEqual(entry["kind"], "download")
        self.assertIn("HTTP 429", entry["reason"])
        self.assertEqual(rec["download_retries"]["rate_limit_waits"], 3)


class TokenTests(Base):
    def test_401_re_requests_the_access_token_once_and_retries(self):
        refreshed = []
        fake = FlakyEumetsat(scripts={2: [ProductError(401), None]}, token_refresher=lambda p: refreshed.append(str(p)))
        self.assertEqual(self.rebuild(fake), 0)
        self.assertEqual(refreshed, [slot_name(DAY0 + timedelta(minutes=20))])
        self.assertEqual(self.load()["download_retries"]["token_refreshes"], 1)

    def test_a_second_401_stops_the_search(self):
        refreshed = []
        fake = FlakyEumetsat(always={2: ProductError(401)}, token_refresher=lambda p: refreshed.append(str(p)))
        with self.assertRaises(ProductError):
            self.rebuild(fake)
        self.assertEqual(len(refreshed), 1)

    def test_default_refresher_forces_a_new_eumdac_token(self):
        class Token:
            renewed = 0

            def _update_token_data(self):
                Token.renewed += 1

        product = mock.Mock()
        product.datastore.token = Token()
        collect.refresh_access_token(product)
        self.assertEqual(Token.renewed, 1)


class ShardAndStatusTests(Base):
    def test_shard_completes_through_transient_errors_and_status_surfaces_the_retries(self):
        root, out, status_path = self.base / "history", self.base / "records", self.base / "status.json"
        make_archive(root, NOW - timedelta(hours=1), date(2026, 10, 26))
        dates = ["2026-10-20", "2026-10-19"]
        fakes = {"2026-10-20": FlakyEumetsat(scripts={4: [incident_error(), incident_error(), None]},
                                             always={9: incident_error()}),
                 "2026-10-19": FlakyEumetsat()}
        collector = lambda day, record, log: collect.rebuild_day_record(day, record, now=NOW, downloader=fakes[day])
        summary = bp.run_shard(1, dates, out, budget_minutes=290, collector=collector)
        self.assertEqual((summary["stopped"], len(summary["collected"])), ("completed", 2))
        result = bp.finalize(plan_dates=dates, shards=1, records_dir=out, archive_dir=root,
                             status_path=status_path, now=NOW)
        self.assertEqual(result["filled"], dates)
        block = json.loads(status_path.read_text())["backfill"]
        self.assertEqual(block["unparsed_products_by_day"], {"2026-10-20": 1})
        self.assertEqual(block["download_retries"], 6)
        self.assertEqual(block["download_retries_by_day"]["2026-10-20"]["retries"], 6)
        self.assertEqual(block["download_retries_by_day"]["2026-10-20"]["products_failed"], 1)
        self.assertEqual(block["download_retries_by_day"]["2026-10-20"]["reasons"], {"ProtocolError": 6})
        self.assertNotIn("2026-10-19", block["download_retries_by_day"])
        rows = {r["date"]: r for r in json.loads((root / "index.json").read_text())["days"]}
        self.assertEqual(rows["2026-10-20"]["coverage"], "partial")

    def test_an_outage_stops_the_shard(self):
        out = self.base / "records"
        fake = FlakyEumetsat(always={i: incident_error() for i in range(144)})
        collector = lambda day, record, log: collect.rebuild_day_record(day, record, now=NOW, downloader=fake)
        summary = bp.run_shard(1, ["2026-10-20", "2026-10-19"], out, budget_minutes=290, collector=collector)
        self.assertEqual(summary["stopped"], "eumetsat_error")
        self.assertIn("consecutive products failed", summary["error"])
        self.assertEqual(summary["collected"], [])

    def test_hourly_run_retries_and_status_json_shows_the_counts(self):
        now = datetime(2026, 10, 20, 12, 5, tzinfo=timezone.utc)
        out, status_path = self.base / "feed.geojson", self.base / "status.json"
        fake = FlakyEumetsat(scripts={3: [incident_error(), None]}, always={6: ProductError(503)})
        with mock.patch.object(collect, "download", fake):
            code = collect.main(["--hours", "6", "--out", str(out), "--status", str(status_path),
                                 "--history-dir", str(self.base / "history"), "--run-record", str(self.record)],
                                now=now)
        self.assertEqual(code, 0)
        rec = self.load()
        self.assertEqual((rec["outcome"], rec["products_unparsed"]), ("success", 1))
        self.assertEqual(rec["download_retries"]["retries"], 5)
        self.assertEqual(rec["download_retries"]["reasons"], {"ProtocolError": 1, "HTTP 503": 4})
        status = status_mod.build_status(now=now, previous={}, record=rec, archive_result=None,
                                         collect_outcome="success", archive_outcome="skipped")
        self.assertEqual(status["last_download_retries"]["retries"], 5)
        self.assertEqual(status["last_download_retries"]["products_failed"], 1)

    def test_hourly_outage_writes_a_failure_record_with_the_retries(self):
        now = datetime(2026, 10, 20, 12, 5, tzinfo=timezone.utc)
        fake = FlakyEumetsat(always={i: ProductError(503) for i in range(36)})
        with mock.patch.object(collect, "download", fake):
            with self.assertRaises(collect.DownloadOutage):
                collect.main(["--hours", "6", "--out", str(self.base / "feed.geojson"),
                              "--status", str(self.base / "status.json"), "--history-dir", str(self.base / "h"),
                              "--run-record", str(self.record)], now=now)
        rec = self.load()
        self.assertEqual(rec["outcome"], "failure")
        self.assertEqual(rec["download_retries"]["products_failed"], 5)
        self.assertEqual(rec["products_unparsed"], 5)
        self.assertFalse((self.base / "feed.geojson").exists())
        status = status_mod.build_status(now=now, previous={}, record=rec, archive_result=None,
                                         collect_outcome="failure", archive_outcome="skipped")
        self.assertEqual(status["last_download_retries"]["products_failed"], 5)


if __name__ == "__main__":
    unittest.main()

"""A stalled EUMETSAT read must never hang a shard (and so the whole backfill).

Incident (parallel backfill run 40564980740, 27-28 Oct 2026): shards 1 and 2
finished in 178 and 208 min, shard 3 was still "in_progress" after 5 h and
nothing was committed, because finalize waits for every shard. eumdac passes
no ``timeout`` to requests, so one stalled TCP read blocks for ever, and the
shard budget (290 min) was only checked between dates, inside a 340-minute
job timeout.

Now:
- every HTTP call gets an explicit (connect, read) timeout, ``collect.HTTP_TIMEOUT``
  (15, 60); a read timeout is transient, retried, and once the retries are used
  up the product's slot is UNPARSED (unknown, never zero);
- the per-day unparsed threshold still stops a date with too many such slots;
- a shard starts no new date after ``SHARD_BUDGET_MINUTES`` (170) and kills a
  date still running at the hard stop (190) or after 45 min, keeps what it
  collected and uploads it inside the 200-minute job timeout;
- finalize runs ``always()`` and ingests whatever shard artifacts exist.

The hanging reads below are REAL sockets to a local server that accepts and
never answers; only the timeouts are shortened. No EUMETSAT, no network.
"""
import json
import socket
import sys
import tempfile
import threading
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import backfill_parallel as bp
import collect
from test_unreadable_product import DAY0, fake_body, good_zip, slot_name

WORKFLOW = ROOT / ".github" / "workflows" / "backfill-parallel-lightning.yml"
SHORT = (0.5, 0.2)  # (connect, read) seconds for the tests


class SilentServer:
    """Accepts TCP connections and never sends a byte: a stalled read."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns, self.stop = [], threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.1)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
                self.conns.append(conn)  # hold it open, never answer
            except OSError:
                continue

    def close(self):
        self.stop.set()
        self.thread.join(2)
        for conn in self.conns:
            conn.close()
        self.sock.close()


class HangingProduct:
    """Fake eumdac product whose ``open().read()`` is a real stalled socket read.

    The stream reads with the read timeout ``collect.HTTP_TIMEOUT`` gives it, as
    urllib3 does once requests passes the timeout down.
    """

    def __init__(self, name, port, *, hang=True, blob=None):
        self.name, self.port, self.hang, self.blob, self.opens = name, port, hang, blob, 0

    def __str__(self):
        return self.name

    def open(self):
        product = self

        class Stream:
            def __enter__(self):
                product.opens += 1
                connect, read = collect.HTTP_TIMEOUT
                self.sock = socket.create_connection(("127.0.0.1", product.port), timeout=connect)
                self.sock.settimeout(read)
                return self

            def read(self):
                if not product.hang:
                    return product.blob
                return self.sock.recv(1)  # stalls: raises TimeoutError after the read timeout

            def __exit__(self, *exc):
                self.sock.close()
                return False
        return Stream()


class RequestTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.server = SilentServer()
        self.addCleanup(self.server.close)

    def test_hanging_read_times_out_is_retried_then_unparsed(self):
        start = DAY0
        names = [slot_name(start + timedelta(minutes=10 * i)) for i in range(4)]
        products = [HangingProduct(names[0], self.server.port, hang=False, blob=good_zip(names[0], [])),
                    HangingProduct(names[1], self.server.port, hang=True),
                    HangingProduct(names[2], self.server.port, hang=False, blob=good_zip(names[2], [])),
                    HangingProduct(names[3], self.server.port, hang=False, blob=good_zip(names[3], []))]
        slept = []
        with mock.patch.object(collect, "HTTP_TIMEOUT", SHORT):
            found, count, readable, unparsed, stats = collect.download_products(
                products, start, start + timedelta(minutes=40), body_decoder=fake_body,
                sleep=slept.append, rng=lambda: 0.0)
        self.assertEqual(count, 3)
        self.assertEqual(products[1].opens, 1 + collect.TRANSIENT_RETRIES)  # retried, then given up
        self.assertEqual([u["product"] for u in unparsed], [names[1]])
        self.assertEqual(unparsed[0]["kind"], "download")
        self.assertIn("TimeoutError", unparsed[0]["reason"])
        self.assertEqual(stats["products_failed"], 1)
        self.assertEqual(stats["retries"], collect.TRANSIENT_RETRIES)
        self.assertEqual(slept, [2.0, 4.0, 8.0, 16.0])
        self.assertNotIn(names[1], readable)  # unknown, never zero

    def test_read_timeouts_are_transient_by_name(self):
        for cls_name in ("ReadTimeout", "ReadTimeoutError", "ConnectTimeout", "TimeoutError"):
            error = type(cls_name, (Exception,), {})("stalled")
            self.assertEqual(collect.classify_download_error(error)[0], "transient", cls_name)
        self.assertEqual(collect.classify_download_error(socket.timeout("timed out"))[0], "transient")

    def test_too_many_timed_out_slots_still_trip_the_per_day_threshold(self):
        start = DAY0
        names = [slot_name(start + timedelta(minutes=10 * i)) for i in range(144)]
        unparsed = [collect.unparsed_entry(n, TimeoutError("timed out"), kind="download") for n in names[:30]]
        with self.assertRaises(collect.TooManyUnparsed):
            collect.check_unparsed(unparsed, names[30:], start, start + timedelta(days=1))
        collect.check_unparsed(unparsed[:28], names[28:], start, start + timedelta(days=1))  # 28/144 < 20 %

    def _fake_requests(self):
        seen = []

        class Session:
            def request(self, method, url, params=None, data=None, headers=None, cookies=None, files=None,
                        auth=None, timeout=None, **rest):
                seen.append(timeout)
                return timeout

            def get(self, url, **kwargs):  # what eumdac.request.get ends up calling
                return self.request("GET", url, **kwargs)
        return types.SimpleNamespace(Session=Session), seen

    def test_every_session_call_gets_the_explicit_timeout(self):
        fake, seen = self._fake_requests()
        old_default = socket.getdefaulttimeout()
        self.addCleanup(socket.setdefaulttimeout, old_default)
        self.assertTrue(collect.install_http_timeouts(requests_module=fake))
        session = fake.Session()
        session.get("https://example.invalid/token")                    # eumdac: no timeout at all
        session.request("GET", "https://example.invalid/x", timeout=None)
        session.get("https://example.invalid/y", timeout=5)             # explicit one is kept
        session.request("GET", "u", None, None, None, None, None, None, None)  # positional None
        self.assertEqual(seen, [(15, 60), (15, 60), 5, (15, 60)])
        self.assertEqual(socket.getdefaulttimeout(), 60.0)
        collect.install_http_timeouts(requests_module=fake)  # idempotent: never double-wrapped
        session.get("z")
        self.assertEqual(seen[-1], (15, 60))
        self.assertEqual(len(seen), 5)

    def test_real_requests_read_times_out_on_a_stalled_server(self):
        try:
            import requests
        except ImportError:
            self.skipTest("requests not installed")
        original = requests.Session.request
        old_default = socket.getdefaulttimeout()
        self.addCleanup(setattr, requests.Session, "request", original)
        self.addCleanup(socket.setdefaulttimeout, old_default)
        collect.install_http_timeouts(SHORT)
        with self.assertRaises(requests.exceptions.ReadTimeout) as caught:
            requests.Session().get(f"http://127.0.0.1:{self.server.port}/product", stream=True)
        self.assertEqual(collect.classify_download_error(caught.exception)[0], "transient")

    def test_search_products_installs_the_timeout_before_eumdac(self):
        calls = []
        with mock.patch.dict("os.environ", {"EUMETSAT_CONSUMER_KEY": "k", "EUMETSAT_CONSUMER_SECRET": "s"}), \
                mock.patch.object(collect, "install_http_timeouts", side_effect=lambda: calls.append("t")), \
                mock.patch.dict(sys.modules, {"eumdac": None}):
            with self.assertRaises(ImportError):
                collect.search_products(DAY0, DAY0 + timedelta(days=1))
        self.assertEqual(calls, ["t"])


class ShardWallClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "records"
        self.dates = ["2026-10-20", "2026-10-17", "2026-10-14"]

    def good(self, day, record, log, timeout=None):
        from test_backfill_parallel import FakeEumetsat
        FakeEumetsat().collector()(day, record, log)

    def test_defaults_match_the_workflow(self):
        self.assertEqual(bp.DEFAULT_SHARD_BUDGET_MINUTES, 170)
        self.assertEqual(bp.DEFAULT_SHARD_BUDGET_MINUTES + bp.HARD_STOP_GRACE_MINUTES, 190)
        text = WORKFLOW.read_text()
        self.assertIn('SHARD_BUDGET_MINUTES: "170"', text)
        self.assertIn('SHARD_HARD_STOP_MINUTES: "190"', text)

    def test_hanging_collect_subprocess_is_killed_and_earlier_records_kept(self):
        """A real child process that hangs (like a stalled read) is killed at the date limit."""
        hang = [sys.executable, "-c", "import time; time.sleep(120)"]

        def collector(day, record, log, timeout=None):
            if day == self.dates[1]:
                with mock.patch.object(bp, "collect_command", lambda d, r: hang):
                    return bp.collect_subprocess(day, record, log, timeout=timeout)
            return self.good(day, record, log)
        summary = bp.run_shard(3, self.dates, self.out, budget_minutes=170,
                               date_timeout_minutes=1.5 / 60, collector=collector)
        self.assertEqual(summary["stopped"], "time_budget")
        self.assertIn(self.dates[1], summary["error"])
        self.assertIn("unknown", summary["error"])
        self.assertEqual([c["date"] for c in summary["collected"]], [self.dates[0]])
        self.assertFalse((self.out / f"rb-{self.dates[1]}.json").exists())
        self.assertFalse(list(self.out.glob(".partial-*")))
        on_disk = json.loads((self.out / "shard-3.json").read_text())
        self.assertEqual(on_disk["stopped"], "time_budget")

    def test_date_limit_never_runs_past_the_hard_stop(self):
        clock, limits = [0.0], []

        def collector(day, record, log, timeout=None):
            limits.append(timeout)
            self.good(day, record, log)
            clock[0] += 80 * 60  # each date "takes" 80 minutes
        summary = bp.run_shard(1, self.dates, self.out, budget_minutes=170, collector=collector,
                               clock=lambda: clock[0])
        # starts at 0 and 80 (< 170), the third at 160 gets only 190 - 160 = 30 min
        self.assertEqual(limits, [45 * 60, 45 * 60, 30 * 60])
        self.assertEqual(summary["hard_stop_minutes"], 190)
        self.assertEqual(summary["stopped"], "completed")

    def test_no_new_date_after_the_budget(self):
        clock, calls = [0.0], []

        def collector(day, record, log, timeout=None):
            calls.append(day)
            self.good(day, record, log)
            clock[0] += 90 * 60
        summary = bp.run_shard(1, self.dates, self.out, budget_minutes=170, collector=collector,
                               clock=lambda: clock[0])
        self.assertEqual(summary["stopped"], "time_budget")
        self.assertEqual(calls, self.dates[:2])  # 0 and 90 min start; 180 >= 170 does not

    def test_cli_default_budget_is_170(self):
        seen = {}

        def fake_run_shard(shard, dates, out_dir, **kw):
            seen.update(kw)
            return {"stopped": "completed", "collected": [], "dates": dates, "error": None}
        with mock.patch.object(bp, "run_shard", fake_run_shard), mock.patch("signal.signal"), \
                mock.patch.dict("os.environ", {}, clear=False):
            bp.main(["shard", "--shard", "1", "--dates", "2026-10-20", "--out-dir", str(self.out)])
        self.assertEqual(seen["budget_minutes"], 170)
        self.assertIsNone(seen["hard_stop_minutes"])
        self.assertEqual(seen["date_timeout_minutes"], 45)


class WorkflowTimeoutTests(unittest.TestCase):
    def setUp(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        self.jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]

    def test_shard_job_and_step_timeouts_bound_the_shard(self):
        shard = self.jobs["shard"]
        self.assertEqual(shard["timeout-minutes"], 200)
        budget = int(shard["env"]["SHARD_BUDGET_MINUTES"])
        hard = int(shard["env"]["SHARD_HARD_STOP_MINUTES"])
        search = next(s for s in shard["steps"] if s.get("id") == "search")
        self.assertLess(budget, hard)
        self.assertLess(hard, search["timeout-minutes"])
        self.assertLess(search["timeout-minutes"], shard["timeout-minutes"])
        self.assertIn("--hard-stop-minutes", search["run"])
        upload = next(s for s in shard["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact"))
        self.assertEqual(upload["if"], "${{ always() }}")

    def test_finalize_runs_always_and_tolerates_missing_artifacts(self):
        fin = self.jobs["finalize"]
        self.assertTrue(fin["if"].startswith("${{ always()"))
        records = next(s for s in fin["steps"] if s.get("with", {}).get("pattern") == "backfill-shard-*")
        self.assertTrue(records["continue-on-error"])
        self.assertTrue(records["with"]["merge-multiple"])


if __name__ == "__main__":
    unittest.main()

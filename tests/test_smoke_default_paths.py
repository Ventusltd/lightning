"""Smoke test: the REAL entry points on their DEFAULT code paths, faked only at the network.

Every other test injects its collaborators (``sleep=``, ``rng=``, ``downloader=``,
``body_decoder=``, stub modules). That is how a ``NameError`` in a default
argument path (``rng or random.random`` without ``import random``, PR #15,
backfill run 40699133665) passed CI and reached the production backfill.

Here nothing is injected. ``collect.py``, ``history.py``, ``grid.py`` and
``status.py`` run as subprocesses, exactly as the workflows run them. The only
fake is a stand-in ``eumdac`` package placed first on ``PYTHONPATH`` (the
EUMETSAT Data Store is the network boundary): it lists product ZIPs from a
local folder and raises ``ReadTimeout`` on the first search and the first
product open, so the default retry, backoff (``random``) and sleep paths
really run (about 2-3 s each).

``requests``, ``netCDF4`` and ``numpy`` are the real packages when installed.
With ``LIGHTNING_SMOKE_REQUIRE_DEPS=1`` (the CI "with requirements" leg)
missing packages fail instead of skip, so the full pipeline can never be
skipped silently there. Without the requirements (the "bare" leg) the
search, retry and download defaults still run up to the NetCDF decode.
"""
import importlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRE_DEPS = os.environ.get("LIGHTNING_SMOKE_REQUIRE_DEPS") == "1"
DEPS = ("requests", "netCDF4", "numpy")
MISSING = [name for name in DEPS if importlib.util.find_spec(name) is None]

# The network boundary only. Mirrors the eumdac surface collect.py uses:
# AccessToken, DataStore.get_collection(id).search(dtstart=, dtend=) with
# total_results, str(product), product.open() as a stream, product.datastore.token.
FAKE_EUMDAC = r'''
"""FAKE eumdac (tests only): serves product ZIPs from $FAKE_EUMDAC_PRODUCTS."""
import os
import re
from datetime import datetime
from pathlib import Path

_SLOT = re.compile(r"_OPE_(\d{14})_(\d{14})_")
_FAILS = {"search": int(os.environ.get("FAKE_EUMDAC_SEARCH_TIMEOUTS", "0")),
          "open": int(os.environ.get("FAKE_EUMDAC_OPEN_TIMEOUTS", "0"))}
CALLS = []


class ReadTimeout(Exception):  # classified as transient by class name, like requests'
    pass


def _maybe_fail(kind):
    CALLS.append(kind)
    if _FAILS[kind] > 0:
        _FAILS[kind] -= 1
        raise ReadTimeout(f"HTTPSConnectionPool(host='api.eumetsat.int', port=443): Read timed out. (fake {kind})")


class AccessToken:
    def __init__(self, credentials):
        key, secret = credentials
        assert key and secret
        self.credentials = credentials

    def _update_token_data(self):
        pass


class _Stream:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.path.read_bytes()


class _Product:
    def __init__(self, path, datastore):
        self.path, self.datastore = path, datastore

    def __str__(self):
        return self.path.name

    def open(self):
        _maybe_fail("open")
        return _Stream(self.path)


class _Results:
    def __init__(self, products):
        self.products = products
        self.total_results = len(products)

    def __iter__(self):
        return iter(self.products)


class _Collection:
    def __init__(self, datastore, collection_id):
        self.datastore, self.collection_id = datastore, collection_id

    def search(self, dtstart, dtend):
        _maybe_fail("search")
        assert dtstart.tzinfo is None and dtend.tzinfo is None  # collect.py passes naive UTC
        found = []
        for path in sorted(Path(os.environ["FAKE_EUMDAC_PRODUCTS"]).iterdir()):
            m = _SLOT.search(path.name)
            start, end = (datetime.strptime(x, "%Y%m%d%H%M%S") for x in m.groups())
            if start < dtend and end > dtstart:
                found.append(_Product(path, self.datastore))
        return _Results(found)


class DataStore:
    def __init__(self, token):
        self.token = token

    def get_collection(self, collection_id):
        assert collection_id == "EO:EUM:DAT:0691"
        return _Collection(self, collection_id)
'''


def product_name(start: datetime) -> str:
    stop = start + timedelta(minutes=10)
    return ("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_"
            f"{stop:%Y%m%d%H%M%S}_L2PF_OPE_{start:%Y%m%d%H%M%S}_{stop:%Y%m%d%H%M%S}_N__O_0001_0000")


def slot_floor(value: datetime) -> datetime:
    return value.replace(minute=value.minute - value.minute % 10, second=0, microsecond=0)


def body_netcdf(flashes) -> bytes:
    """A real LFL-like BODY NetCDF (longitude, latitude, flash_time) written with netCDF4."""
    from netCDF4 import Dataset
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "BODY.nc"
        with Dataset(path, "w") as ds:
            ds.createDimension("flashes", len(flashes))
            lon = ds.createVariable("longitude", "f4", ("flashes",))
            lat = ds.createVariable("latitude", "f4", ("flashes",))
            t = ds.createVariable("flash_time", "f8", ("flashes",))
            t.units = "seconds since 2000-01-01 00:00:00"
            t.calendar = "standard"
            epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
            lon[:] = [f[0] for f in flashes]
            lat[:] = [f[1] for f in flashes]
            t[:] = [(f[2] - epoch).total_seconds() for f in flashes]
        return path.read_bytes()


def product_zip(name: str, body: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(f"{name}/{name}_BODY.nc", body)
        archive.writestr(f"{name}/EOPMetadata.xml", "<fake/>")
    return buf.getvalue()


class SmokeBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        fake = self.base / "fakes" / "eumdac"
        fake.mkdir(parents=True)
        (fake / "__init__.py").write_text(FAKE_EUMDAC)
        self.products = self.base / "products"
        self.products.mkdir()
        self.data = self.base / "data"
        self.data.mkdir()
        self.env = {**os.environ, "PYTHONPATH": str(self.base / "fakes"), "PYTHONDONTWRITEBYTECODE": "1",
                    "EUMETSAT_CONSUMER_KEY": "fake-key", "EUMETSAT_CONSUMER_SECRET": "fake-secret",
                    "FAKE_EUMDAC_PRODUCTS": str(self.products)}
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def run_py(self, *args, extra_env=None, check=True):
        done = subprocess.run([sys.executable, *map(str, args)], cwd=self.base, capture_output=True, text=True,
                              env={**self.env, **(extra_env or {})}, timeout=180)
        if check:
            self.assertEqual(done.returncode, 0, f"{args}\nSTDOUT:\n{done.stdout}\nSTDERR:\n{done.stderr}")
        return done

    def assert_no_crash_of_our_own(self, done):
        for marker in ("NameError", "UnboundLocalError", "AttributeError: module", "ImportError: cannot import"):
            self.assertNotIn(marker, done.stderr, done.stderr)


class ImportSmokeTests(unittest.TestCase):
    def test_every_module_imports_in_a_fresh_interpreter(self):
        for name in ("collect", "history", "grid", "status", "backfill_parallel"):
            done = subprocess.run([sys.executable, "-c", f"import {name}"], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"import {name}: {done.stderr}")

    def test_every_cli_answers_help(self):
        for script in ("collect.py", "history.py", "grid.py", "status.py", "backfill_parallel.py"):
            done = subprocess.run([sys.executable, str(ROOT / script), "--help"], cwd=ROOT,
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"{script} --help: {done.stderr}")


class DefaultSearchAndDownloadSmokeTests(SmokeBase):
    """Runs with or without the requirements: the decode is never reached."""

    def test_rebuild_day_runs_default_search_retry_and_download_paths(self):
        day = (self.now - timedelta(days=3)).date()
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
        # Not a ZIP: decode fails with ValueError (no netCDF4 needed), every slot is
        # unparsed and collect.py stops with TooManyUnparsed AFTER the defaults ran.
        (self.products / product_name(start)).write_bytes(b"not a zip")
        record = self.base / "rb.json"
        done = self.run_py(ROOT / "collect.py", "--rebuild-day", day.isoformat(), "--run-record", record,
                           extra_env={"FAKE_EUMDAC_SEARCH_TIMEOUTS": "1", "FAKE_EUMDAC_OPEN_TIMEOUTS": "1"},
                           check=False)
        self.assert_no_crash_of_our_own(done)
        self.assertIn("RETRY search after attempt 1", done.stderr)
        self.assertIn("RETRY product", done.stderr)
        self.assertNotEqual(done.returncode, 0)
        failure = json.loads(record.read_text())
        self.assertEqual(failure["outcome"], "failure")
        self.assertTrue(failure["error"].startswith("TooManyUnparsed"), failure["error"])
        self.assertEqual(failure["download_retries"]["retries"], 1)


class FullPipelineSmokeTests(SmokeBase):
    """collect -> history ingest -> verify -> grid verify --deep -> status, and a rebuild-day backfill."""

    def setUp(self):
        if MISSING:
            if REQUIRE_DEPS:
                self.fail(f"LIGHTNING_SMOKE_REQUIRE_DEPS=1 but {MISSING} not installed (pip install -r requirements.txt)")
            self.skipTest(f"requirements not installed: {MISSING}")
        super().setUp()

    def add_product(self, start: datetime, flashes):
        name = product_name(start)
        (self.products / name).write_bytes(product_zip(name, body_netcdf(flashes)))
        return name

    def test_hourly_and_backfill_paths_end_to_end(self):
        # Hourly: three readable 10-minute products inside a 6 h window.
        slot = slot_floor(self.now - timedelta(hours=2))
        for i in range(3):
            s = slot + timedelta(minutes=10 * i)
            self.add_product(s, [(-2.0 + i * 0.1, 52.0, s + timedelta(minutes=3)),
                                 (20.0, 10.0, s + timedelta(minutes=4))])  # outside the UK view: dropped
        feed, status, history_dir = self.data / "uk_lightning.geojson", self.data / "status.json", self.data / "history"
        run = self.base / "run.json"
        done = self.run_py(ROOT / "collect.py", "--hours", "6", "--out", feed, "--status", status,
                           "--history-dir", history_dir, "--run-record", run,
                           extra_env={"FAKE_EUMDAC_SEARCH_TIMEOUTS": "1", "FAKE_EUMDAC_OPEN_TIMEOUTS": "1"})
        self.assert_no_crash_of_our_own(done)
        self.assertIn("RETRY search after attempt 1", done.stderr)
        self.assertEqual(len(json.loads(feed.read_text())["features"]), 3)
        record = json.loads(run.read_text())
        self.assertEqual((record["outcome"], record["download_retries"]["retries"]), ("success", 1))

        result = self.base / "archive-result.json"
        self.run_py(ROOT / "history.py", "ingest", "--archive-dir", history_dir, "--run-record", run,
                    "--seed-scan-windows", self.base / "no-seed.json", "--result-out", result)
        self.run_py(ROOT / "history.py", "verify", "--archive-dir", history_dir)
        self.run_py(ROOT / "grid.py", "verify", "--deep", "--archive-dir", history_dir)
        self.run_py(ROOT / "status.py", "write", "--status", status, "--run-record", run, "--archive-result", result,
                    "--collect-outcome", "success", "--archive-outcome", "success", "--trigger", "schedule")
        self.assertEqual(json.loads(status.read_text())["last_attempt_outcome"], "success")

        # Backfill: a whole-day rebuild of a never-searched date, then its ingest.
        day = (self.now - timedelta(days=3)).date()
        noon = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
        for i in range(3):
            s = noon + timedelta(minutes=10 * i)
            self.add_product(s, [(-1.0, 53.0, s + timedelta(minutes=5))])
        rb = self.base / "rb.json"
        done = self.run_py(ROOT / "collect.py", "--rebuild-day", day.isoformat(), "--run-record", rb)
        self.assert_no_crash_of_our_own(done)
        self.run_py(ROOT / "history.py", "backfill", "--archive-dir", history_dir, "--day", day.isoformat(),
                    "--rebuild-record", rb)
        self.run_py(ROOT / "history.py", "verify", "--archive-dir", history_dir)
        self.run_py(ROOT / "grid.py", "verify", "--deep", "--archive-dir", history_dir)
        rows = {r["date"]: r for r in json.loads((history_dir / "index.json").read_text())["days"]}
        self.assertEqual(rows[day.isoformat()]["count"], 3)

        # The same checks the data workflows run before every push.
        self.run_py(ROOT / "ci" / "verify_data.py", "--data-dir", self.data, "--deep")


if __name__ == "__main__":
    unittest.main()

"""Parallel backfill: shard planning, shard searches and the single finalize ingest.

No network, no EUMETSAT, no GitHub. Shards search with a FAKE downloader
(10-minute slot names, synthetic "TEST-" flashes only inside temporary
directories, or a fake 429 rate limit); the real history.py and status.py run
unchanged. The shell tests run the real step scripts of
backfill-parallel-lightning.yml in a throwaway repository with a bare origin.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backfill_parallel as bp
import collect
import history
from collect import build_feature
from test_backfill_workflow import FAKE_DOWNLOAD, outputs, step, substitute

WORKFLOW = ROOT / ".github" / "workflows" / "backfill-parallel-lightning.yml"
HOURLY = ROOT / ".github" / "workflows" / "update-lightning.yml"
NOW = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)


def slot_name(start):
    end = start + timedelta(minutes=10)
    return ("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_"
            f"{end:%Y%m%d%H%M%S}_L2PF_OPE_{start:%Y%m%d%H%M%S}_{end:%Y%m%d%H%M%S}_N__O_0001_0000")


class FakeEumetsat:
    """Per-date fake downloader: one flash per date, optional skipped slots, rate limits."""

    def __init__(self, fail_days=(), skip=None, quiet_days=()):
        self.fail, self.skip, self.quiet, self.calls = set(fail_days), skip or {}, set(quiet_days), []

    def __call__(self, start, end):
        day = start.date().isoformat()
        self.calls.append(day)
        if day in self.fail:
            raise RuntimeError("HTTP 429 Too Many Requests (fake EUMETSAT rate limit)")
        names, cursor, i = [], start, 0
        while cursor < end:
            if i not in self.skip.get(day, ()):
                names.append(slot_name(cursor))
            cursor += timedelta(minutes=10)
            i += 1
        flashes = [] if day in self.quiet else [build_feature(-2.0, 52.0, start + timedelta(hours=1, minutes=3), "TEST-" + day, 0)]
        return flashes, len(names), names

    def collector(self):
        return lambda day, record, log, timeout=None: collect.rebuild_day_record(day, record, now=NOW, downloader=self)


def make_archive(root: Path, now: datetime, searched: date) -> None:
    """An index whose only searched date is ``searched`` (hours 01-07, one flash)."""
    start = datetime.combine(searched, time.min, tzinfo=timezone.utc)
    history.archive_observations(
        root, [build_feature(-2.0, 52.0, start + timedelta(hours=2), "TEST-hourly", 0)], now=now,
        scanned_windows=[history.successful_scan((start + timedelta(hours=1)).isoformat(),
                                                 (start + timedelta(hours=7)).isoformat(), 36)],
        lookback_hours=6.0)


def index_rows(root: Path) -> dict:
    return {r["date"]: r for r in json.loads((root / "index.json").read_text())["days"]}


class SplitTests(unittest.TestCase):
    def test_round_robin_newest_first_and_empty_shards_dropped(self):
        dates = [f"2026-10-{d:02d}" for d in range(20, 13, -1)]  # 7 dates, newest first
        self.assertEqual(bp.split_shards(dates, 3), [
            {"shard": 1, "dates": "2026-10-20 2026-10-17 2026-10-14"},
            {"shard": 2, "dates": "2026-10-19 2026-10-16"},
            {"shard": 3, "dates": "2026-10-18 2026-10-15"}])
        self.assertEqual(len(bp.split_shards(dates[:2], 6)), 2)
        self.assertEqual(bp.split_shards([], 6), [])

    def test_parallelism_is_capped_at_six_for_eumetsat_fair_use(self):
        self.assertEqual(bp.MAX_SHARDS, 6)
        for bad in (0, 7, 12):
            with self.assertRaises(ValueError):
                bp.split_shards(["2026-10-20"], bad)
        text = WORKFLOW.read_text()
        self.assertIn("max-parallel: 6", text)

    def test_plan_accepts_a_whole_year_but_not_more(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "history"
            make_archive(root, NOW, date(2026, 10, 26))
            dates, matrix = bp.plan(root, end_date="", days=366, shards=6, now=NOW)
            self.assertEqual(dates[0], "2026-10-25")  # yesterday (26th) was searched by the hourly job
            self.assertNotIn("2026-10-26", dates)
            self.assertEqual(len(matrix), 6)
            self.assertEqual(sorted(sum((m["dates"].split() for m in matrix), [])), sorted(dates))
            with self.assertRaises(ValueError):
                bp.plan(root, end_date="", days=367, shards=6, now=NOW)
            with self.assertRaises(ValueError):
                bp.plan(root, end_date="2026-10-27", days=1, shards=6, now=NOW)


    def test_an_empty_plan_still_emits_a_valid_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "history", Path(tmp) / "github_output"
            make_archive(root, NOW, date(2026, 10, 26))
            old = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(out)
            try:
                bp.main(["plan", "--archive-dir", str(root), "--end-date", "2026-10-26", "--days", "1",
                         "--plan-out", str(Path(tmp) / "plan.txt"), "--now", NOW.isoformat()])
            finally:
                if old is None:
                    os.environ.pop("GITHUB_OUTPUT")
                else:
                    os.environ["GITHUB_OUTPUT"] = old
            got = outputs(out)
            self.assertEqual((got["count"], got["shards"]), ("0", "0"))
            self.assertEqual(json.loads(got["matrix"])["include"], [{"shard": 0, "dates": ""}])


class ShardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / "records"
        self.dates = ["2026-10-20", "2026-10-17", "2026-10-14"]

    def tearDown(self):
        self.temp.cleanup()

    def test_shard_writes_only_records_and_a_summary(self):
        fake = FakeEumetsat()
        summary = bp.run_shard(1, self.dates, self.out, budget_minutes=290, collector=fake.collector())
        self.assertEqual(summary["stopped"], "completed")
        self.assertEqual([c["date"] for c in summary["collected"]], self.dates)
        self.assertEqual(sorted(p.name for p in self.out.iterdir() if p.suffix == ".json"),
                         ["rb-2026-10-14.json", "rb-2026-10-17.json", "rb-2026-10-20.json", "shard-1.json"])
        on_disk = json.loads((self.out / "shard-1.json").read_text())
        self.assertEqual(on_disk, summary)
        for item in summary["collected"]:
            self.assertEqual(item["sha256"], bp.sha256_file(self.out / item["record"]))

    def test_rate_limit_stops_the_shard_and_never_attempts_later_dates(self):
        fake = FakeEumetsat(fail_days={"2026-10-17"})
        summary = bp.run_shard(2, self.dates, self.out, budget_minutes=290, collector=fake.collector())
        self.assertEqual(summary["stopped"], "eumetsat_error")
        self.assertIn("429", summary["error"])
        self.assertEqual(fake.calls, ["2026-10-20", "2026-10-17"])
        self.assertEqual([c["date"] for c in summary["collected"]], ["2026-10-20"])
        self.assertFalse((self.out / "rb-2026-10-17.json").exists())
        self.assertFalse(list(self.out.glob(".partial-*")))

    def test_time_budget_stops_before_starting_a_date(self):
        clock = [0.0]
        fake = FakeEumetsat()

        def slow(day, record, log, timeout=None):  # each whole-day search "takes" 100 minutes
            fake.collector()(day, record, log)
            clock[0] += 100 * 60
        summary = bp.run_shard(1, self.dates, self.out, budget_minutes=150, collector=slow, clock=lambda: clock[0])
        self.assertEqual(summary["stopped"], "time_budget")
        self.assertEqual(fake.calls, ["2026-10-20", "2026-10-17"])
        self.assertFalse((self.out / "rb-2026-10-14.json").exists())

    def test_cancellation_mid_search_keeps_finished_records_and_drops_the_partial_one(self):
        fake = FakeEumetsat()

        def collector(day, record, log, timeout=None):
            if day == "2026-10-17":
                record.write_text('{"schema": "uk-lightning-run-rec')  # killed mid-write
                raise bp.Cancelled("signal 15")
            fake.collector()(day, record, log)
        summary = bp.run_shard(3, self.dates, self.out, budget_minutes=290, collector=collector)
        self.assertEqual(summary["stopped"], "cancelled")
        self.assertEqual([c["date"] for c in summary["collected"]], ["2026-10-20"])
        self.assertFalse(list(self.out.glob(".partial-*")))
        self.assertEqual(json.loads((self.out / "shard-3.json").read_text())["stopped"], "cancelled")

    def test_stagger_pauses_before_the_first_search(self):
        slept = []
        bp.run_shard(4, self.dates[:1], self.out, budget_minutes=290, stagger_seconds=60,
                     collector=FakeEumetsat().collector(), sleep=slept.append)
        self.assertEqual(slept, [60])


class FinalizeTests(unittest.TestCase):
    """Records from several shards, ingested sequentially into one archive."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root, self.records, self.status = base / "history", base / "records", base / "status.json"
        make_archive(self.root, NOW - timedelta(hours=1), date(2026, 10, 26))
        self.heartbeat = {"schema": "uk-lightning-status.v1", "written_utc": "2026-10-27T05:18:00.000Z",
                          "health": "ok", "freshness": "ok", "last_attempt_outcome": "success",
                          "consecutive_failures": 0, "last_successful_scan_end_utc": "2026-10-27T05:10:09.000Z"}
        self.status.write_text(json.dumps(self.heartbeat))
        self.plan = [f"2026-10-{d:02d}" for d in range(25, 17, -1)]  # 8 never-searched dates

    def tearDown(self):
        self.temp.cleanup()

    def shards(self, n=3, **fake):
        fake = FakeEumetsat(**fake)
        summaries = [bp.run_shard(m["shard"], m["dates"].split(), self.records, budget_minutes=290,
                                  collector=fake.collector()) for m in bp.split_shards(self.plan, n)]
        return summaries

    def finalize(self, n=3):
        return bp.finalize(plan_dates=self.plan, shards=n, records_dir=self.records, archive_dir=self.root,
                           status_path=self.status, now=NOW)

    def test_all_shards_ingested_in_one_pass_with_real_coverage(self):
        self.shards(skip={"2026-10-22": {20, 21}}, quiet_days={"2026-10-19"})  # 03:20-03:40 not examined
        result = self.finalize()
        self.assertEqual(result["stopped"], "completed")
        self.assertEqual(result["filled"], self.plan)
        rows = index_rows(self.root)
        for d in self.plan:
            self.assertEqual(rows[d]["count"], 0 if d == "2026-10-19" else 1, d)
        self.assertEqual((rows["2026-10-19"]["coverage"], rows["2026-10-19"]["scanned_seconds"]), ("full", 86400))
        # two missing slots stay unknown: partial coverage, never a full-day claim
        self.assertEqual(rows["2026-10-22"]["coverage"], "partial")
        self.assertEqual(rows["2026-10-22"]["scanned_seconds"], 86400 - 1200)
        self.assertEqual(history.verify_archive(self.root)["integrity"], "ok")
        status = json.loads(self.status.read_text())
        for key, value in self.heartbeat.items():
            self.assertEqual(status[key], value, key)
        self.assertEqual(status["backfill"]["mode"], "parallel")
        self.assertEqual(status["backfill"]["remaining_unknown_days"], [])

    def test_a_rate_limited_shard_leaves_its_dates_unknown_not_zero(self):
        self.shards(fail_days={"2026-10-24"})  # shard 2: 24, 21, 18 -> stops at once
        result = self.finalize()
        self.assertEqual(result["stopped"], "eumetsat_error")
        self.assertIn("429", result["error"])
        rows = index_rows(self.root)
        for d in ("2026-10-24", "2026-10-21", "2026-10-18"):
            self.assertEqual((rows[d]["coverage"], rows[d]["count"]), ("none", None), d)
        for d in ("2026-10-25", "2026-10-23", "2026-10-22", "2026-10-20", "2026-10-19"):
            self.assertEqual(rows[d]["count"], 1, d)
        status = json.loads(self.status.read_text())
        self.assertEqual(status["backfill"]["remaining_unknown_days"], ["2026-10-24", "2026-10-21", "2026-10-18"])
        self.assertEqual(status["backfill"]["outcome"], "stopped")

    def test_missing_shard_artifact_is_red_and_its_dates_stay_unknown(self):
        self.shards()
        (self.records / "shard-3.json").unlink()
        result = self.finalize()
        self.assertEqual(result["stopped"], "shard_missing")
        rows = index_rows(self.root)
        for d in ("2026-10-23", "2026-10-20"):
            self.assertIsNone(rows[d]["count"], d)

    def test_a_record_changed_after_its_shard_summary_is_refused(self):
        self.shards()
        record = self.records / "rb-2026-10-22.json"
        data = json.loads(record.read_text())
        data["features"] = []  # a flash dropped in transit would otherwise become a false zero
        record.write_text(json.dumps(data))
        result = self.finalize()
        self.assertEqual(result["stopped"], "archive_refused")
        self.assertIn("sha256", result["error"])
        rows = index_rows(self.root)
        self.assertEqual(rows["2026-10-23"]["count"], 1)  # newer dates before the refusal are kept
        self.assertIsNone(rows["2026-10-22"]["count"])
        # only the refused date is skipped; the ingest continues with the older dates
        self.assertEqual(rows["2026-10-18"]["count"], 1)
        self.assertEqual(result["filled"], [d for d in self.plan if d != "2026-10-22"])
        status = json.loads(self.status.read_text())["backfill"]
        self.assertEqual(status["remaining_unknown_days"], ["2026-10-22"])
        self.assertIn("sha256", status["refused_days"]["2026-10-22"])

    def test_reingest_on_a_newer_main_is_idempotent(self):
        self.shards()
        first = self.finalize()
        snapshot = (self.root / "index.json").read_text()
        second = self.finalize()
        self.assertEqual(first["filled"], self.plan)
        self.assertEqual(second["filled"], [])
        self.assertEqual(second["skipped"], self.plan)
        self.assertEqual(second["stopped"], "completed")
        self.assertEqual(json.loads((self.root / "index.json").read_text())["days"], json.loads(snapshot)["days"])


class WaitHourlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gh = Path(self.temp.name) / "gh"
        self.count = Path(self.temp.name) / "calls"

    def tearDown(self):
        self.temp.cleanup()

    def fake_gh(self, busy_calls: int, fail=False):
        self.gh.write_text(
            "#!/bin/sh\n"
            f'n=$(cat "{self.count}" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "{self.count}"\n'
            + ("exit 1\n" if fail else
               f'if [ "$n" -le {busy_calls} ]; then echo \'[{{"status":"in_progress"}},{{"status":"completed"}}]\'; '
               "else echo '[{\"status\":\"completed\"}]'; fi\n"))
        self.gh.chmod(0o755)

    def test_waits_while_the_hourly_job_runs(self):
        self.fake_gh(2)
        slept = []
        self.assertEqual(bp.wait_for_hourly(str(self.gh), "o/r", "update-lightning.yml", max_minutes=20,
                                            poll_seconds=5, sleep=slept.append), "idle")
        self.assertEqual(slept, [5, 5])

    def test_wait_is_bounded_and_never_fatal(self):
        self.fake_gh(10 ** 6)
        ticks = iter(range(0, 10 ** 6, 600))
        self.assertEqual(bp.wait_for_hourly(str(self.gh), "o/r", "w", max_minutes=20, poll_seconds=0,
                                            clock=lambda: next(ticks), sleep=lambda s: None), "timeout")
        self.fake_gh(0, fail=True)
        self.assertEqual(bp.wait_for_hourly(str(self.gh), "o/r", "w", max_minutes=20), "unknown")


class WorkflowTextTests(unittest.TestCase):
    def test_manual_only_with_its_own_concurrency_group(self):
        text = WORKFLOW.read_text()
        on_block = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("workflow_dispatch:", on_block)
        for trigger in ("schedule:", "push:", "pull_request:", "cron:"):
            self.assertNotIn(trigger, on_block)
        group = text.split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("group: uk-lightning-backfill", group)
        self.assertIn("cancel-in-progress: false", group)
        hourly_group = HOURLY.read_text().split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn("uk-lightning-backfill", hourly_group)
        self.assertNotIn("uk-lightning-update", group)

    def test_structure_matrix_artifacts_and_one_writer(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        wf = yaml.safe_load(WORKFLOW.read_text())
        jobs = wf["jobs"]
        self.assertEqual(list(jobs), ["plan", "shard", "finalize"])
        self.assertEqual(wf["permissions"], {"contents": "read"})
        self.assertEqual(jobs["finalize"]["permissions"]["contents"], "write")
        self.assertNotIn("permissions", jobs["shard"])  # shards never write the repository
        strategy = jobs["shard"]["strategy"]
        self.assertEqual(strategy["max-parallel"], 6)
        self.assertTrue(strategy["fail-fast"])
        self.assertIn("fromJSON(needs.plan.outputs.matrix)", strategy["matrix"])
        uploads = [s for s in jobs["shard"]["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")]
        self.assertEqual(uploads[0]["if"], "${{ always() }}")
        self.assertIn("always()", jobs["finalize"]["if"])
        self.assertEqual(jobs["finalize"]["needs"], ["plan", "shard"])
        secrets = [j for j, body in jobs.items() if "secrets.EUMETSAT" in json.dumps(body)]
        self.assertEqual(secrets, ["shard"])
        self.assertLessEqual(jobs["shard"]["timeout-minutes"], 360)
        self.assertLess(int(jobs["shard"]["env"]["SHARD_BUDGET_MINUTES"]) + 25, jobs["shard"]["timeout-minutes"])

    def test_listed_in_the_validation_workflow(self):
        self.assertIn("backfill-parallel-lightning.yml", (ROOT / ".github/workflows/test-lightning.yml").read_text())
        self.assertIn("backfill_parallel.py", (ROOT / ".github/workflows/test-lightning.yml").read_text())


PLAN = "Plan never-searched UTC dates and shards"
SEARCH = "Search this shard's dates (run records only)"
SHARD_SURFACE = "Surface shard stop"
FINALIZE = "Ingest, verify and commit (one commit)"
SURFACE = "Surface backfill stop"


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class ParallelWorkflowShellTests(unittest.TestCase):
    """plan job -> N shard jobs (separate runner dirs) -> artifacts -> finalize job."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = base = Path(self.temp.name)
        self.env = {**os.environ, "HOME": str(base), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                    "GITHUB_REF": "refs/heads/main", "PYTHONDONTWRITEBYTECODE": "1",
                    "SHARD_STAGGER_STEP_SECONDS": "0", "HOURLY_POLL_SECONDS": "0"}
        bin_dir = base / "bin"
        bin_dir.mkdir()
        (bin_dir / "python").symlink_to(sys.executable)
        gh = bin_dir / "fake-gh"
        gh.write_text("#!/bin/sh\necho '[]'\n")
        gh.chmod(0o755)
        self.env["GH_BIN"] = str(gh)
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self.env.pop("EUMETSAT_CONSUMER_KEY", None)
        self.origin, self.repo = base / "origin.git", base / "repo"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=base)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=base)
        self.git("remote", "add", "origin", str(self.origin))
        for name in ("history.py", "grid.py", "status.py", "backfill_parallel.py"):
            shutil.copy(ROOT / name, self.repo / name)
        source = (ROOT / "collect.py").read_text()
        source = source.replace("def download(start: datetime, end: datetime):", "def _real_download(start, end):")
        source = source.replace('\nif __name__ == "__main__":', FAKE_DOWNLOAD + '\nif __name__ == "__main__":')
        (self.repo / "collect.py").write_text(source)
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        shutil.copytree(ROOT / "ci", self.repo / "ci")  # finalize runs ci/verify_data.py --staged
        now = datetime.now(timezone.utc)
        self.today = now.date()
        self.days = [(self.today - timedelta(days=n)).isoformat() for n in range(1, 9)]  # d1 newest .. d8
        make_archive(self.repo / "data" / "history", now, self.today - timedelta(days=2))  # d2 searched
        self.heartbeat = {"schema": "uk-lightning-status.v1", "written_utc": "2026-10-27T05:18:00.000Z",
                          "health": "ok", "freshness": "ok", "last_attempt_outcome": "success",
                          "consecutive_failures": 0, "last_successful_scan_end_utc": "2026-10-27T05:10:09.000Z"}
        (self.repo / "data" / "status.json").write_text(json.dumps(self.heartbeat, indent=1) + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial archive")
        self.git("push", "-q", "-u", "origin", "main")
        self.base_head = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def runner(self, name):
        path = self.base / "runners" / name
        path.mkdir(parents=True, exist_ok=True)
        return {"RUNNER_TEMP": str(path), "GITHUB_OUTPUT": str(path / "github_output")}

    def run_step(self, name, env, values=None):
        script = substitute(step(WORKFLOW, name), values or {})
        return subprocess.run(["bash", "-e", "-c", script], cwd=self.repo, env={**self.env, **env},
                              capture_output=True, text=True)

    def pipeline(self, shards="3", extra=None):
        extra = extra or {}
        plan_env = {**self.runner("plan"), "END_DATE": "", "DAYS": "8", "SHARDS": shards, "DRY_RUN": "false"}
        plan = self.run_step(PLAN, plan_env)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        planned = outputs(Path(plan_env["GITHUB_OUTPUT"]))
        final = self.runner("finalize")
        (Path(final["RUNNER_TEMP"]) / "plan").mkdir()
        shutil.copy(Path(plan_env["RUNNER_TEMP"]) / "plan-artifact" / "backfill-plan.txt",
                    Path(final["RUNNER_TEMP"]) / "plan" / "backfill-plan.txt")
        records = Path(final["RUNNER_TEMP"]) / "records"
        records.mkdir()
        shard_results = []
        for entry in json.loads(planned["matrix"])["include"]:
            env = {**self.runner(f"shard-{entry['shard']}"), "SHARD": str(entry["shard"]), "DATES": entry["dates"],
                   **extra}
            search = self.run_step(SEARCH, env)
            out = outputs(Path(env["GITHUB_OUTPUT"]))
            surface = self.run_step(SHARD_SURFACE, {**env, "STOPPED": out.get("stopped", ""), "ERROR": out.get("error", "")})
            shard_results.append((entry, search, out, surface))
            for f in (Path(env["RUNNER_TEMP"]) / "records").iterdir():  # upload/download-artifact, merge-multiple
                if not f.name.startswith("."):
                    shutil.copy(f, records / f.name)
            self.assertEqual(self.git("status", "--porcelain"), "", "a shard must not touch the repository")
        fin = self.run_step(FINALIZE, {**final, "SHARDS": planned["shards"], **extra})
        out = outputs(Path(final["GITHUB_OUTPUT"]))
        surface = self.run_step(SURFACE, {**final, "STOPPED": out.get("stopped", ""), "ERROR": out.get("error", ""),
                                          "OUTCOME": "success" if fin.returncode == 0 else "failure"})
        self.git("fetch", "-q", "origin")
        return planned, shard_results, fin, out, surface

    def rows(self):
        index = json.loads(self.git("show", "origin/main:data/history/index.json"))
        return {r["date"]: r for r in index["days"]}

    def test_three_shards_then_one_verified_commit(self):
        planned, shards, fin, out, surface = self.pipeline("3")
        self.assertEqual(planned["count"], "7")
        self.assertEqual(planned["shards"], "3")
        self.assertEqual(fin.returncode, 0, fin.stderr + fin.stdout)
        self.assertEqual(surface.returncode, 0, surface.stdout)
        self.assertEqual((out["stopped"], out["pushed"], out["filled"]), ("completed", "yes", "7"))
        self.assertEqual(self.git("rev-list", "--count", f"{self.base_head}..origin/main").strip(), "1")
        rows = self.rows()
        for d in self.days:
            if d == self.days[1]:
                self.assertEqual(rows[d]["count"], 1)  # the hourly-searched date is untouched
                continue
            self.assertEqual((rows[d]["coverage"], rows[d]["scanned_seconds"], rows[d]["count"]), ("full", 86400, 1), d)
        status = json.loads(self.git("show", "origin/main:data/status.json"))
        for key, value in self.heartbeat.items():
            self.assertEqual(status[key], value, key)
        self.assertEqual(status["backfill"]["shards"], 3)

    def test_rate_limit_in_one_shard_fails_it_but_other_records_are_committed(self):
        d = self.days
        planned, shards, fin, out, surface = self.pipeline("3", {"FAKE_FAIL_DAYS": d[2]})  # shard 2: d3, d6
        by_shard = {e["shard"]: (o, s) for e, _, o, s in shards}
        self.assertEqual(by_shard[2][0]["stopped"], "eumetsat_error")
        self.assertNotEqual(by_shard[2][1].returncode, 0)  # the failing shard goes red -> fail-fast
        self.assertEqual(by_shard[1][1].returncode, 0)
        self.assertEqual(fin.returncode, 0, fin.stderr)
        self.assertEqual(out["stopped"], "eumetsat_error")
        self.assertNotEqual(surface.returncode, 0)
        rows = self.rows()
        for day in (d[2], d[5]):
            self.assertEqual((rows[day]["coverage"], rows[day]["count"]), ("none", None), day)
        for day in (d[0], d[3], d[4], d[6], d[7]):
            self.assertEqual(rows[day]["count"], 1, day)

    def test_push_rejected_by_a_concurrent_hourly_commit_reingests_on_the_new_main(self):
        hourly = self.base / "hourly"
        self.git("clone", "-q", str(self.origin), str(hourly), cwd=self.base)
        hook = self.repo / ".git" / "hooks" / "pre-push"
        marker = self.base / "hook-fired"
        hook.write_text(
            "#!/bin/sh\n"
            "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE\n"
            f'[ -e "{marker}" ] && exit 0\n'
            f'touch "{marker}"\n'
            f'cd "{hourly}" && git pull -q origin main && '
            "sed -i 's/2026-10-27T05:18:00.000Z/2026-10-27T06:18:00.000Z/' data/status.json && "
            "git commit -qam 'data: archive UTC day shards, index v2 and collector status' && git push -q origin main\n")
        hook.chmod(0o755)
        planned, shards, fin, out, surface = self.pipeline("2")
        self.assertTrue(marker.exists())
        self.assertEqual(fin.returncode, 0, fin.stderr + fin.stdout)
        self.assertIn("re-ingesting on the new main", fin.stdout)
        self.assertEqual(out["pushed"], "yes")
        log = self.git("log", "--format=%s", "origin/main").splitlines()
        self.assertTrue(log[0].startswith("data: parallel backfill of 7"))
        self.assertTrue(log[1].startswith("data: archive UTC day shards"))  # the hourly commit is kept
        status = json.loads(self.git("show", "origin/main:data/status.json"))
        self.assertEqual(status["written_utc"], "2026-10-27T06:18:00.000Z")  # its heartbeat survives
        self.assertEqual(len(status["backfill"]["filled_days"]), 7)

    def test_plan_refuses_more_than_six_shards_and_writing_runs_off_main(self):
        env = {**self.runner("plan"), "END_DATE": "", "DAYS": "8", "SHARDS": "7", "DRY_RUN": "false"}
        self.assertNotEqual(self.run_step(PLAN, env).returncode, 0)
        env.update(SHARDS="6", GITHUB_REF="refs/heads/feature")
        off_main = self.run_step(PLAN, env)
        self.assertNotEqual(off_main.returncode, 0)
        self.assertIn("must run on main", off_main.stdout)
        env["DRY_RUN"] = "true"
        self.assertEqual(self.run_step(PLAN, env).returncode, 0)


if __name__ == "__main__":
    unittest.main()

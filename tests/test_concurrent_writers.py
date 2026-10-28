"""The hourly collector and a backfill commit to main at the same time.

They run in separate concurrency groups (a backfill can no longer queue the
hourly feed), so either one can find main moved under it. These tests run the
real commit-step shells of BOTH workflows in two clones of one bare 'origin'
(no network, no EUMETSAT, no GitHub) and check that the loser of the push race
replays its own inputs on top of the winner's commit: nothing is lost, nothing
is textually merged, the archive verifies, and status.json keeps the hourly
heartbeat and the backfill block side by side.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import history
from collect import build_feature
from test_backfill_workflow import BACKFILL, FAKE_DOWNLOAD, HOURLY, outputs, step, substitute

HOURLY_ARCHIVE_COMMIT = "Commit archive and status (archive commit)"
HOURLY_FEED_COMMIT = "Commit latest public GeoJSON (feed commit)"
BF_PLAN, BF_SEARCH, BF_COMMIT = ("Plan never-searched UTC dates", "Search and ingest each date",
                                 "Verify and commit backfilled dates (one commit)")
WORKFLOWS = ROOT / ".github" / "workflows"


class ConcurrencyDesignTests(unittest.TestCase):
    """The hourly group belongs to the hourly workflow alone; no textual rebase of generated files."""

    @staticmethod
    def group(path):
        block = path.read_text().split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
        return re.search(r"group:\s*(\S+)", block).group(1)

    def test_no_other_workflow_shares_the_hourly_group(self):
        for path in WORKFLOWS.glob("*.yml"):
            if path.name != HOURLY.name and "\nconcurrency:\n" in path.read_text():
                self.assertNotEqual(self.group(path), self.group(HOURLY), path.name)

    def test_hourly_run_is_bounded_under_an_hour_and_never_cancelled(self):
        text = HOURLY.read_text()
        self.assertRegex(text, r"concurrency:\n  group: uk-lightning-update\n  cancel-in-progress: false")
        timeouts = [int(t) for t in re.findall(r"timeout-minutes:\s*(\d+)", text)]
        self.assertTrue(timeouts and max(timeouts) <= 55, timeouts)

    def test_two_cron_slots_and_a_gate_that_only_scheduled_runs_can_skip(self):
        text = HOURLY.read_text()
        on_block = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(re.findall(r'cron: "([^"]+)"', on_block), ["17 * * * *", "47 * * * *"])
        self.assertIn("needs: gate", text)
        self.assertIn("if: needs.gate.outputs.due == 'true'", text)
        self.assertIn('status.py due --status data/status.json --event "${{ github.event_name }}"', text)

    def test_status_write_records_trigger_and_run_timing(self):
        text = HOURLY.read_text()
        self.assertIn("actions: read", text)
        for flag in ("--trigger", "--run-id", "--run-created", "--job-started"):
            self.assertIn(flag, text)

    def test_no_pull_rebase_of_generated_files_in_any_writer(self):
        for path in (HOURLY, BACKFILL):
            self.assertNotIn("pull --rebase", path.read_text(), path.name)
            self.assertIn("ci/push-with-replay.sh", path.read_text(), path.name)


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class PushRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = {**os.environ, "HOME": str(self.base), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                    "GITHUB_REF": "refs/heads/main", "PYTHONDONTWRITEBYTECODE": "1", "PUSH_RETRY_SLEEP": "0"}
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "python").symlink_to(sys.executable)
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self.env.pop("EUMETSAT_CONSUMER_KEY", None)
        self.origin = self.base / "origin.git"
        seed = self.base / "seed"
        self.git(self.base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.base, "init", "-q", "-b", "main", str(seed))
        self.git(seed, "remote", "add", "origin", str(self.origin))
        for name in ("history.py", "grid.py", "status.py"):
            shutil.copy(ROOT / name, seed / name)
        source = (ROOT / "collect.py").read_text()
        source = source.replace("def download(start: datetime, end: datetime):", "def _real_download(start, end):")
        source = source.replace('\nif __name__ == "__main__":', FAKE_DOWNLOAD + '\nif __name__ == "__main__":')
        (seed / "collect.py").write_text(source)
        shutil.copytree(ROOT / "ci", seed / "ci")
        (seed / ".gitignore").write_text("__pycache__/\n")

        self.now = datetime.now(timezone.utc)
        self.today = self.now.date()
        self.days = [(self.today - timedelta(days=n)).isoformat() for n in range(1, 5)]  # d1 newest .. d4
        searched = datetime.combine(self.today - timedelta(days=2), time.min, tzinfo=timezone.utc)
        history.archive_observations(
            seed / "data" / "history", [build_feature(-2.0, 52.0, searched + timedelta(hours=2), "TEST-hourly", 0)],
            now=self.now, scanned_windows=[history.successful_scan((searched + timedelta(hours=1)).isoformat(),
                                                                   (searched + timedelta(hours=7)).isoformat(), 36)],
            lookback_hours=6.0)
        (seed / "data" / "status.json").write_text(json.dumps({
            "schema": "uk-lightning-status.v1", "health": "ok", "freshness": "ok",
            "last_attempt_utc": (self.now - timedelta(hours=1)).isoformat(), "last_attempt_outcome": "success",
            "consecutive_failures": 0, "last_successful_scan_end_utc": (self.now - timedelta(hours=1)).isoformat(),
            "integrity": {"state": "ok", "alert": None, "lost_days": []}}, indent=1) + "\n")
        (seed / "data" / "uk_lightning.geojson").write_text('{"features": []}\n')
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "-q", "-m", "initial archive")
        self.git(seed, "push", "-q", "-u", "origin", "main")
        self.hourly = self.clone("hourly")
        self.backfill = self.clone("backfill")

    def tearDown(self):
        self.temp.cleanup()

    def clone(self, name):
        path = self.base / name
        self.git(self.base, "clone", "-q", str(self.origin), str(path))
        (self.base / f"{name}-runner").mkdir()
        return path

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True, text=True).stdout

    def runner(self, repo):
        return self.base / f"{repo.name}-runner"

    def sh(self, repo, script, extra=None):
        env = {**self.env, "RUNNER_TEMP": str(self.runner(repo)), "GITHUB_OUTPUT": str(self.runner(repo) / "out"),
               **(extra or {})}
        return subprocess.run(["bash", "-e", "-c", script], cwd=repo, env=env, capture_output=True, text=True)

    # --- the two writers, using the real workflow step shells -----------------
    def hourly_collect(self, start, end, flash_at, flash_id):
        """Step 1 of update-lightning.yml (EUMETSAT) replaced by its outputs: run record and feed."""
        record = {"schema": "uk-lightning-run-record.v1", "outcome": "success", "error": None,
                  "run_utc": history.format_utc(self.now), "products_examined": 6, "lookback_hours": 6.0,
                  "slots_parsed": True, "scanned_ranges": [{"start_utc": history.format_utc(start),
                                                            "end_utc": history.format_utc(end)}],
                  "features": [build_feature(-2.0, 52.0, flash_at, flash_id, 0)]}
        (self.runner(self.hourly) / "lightning-run-record.json").write_text(json.dumps(record))
        (self.hourly / "data" / "uk_lightning.geojson").write_text(json.dumps({"features": [flash_id]}) + "\n")
        self.hourly_env = {"ARCHIVE_OUTCOME": "success", "COLLECT_OUTCOME": "success", "TRIGGER": "schedule",
                           "RUN_ID": "4242", "RUN_CREATED_UTC": history.format_utc(self.now - timedelta(minutes=2)),
                           "JOB_STARTED_UTC": history.format_utc(self.now - timedelta(minutes=1))}

    def hourly_commit(self, before_feed=None, before_archive_commit=None):
        """Steps 2-5 of update-lightning.yml in order; the hooks let the other writer push in between."""
        if before_feed:
            before_feed()
        feed = self.sh(self.hourly, substitute(step(HOURLY, HOURLY_FEED_COMMIT), {}))
        self.assertEqual(feed.returncode, 0, feed.stdout + feed.stderr)
        archive = self.sh(self.hourly, substitute(step(HOURLY, "Append to the one-year UTC archive"), {}))
        self.assertEqual(archive.returncode, 0, archive.stderr)
        status = self.sh(self.hourly, substitute(step(HOURLY, "Write data/status.json (always)"), {
            "steps.collect.outcome": "success", "steps.archive.outcome": "success", "github.event_name": "schedule",
            "github.run_id": "4242"}), self.hourly_env)
        self.assertEqual(status.returncode, 0, status.stderr)
        if before_archive_commit:
            before_archive_commit()
        done = self.sh(self.hourly, substitute(step(HOURLY, HOURLY_ARCHIVE_COMMIT), {}), self.hourly_env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return feed, done

    def backfill_search(self):
        plan = self.sh(self.backfill, substitute(step(BACKFILL, BF_PLAN), {}), {"END_DATE": "", "DAYS": "4"})
        self.assertEqual(plan.returncode, 0, plan.stderr)
        search = self.sh(self.backfill, substitute(step(BACKFILL, BF_SEARCH), {}))
        self.assertEqual(search.returncode, 0, search.stderr)
        return outputs(self.runner(self.backfill) / "out")

    def backfill_commit(self, out):
        done = self.sh(self.backfill, substitute(step(BACKFILL, BF_COMMIT), {}),
                       {"STOPPED": out["stopped"], "ERROR": out.get("error", "")})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    # --- what landed on origin/main --------------------------------------------
    def main_view(self):
        check = self.base / "check"
        if check.exists():
            shutil.rmtree(check)
        self.git(self.base, "clone", "-q", str(self.origin), str(check))
        self.assertEqual(history.main(["verify", "--archive-dir", str(check / "data" / "history")]), 0)
        index = json.loads((check / "data" / "history" / "index.json").read_text())
        status = json.loads((check / "data" / "status.json").read_text())
        ids = set()
        for f in (check / "data" / "history" / "days").rglob("*.json"):
            ids |= {feat["id"] for feat in json.loads(f.read_text()).get("features", [])}
        return {r["date"]: r for r in index["days"]}, status, ids, check

    def test_hourly_push_loses_to_a_backfill_and_replays_its_archive_and_heartbeat(self):
        out = self.backfill_search()
        start = self.now - timedelta(hours=2)
        self.hourly_collect(start, self.now - timedelta(minutes=10), self.now - timedelta(hours=1), "TEST-hourly-new")
        # The backfill pushes after the hourly job ingested but before it committed the archive.
        feed, done = self.hourly_commit(before_archive_commit=lambda: self.backfill_commit(out))
        self.assertIn("pushed on attempt 1", feed.stdout)
        self.assertIn("push rejected (attempt 1/5)", done.stdout)
        self.assertIn("pushed on attempt 2", done.stdout)
        rows, status, ids, check = self.main_view()
        d1, d2, d3, d4 = self.days
        for d in (d3, d4):
            self.assertEqual((rows[d]["coverage"], rows[d]["count"]), ("full", 1), d)
        self.assertIn("TEST-hourly-new:0", ids)          # the hourly flash survived the replay
        self.assertIn(f"TEST-fake-{d3}:0", ids)          # and the backfill's flashes were not overwritten
        self.assertEqual(status["backfill"]["filled_days"], [d1, d3, d4])
        self.assertEqual(status["last_attempt_utc"], history.format_utc(self.now))
        self.assertEqual(status["heartbeat"]["this_run"]["run_id"], "4242")
        self.assertEqual(status["heartbeat"]["this_run"]["trigger"], "schedule")
        self.assertEqual(json.loads((check / "data" / "uk_lightning.geojson").read_text()), {"features": ["TEST-hourly-new"]})
        log = self.git(check, "log", "--format=%s")
        self.assertIn("data: backfill 3 never-searched", log)
        self.assertIn("data: archive UTC day shards", log)

    def test_backfill_push_loses_to_the_hourly_job_and_skips_a_date_the_hourly_job_searched(self):
        out = self.backfill_search()
        d1, d2, d3, d4 = self.days
        d1_start = datetime.combine(self.today - timedelta(days=1), time(22, 0), tzinfo=timezone.utc)
        self.hourly_collect(d1_start, d1_start + timedelta(minutes=110), d1_start + timedelta(minutes=30), "TEST-hourly-d1")
        self.hourly_commit()                            # the hourly job wins; it touched d1 (yesterday)
        done = self.backfill_commit(out)                # rejected, replayed: d1 is no longer never searched
        self.assertIn("push rejected (attempt 1/5)", done.stdout)
        self.assertIn(f"replay: {d1} is no longer never-searched", done.stdout)
        rows, status, ids, _ = self.main_view()
        self.assertEqual(rows[d1]["coverage"], "partial")   # the hourly job's row is kept, not overwritten
        self.assertIn("TEST-hourly-d1:0", ids)
        self.assertNotIn(f"TEST-fake-{d1}:0", ids)
        for d in (d3, d4):
            self.assertEqual((rows[d]["coverage"], rows[d]["count"]), ("full", 1), d)
        self.assertEqual(status["backfill"]["filled_days"], [d3, d4])
        self.assertEqual(status["backfill"]["remaining_unknown_days"], [d1])
        # The hourly heartbeat committed by the winner is untouched by the backfill replay.
        self.assertEqual(status["last_attempt_utc"], history.format_utc(self.now))
        self.assertEqual(status["heartbeat"]["this_run"]["run_id"], "4242")

    def test_feed_push_loses_to_a_backfill_and_the_archive_then_builds_on_the_backfill(self):
        out = self.backfill_search()
        self.hourly_collect(self.now - timedelta(hours=2), self.now - timedelta(minutes=10),
                            self.now - timedelta(hours=1), "TEST-hourly-feed")
        feed, done = self.hourly_commit(before_feed=lambda: self.backfill_commit(out))
        self.assertIn("push rejected (attempt 1/5)", feed.stdout)
        self.assertIn("pushed on attempt 2", feed.stdout)
        self.assertIn("pushed on attempt 1", done.stdout)  # the archive step already ran on the new main
        rows, status, ids, check = self.main_view()
        self.assertEqual(json.loads((check / "data" / "uk_lightning.geojson").read_text()), {"features": ["TEST-hourly-feed"]})
        self.assertTrue({"TEST-hourly-feed:0", f"TEST-fake-{self.days[2]}:0"} <= ids)
        self.assertEqual(status["backfill"]["filled_days"], [self.days[0], self.days[2], self.days[3]])
        self.assertEqual(status["heartbeat"]["this_run"]["run_id"], "4242")

    def test_refuses_to_reset_over_unrelated_uncommitted_changes(self):
        out = self.backfill_search()
        self.backfill_commit(out)
        (self.hourly / "data" / "uk_lightning.geojson").write_text('{"features": ["x"]}\n')
        (self.hourly / "history.py").write_text("# local edit that a reset would destroy\n")
        done = self.sh(self.hourly, substitute(step(HOURLY, HOURLY_FEED_COMMIT), {}))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("refusing to reset over uncommitted changes outside the step's paths: history.py", done.stdout)
        self.assertEqual((self.hourly / "history.py").read_text(), "# local edit that a reset would destroy\n")

    def test_pushes_to_the_checked_out_branch_never_to_main(self):
        main_before = self.git(self.hourly, "rev-parse", "origin/main")
        self.git(self.hourly, "checkout", "-q", "-b", "feature")
        (self.hourly / "data" / "uk_lightning.geojson").write_text('{"features": ["branch"]}\n')
        done = self.sh(self.hourly, "bash ci/push-with-replay.sh 'feed on a branch' true data/uk_lightning.geojson")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.git(self.hourly, "fetch", "-q", "origin")
        self.assertEqual(self.git(self.hourly, "rev-parse", "origin/main"), main_before)
        self.assertEqual(self.git(self.hourly, "log", "-1", "--format=%s", "origin/feature").strip(), "feed on a branch")

    def test_gives_up_loudly_after_the_attempt_limit(self):
        out = self.backfill_search()
        self.hourly_collect(self.now - timedelta(hours=2), self.now - timedelta(minutes=10),
                            self.now - timedelta(hours=1), "TEST-hourly-x")
        self.hourly_commit()
        done = self.sh(self.backfill, substitute(step(BACKFILL, BF_COMMIT), {}),
                       {"STOPPED": out["stopped"], "ERROR": "", "PUSH_ATTEMPTS": "1"})
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("::error::push-with-replay: push rejected 1 times", done.stdout)


if __name__ == "__main__":
    unittest.main()

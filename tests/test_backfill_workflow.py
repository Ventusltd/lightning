"""Executes the shell of backfill-lightning.yml in throwaway git repositories.

No network, no EUMETSAT, no GitHub. Each test builds a temporary repository
with a local bare 'origin', a real archive (history.py) and status, and a copy
of collect.py whose ``download`` is replaced by a FAKE downloader (10-minute
slot names, one synthetic "TEST-" flash per date, or a fake 429 rate-limit
error). The real history.py and status.py run unchanged.
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
import history
from collect import build_feature

BACKFILL = ROOT / ".github" / "workflows" / "backfill-lightning.yml"
HOURLY = ROOT / ".github" / "workflows" / "update-lightning.yml"
PLAN = "Plan never-searched UTC dates"
SEARCH = "Search and ingest each date"
COMMIT = "Verify and commit backfilled dates (one commit)"
SURFACE = "Surface backfill stop"

FAKE_DOWNLOAD = '''

def download(start, end):  # FAKE (tests only): replaces the EUMETSAT download
    sys.stdin.read()  # a child that drains stdin must not eat the plan the loop reads
    day = start.date().isoformat()
    if day in os.environ.get("FAKE_FAIL_DAYS", "").split(","):
        raise RuntimeError("HTTP 429 Too Many Requests (fake EUMETSAT rate limit)")
    ghost = day in os.environ.get("FAKE_GHOST_DAYS", "").split(",")
    names, cursor, i = [], start, 0
    while cursor < end:
        if not (ghost and i == 5):
            stop = cursor + timedelta(minutes=10)
            names.append("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--x-x--ARC-x_C_EUMT_"
                         f"{stop:%Y%m%d%H%M%S}_L2PF_OPE_{cursor:%Y%m%d%H%M%S}_{stop:%Y%m%d%H%M%S}_N__O_0001_0000")
        cursor += timedelta(minutes=10)
        i += 1
    flashes = [build_feature(-2.0, 52.0, start + timedelta(hours=1, minutes=3), "TEST-fake-" + day, 0)]
    if ghost:  # a flash inside the slot the fake search did not examine: history.py must refuse it
        flashes.append(build_feature(-2.0, 52.0, start + timedelta(minutes=55), "TEST-ghost-" + day, 0))
    return flashes, len(names), names

'''


def step(workflow: Path, name: str) -> str:
    lines = workflow.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    run = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "run: |")
    body, indent = [], None
    for line in lines[run + 1:]:
        if line.strip() and indent is None:
            indent = len(line) - len(line.lstrip())
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:] if line.strip() else "")
    return "\n".join(body) + "\n"


def substitute(script: str, values: dict) -> str:
    def value(match):
        key = match.group(1).strip()
        if key in values:
            return values[key]
        raise KeyError(f"unhandled workflow expression {key}")
    return re.sub(r"\$\{\{\s*([^}]+?)\s*\}\}", value, script)


def outputs(path: Path) -> dict:
    out, lines, i = {}, path.read_text().splitlines() if path.exists() else [], 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line:
            key, marker = line.split("<<", 1)
            j = lines.index(marker, i + 1)
            out[key] = "\n".join(lines[i + 1:j])
            i = j + 1
            continue
        key, _, val = line.partition("=")
        out[key] = val
        i += 1
    return out


class BackfillWorkflowTextTests(unittest.TestCase):
    """Structure checks without a YAML library (CI's Python has none installed)."""

    def test_manual_dispatch_only_with_the_three_inputs(self):
        text = BACKFILL.read_text()
        on_block = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("workflow_dispatch:", on_block)
        for trigger in ("schedule:", "push:", "pull_request:", "cron:"):
            self.assertNotIn(trigger, on_block)
        for name in ("end_date:", "days:", "dry_run:"):
            self.assertIn(name, on_block)
        self.assertRegex(on_block, r'days:\n(?:\s+.*\n)*?\s+default: "7"')
        self.assertRegex(on_block, r"dry_run:\n(?:\s+.*\n)*?\s+type: boolean")

    def test_has_its_own_concurrency_group_so_it_never_queues_the_hourly_feed(self):
        def group(path):
            block = path.read_text().split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
            return (re.search(r"group:\s*(\S+)", block).group(1), re.search(r"cancel-in-progress:\s*(\S+)", block).group(1))
        self.assertEqual(group(BACKFILL), ("uk-lightning-backfill", "false"))
        self.assertEqual(group(HOURLY), ("uk-lightning-update", "false"))
        self.assertNotEqual(group(BACKFILL)[0], group(HOURLY)[0])

    def test_search_step_uses_the_eumetsat_secrets_and_rebuild_day(self):
        text = BACKFILL.read_text()
        self.assertIn("${{ secrets.EUMETSAT_CONSUMER_KEY }}", text)
        self.assertIn("${{ secrets.EUMETSAT_CONSUMER_SECRET }}", text)
        script = step(BACKFILL, SEARCH)
        self.assertIn('collect.py --rebuild-day "$D" --run-record "$rec"', script)
        self.assertIn('"$RUNNER_TEMP/rb-$D.json"', script)
        self.assertIn("history.py backfill", script)


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class BackfillWorkflowShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.runner_temp = base / "runner"
        self.runner_temp.mkdir()
        self.env = {**os.environ, "HOME": str(base), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                    "RUNNER_TEMP": str(self.runner_temp), "GITHUB_REF": "refs/heads/main",
                    "GITHUB_OUTPUT": str(base / "github_output"), "PYTHONDONTWRITEBYTECODE": "1"}
        bin_dir = base / "bin"
        bin_dir.mkdir()
        (bin_dir / "python").symlink_to(sys.executable)
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self.env.pop("EUMETSAT_CONSUMER_KEY", None)
        self.origin = base / "origin.git"
        self.repo = base / "repo"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=base)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=base)
        self.git("remote", "add", "origin", str(self.origin))
        for name in ("history.py", "grid.py", "status.py"):
            shutil.copy(ROOT / name, self.repo / name)
        source = (ROOT / "collect.py").read_text()
        source = source.replace("def download(start: datetime, end: datetime):", "def _real_download(start, end):")
        source = source.replace('\nif __name__ == "__main__":', FAKE_DOWNLOAD + '\nif __name__ == "__main__":')
        (self.repo / "collect.py").write_text(source)
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        shutil.copytree(ROOT / "ci", self.repo / "ci")

        now = datetime.now(timezone.utc)
        self.today = now.date()
        self.days = [(self.today - timedelta(days=n)).isoformat() for n in range(1, 5)]  # d1 newest .. d4
        searched = datetime.combine(self.today - timedelta(days=2), time.min, tzinfo=timezone.utc)
        history.archive_observations(
            self.repo / "data" / "history", [build_feature(-2.0, 52.0, searched + timedelta(hours=2), "TEST-hourly", 0)],
            now=now, scanned_windows=[history.successful_scan((searched + timedelta(hours=1)).isoformat(),
                                                              (searched + timedelta(hours=7)).isoformat(), 36)],
            lookback_hours=6.0)
        self.heartbeat = {"schema": "uk-lightning-status.v1", "written_utc": "2026-10-27T05:18:00.000Z",
                          "health": "ok", "freshness": "ok", "last_attempt_outcome": "success",
                          "consecutive_failures": 0, "last_successful_scan_end_utc": "2026-10-27T05:10:09.000Z",
                          "integrity": {"state": "ok", "alert": None, "lost_days": []},
                          "archive": {"outcome": "success", "archived_observations": 1}}
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

    def run_step(self, name, extra=None, values=None):
        script = substitute(step(BACKFILL, name), values or {})
        return subprocess.run(["bash", "-e", "-c", script], cwd=self.repo, env={**self.env, **(extra or {})},
                              capture_output=True, text=True)

    def rows(self, ref="origin/main"):
        index = json.loads(self.git("show", f"{ref}:data/history/index.json"))
        return {r["date"]: r for r in index["days"]}

    def full_run(self, extra=None):
        plan = self.run_step(PLAN, {"END_DATE": "", "DAYS": "4"})
        self.assertEqual(plan.returncode, 0, plan.stderr)
        search = self.run_step(SEARCH, extra)
        out = outputs(Path(self.env["GITHUB_OUTPUT"]))
        commit_env = {**(extra or {}), "STOPPED": out.get("stopped", ""), "ERROR": out.get("error", "")}
        commit = self.run_step(COMMIT, commit_env) if search.returncode == 0 else None
        surface = self.run_step(SURFACE, {**(extra or {}), "STOPPED": out.get("stopped", ""), "ERROR": out.get("error", ""),
                                     "OUTCOME": "success" if search.returncode == 0 else "failure"})
        self.git("fetch", "-q", "origin")
        return plan, search, commit, surface, out

    def test_plan_lists_never_searched_dates_newest_first(self):
        plan = self.run_step(PLAN, {"END_DATE": "", "DAYS": "4"})
        self.assertEqual(plan.returncode, 0, plan.stderr)
        d1, d2, d3, d4 = self.days
        self.assertEqual((self.runner_temp / "backfill-plan.txt").read_text().split(), [d1, d3, d4])
        self.assertEqual(outputs(Path(self.env["GITHUB_OUTPUT"]))["count"], "3")

    def test_plan_rejects_more_than_31_days_and_an_unfinished_end_date(self):
        self.assertNotEqual(self.run_step(PLAN, {"END_DATE": "", "DAYS": "32"}).returncode, 0)
        self.assertNotEqual(self.run_step(PLAN, {"END_DATE": self.today.isoformat(), "DAYS": "1"}).returncode, 0)

    def test_full_backfill_is_one_verified_commit_and_leaves_the_heartbeat_alone(self):
        plan, search, commit, surface, out = self.full_run()
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertEqual(commit.returncode, 0, commit.stderr)
        self.assertEqual(surface.returncode, 0, surface.stdout)
        d1, d2, d3, d4 = self.days
        self.assertEqual(out["stopped"], "completed")
        self.assertEqual(self.git("rev-list", "--count", f"{self.base_head}..origin/main").strip(), "1")
        rows = self.rows()
        for d in (d1, d3, d4):
            self.assertEqual((rows[d]["coverage"], rows[d]["scanned_seconds"], rows[d]["count"]), ("full", 86400, 1), d)
        self.assertEqual(rows[d2]["count"], 1)  # the hourly-searched date is untouched
        status = json.loads(self.git("show", "origin/main:data/status.json"))
        for key in ("written_utc", "last_attempt_outcome", "consecutive_failures", "last_successful_scan_end_utc"):
            self.assertEqual(status[key], self.heartbeat[key], key)
        self.assertEqual(status["backfill"]["filled_days"], [d1, d3, d4])
        self.assertEqual(status["archive"]["archived_observations"], 4)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_rate_limit_stops_cleanly_commits_verified_dates_and_leaves_the_rest_unknown(self):
        d1, d2, d3, d4 = self.days
        plan, search, commit, surface, out = self.full_run({"FAKE_FAIL_DAYS": d3})
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertEqual(commit.returncode, 0, commit.stderr)
        self.assertEqual(out["stopped"], "eumetsat_error")
        self.assertIn("429", out["error"])
        rows = self.rows()
        self.assertEqual(rows[d1]["count"], 1)
        for d in (d3, d4):  # never a zero for a date the search did not cover
            self.assertEqual((rows[d]["coverage"], rows[d]["count"]), ("none", None), d)
        self.assertFalse((self.runner_temp / f"rb-{d4}.json").exists(), "the loop must stop at the first error")
        status = json.loads(self.git("show", "origin/main:data/status.json"))
        self.assertEqual(status["backfill"]["remaining_unknown_days"], [d3, d4])
        self.assertNotEqual(surface.returncode, 0)
        self.assertIn("::error::Backfill stopped (eumetsat_error)", surface.stdout)

    def test_archive_refusal_stops_and_keeps_that_date_unknown(self):
        d1, d2, d3, d4 = self.days
        plan, search, commit, surface, out = self.full_run({"FAKE_GHOST_DAYS": d1})
        self.assertEqual(out["stopped"], "archive_refused")
        self.assertIn("outside the examined slots", out["error"])
        rows = self.rows()
        for d in (d1, d3, d4):
            self.assertIsNone(rows[d]["count"], d)
        self.assertNotEqual(surface.returncode, 0)
        status = json.loads(self.git("show", "origin/main:data/status.json"))
        self.assertEqual(status["backfill"]["stopped_reason"], "archive_refused")

    def test_time_budget_stops_before_any_search_and_only_warns(self):
        plan, search, commit, surface, out = self.full_run({"BACKFILL_BUDGET_MINUTES": "0"})
        self.assertEqual(out["stopped"], "time_budget")
        self.assertEqual(out["filled"], "0")
        self.assertFalse(list(self.runner_temp.glob("rb-*.json")))
        self.assertEqual(surface.returncode, 0)
        self.assertIn("::warning::", surface.stdout)

    def test_refuses_to_write_outside_main(self):
        self.run_step(PLAN, {"END_DATE": "", "DAYS": "4"})
        search = self.run_step(SEARCH, {"GITHUB_REF": "refs/heads/feature"})
        self.assertNotEqual(search.returncode, 0)
        self.assertIn("must run on main", search.stdout)
        self.assertEqual(self.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()

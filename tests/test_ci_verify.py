"""ci/verify_data.py, the PUSH_VERIFY gate of ci/push-with-replay.sh and the CI wiring.

No network. The archive fixtures are written by the real history.py.
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
import history  # noqa: E402
from collect import build_feature  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
VERIFY = [sys.executable, str(ROOT / "ci" / "verify_data.py")]


def make_data(data: Path) -> None:
    now = datetime.now(timezone.utc)
    start = datetime.combine(now.date() - timedelta(days=2), time.min, tzinfo=timezone.utc)
    history.archive_observations(
        data / "history", [build_feature(-2.0, 52.0, start + timedelta(hours=2), "TEST-hourly", 0)], now=now,
        scanned_windows=[history.successful_scan((start + timedelta(hours=1)).isoformat(),
                                                 (start + timedelta(hours=7)).isoformat(), 36)],
        lookback_hours=6.0)
    (data / "status.json").write_text(json.dumps({"schema": "uk-lightning-status.v1"}) + "\n")
    (data / "uk_lightning.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "metadata": {},
        "features": [build_feature(-2.0, 52.0, start + timedelta(hours=2), "TEST-hourly", 0)]}) + "\n")


def corrupt_a_shard(data: Path) -> None:
    index = json.loads((data / "history" / "index.json").read_text())
    row = next(r for r in index["days"] if r.get("path"))
    shard = data / "history" / row["path"]
    shard.write_text(shard.read_text().replace("TEST-hourly", "TEST-hourlx"))


class VerifyDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.data = self.base / "data"
        make_data(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def verify(self, *args):
        return subprocess.run([*VERIFY, "--data-dir", str(self.data), *args], cwd=self.base,
                              capture_output=True, text=True)

    def test_good_data_passes_quick_and_deep(self):
        for args in ((), ("--deep",)):
            done = self.verify(*args)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("archive ok", done.stdout)
            self.assertIn("grid ok", done.stdout)

    def test_a_shard_differing_from_its_index_row_fails(self):
        corrupt_a_shard(self.data)
        done = self.verify()
        self.assertEqual(done.returncode, 1)
        self.assertIn("::error::verify-data archive", done.stdout)

    def test_a_malformed_feed_or_status_fails(self):
        (self.data / "uk_lightning.geojson").write_text('{"features": []}\n')
        done = self.verify()
        self.assertEqual(done.returncode, 1)
        self.assertIn("::error::verify-data feed", done.stdout)
        make_data(self.data)
        (self.data / "status.json").write_text("{not json\n")
        self.assertIn("::error::verify-data status", self.verify().stdout)

    def test_a_grid_problem_warns_in_quick_mode_and_fails_deep(self):
        # history.py ingest never lets a grid failure block the archive; the deep
        # check on main turns it red instead.
        next((self.data / "history" / "grid").glob("*.json")).unlink()
        quick = self.verify()
        self.assertEqual(quick.returncode, 0, quick.stdout)
        self.assertIn("::warning::verify-data grid", quick.stdout)
        deep = self.verify("--deep")
        self.assertEqual(deep.returncode, 1)
        self.assertIn("::error::verify-data grid", deep.stdout)


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class PushVerifyTests(unittest.TestCase):
    """push-with-replay.sh runs PUSH_VERIFY on the staged paths before every commit."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        bin_dir = base / "bin"
        bin_dir.mkdir()
        (bin_dir / "python").symlink_to(sys.executable)
        self.env = {**os.environ, "HOME": str(base), "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1",
                    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PUSH_VERIFY": "python ci/verify_data.py --staged", "PUSH_RETRY_SLEEP": "0"}
        self.origin, self.repo = base / "origin.git", base / "repo"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=base)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=base)
        self.git("remote", "add", "origin", str(self.origin))
        for name in ("history.py", "grid.py", "collect.py"):
            shutil.copy(ROOT / name, self.repo / name)
        shutil.copytree(ROOT / "ci", self.repo / "ci")
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        make_data(self.repo / "data")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial")
        self.git("push", "-q", "-u", "origin", "main")
        self.head = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def push(self, *paths):
        return subprocess.run(["bash", "ci/push-with-replay.sh", "data: test", "true", *paths], cwd=self.repo,
                              env=self.env, capture_output=True, text=True)

    def test_an_unverified_archive_is_never_committed_or_pushed(self):
        corrupt_a_shard(self.repo / "data")
        done = self.push("data/status.json", "data/history")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("PUSH_VERIFY failed", done.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.head)
        self.assertEqual(self.git("ls-remote", "origin", "main").split()[0], self.head)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_a_heartbeat_commit_is_not_blocked_by_an_unstaged_archive_problem(self):
        corrupt_a_shard(self.repo / "data")
        (self.repo / "data" / "status.json").write_text(json.dumps({"schema": "uk-lightning-status.v1", "n": 2}))
        done = self.push("data/status.json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotEqual(self.git("ls-remote", "origin", "main").split()[0], self.head)

    def test_a_good_archive_change_is_pushed(self):
        (self.repo / "data" / "uk_lightning.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "metadata": {"n": 2}, "features": []}) + "\n")
        done = self.push("data/uk_lightning.geojson")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("verify-data: feed ok", done.stdout)


def step_block(path: Path, name: str) -> str:
    """The YAML text of one step, from its '- name:' line to the next step."""
    text = path.read_text()
    start = text.index(f"- name: {name}\n")
    nxt = text.find("\n      - ", start + 1)
    return text[start:] if nxt < 0 else text[start:nxt]


class CiWiringTests(unittest.TestCase):
    QUICK = "PUSH_VERIFY: python ci/verify_data.py --staged"

    def test_every_data_commit_runs_the_quick_verify_before_pushing(self):
        hourly = WORKFLOWS / "update-lightning.yml"
        self.assertIn(self.QUICK, step_block(hourly, "Commit latest public GeoJSON (feed commit)"))
        self.assertIn(self.QUICK, step_block(hourly, "Commit archive and status (archive commit)"))
        self.assertIn(self.QUICK, step_block(WORKFLOWS / "backfill-lightning.yml",
                                             "Verify and commit backfilled dates (one commit)"))
        finalize = step_block(WORKFLOWS / "backfill-parallel-lightning.yml", "Ingest, verify and commit (one commit)")
        self.assertLess(finalize.index("python ci/verify_data.py --staged"), finalize.index("git push"))
        for path in WORKFLOWS.glob("*.yml"):  # no data push bypasses the gate
            text = path.read_text()
            if "git push" in text or "push-with-replay.sh" in text:
                self.assertIn("ci/verify_data.py --staged", text, path.name)

    def test_test_job_installs_requirements_and_the_smoke_test_cannot_skip_there(self):
        text = (WORKFLOWS / "test-lightning.yml").read_text()
        self.assertIn("deps: [requirements, bare]", text)
        self.assertIn("run: pip install -r requirements.txt", text)
        self.assertIn("LIGHTNING_SMOKE_REQUIRE_DEPS: ${{ matrix.deps == 'requirements' && '1' || '0' }}", text)
        self.assertIn("pyflakes", text)
        for watched in ("requirements.txt", ".github/workflows/verify-lightning-data.yml"):
            self.assertEqual(text.count(f'- "{watched}"'), 2, watched)  # push and pull_request

    def test_deep_verify_runs_after_every_data_writer_and_human_data_push(self):
        text = (WORKFLOWS / "verify-lightning-data.yml").read_text()
        self.assertIn("python grid.py verify --deep --archive-dir data/history", text)
        self.assertIn("python history.py verify --archive-dir data/history", text)
        self.assertRegex(text, r'push:\n    branches: \[main\]\n    paths:\n      - "data/\*\*"')
        # workflow_run matches by workflow NAME: a renamed writer would silently
        # stop being verified, so the names must be the writers' real names.
        listed = re.findall(r'^      - "([^"]+)"$', text.split("workflow_run:", 1)[1].split("types:", 1)[0], re.M)
        writers = sorted(re.search(r"^name: (.+)$", p.read_text(), re.M).group(1)
                         for p in WORKFLOWS.glob("*.yml")
                         if "git push" in p.read_text() or "push-with-replay.sh" in p.read_text())
        self.assertEqual(sorted(listed), writers)
        group = re.search(r"concurrency:\n  group: (\S+)", text).group(1)
        self.assertNotIn(group, ("uk-lightning-update", "uk-lightning-backfill"))


if __name__ == "__main__":
    unittest.main()

"""Executes the shell of update-lightning.yml steps in throwaway git repositories.

No network, no EUMETSAT, no GitHub: each test builds a temporary repository
with a local bare 'origin', substitutes the ${{ ... }} expressions the step
uses, and runs the step's script with bash -e (the GitHub Actions default).
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "update-lightning.yml"
ARCHIVE_COMMIT = "Commit archive and status (archive commit)"
ARCHIVE_STEP = "Append to the one-year UTC archive"


def step_script(name: str) -> str:
    """The literal 'run: |' block of the named step (no YAML library needed)."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
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


def substitute(script: str, outcomes: dict) -> str:
    def value(match):
        key = match.group(1).strip()
        if key in outcomes:
            return outcomes[key]
        raise KeyError(f"unhandled workflow expression {key}")
    return re.sub(r"\$\{\{\s*([^}]+?)\s*\}\}", value, script)


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class WorkflowShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.env = {**os.environ, "HOME": str(base), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        self.origin = base / "origin.git"
        self.repo = base / "repo"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=base)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=base)
        self.git("remote", "add", "origin", str(self.origin))
        (self.repo / "data").mkdir()
        (self.repo / "data" / "uk_lightning.geojson").write_text("{}\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial")
        self.git("push", "-q", "-u", "origin", "main")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def run_step(self, name, outcomes, extra_env=None):
        script = substitute(step_script(name), outcomes)
        return subprocess.run(["bash", "-e", "-c", script], cwd=self.repo, env={**self.env, **(extra_env or {})},
                              capture_output=True, text=True)

    def pushed_files(self):
        return set(self.git("ls-tree", "-r", "--name-only", "origin/main").split())

    # --- D-4: the heartbeat is committed before data/history exists ---------------
    def test_status_is_committed_when_data_history_does_not_exist_yet(self):
        (self.repo / "data" / "status.json").write_text('{"last_attempt_outcome": "failure"}\n')
        self.assertFalse((self.repo / "data" / "history").exists())
        done = self.run_step(ARCHIVE_COMMIT, {"steps.archive.outcome": "skipped"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.git("fetch", "-q", "origin")
        self.assertIn("data/status.json", self.pushed_files())

    def test_archive_and_status_are_committed_together_after_a_good_run(self):
        (self.repo / "data" / "status.json").write_text('{"last_attempt_outcome": "success"}\n')
        (self.repo / "data" / "history" / "days").mkdir(parents=True)
        (self.repo / "data" / "history" / "index.json").write_text("{}\n")
        done = self.run_step(ARCHIVE_COMMIT, {"steps.archive.outcome": "success"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.git("fetch", "-q", "origin")
        self.assertTrue({"data/status.json", "data/history/index.json"} <= self.pushed_files())


if __name__ == "__main__":
    unittest.main()

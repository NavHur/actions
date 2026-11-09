"""The checks action end to end: run.sh in a scratch NavHur repository, running both real checks.

Downloads the complexity action's pinned releases, so it needs network and GH_TOKEN (or a signed-in gh).
Run from the repository root: python3 -m unittest discover -s checks -p "test_*.py"
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

RUN = Path(__file__).resolve().parent / "run.sh"
OVERSIZED = "x" * 20_001 + "\n"  # one line, over file-size's 20,000-byte cap
OVER_COMPLEXITY = """\
def nested(a, b, c):
    if a:
        if b:
            if c:
                return 1 if c else 2
    return 0
"""  # cognitive 10, over NavHur's 6
SIMPLE = "def simple():\n    return 1\n"


class Action(unittest.TestCase):

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.repo = self.scratch / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://github.com/NavHur/example.git")
        self.commit({"README.md": "example\n"})
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        command = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args]
        return subprocess.run(command, cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def commit(self, files: dict[str, str]) -> None:
        for path, text in files.items():
            (self.repo / path).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "change")

    def run_action(self, base: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "BASE": base or self.base, "RUNNER_TEMP": str(self.scratch / "runner")}
        return subprocess.run(["bash", str(RUN)], cwd=self.repo, env=env, capture_output=True, text=True)

    def test_a_change_within_both_limits_passes(self):
        self.commit({"ok.py": SIMPLE})
        result = self.run_action()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("file-size: passed", result.stdout)
        self.assertIn("complexity: passed", result.stdout)

    def test_complexity_still_runs_after_file_size_fails(self):
        self.commit({"big.txt": OVERSIZED, "ok.py": SIMPLE})
        result = self.run_action()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error title=file-size::", result.stdout)
        self.assertIn("complexity: passed", result.stdout)

    def test_both_failures_are_reported(self):
        self.commit({"big.txt": OVERSIZED, "over.py": OVER_COMPLEXITY})
        result = self.run_action()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error title=file-size::", result.stdout)
        self.assertIn("::error title=complexity::", result.stdout)
        self.assertIn("nested: cognitive 10 > 6", result.stdout)
        self.assertIn("checks: failed: file-size complexity", result.stdout)

    def test_an_event_without_a_base_commit_compares_nothing(self):
        result = self.run_action(base="0" * 40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing to compare", result.stdout)
        self.assertNotIn("passed", result.stdout)


if __name__ == "__main__":
    unittest.main()

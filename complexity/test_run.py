"""The complexity action end to end: run.sh in a scratch NavHur repository, with the pinned binaries.

Downloads the pinned releases, so it needs network and GH_TOKEN (or a signed-in gh).
Run from the repository root: python3 -m unittest discover -s complexity -p "test_*.py"
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

RUN = Path(__file__).resolve().parent / "run.sh"
AT_THE_LIMIT = """\
def nested(a, b, c):
    if a:
        if b:
            if c:
                return 1
    return 0
"""  # cognitive 6: 1 + 2 + 3
OVER_THE_LIMIT = AT_THE_LIMIT.replace("return 1", "return 1 if c else 2")  # cognitive 10


class Action(unittest.TestCase):

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.repo = self.scratch / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://github.com/NavHur/example.git")
        self.commit("README.md", "example\n")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        command = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args]
        return subprocess.run(command, cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def commit(self, path: str, text: str) -> None:
        (self.repo / path).write_text(text)
        self.git("add", path)
        self.git("commit", "-qm", f"add {path}")

    def run_action(self) -> subprocess.CompletedProcess:
        env = {**os.environ, "BASE": self.base, "RUNNER_TEMP": str(self.scratch / "runner")}
        return subprocess.run(["bash", str(RUN)], cwd=self.repo, env=env, capture_output=True, text=True)

    def test_a_function_at_the_limit_passes(self):
        self.commit("ok.py", AT_THE_LIMIT)
        result = self.run_action()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Pedro's limits for NavHur: cognitive > 6, cyclomatic > 18", result.stdout)

    def test_a_changed_function_over_the_limit_fails(self):
        self.commit("over.py", OVER_THE_LIMIT)
        result = self.run_action()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("nested: cognitive 10 > 6", result.stdout)

    def test_no_base_commit_skips(self):
        self.base = "0" * 40
        result = self.run_action()
        self.assertEqual(result.returncode, 0)
        self.assertIn("nothing to compare", result.stdout)


if __name__ == "__main__":
    unittest.main()

"""Ask a Godot repository's godot-guard for its verdicts, so audits tag what its lint enforces.

godot-guard (github.com/NavHur/godot-guard) runs complexity and comment
rules from `.godot-guard.yml` with a ratchet baseline. Its `check --json`
(v0.2+) reports the effective limits and rule levels, the files it checked,
and every problem `check` reports; this module never reads the config itself.
Its `complexity --json` (v0.6+) scores GDScript functions for every repository,
with or without that config.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from revision import materialize, read_file, run

CONFIG = ".godot-guard.yml"
BASELINE = ".godot-guard-baseline.json"
MINIMUM = (0, 2)
FORMAT = 1
SEVERITY = {"error": "error", "warn": "warning"}
VERSION = re.compile(r"(\d+)\.(\d+)")


@dataclass
class Verdicts:
    """One `godot-guard check --json` report."""

    binary: str
    limits: dict[str, int]
    levels: dict[str, str]
    files: set[str]
    problems: dict[tuple[str, str, int], str]  # (rule, path, line) -> "error" or "warning"

    def severity(self, rule: str) -> str | None:
        """'error' or 'warning' while the rule is on, None when it is off."""
        return SEVERITY.get(self.levels.get(rule, "error"))

    def at(self, rule: str, path: str, line: int) -> str | None:
        """The severity of the problem godot-guard reports for `rule` on that line, if any."""
        return self.problems.get((rule, path, line))


def candidates(root: Path) -> list[str]:
    """PBP_GODOT_GUARD, then the repository's virtualenv, then PATH: the executables that exist."""
    paths = [os.environ.get("PBP_GODOT_GUARD", ""), str(root / ".venv" / "bin" / "godot-guard"),
             shutil.which("godot-guard") or ""]
    return [path for path in paths if path and os.access(path, os.X_OK)]


def find(root: Path) -> str | None:
    """The godot-guard the repository uses, whose verdicts the audit reports."""
    return next(iter(candidates(root)), None)


def scorer(root: Path) -> str | None:
    """The first godot-guard with `complexity --json`, which scores GDScript for this skill.

    Scores do not depend on the repository's configuration, so a stale godot-guard in its virtualenv
    gives way to a newer one on PATH. The subcommand is probed rather than inferred from a version.
    """
    return next((binary for binary in candidates(root) if scores_gdscript(binary)), None)


def scores_gdscript(binary: str) -> bool:
    try:
        result = subprocess.run([binary, "complexity", "--json", os.devnull], capture_output=True, text=True,
                                timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.startswith("{")


def version_of(binary: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = VERSION.search(result.stdout)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def usable(root: Path, notes: list[str]) -> str | None:
    """A godot-guard with `check --json`, or None with a coverage note naming what was found."""
    binary = find(root)
    version = version_of(binary) if binary else None
    if version is not None and version >= MINIMUM:
        return binary
    if binary is None:
        found = f"no godot-guard (PBP_GODOT_GUARD, {root / '.venv/bin/godot-guard'}, PATH)"
    elif version is None:
        found = f"{binary} (version unknown)"
    else:
        found = f"godot-guard {version[0]}.{version[1]} at {binary}"
    notes.append(f"{CONFIG}: found {found}; tagging needs godot-guard 0.2+ (`check --json`), so its limits "
                 "were not applied and GDScript stays [not lint-checked]")
    return None


def check(root: Path, head: str | None, files: list[str], notes: list[str]) -> Verdicts | None:
    """godot-guard's verdicts on `files` at `head` (the working tree when None).

    None when there is nothing to check, the repository has no config at that revision, or no usable godot-guard.
    """
    if not files or read_file(root, head, CONFIG) is None:
        return None
    binary = usable(root, notes)
    if binary is None:
        return None
    if head is None:
        return ask(binary, root, files, notes)
    with tempfile.TemporaryDirectory(prefix="pbp-godot-guard-") as tmp:
        settings = [name for name in (CONFIG, BASELINE) if read_file(root, head, name) is not None]
        materialize(root, head, settings + files, Path(tmp))
        return ask(binary, Path(tmp), files, notes)


def ask(binary: str, cwd: Path, files: list[str], notes: list[str]) -> Verdicts | None:
    result = run([binary, "check", "--json", *files], cwd)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        notes.append(f"{binary} check --json failed (exit {result.returncode}: {result.stderr.strip()[:300]}); "
                     "GDScript stays [not lint-checked]")
        return None
    if report.get("version") != FORMAT:
        notes.append(f"{binary} check --json reports format {report.get('version')!r}, not {FORMAT}; "
                     "GDScript stays [not lint-checked]")
        return None
    problems: dict[tuple[str, str, int], str] = {}
    for problem in report["problems"]:
        key = (problem["rule"], problem["path"], problem["line"])
        if problems.get(key) != "error":
            problems[key] = SEVERITY.get(problem["level"], "error")
    return Verdicts(binary, report["limits"], report["levels"], set(report["files"]), problems)

#!/usr/bin/env python3
"""Score every changed function against Pedro's complexity limits.

Usage: measure_complexity.py BASE [HEAD] [--all | --over-only] [--repo DIR]
                             [--cognitive-limit N] [--cyclomatic-limit N] [--check]

  BASE         base revision; the diff starts at merge-base(BASE, HEAD).
               EMPTY measures every tracked file (whole-repository scope).
  HEAD         revision to measure. Omit it to measure the working tree,
               including uncommitted and untracked files.
  --all        list every function in the changed files, not only changed ones.
  --over-only  list only functions over a limit (summary counts still print).
  --repo       repository or worktree to measure (default: current directory).
  --cognitive-limit, --cyclomatic-limit
               a stricter limit the repository documents (AGENTS.md, a custom
               check script). A value looser than Pedro's limit is ignored.
  --check      exit 1 when a changed function is over a limit or an analyzer
               is missing or fails (CI); without it the exit status is 0. A
               file that does not parse is listed, not failed: the
               repository's compiler or linter owns syntax.

Limits: Pedro's depend on the GitHub owner in origin's URL. NavHur
repositories: cognitive above 6 and cyclomatic above 18 are over; every other
repository: cognitive above 8 and cyclomatic above 16. Equal passes. The
stricter of Pedro's limit and the repository's wins.

Languages and how each is measured:
- JS, TS, Svelte, Vue, Astro: the repository's oxlint and the complexity rules
  of the file's .oxlintrc.json, copied into a temporary config with max 0.
  Configs without a cognitive rule get fleet's `pbp/cognitive-complexity`
  plugin; files no config owns use classic `complexity` plus that plugin, run
  by the repository's oxlint or this skill's own. A second pass with the
  repository's real rules tags each excess `[lint: error]`, `[lint: warning]`,
  or `[lint passes]`, and reports functions the repository's stricter max
  catches.
- Python, PHP, Rust: fleet's pbp-complexity (PBP_COMPLEXITY, PATH, or this fleet
  checkout's target/release); GDScript: godot-guard's `complexity --json`
  (0.6+; PBP_GODOT_GUARD, the repository's .venv, or PATH). bin/sync installs
  both. Tagged `[not lint-checked]`. Python limits in ruff/flake8
  (`max-complexity`) or complexipy (`max-complexity-allowed`) config count as
  repository limits.
  In a Godot repository with a `.godot-guard.yml`, godot-guard 0.2+
  (PBP_GODOT_GUARD, the repository's .venv, or PATH) reports its limits,
  which count too, and its verdict on each GDScript function it checks.
The script never edits the repository: files are copied (or extracted from
HEAD) into a temporary tree. Exit status is 0 when the run completes. Under --check it is 1 when a changed
function is over a limit or an analyzer is missing or fails (the INCOMPLETE line
names those files; UNPARSED names files that do not parse, which do not fail it). Any other nonzero exit means the tool itself failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import godot_guard
from measure_js import measure_js
from measured_function import Function, pedro_limits
from revision import (LANGUAGES, changed_files, diff_changes, git, github_owner, language_of, materialize,
                      read_file, run)

SKILL_DIR = Path(__file__).resolve().parent.parent


# Python, GDScript, PHP, Rust: Rust analyzers

FLEET_BUILD = SKILL_DIR.parent.parent / "target" / "release" / "pbp-complexity"
LANGUAGE_NAMES = {"python": "Python", "gdscript": "GDScript", "php": "PHP", "rust": "Rust"}
INSTALL = "run fleet's `bin/sync --apply`"


def pbp_complexity() -> str | None:
    """PBP_COMPLEXITY, then PATH (bin/sync installs it), then a release build in this fleet checkout."""
    paths = [os.environ.get("PBP_COMPLEXITY", ""), shutil.which("pbp-complexity") or "", str(FLEET_BUILD)]
    return next((p for p in paths if p and os.access(p, os.X_OK)), None)


def analyzer(language: str, root: Path) -> tuple[list[str] | None, str]:
    """The command that scores `language` files, or None and what is missing."""
    if language == "gdscript":
        binary = godot_guard.scorer(root)
        missing = f"no godot-guard with `complexity --json` (0.6+; PBP_GODOT_GUARD, {root / '.venv/bin/godot-guard'}, PATH)"
        return ([binary, "complexity", "--json"] if binary else None), missing
    binary = pbp_complexity()
    return ([binary, language] if binary else None), f"no pbp-complexity (PBP_COMPLEXITY, PATH, {FLEET_BUILD})"


def repo_python_limits(root: Path, head: str | None) -> tuple[dict[str, int], list[str]]:
    """Complexity maxes a Python repository configures for ruff, flake8, or complexipy."""
    limits, notes = {}, []
    patterns = {"cyclomatic": r"^\s*max-complexity\s*=\s*(\d+)", "cognitive": r"^\s*max-complexity-allowed\s*=\s*(\d+)"}
    for name in ("pyproject.toml", "ruff.toml", ".ruff.toml", "setup.cfg", ".flake8", "tox.ini"):
        text = read_file(root, head, name)
        if not text:
            continue
        for metric, pattern in patterns.items():
            match = re.search(pattern, text, re.M)
            if match and int(match.group(1)) < limits.get(metric, 10**6):
                limits[metric] = int(match.group(1))
                notes.append(f"{name}: repository {metric} max {match.group(1)}")
    return limits, notes


def measure_external(language: str, root: Path, head: str | None, files: list[str], tree: Path,
                     notes: list[str], unmeasured: list[str], unparsed: list[str]) -> list[Function]:
    command, missing = analyzer(language, root)
    if command is None:
        notes.append(f"{LANGUAGE_NAMES[language]}: {missing}; {len(files)} file(s) not measured; {INSTALL}")
        unmeasured += files
        return []
    materialize(root, head, files, tree)
    result = run([*command, *[str(tree / f) for f in files]], tree)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        notes.append(f"{language}: analyzer failed ({result.stderr.strip()[:300]}); {len(files)} file(s) not measured")
        unmeasured += files
        return []
    print(f"Analyzer: {language} -> {len(files)} file(s) with {command[0]}")
    functions = []
    for absolute, value in report.items():
        path = str(Path(absolute).relative_to(tree))
        if isinstance(value, dict):
            notes.append(f"{path}: does not parse ({value.get('error')}); not measured")
            unparsed.append(path)
            continue
        for item in value:
            functions.append(Function(path, item["name"], item["line"], item["end_line"],
                                      item["cognitive"], item["cyclomatic"]))
    return functions


# Report


def effective_limits(args, pedro: dict[str, int], root: Path, head: str | None, has_python: bool,
                     notes: list[str], guard: godot_guard.Verdicts | None = None) -> dict[str, dict[str, int]]:
    """Limits per language: Pedro's, tightened by flags and repository config."""
    base = dict(pedro)
    for metric in base:
        value = getattr(args, f"{metric}_limit")
        if value is None:
            continue
        if value < base[metric]:
            base[metric] = value
            notes.append(f"--{metric}-limit {value}: stricter than Pedro's {pedro[metric]}; applied")
        else:
            notes.append(f"--{metric}-limit {value} is looser than Pedro's {pedro[metric]}; ignored")
    limits = {language: dict(base) for language in LANGUAGES}
    if has_python:
        repo, repo_notes = repo_python_limits(root, head)
        notes += repo_notes
        for metric, value in repo.items():
            limits["python"][metric] = min(limits["python"][metric], value)
    tighten_for_guard(limits["gdscript"], guard, notes)
    return limits


GUARD_RULES = {"cognitive": "cognitive-complexity", "cyclomatic": "cyclomatic-complexity"}


def tighten_for_guard(limits: dict[str, int], guard: godot_guard.Verdicts | None, notes: list[str]) -> None:
    if guard is None:
        return
    for metric in GUARD_RULES:
        limits[metric] = min(limits[metric], guard.limits[metric])
    notes.append(f"{godot_guard.CONFIG}: repository cognitive max {guard.limits['cognitive']}, "
                 f"cyclomatic max {guard.limits['cyclomatic']}; GDScript tagged by {guard.binary} check")
    for rule in GUARD_RULES.values():
        if guard.severity(rule) != "error":
            notes.append(f"{godot_guard.CONFIG}: `{rule}` is {guard.levels.get(rule)}; lint does not fail it")


def guard_lint(function: Function, guard: godot_guard.Verdicts) -> dict[str, tuple[str, int | None]]:
    """The metrics godot-guard reports at the function's first line, where it puts every complexity problem.

    A stale baseline entry for a function now within the limit is a baseline problem, not an excess.
    """
    flagged = {}
    for metric, rule in GUARD_RULES.items():
        severity, score = guard.at(rule, function.path, function.line), function.score(metric)
        if severity and score is not None and score > guard.limits[metric]:
            flagged[metric] = (severity, guard.limits[metric])
    return flagged


def tag_with_guard(functions: list[Function], guard: godot_guard.Verdicts | None) -> None:
    """Functions in files godot-guard checked; the rest (excluded, hidden, outside its paths) stay untagged."""
    if guard is None:
        return
    for function in functions:
        if function.path in guard.files:
            function.lint = guard_lint(function, guard)


def print_result(changed: list[Function], files: list[str], by_language: dict[str, list[str]],
                 unmeasured: list[str], unparsed: list[str]) -> list[Function]:
    """Print the summary line, the INCOMPLETE note, and each changed function over a limit; return those."""
    over = [f for f in changed if f.over()]
    tags = [f.lint_tag() for f in over]
    failing = sum(tag.startswith(" [lint:") for tag in tags)
    passing = tags.count(" [lint passes]")
    unchecked = tags.count(" [not lint-checked]")
    counts = ", ".join(f"{len(group)} {language}" for language, group in sorted(by_language.items()))
    print(f"Result: {len(over)} function{'s' if len(over) != 1 else ''} over threshold among {len(changed)} "
          f"changed functions in {len(files)} changed source files ({counts}): {failing} fail the repository's "
          f"lint, {passing} pass it, {unchecked} not lint-checked.")
    if unmeasured:
        print(f"INCOMPLETE: {len(unmeasured)} changed file(s) not measured ({', '.join(sorted(unmeasured))}); "
              "see the coverage notes.")
    if unparsed:
        print(f"UNPARSED: {len(unparsed)} changed file(s) do not parse ({', '.join(sorted(unparsed))}); the "
              "repository's own compiler or linter owns syntax, so --check does not fail them.")
    for function in over:
        print(f"  {function.path}:{function.line} {function.name}: {'; '.join(function.over())}{function.lint_tag()}")
    return over


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base")
    parser.add_argument("head", nargs="?")
    listing = parser.add_mutually_exclusive_group()
    listing.add_argument("--all", action="store_true", help="list unchanged functions in changed files too")
    listing.add_argument("--over-only", action="store_true", help="list only functions over a limit")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--cognitive-limit", type=int)
    parser.add_argument("--cyclomatic-limit", type=int)
    parser.add_argument("--check", action="store_true", help="exit 1 when something fails the limits")
    args = parser.parse_args()

    root = Path(git(Path(args.repo), "rev-parse", "--show-toplevel").strip())
    if args.base == "EMPTY":  # whole-repository scope: diff from the empty tree
        merge_base = git(root, "hash-object", "-t", "tree", "/dev/null").strip()
    else:
        merge_base = git(root, "merge-base", args.base, args.head or "HEAD").strip()
    files = changed_files(root, merge_base, args.head)
    by_language: dict[str, list[str]] = {}
    for path in files:
        by_language.setdefault(language_of(path), []).append(path)
    notes: list[str] = []
    guard = godot_guard.check(root, args.head, by_language.get("gdscript", []), notes)
    owner = github_owner(root)
    pedro = pedro_limits(owner)
    limits = effective_limits(args, pedro, root, args.head, "python" in by_language, notes, guard)
    print(f"Complexity of {root} {args.base}..{args.head or 'working tree'} (merge-base {merge_base[:10]})")
    print(f"Pedro's limits for {owner or 'a repository without a GitHub origin'}: cognitive > {pedro['cognitive']}, "
          f"cyclomatic > {pedro['cyclomatic']} (equal passes; a stricter repository limit wins)")
    if not files:
        print("No changed source files.\nResult: 0 functions over threshold (nothing to measure).")
        return 0

    changes = diff_changes(root, merge_base, args.head, files)
    functions: list[Function] = []
    unmeasured: list[str] = []
    unparsed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="measure-complexity-") as tmp:
        tree = Path(tmp)
        for language, group in sorted(by_language.items()):
            if language == "js":
                found = measure_js(root, args.head, group, tree, notes, unmeasured, unparsed, pedro)
            else:
                found = measure_external(language, root, args.head, group, tree, notes, unmeasured, unparsed)
            for function in found:
                function.limits = dict(limits[language])
            functions += found
    tag_with_guard(functions, guard)

    def is_changed(function: Function) -> bool:
        # Deletion-only changes are excluded by policy; head coordinates cannot tell a deleted tail from a deleted neighbour.
        return changes[function.path].adds_to(function.line, function.end_line)

    changed = [f for f in functions if is_changed(f)]
    shown = functions if args.all else changed
    if args.over_only:
        shown = [f for f in shown if f.over()]
    trivial = [f for f in shown if (f.cyclomatic or 0) <= 1 and not f.cognitive and not f.over()]
    shown = [f for f in shown if f not in trivial]
    print()
    current = None
    for function in sorted(shown, key=lambda f: (f.path, f.line)):
        if function.path != current:
            current = function.path
            print(current)
        cognitive = "-" if function.cognitive is None else str(function.cognitive)
        cyclomatic = "-" if function.cyclomatic is None else str(function.cyclomatic)
        flag = "  OVER: " + "; ".join(function.over()) + function.lint_tag() if function.over() else ""
        mark = "" if not args.all or is_changed(function) else " (unchanged)"
        print(f"  L{function.line}-{function.end_line} {function.name}{mark}: "
              f"cognitive {cognitive}, cyclomatic {cyclomatic}{flag}")
    if not shown:
        print("(no functions to list)")
    if trivial:
        print(f"({len(trivial)} trivial functions with cyclomatic 1 and cognitive 0 not listed)")
    if notes:
        print("\nCoverage notes:")
        for note in notes:
            print(f"  {note}")
    print("\n'-' cognitive: score 0, or counted inside the enclosing function (nested functions).")
    over = print_result(changed, files, by_language, unmeasured, unparsed)
    return 1 if args.check and (over or unmeasured) else 0


if __name__ == "__main__":
    sys.exit(main())

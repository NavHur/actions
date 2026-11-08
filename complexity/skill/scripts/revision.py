"""The revision being measured: the files and lines it changes, their contents, and a temporary copy to run tools on."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

HOST_CHECK = os.path.expanduser("~/.local/bin/host-check")
LANGUAGES = {
    "js": {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".svelte", ".vue", ".astro"},
    "python": {".py"},
    "gdscript": {".gd"},
    "php": {".php"},
    "rust": {".rs"},
}


def language_of(path: str) -> str | None:
    suffix = PurePosixPath(path).suffix
    return next((name for name, suffixes in LANGUAGES.items() if suffix in suffixes), None)


def git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {result.stderr.decode().strip()}")
    return result.stdout


def git(root: Path, *args: str) -> str:
    return git_bytes(root, *args).decode()


GITHUB_ORIGIN = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^/@]*@)?|[^/@:]+@)(?:(?:ssh\.)?github\.com|github-[\w.-]+)(?::\d+)?[:/]([^/:]+)/", re.I)
# An ssh config Host alias such as `github-personal` (one key per GitHub account) stands for github.com.


def github_owner(root: Path) -> str | None:
    """The GitHub owner in origin's URL (https, ssh, scp-style, or a github-* ssh alias), or None."""
    result = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                            capture_output=True, text=True, check=False)
    match = GITHUB_ORIGIN.match(result.stdout.strip())
    return match.group(1) if result.returncode == 0 and match else None


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    if os.environ.get("HOST_CHECK_ACTIVE") != "1" and os.access(HOST_CHECK, os.X_OK):
        command = [HOST_CHECK, *command]
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def changed_files(root: Path, base: str, head: str | None) -> list[str]:
    files = git(root, "diff", "--name-only", "--diff-filter=d", "-z", base, *([head] if head else [])).split("\0")
    if head is None:
        files += git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted({f for f in files if f and language_of(f) and "node_modules/" not in f})


@dataclass
class FileChanges:
    """Head lines a revision touches in one file; `added` None means the whole file is new."""

    added: set[int] | None = field(default_factory=set)
    # A deletion sits between head line n and n + 1; n is kept (0 is before the first line).
    gaps: set[int] = field(default_factory=set)

    def adds_to(self, line: int, end_line: int) -> bool:
        """Whether an added or edited line (or a new file) falls in the span; deletions never do."""
        return self.added is None or any(line <= n <= end_line for n in self.added)

    def touches(self, line: int, end_line: int) -> bool:
        return self.adds_to(line, end_line) or any(line <= n < end_line for n in self.gaps)

    def add_hunk(self, row: str) -> None:
        match = HUNK.match(row)
        start, count = int(match.group(1)), int(match.group(2) or 1)
        if count:
            self.added.update(range(start, start + count))
        else:
            self.gaps.add(start)


HUNK = re.compile(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@")


def sources(root: Path, base: str, head: str | None) -> dict[str, str | None]:
    """Each changed path with the base path its content came from (itself unless renamed or copied; None if added)."""
    fields = git(root, "diff", "-M", "--name-status", "-z", base, *([head] if head else [])).split("\0")
    found: dict[str, str | None] = {}
    index = 0
    while index + 1 < len(fields):
        status = fields[index][0]
        width = 2 if status in "RC" else 1
        paths = fields[index + 1:index + 1 + width]
        found[paths[-1]] = None if status == "A" else paths[0]
        index += 1 + width
    return found


DIFF_FLAGS = ["diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv"]


def hunk_rows(diff: str) -> list[str]:
    return [row for row in diff.split("\n") if row.startswith("@@")]


def file_hunks(root: Path, base: str, head: str | None, source: str, path: str) -> list[str]:
    """The `@@` rows of the diff between exactly two files: `source` at base and `path` at head or on disk."""
    if head:
        return hunk_rows(git(root, *DIFF_FLAGS, f"{base}:{source}", f"{head}:{path}"))
    old = git_bytes(root, "show", f"{base}:{source}")
    with tempfile.TemporaryDirectory(prefix="revision-old-") as tmp:
        before = Path(tmp) / "before"
        before.write_bytes(old)
        # Run in the repository: --no-index reads .gitattributes (eol, clean filters) from the working directory.
        result = subprocess.run(["git", "-C", str(root), *DIFF_FLAGS, "--no-index", "--", str(before), str(root / path)],
                                capture_output=True, check=False)
    if result.returncode not in (0, 1):  # git diff --no-index exits 1 when the files differ
        sys.exit(f"git diff --no-index failed for {path}: {result.stderr.decode().strip()}")
    return hunk_rows(result.stdout.decode())


def read_changes(root: Path, base: str, head: str | None, change: FileChanges, path: str, source: str | None) -> None:
    """Fill one file's changes; no `source` means it is added, so every line is new and nothing is diffed."""
    if source is None:
        change.added = None
        return
    for row in file_hunks(root, base, head, source, path):
        change.add_hunk(row)


def mark_untracked_new(root: Path, changes: dict[str, FileChanges]) -> None:
    for path in git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", *changes).split("\0"):
        if path in changes:
            changes[path].added = None


def diff_changes(root: Path, base: str, head: str | None, files: list[str]) -> dict[str, FileChanges]:
    """Each changed file's added lines and deletion points; untracked files (working tree) are wholly new."""
    changes: dict[str, FileChanges] = {path: FileChanges() for path in files}
    for path, source in sources(root, base, head).items():
        if path in changes:
            read_changes(root, base, head, changes[path], path, source)
    if head is None:
        mark_untracked_new(root, changes)
    return changes


def materialize(root: Path, head: str | None, paths: list[str], tree: Path) -> None:
    if not paths:
        return
    if head:
        archive = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", head, "--", *paths],
                                 capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True)
        return
    for path in paths:
        source, target = root / path, tree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("node_modules"))
        elif source.exists():
            shutil.copy2(source, target)


def read_file(root: Path, head: str | None, path: str) -> str | None:
    if head:
        result = subprocess.run(["git", "-C", str(root), "show", f"{head}:{path}"], capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else None
    target = root / path
    return target.read_text(errors="replace") if target.exists() else None

#!/usr/bin/env python3
"""Keep files under 20,000 bytes, about 5,000 tokens at four bytes per token,
so an agent can read a whole file in one call.

    file-size check --staged      # the index against HEAD: what `git commit` records
    file-size check --tracked     # tracked worktree files against HEAD: what `git commit -a` records
    file-size check --base REF    # HEAD against its merge-base with REF: before a push, and in CI
    file-size report [PATH ...]   # every tracked file over the cap

check is a ratchet: it fails on a file over the cap that is new or grew, and
warns about one that was already over and did not grow. Binary files, lockfiles,
minified files, submodules, files that carry an `@generated` or Go
`Code generated ... DO NOT EDIT.` line in their first lines, and paths marked
linguist-generated or linguist-vendored in .gitattributes are skipped. Exit
status: 0 pass, 1 a file over the cap, 2 a usage or Git error.
"""
from __future__ import annotations

import argparse
import enum
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Optional

LIMIT_BYTES = 20_000
BYTES_PER_TOKEN = 4
BINARY_SNIFF_BYTES = 8000  # Git's heuristic: a NUL byte this early means binary
SKIP_CHUNK_BYTES = 64 * 1024  # how much of a large blob to read and drop at a time
GENERATED_SNIFF_LINES = 5  # a generated-code marker belongs in a file's first lines
GENERATED_MARKER = re.compile(rb"@generated\b|^// Code generated .* DO NOT EDIT\.\s*$")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SKIPPED_NAMES = ("*.lock", "*-lock.json", "*-lock.yaml", "*.lockb", "go.sum", "*.min.js", "*.min.css",
                 "*.map", "*.snap", "npm-shrinkwrap.json", "packages.lock.json", "gradle.lockfile",
                 "Package.resolved")
SKIP_ATTRIBUTES = ("linguist-generated", "linguist-vendored")
SPLIT_ADVICE = """\
Agents read a whole file before editing it, so each file has to fit in one read.
Split each file above along a seam, not by length:
  1. Find its separate responsibilities: a class, a group of functions that
     work together, a section under its own heading or comment banner.
  2. Move each into its own file, named for what it does, next to the original.
  3. Point callers at the new files. Do not leave a file that only re-exports.
  4. Move a constant, type, or fixture that one part uses along with that part.
  5. Split a test file the way its code splits: one test file per module.
Do not split into part1/part2 files or a grab-bag utils file, and do not squeeze
lines or strip comments to fit. Mark a generated or vendored file
linguist-generated or linguist-vendored in .gitattributes instead; a generated
file may carry an @generated or `Code generated ... DO NOT EDIT.` line at its top."""

NOT_A_REPOSITORY = "not a git repository"
NEW_REVISION = "HEAD"


class GitError(Exception):
    """A git command failed: not a repository, an unknown ref, or an unreadable blob."""


class NewSide(enum.Enum):
    """Where the new version of each file is read from."""
    WORKTREE = "worktree"
    INDEX = "index"
    REVISION = "revision"


@dataclass(frozen=True)
class Comparison:
    """The old revision against a new side; `env` points every git call at another index."""
    old_rev: str
    new_side: NewSide
    env: Optional[dict] = None

    @property
    def cached(self):
        return self.new_side is NewSide.INDEX

    @property
    def diff_args(self):
        """The arguments that make `git diff` compare old_rev with the new side."""
        return {NewSide.INDEX: ("--cached", self.old_rev), NewSide.WORKTREE: (self.old_rev,),
                NewSide.REVISION: (self.old_rev, NEW_REVISION)}[self.new_side]

    def new_ref(self, path):
        """The object name of the new version of a path in the index or the new revision."""
        if self.new_side is NewSide.WORKTREE:
            raise ValueError("a worktree file has no object name")
        return f":0:{path}" if self.cached else f"{NEW_REVISION}:{path}"


@dataclass(frozen=True)
class Change:
    path: str
    old_path: Optional[str]  # None for a new file


@dataclass(frozen=True)
class Finding:
    path: str
    size: int
    old_size: Optional[int]


def git(*args, cwd, stdin=None, env=None):
    result = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True, env=env)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def decode(raw):
    return raw.decode("utf-8", "surrogateescape")


def repo_root(cwd, env=None):
    return decode(git("rev-parse", "--show-toplevel", cwd=cwd, env=env)).strip()


def find_root(directory):
    """The root of the repository holding directory, or None when it is missing or in no repository."""
    if not os.path.isdir(directory):
        return None
    try:
        return repo_root(directory, env={**os.environ, "LC_ALL": "C"})
    except GitError as exc:
        if NOT_A_REPOSITORY in str(exc):
            return None
        raise


def head_or_empty_tree(root):
    """HEAD, or the empty tree before the first commit, so every file counts as new."""
    result = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root, capture_output=True)
    return decode(result.stdout).strip() if result.returncode == 0 else EMPTY_TREE


def staged_sides(root, env=None):
    """The index against HEAD."""
    head = head_or_empty_tree(root)
    return Comparison(head, NewSide.INDEX, env)


def tracked_sides(root, env=None):
    """The tracked files in the worktree against HEAD."""
    head = head_or_empty_tree(root)
    return Comparison(head, NewSide.WORKTREE, env)


def base_sides(ref, root):
    """HEAD against its merge-base with ref."""
    merge_base = decode(git("merge-base", ref, "HEAD", cwd=root)).strip()
    return Comparison(merge_base, NewSide.REVISION)


def changes(comparison, root):
    """The added, modified, and renamed files; a submodule pointer is not a file."""
    raw = git("diff", "--name-status", "-z", "-M", "--diff-filter=AMR", "--ignore-submodules=all",
              *comparison.diff_args, cwd=root, env=comparison.env)
    fields, found, i = decode(raw).split("\0"), [], 0
    while i < len(fields) - 1:
        status = fields[i]
        if status.startswith("R"):
            found.append(Change(fields[i + 2], fields[i + 1]))
            i += 3
        else:
            found.append(Change(fields[i + 1], fields[i + 1] if status == "M" else None))
            i += 2
    return found


def skipped_by_name(path):
    return any(fnmatch.fnmatch(os.path.basename(path), pattern) for pattern in SKIPPED_NAMES)


def skipped_by_attribute(paths, cached, root, env):
    if not paths:
        return set()
    flags = ["--cached"] if cached else []
    stdin = "\0".join(paths).encode("utf-8", "surrogateescape") + b"\0"
    fields = decode(git("check-attr", *flags, "-z", "--stdin", *SKIP_ATTRIBUTES, cwd=root, stdin=stdin,
                        env=env)).split("\0")
    return {fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] in ("set", "true")}


def candidates(paths, cached, root, env=None):
    named = [p for p in paths if not skipped_by_name(p)]
    marked = skipped_by_attribute(named, cached, root, env)
    return [p for p in named if p not in marked]


def is_binary(content):
    return b"\0" in content[:BINARY_SNIFF_BYTES]


def is_generated(head):
    """Whether one of the first lines carries a generated-code marker."""
    lines = head.split(b"\n", GENERATED_SNIFF_LINES)[:GENERATED_SNIFF_LINES]
    return any(GENERATED_MARKER.search(line) for line in lines)


def is_authored(head):
    """Whether the start of a file reads as text a person writes: not binary, not marked generated."""
    return not is_binary(head) and not is_generated(head)


def object_sizes(refs, root, env):
    """The size in bytes of each object named `REV:path`, from one `git cat-file --batch-check`."""
    if not refs:
        return []
    stdin = "".join(f"{ref}\n" for ref in refs).encode("utf-8", "surrogateescape")
    lines = decode(git("cat-file", "--batch-check=%(objectsize)", cwd=root, stdin=stdin, env=env)).splitlines()
    if len(lines) != len(refs) or not all(line.isdigit() for line in lines):
        raise GitError(f"git cat-file --batch-check could not size {', '.join(refs)}")
    return [int(line) for line in lines]


def worktree_size(root, path):
    """Bytes in a worktree file, or in a symlink's text as Git stores it."""
    full = os.path.join(root, path)
    return len(os.fsencode(os.readlink(full))) if os.path.islink(full) else os.path.getsize(full)


def new_sizes(comparison, paths, root):
    """Each path's size in the new version."""
    if comparison.new_side is NewSide.WORKTREE:
        return {path: worktree_size(root, path) for path in paths}
    sizes = object_sizes([comparison.new_ref(path) for path in paths], root, comparison.env)
    return dict(zip(paths, sizes))


def old_sizes(comparison, changed, root):
    """The old size of each changed file that existed before, by its new path."""
    existing = [c for c in changed if c.old_path is not None]
    sizes = object_sizes([f"{comparison.old_rev}:{c.old_path}" for c in existing], root, comparison.env)
    return {c.path: size for c, size in zip(existing, sizes)}


def skip_bytes(stream, count, ref):
    """Read and drop count bytes of the object `ref`, so a large blob is never held in memory."""
    while count > 0:
        chunk = stream.read(min(count, SKIP_CHUNK_BYTES))
        if not chunk:
            raise GitError(f"git cat-file --batch ended inside {ref}")
        count -= len(chunk)


def read_object_head(stream, ref):
    """The first BINARY_SNIFF_BYTES of the next object in `git cat-file --batch` output."""
    header = stream.readline().rstrip(b"\n").rsplit(b" ", 2)  # "<oid> <type> <size>", or "<ref> missing"
    if len(header) != 3 or not header[2].isdigit():
        raise GitError(f"git cat-file --batch could not read {ref}")
    size = int(header[2])
    head = stream.read(min(size, BINARY_SNIFF_BYTES))
    skip_bytes(stream, size - len(head) + 1, ref)  # the rest of the object, and the newline that ends it
    return head


def object_heads(refs, root, env):
    """The start of each object named `REV:path`, streamed from one `git cat-file --batch`."""
    if not refs:
        return []
    with tempfile.TemporaryFile() as stdin:  # a file, so a long ref list cannot fill a pipe while git waits on us
        stdin.write("".join(f"{ref}\n" for ref in refs).encode("utf-8", "surrogateescape"))
        stdin.seek(0)
        with subprocess.Popen(["git", "cat-file", "--batch"], cwd=root, stdin=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env) as proc:
            try:
                heads = [read_object_head(proc.stdout, ref) for ref in refs]
            except GitError as exc:
                proc.kill()
                detail = proc.communicate()[1].decode(errors="replace").strip()
                raise GitError(f"{exc}: {detail}" if detail else str(exc)) from exc
            _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise GitError(f"git cat-file --batch: {stderr.decode(errors='replace').strip()}")
    return heads


def worktree_head(root, path):
    """The start of a worktree file, or of a symlink's text as Git stores it."""
    full = os.path.join(root, path)
    if os.path.islink(full):
        return os.fsencode(os.readlink(full))[:BINARY_SNIFF_BYTES]
    with open(full, "rb") as f:
        return f.read(BINARY_SNIFF_BYTES)


def new_heads(comparison, paths, root):
    """The start of each path's new version, enough to tell binary from text and to see a generated-code marker."""
    if comparison.new_side is NewSide.WORKTREE:
        return {path: worktree_head(root, path) for path in paths}
    heads = object_heads([comparison.new_ref(path) for path in paths], root, comparison.env)
    return dict(zip(paths, heads))


def findings_over_cap(comparison, changed, root):
    """A Finding for each change whose new version is an authored text file over the cap."""
    sizes = new_sizes(comparison, [c.path for c in changed], root)
    over = [c for c in changed if sizes[c.path] > LIMIT_BYTES]
    heads = new_heads(comparison, [c.path for c in over], root)
    authored = [c for c in over if is_authored(heads[c.path])]
    old = old_sizes(comparison, authored, root)
    return [Finding(c.path, sizes[c.path], old.get(c.path)) for c in authored]


def check(comparison, root):
    """(Files that are new or grew while over the cap, files over the cap that did not grow)."""
    found = changes(comparison, root)
    kept = set(candidates([c.path for c in found], comparison.cached, root, comparison.env))
    findings = findings_over_cap(comparison, [c for c in found if c.path in kept], root)
    grew = [f for f in findings if f.old_size is None or f.size > f.old_size]
    return grew, [f for f in findings if f not in grew]


def report(paths, cwd):
    root = repo_root(cwd)
    listed = decode(git("ls-files", "-z", "--full-name", "--", *paths, cwd=cwd)).split("\0")
    over = []
    for path in candidates([p for p in listed if p], False, root):
        full = os.path.join(root, path)
        if os.path.isfile(full) and os.path.getsize(full) > LIMIT_BYTES:
            with open(full, "rb") as f:
                if is_authored(f.read(BINARY_SNIFF_BYTES)):
                    over.append(Finding(path, os.path.getsize(full), None))
    return sorted(over, key=lambda f: -f.size)


def cap_label():
    return f"the {LIMIT_BYTES:,}-byte cap (about {LIMIT_BYTES // BYTES_PER_TOKEN:,} tokens)"


def describe(finding):
    return f"  {finding.path}: {finding.size:,} bytes"


def describe_change(finding):
    history = "new" if finding.old_size is None else f"was {finding.old_size:,}"
    return f"{describe(finding)}, {history}"


def failure_message(grew):
    lines = [f"file-size: {len(grew)} file(s) over {cap_label()}:"] + [describe_change(f) for f in grew]
    return "\n".join(lines + [SPLIT_ADVICE])


def warning_message(unchanged):
    lines = [f"file-size: {len(unchanged)} file(s) already over {cap_label()} pass because they did not grow; "
             "split one when you next change it substantially:"]
    return "\n".join(lines + [describe_change(f) for f in unchanged])


def comparison_for(args, root):
    if args.base is not None:
        return base_sides(args.base, root)
    return tracked_sides(root) if args.mode == "tracked" else staged_sides(root)


def run_report(paths):
    over = report(paths, os.getcwd())
    print("\n".join(describe(f) for f in over) or f"No file over {cap_label()}.")
    return 0


def run_check(args):
    root = repo_root(os.getcwd())
    grew, unchanged = check(comparison_for(args, root), root)
    if unchanged:
        print(warning_message(unchanged))
    if grew:
        print(failure_message(grew))
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="file-size", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    checking = sub.add_parser("check", help="fail on files over the cap that are new or grew")
    which = checking.add_mutually_exclusive_group(required=True)
    which.add_argument("--staged", action="store_const", const="staged", dest="mode")
    which.add_argument("--tracked", action="store_const", const="tracked", dest="mode")
    which.add_argument("--base", metavar="REF")
    sub.add_parser("report", help="list tracked files over the cap").add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    try:
        return run_report(args.paths) if args.command == "report" else run_check(args)
    except (GitError, OSError) as exc:
        print(f"file-size: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

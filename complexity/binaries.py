"""Install the pinned release binaries in binaries.json into ~/.local/bin.

    python3 binaries.py MANIFEST BACKUP [--apply]

Each entry names a GitHub repository, a release tag, and the sha256 of the
`<name>-<target>.tar.gz` asset for each target this fleet runs. The binary is
the archive's top-level `<name>`, or the entry's `member` path, where `{target}`
stands for the target. It unpacks to
~/.local/share/fleet/bin/<name>/<tag>/<name>, and ~/.local/bin/<name>
links to it, so changing the tag swaps the binary and keeps the old one for a
rollback. A file already at ~/.local/bin/<name> that is not this link (a
`cargo install`, a manual copy) moves to BACKUP first.

Prints one plan line per change, in bin/sync's format. A download whose
checksum differs from the manifest is refused and nothing is installed.
Downloads go through `gh release download`, so a private repository's release
(fleet's own) works wherever gh is signed in, or GH_TOKEN is set in CI.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HOME = Path.home()
BIN = HOME / ".local" / "bin"
STORE = HOME / ".local" / "share" / "fleet" / "bin"
TARGETS = {("Darwin", "arm64"): "aarch64-apple-darwin", ("Linux", "x86_64"): "x86_64-unknown-linux-gnu"}


def say(verb: str, text: str) -> None:
    print(f"  {verb:<10} {text}", flush=True)


def short(path: Path) -> str:
    return f"~/{path.relative_to(HOME)}" if path.is_relative_to(HOME) else str(path)


def download(repo: str, tag: str, asset: str, expected: str, into: Path) -> Path:
    archive = into / "asset.tar.gz"
    fetched = subprocess.run(["gh", "release", "download", tag, "--repo", repo, "--pattern", asset,
                              "--output", str(archive), "--clobber"], capture_output=True, text=True)
    if fetched.returncode != 0:
        raise SystemExit(f"{repo} {tag} {asset}: gh release download failed: {fetched.stderr.strip()}")
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f"{repo} {tag} {asset}: sha256 {actual} does not match binaries.json ({expected})")
    return archive


def unpack(archive: Path, path: str, destination: Path) -> None:
    with tarfile.open(archive) as tar:
        member = tar.getmember(path)
        if not member.isfile():
            raise SystemExit(f"{archive}: {path} is not a regular file")
        source = tar.extractfile(member)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(".partial")
        with open(partial, "wb") as out:
            shutil.copyfileobj(source, out)
        partial.chmod(0o755)
        partial.replace(destination)


def retire(path: Path, backup: Path) -> None:
    destination = backup / path.relative_to(HOME)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), destination)


def install(name: str, entry: dict, target: str, backup: Path, apply: bool) -> None:
    binary = STORE / name / entry["tag"] / name
    link = BIN / name
    if link.is_symlink() and link.readlink() == binary and binary.exists():
        return
    replace_link(link, backup, apply)
    say("binary", f"{name} {entry['tag']} ({target})")
    if not apply:
        return
    fetch_binary(name, entry, target, binary)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(binary)


def replace_link(link: Path, backup: Path, apply: bool) -> None:
    if link.exists() or link.is_symlink():
        say("replace", short(link))
        if apply:
            retire(link, backup)


def fetch_binary(name: str, entry: dict, target: str, binary: Path) -> None:
    if not binary.exists():
        asset = f"{name}-{target}.tar.gz"
        member = entry.get("member", name).replace("{target}", target)
        with tempfile.TemporaryDirectory() as scratch:
            unpack(download(entry["repo"], entry["tag"], asset, entry["sha256"][target], Path(scratch)), member, binary)


def main(argv: list[str]) -> int:
    manifest, backup, apply = json.loads(Path(argv[0]).read_text()), Path(argv[1]), "--apply" in argv
    target = TARGETS.get((platform.system(), platform.machine()))
    if target is None:
        say("warning", f"no release target for {platform.system()} {platform.machine()}; skipping binaries")
        return 0
    for name, entry in manifest["binaries"].items():
        if target not in entry["sha256"]:
            say("warning", f"{name} {entry['tag']} has no {target} build; skipping")
            continue
        install(name, entry, target, backup, apply)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

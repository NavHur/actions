"""Install the skill's pinned oxlint into SKILL/node_modules from the npm registry, without a package manager.

    python3 oxlint.py SKILL

pnpm took 6-13 s per CI job, most of it `npx pnpm@10` fetching pnpm itself; the two tarballs oxlint needs take
under a second. The version comes from SKILL/package.json and each tarball's sha512 from SKILL/pnpm-lock.yaml,
so the pin stays wherever fleet sets it, and a download whose hash differs is refused before anything unpacks.
The layout is what measure_js.py looks for: node_modules/.bin/oxlint, with oxlint's native binding beside it.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import platform
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REGISTRY = "https://registry.npmjs.org"
FETCH_ATTEMPTS = 3  # pnpm retried failed fetches too
RETRY_BACKOFF_SECONDS = 1  # doubles after each failed attempt
# The targets binaries.py installs the rest of the action's analyzers for; pnpm would pick the same bindings.
BINDINGS = {("Linux", "x86_64"): "@oxlint/binding-linux-x64-gnu", ("Darwin", "arm64"): "@oxlint/binding-darwin-arm64"}


def integrity(lockfile: str, package: str, version: str) -> str:
    """The sha512 digest the lockfile records for package@version, as base64."""
    match = re.search(rf"^  '?{re.escape(package)}@{re.escape(version)}'?:\n    resolution: "
                      r"\{integrity: sha512-([A-Za-z0-9+/=]+)\}", lockfile, re.MULTILINE)
    if match is None:
        sys.exit(f"oxlint.py: pnpm-lock.yaml has no sha512 integrity for {package}@{version}")
    return match.group(1)


def tarball_url(package: str, version: str) -> str:
    return f"{REGISTRY}/{package}/-/{package.split('/')[-1]}-{version}.tgz"


def download_once(package: str, version: str) -> bytes:
    """One download attempt; a 4xx is the caller's mistake, so it exits here instead of being retried."""
    try:
        with urllib.request.urlopen(tarball_url(package, version), timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code < 500:
            sys.exit(f"oxlint.py: could not download {package}@{version}: {error}")
        raise


def download(package: str, version: str) -> bytes:
    """Retry network failures and HTTP 5xx, as pnpm did; an IncompleteRead is an HTTPException, not an OSError."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return download_once(package, version)
        except (OSError, http.client.HTTPException) as error:
            last_error = error
        if attempt < FETCH_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))
    sys.exit(f"oxlint.py: could not download {package}@{version}: {last_error}")


def fetch(package: str, version: str, expected: str) -> bytes:
    data = download(package, version)
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != expected:
        sys.exit(f"oxlint.py: {package}@{version} has sha512-{actual}, but pnpm-lock.yaml pins sha512-{expected}")
    return data


def retire(path: Path) -> None:
    """Remove path without following it: a symlink to a shared install goes, its target stays."""
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def unpack(data: bytes, into: Path) -> None:
    """npm tarballs hold one top-level `package/` directory; its contents become `into`.

    No extraction filter: the lockfile's sha512 already pinned these exact bytes, and the filter needs Python 3.12.
    """
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = [m for m in archive.getmembers() if m.name.startswith("package/")]
        for member in members:
            member.name = member.name.removeprefix("package/")
        into.mkdir(parents=True)
        archive.extractall(into, members=members)


def main(skill: Path) -> None:
    binding = BINDINGS.get((platform.system(), platform.machine()))
    if binding is None:
        sys.exit(f"oxlint.py: no oxlint binding for {platform.system()} {platform.machine()}")
    version = json.loads((skill / "package.json").read_text())["devDependencies"]["oxlint"]
    lockfile = (skill / "pnpm-lock.yaml").read_text()
    packages = ["oxlint", binding]
    with ThreadPoolExecutor(len(packages)) as pool:
        tarballs = list(pool.map(lambda p: fetch(p, version, integrity(lockfile, p, version)), packages))
    modules = skill / "node_modules"
    retire(modules)  # whatever an earlier install, or a pnpm install in the copied skill, left
    for package, data in zip(packages, tarballs):
        unpack(data, modules / package)
    (modules / ".bin").mkdir()
    (modules / ".bin" / "oxlint").symlink_to("../oxlint/bin/oxlint")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

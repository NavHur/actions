"""oxlint.py's install and its refusals, against in-memory tarballs: no network.

Run from the repository root: python3 -m unittest discover -s complexity -p "test_oxlint.py"
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import oxlint

VERSION = "1.2.3"
BINDING = "@oxlint/binding-linux-x64-gnu"


def tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def sha512(data: bytes) -> str:
    return base64.b64encode(hashlib.sha512(data).digest()).decode()


TARBALLS = {
    "oxlint": tarball({"bin/oxlint": b"#!/usr/bin/env node\n", "package.json": b"{}"}),
    BINDING: tarball({"oxlint.node": b"binding"}),
}


def lockfile(integrities: dict[str, str]) -> str:
    return "".join(f"  '{p}@{VERSION}':\n    resolution: {{integrity: sha512-{i}}}\n" for p, i in integrities.items())


def fake_urlopen(served: dict[str, bytes]):
    def urlopen(url, timeout):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = served[url]
        return response
    return urlopen


class Install(unittest.TestCase):

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.skill = Path(scratch.name)
        (self.skill / "package.json").write_text(json.dumps({"devDependencies": {"oxlint": VERSION}}))
        self.write_lock({p: sha512(data) for p, data in TARBALLS.items()})
        self.served = {oxlint.tarball_url(p, VERSION): data for p, data in TARBALLS.items()}
        for target, value in [("platform.system", "Linux"), ("platform.machine", "x86_64")]:
            patcher = mock.patch(f"oxlint.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("oxlint.urllib.request.urlopen", new=fake_urlopen(self.served))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_lock(self, integrities: dict[str, str]):
        (self.skill / "pnpm-lock.yaml").write_text(lockfile(integrities))

    def test_links_the_bin_to_the_package(self):
        oxlint.main(self.skill)
        link = self.skill / "node_modules" / ".bin" / "oxlint"
        self.assertEqual(link.resolve(), (self.skill / "node_modules" / "oxlint" / "bin" / "oxlint").resolve())
        self.assertTrue((self.skill / "node_modules" / BINDING / "oxlint.node").is_file())

    def test_a_second_run_converges(self):
        oxlint.main(self.skill)
        stale = self.skill / "node_modules" / "oxlint" / "stale"
        stale.write_text("left by an older version")
        oxlint.main(self.skill)
        link = self.skill / "node_modules" / ".bin" / "oxlint"
        self.assertEqual(link.resolve(), (self.skill / "node_modules" / "oxlint" / "bin" / "oxlint").resolve())
        self.assertFalse(stale.exists())

    def test_a_failed_fetch_leaves_the_prior_install_alone(self):
        oxlint.main(self.skill)
        self.served[oxlint.tarball_url(BINDING, VERSION)] = b"tampered"
        with self.assertRaises(SystemExit):
            oxlint.main(self.skill)
        self.assertTrue((self.skill / "node_modules" / "oxlint" / "bin" / "oxlint").is_file())
        self.assertTrue((self.skill / "node_modules" / ".bin" / "oxlint").is_symlink())

    def test_refuses_a_lockfile_without_integrity(self):
        self.write_lock({"oxlint": sha512(TARBALLS["oxlint"])})
        with self.assertRaises(SystemExit) as refused:
            oxlint.main(self.skill)
        self.assertIn(f"no sha512 integrity for {BINDING}@{VERSION}", str(refused.exception))
        self.assertFalse((self.skill / "node_modules").exists())

    def test_refuses_a_hash_mismatch(self):
        self.served[oxlint.tarball_url("oxlint", VERSION)] = b"tampered"
        with self.assertRaises(SystemExit) as refused:
            oxlint.main(self.skill)
        self.assertIn(f"oxlint@{VERSION} has sha512-{sha512(b'tampered')}", str(refused.exception))
        self.assertFalse((self.skill / "node_modules").exists())

    def test_refuses_an_unsupported_platform(self):
        with mock.patch("oxlint.platform.system", return_value="Windows"):
            with self.assertRaises(SystemExit) as refused:
                oxlint.main(self.skill)
        self.assertIn("no oxlint binding for Windows", str(refused.exception))
        self.assertFalse((self.skill / "node_modules").exists())


if __name__ == "__main__":
    unittest.main()

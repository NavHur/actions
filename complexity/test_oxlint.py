"""oxlint.py's install and its refusals, against in-memory tarballs: no network.

Run from the repository root: python3 -m unittest discover -s complexity -p "test_oxlint.py"
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import tarfile
import tempfile
import unittest
import urllib.error
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

    def pnpm_install(self, modules: Path):
        """pnpm's layout: node_modules/oxlint is a symlink into .pnpm, and .bin/oxlint a plain shim."""
        store = modules / ".pnpm" / f"oxlint@{VERSION}" / "node_modules" / "oxlint"
        (store / "bin").mkdir(parents=True)
        (store / "bin" / "oxlint").write_text("from the pnpm store")
        (modules / "oxlint").symlink_to(store.relative_to(modules), target_is_directory=True)
        (modules / ".bin").mkdir()
        (modules / ".bin" / "oxlint").write_text("#!/bin/sh\n# pnpm's shim\n")

    def test_it_replaces_a_pnpm_install(self):
        modules = self.skill / "node_modules"
        self.pnpm_install(modules)
        oxlint.main(self.skill)
        self.assertFalse((modules / "oxlint").is_symlink())
        self.assertEqual((modules / "oxlint" / "bin" / "oxlint").read_bytes(), b"#!/usr/bin/env node\n")
        self.assertTrue((modules / ".bin" / "oxlint").is_symlink())

    def test_a_node_modules_symlinked_to_a_shared_install_leaves_that_install_alone(self):
        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        self.pnpm_install(Path(shared.name))
        (self.skill / "node_modules").symlink_to(shared.name, target_is_directory=True)
        oxlint.main(self.skill)
        self.assertFalse((self.skill / "node_modules").is_symlink())
        self.assertTrue((self.skill / "node_modules" / "oxlint" / "bin" / "oxlint").is_file())
        self.assertTrue((Path(shared.name) / "oxlint").is_symlink())
        self.assertEqual((Path(shared.name) / ".bin" / "oxlint").read_text(), "#!/bin/sh\n# pnpm's shim\n")

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

    def flaky_urlopen(self, failures: list):
        """Raise each failure in turn for oxlint's tarball, then serve; self.calls counts requests for it."""
        served = fake_urlopen(self.served)
        pending = list(failures)
        self.calls = 0

        def urlopen(url, timeout):
            if url == oxlint.tarball_url("oxlint", VERSION):
                self.calls += 1
                if pending:
                    raise pending.pop(0)
            return served(url, timeout)
        return urlopen

    def install_with(self, failures: list):
        urlopen = self.flaky_urlopen(failures)
        with mock.patch("oxlint.urllib.request.urlopen", new=urlopen), \
                mock.patch("oxlint.time.sleep") as sleep:
            oxlint.main(self.skill)
        return sleep

    def http_error(self, code: int):
        return urllib.error.HTTPError(oxlint.tarball_url("oxlint", VERSION), code, "status", None, None)

    def test_retries_a_transient_failure(self):
        for failure in [urllib.error.URLError("reset"), self.http_error(503), TimeoutError("slow"),
                        http.client.IncompleteRead(b"par")]:
            with self.subTest(failure=type(failure).__name__):
                self.install_with([failure])
                self.assertEqual(self.calls, 2)
                self.assertTrue((self.skill / "node_modules" / "oxlint" / "bin" / "oxlint").is_file())

    def test_gives_up_after_three_attempts(self):
        with self.assertRaises(SystemExit) as refused:
            self.install_with([urllib.error.URLError("down")] * 3)
        self.assertIn(f"could not download oxlint@{VERSION}", str(refused.exception))
        self.assertIn("down", str(refused.exception))
        self.assertEqual(self.calls, 3)
        self.assertFalse((self.skill / "node_modules").exists())

    def test_does_not_retry_a_404(self):
        with mock.patch("oxlint.time.sleep") as sleep, self.assertRaises(SystemExit) as refused:
            urlopen = self.flaky_urlopen([self.http_error(404)])
            with mock.patch("oxlint.urllib.request.urlopen", new=urlopen):
                oxlint.main(self.skill)
        self.assertEqual(self.calls, 1)
        self.assertIn(f"could not download oxlint@{VERSION}", str(refused.exception))
        sleep.assert_not_called()

    def test_refuses_an_unsupported_platform(self):
        with mock.patch("oxlint.platform.system", return_value="Windows"):
            with self.assertRaises(SystemExit) as refused:
                oxlint.main(self.skill)
        self.assertIn("no oxlint binding for Windows", str(refused.exception))
        self.assertFalse((self.skill / "node_modules").exists())


if __name__ == "__main__":
    unittest.main()

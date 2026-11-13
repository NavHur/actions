import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import ci_cache  # noqa: E402


class Checkout:
    """A git checkout of a tiny Godot project, with its own CI state directory."""

    def __init__(self, path, state):
        self.path = path
        self.env = dict(os.environ, FLEET_CI_STATE=str(state), GITHUB_REPOSITORY="owner/game")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.commit(
            {
                "project.godot": "config_version=5\n",
                "art/hero.png": "png",
                "art/hero.png.import": "[remap]\n",
                "main.gd": "extends Node\n",
            }
        )

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.path, check=True, capture_output=True)

    def commit(self, files):
        for name, text in files.items():
            (self.path / name).parent.mkdir(parents=True, exist_ok=True)
            (self.path / name).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "commit")

    def godot_dir(self):
        (self.path / ".godot").mkdir(exist_ok=True)

    def run_raw(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "ci_cache.py"), *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def run(self, *args):
        """The command's outputs as a dict, read from the GITHUB_OUTPUT file it appended to."""
        with tempfile.NamedTemporaryFile("r") as output:
            subprocess.run(
                [sys.executable, str(ROOT / "ci_cache.py"), *args],
                cwd=self.path,
                check=True,
                capture_output=True,
                env=dict(self.env, GITHUB_OUTPUT=output.name),
            )
            return dict(line.rstrip("\n").split("=", 1) for line in output)


class CiCacheTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        (self.temp / "work").mkdir()
        self.repo = Checkout(self.temp / "work", self.temp / "state")

    def test_a_recorded_tree_passes_until_the_tree_or_key_changes(self):
        self.assertEqual(self.repo.run("passed-check", "gate")["hit"], "false")
        self.repo.run("passed-record", "gate")
        self.assertEqual(self.repo.run("passed-check", "gate")["hit"], "true")
        self.assertEqual(self.repo.run("passed-check", "gate-godot-4.8")["hit"], "false")
        self.repo.commit({"main.gd": "extends Node2D\n"})
        self.assertEqual(self.repo.run("passed-check", "gate")["hit"], "false")

    def test_a_merge_commit_with_the_tested_tree_passes(self):
        self.repo.git("switch", "-q", "-c", "feature")
        self.repo.commit({"main.gd": "extends Node2D\n"})
        self.repo.run("passed-record", "gate")
        self.repo.git("switch", "-q", "main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "merge", "feature")
        self.assertEqual(self.repo.run("passed-check", "gate")["hit"], "true")

    def test_restore_pins_assets_and_round_trips_the_import_cache(self):
        first = self.repo.run("godot-restore", "godot-4.7.1")
        self.assertEqual(first["hit"], "false")
        self.assertTrue(first["key"].startswith("godot-4.7.1-"))
        self.assertEqual(os.stat(self.repo.path / "art/hero.png").st_mtime, 0)
        (self.repo.path / ".godot/imported").mkdir(parents=True)
        (self.repo.path / ".godot/imported/hero.ctex").write_text("texture")
        self.repo.run("godot-save", first["key"])

        (self.temp / "clone").mkdir()
        clone = Checkout(self.temp / "clone", self.temp / "state")
        second = clone.run("godot-restore", "godot-4.7.1")
        self.assertEqual(second, {"hit": "true", "key": first["key"]})
        self.assertEqual((clone.path / ".godot/imported/hero.ctex").read_text(), "texture")

    def test_a_changed_asset_misses(self):
        self.repo.godot_dir()
        key = self.repo.run("godot-restore", "godot-4.7.1")["key"]
        self.repo.run("godot-save", key)
        self.repo.commit({"art/hero.png": "new png"})
        self.assertEqual(self.repo.run("godot-restore", "godot-4.7.1")["hit"], "false")

    def test_a_csv_source_with_an_import_file_is_pinned_and_keyed(self):
        self.repo.commit({"data/items.csv": "a,b\n", "data/items.csv.import": "[remap]\n"})
        before = self.repo.run("godot-restore", "godot-4.7.1")["key"]
        self.assertEqual(os.stat(self.repo.path / "data/items.csv").st_mtime, 0)
        self.assertEqual(os.stat(self.repo.path / "data/items.csv.import").st_mtime, 0)
        self.repo.commit({"data/items.csv": "a,b,c\n"})
        self.assertNotEqual(self.repo.run("godot-restore", "godot-4.7.1")["key"], before)

    def test_a_project_that_ignores_import_files_still_pins_and_keys_its_images(self):
        self.repo.git("rm", "-q", "--cached", "art/hero.png.import")
        self.repo.commit({".gitignore": "*.import\n"})
        self.repo.godot_dir()
        key = self.repo.run("godot-restore", "godot-4.7.1")["key"]
        self.assertEqual(os.stat(self.repo.path / "art/hero.png").st_mtime, 0)
        self.repo.run("godot-save", key)
        self.repo.commit({"art/hero.png": "new png"})
        self.assertEqual(self.repo.run("godot-restore", "godot-4.7.1")["hit"], "false")

    def test_a_gdignored_png_stays_out_of_the_key_when_import_files_are_tracked(self):
        self.repo.commit({"assets/source/.gdignore": "", "assets/source/art.png": "one"})
        before = self.repo.run("godot-restore", "godot-4.7.1")["key"]
        self.repo.commit({"assets/source/art.png": "two"})
        self.assertEqual(self.repo.run("godot-restore", "godot-4.7.1")["key"], before)

    def test_a_png_without_a_tracked_import_file_is_keyed_when_other_import_files_are_tracked(self):
        self.repo.commit({"art/ignored.png": "one"})
        before = self.repo.run("godot-restore", "godot-4.7.1")["key"]
        self.assertEqual(os.stat(self.repo.path / "art/ignored.png").st_mtime, 0)
        self.repo.commit({"art/ignored.png": "two"})
        self.assertNotEqual(self.repo.run("godot-restore", "godot-4.7.1")["key"], before)

    def test_gdignored_directories_stay_out_of_the_key_when_import_files_are_ignored(self):
        self.repo.git("rm", "-q", "--cached", "art/hero.png.import")
        self.repo.commit({".gitignore": "*.import\n", "docs/.gdignore": "", "docs/shot.png": "one"})
        before = self.repo.run("godot-restore", "godot-4.7.1")["key"]
        self.repo.commit({"docs/shot.png": "two"})
        self.assertEqual(self.repo.run("godot-restore", "godot-4.7.1")["key"], before)
        self.repo.commit({"art/hero.png": "new png"})
        self.assertNotEqual(self.repo.run("godot-restore", "godot-4.7.1")["key"], before)

    def test_save_without_a_godot_directory_fails_and_saves_nothing(self):
        result = self.repo.run_raw("godot-save", "key")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ci-cache:", result.stderr)
        self.assertIn(".godot", result.stderr)
        self.assertEqual(self.entries(), [])

    def save_entries(self, count):
        """Save `count` entries with increasing mtimes, so their order is deterministic."""
        self.repo.godot_dir()
        for version in range(count):
            self.repo.run("godot-save", f"key-{version}")
            entry = self.state_entry(f"key-{version}")
            os.utime(entry, (1000 + version, 1000 + version))

    def state_entry(self, key):
        return self.temp / "state/owner/game/godot" / hashlib.sha256(key.encode()).hexdigest()

    def entries(self):
        root = self.temp / "state/owner/game/godot"
        return sorted(path for path in root.iterdir() if not path.name.startswith(".")) if root.is_dir() else []

    def test_save_keeps_exactly_the_last_three_saves(self):
        self.save_entries(5)
        self.assertEqual(self.entries(), sorted(self.state_entry(f"key-{version}") for version in (2, 3, 4)))
        leftovers = [
            path
            for path in self.state_entry("key-0").parent.iterdir()
            if path.name.startswith((".trash-", ".staging-"))
        ]
        self.assertEqual(leftovers, [])

    def test_save_reclaims_only_staging_copies_older_than_an_hour(self):
        self.save_entries(1)
        entries = self.state_entry("key-0").parent
        old, fresh = entries / ".staging-old", entries / ".staging-fresh"
        for staging in (old, fresh):
            (staging / ".godot").mkdir(parents=True)
        stale = time.time() - 2 * 3600
        os.utime(old, (stale, stale))
        self.repo.run("godot-save", "key-1")
        self.assertFalse(old.exists())
        self.assertTrue(fresh.is_dir())

    def test_a_restore_hit_keeps_its_entry_alive(self):
        self.repo.godot_dir()
        key_a = self.repo.run("godot-restore", "godot-4.7.1")["key"]
        self.repo.run("godot-save", key_a)
        os.utime(self.state_entry(key_a), (1000, 1000))
        for version, name in enumerate("BC", start=1):
            self.repo.commit({"art/hero.png": f"png {name}"})
            key = self.repo.run("godot-restore", "godot-4.7.1")["key"]
            self.repo.run("godot-save", key)
            os.utime(self.state_entry(key), (1000 + version, 1000 + version))
        self.repo.git("checkout", "-q", "HEAD~2", "--", "art/hero.png")
        self.assertEqual(self.repo.run("godot-restore", "godot-4.7.1"), {"hit": "true", "key": key_a})
        self.repo.commit({"art/hero.png": "png D"})
        key = self.repo.run("godot-restore", "godot-4.7.1")["key"]
        self.repo.run("godot-save", key)
        self.repo.git("checkout", "-q", "HEAD~3", "--", "art/hero.png")
        self.assertEqual(self.repo.run("godot-restore", "godot-4.7.1")["hit"], "true")

    def test_a_restore_in_progress_holds_off_pruning_until_the_next_save(self):
        self.save_entries(1)
        with open(self.state_entry("key-0").parent / ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)  # another runner's restore, mid-copy
            self.save_entries(5)
            self.assertEqual(len(self.entries()), 5)
        self.repo.run("godot-save", "key-5")
        self.assertEqual(len(self.entries()), 3)

    def test_a_save_that_loses_the_race_for_its_key_keeps_the_winners_entry(self):
        self.repo.godot_dir()
        entry = self.state_entry("key")
        make_staging = tempfile.mkdtemp

        def concurrent_save_lands_first(**kwargs):
            staging = make_staging(**kwargs)
            (entry / ".godot").mkdir(parents=True)
            (entry / ".godot/winner").write_text("")
            return staging

        cwd = os.getcwd()
        os.chdir(self.repo.path)
        try:
            with (
                mock.patch.dict(os.environ, self.repo.env),
                mock.patch.object(ci_cache.tempfile, "mkdtemp", concurrent_save_lands_first),
            ):
                ci_cache.main(["godot-save", "key"])
        finally:
            os.chdir(cwd)
        self.assertEqual(os.listdir(entry / ".godot"), ["winner"])
        self.assertEqual([path.name for path in entry.parent.iterdir() if path.name.startswith(".staging-")], [])

    def test_pruning_survives_a_sibling_save_publishing_its_staging_copy(self):
        entries = self.temp / "entries"
        entries.mkdir()
        published = entries / ".staging-published"  # listed, then renamed to its entry before the stat
        with mock.patch.object(ci_cache.Path, "glob", return_value=[published]):
            ci_cache.reap_staging(entries)


if __name__ == "__main__":
    unittest.main()

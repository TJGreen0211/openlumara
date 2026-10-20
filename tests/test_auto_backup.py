"""
Self-contained unit test for modules/auto_backup.py (audit 2.5):
  - _create_zip zips the data folder, skips a backup dir nested inside it
    (separator-aware), writes atomically via .tmp, and cleans up on failure.
  - _prune_backups rotates to the newest keep_backups zips (0 = keep all).
  - keep_backups setting exists.

It does NOT import the real `core` package. Instead it injects lightweight
stand-ins into sys.modules, like tests/test_html_preview.py.

Run:  venv\\Scripts\\python tests\\test_auto_backup.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class _FakeConfig:
    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)


class _ModuleBase:
    def __init__(self):
        self.name = "under_test"
        self.config = _FakeConfig({})

    def result(self, data, success=True):
        return {"success": success, "data": data}


def _command(*args, **kwargs):
    def decorator(fn):
        return fn
    return decorator


_core_mod = types.ModuleType("core")
_core_mod.debug = False
_core_mod.log = lambda category, msg: None
_core_mod.get_path = lambda p: os.path.abspath(p)
_core_mod.detail_error = lambda e: str(e)
_core_mod.current_user = types.SimpleNamespace(get=lambda: None)
sys.modules["core"] = _core_mod

_module_mod = types.ModuleType("core.module")
_module_mod.Module = _ModuleBase
_module_mod.command = _command
_core_mod.module = _module_mod

_spec = importlib.util.spec_from_file_location(
    "ol_auto_backup_under_test", os.path.join(ROOT, "modules", "auto_backup.py"))
ab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ab)


def _make_module(backup_path, keep=10):
    m = ab.AutoBackup.__new__(ab.AutoBackup)
    m.name = "auto_backup"
    m.config = _FakeConfig({"keep_backups": keep})
    m.backup_path = backup_path
    m._log = lambda *args: None
    return m


class CreateZipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_backup_")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.data, "chats"))
        with open(os.path.join(self.data, "chats", "a.txt"), "w") as f:
            f.write("hello")
        self.backup = os.path.join(self.tmp, "backups")
        os.makedirs(self.backup)
        self.module = _make_module(self.backup)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zips_data_folder(self):
        zip_path = os.path.join(self.backup, "openlumara_backup_x.zip")
        size = self.module._create_zip(self.data, zip_path)
        self.assertGreater(size, 0)
        with zipfile.ZipFile(zip_path) as z:
            self.assertIn(os.path.join("chats", "a.txt").replace("\\", "/"),
                          [n.replace("\\", "/") for n in z.namelist()])

    def test_excludes_session_secret(self):
        with open(os.path.join(self.data, "session_secret"), "w") as f:
            f.write("signing-key")
        # only the data-root file is the webui key; same name elsewhere is kept
        with open(os.path.join(self.data, "chats", "session_secret"), "w") as f:
            f.write("not the key")

        zip_path = os.path.join(self.backup, "openlumara_backup_x.zip")
        self.module._create_zip(self.data, zip_path)

        with zipfile.ZipFile(zip_path) as z:
            names = [n.replace("\\", "/") for n in z.namelist()]
        self.assertNotIn("session_secret", names)
        self.assertIn("chats/session_secret", names)

    def test_skips_nested_backup_dir(self):
        # backup dir inside the data folder must not be zipped into the backup
        nested = os.path.join(self.data, "backups")
        os.makedirs(nested)
        with open(os.path.join(nested, "openlumara_backup_old.zip"), "wb") as f:
            f.write(b"old")
        self.module.backup_path = nested

        zip_path = os.path.join(nested, "openlumara_backup_new.zip")
        self.module._create_zip(self.data, zip_path)

        with zipfile.ZipFile(zip_path) as z:
            names = [n.replace("\\", "/") for n in z.namelist()]
        self.assertFalse(any("backups" in n for n in names), names)

    def test_sibling_prefix_dir_not_skipped(self):
        # /tmp/x/backups_extra shares the "backups" string prefix but is not
        # inside the backup dir - it must be included
        sibling = os.path.join(self.tmp, "backups_extra")
        os.makedirs(sibling)
        with open(os.path.join(sibling, "keep.txt"), "w") as f:
            f.write("keep")

        zip_path = os.path.join(self.backup, "openlumara_backup_x.zip")
        self.module._create_zip(self.tmp, zip_path)

        with zipfile.ZipFile(zip_path) as z:
            names = [n.replace("\\", "/") for n in z.namelist()]
        self.assertIn("backups_extra/keep.txt", names)

    def test_failure_leaves_no_partial_zip(self):
        zip_path = os.path.join(self.backup, "openlumara_backup_x.zip")
        with mock.patch("zipfile.ZipFile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.module._create_zip(self.data, zip_path)
        self.assertFalse(os.path.exists(zip_path))
        self.assertFalse(os.path.exists(zip_path + ".tmp"))


class PruneBackupsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_prune_")
        for i in range(5):
            path = os.path.join(self.tmp, f"openlumara_backup_000{i}.zip")
            with open(path, "w") as f:
                f.write(f"zip {i}")
            os.utime(path, (1000 + i * 100, 1000 + i * 100))
        # a non-backup file that must never be touched
        with open(os.path.join(self.tmp, "important.txt"), "w") as f:
            f.write("keep me")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remaining(self):
        return sorted(os.listdir(self.tmp))

    def test_rotates_to_newest_keep(self):
        _make_module(self.tmp, keep=2)._prune_backups()
        self.assertEqual(self._remaining(), [
            "important.txt", "openlumara_backup_0003.zip", "openlumara_backup_0004.zip"])

    def test_keep_zero_keeps_all(self):
        _make_module(self.tmp, keep=0)._prune_backups()
        self.assertEqual(len(self._remaining()), 6)

    def test_keep_above_count_deletes_nothing(self):
        _make_module(self.tmp, keep=10)._prune_backups()
        self.assertEqual(len(self._remaining()), 6)


class SettingsTest(unittest.TestCase):
    def test_keep_backups_setting_exists(self):
        self.assertIn("keep_backups", ab.AutoBackup.settings)
        self.assertEqual(ab.AutoBackup.settings["keep_backups"]["default"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)

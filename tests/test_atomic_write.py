"""
Self-contained unit test for atomic writes in core/storage.py (audit 2.1):
  - atomic_write writes content and leaves no .tmp sibling behind.
  - a failed write/replace leaves the ORIGINAL file untouched and cleans up
    the temp file (the crash/full-disk scenario that used to truncate the
    real file in place).
  - StorageList / StorageDict / StorageText round-trip through the new path.

It does NOT import the real `core` package. Instead it injects a lightweight
stand-in `core` module into sys.modules, like tests/test_html_preview.py.

Run:  venv\\Scripts\\python tests\\test_atomic_write.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_core_mod = types.ModuleType("core")
_core_mod.debug = False
_core_mod.log = lambda category, msg: None
_core_mod.get_data_path = lambda subpath=None, user=None: tempfile.mkdtemp()
_core_mod.sandbox_path = lambda base, name: os.path.join(base, name)
sys.modules["core"] = _core_mod

_spec = importlib.util.spec_from_file_location(
    "ol_storage_under_test", os.path.join(ROOT, "core", "storage.py"))
storage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(storage)


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_atomic_")
        self.path = os.path.join(self.tmp, "target.txt")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_and_no_tmp_leftover(self):
        storage.atomic_write(self.path, "hello")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello")
        self.assertEqual(os.listdir(self.tmp), ["target.txt"])

    def test_binary_write(self):
        binpath = os.path.join(self.tmp, "target.mp")
        storage.atomic_write(binpath, b"\x00\x01\x02", binary=True)
        with open(binpath, "rb") as f:
            self.assertEqual(f.read(), b"\x00\x01\x02")
        self.assertEqual(os.listdir(self.tmp), ["target.mp"])

    def test_overwrite_replaces_fully(self):
        storage.atomic_write(self.path, "a very long original content")
        storage.atomic_write(self.path, "short")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "short")

    def test_failed_replace_preserves_original(self):
        storage.atomic_write(self.path, "original")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                storage.atomic_write(self.path, "new content")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "original")
        # temp file must be cleaned up, only the original remains
        self.assertEqual(os.listdir(self.tmp), ["target.txt"])

    def test_failed_write_preserves_original(self):
        storage.atomic_write(self.path, "original")
        with self.assertRaises(TypeError):
            storage.atomic_write(self.path, b"bytes in text mode")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "original")
        self.assertEqual(os.listdir(self.tmp), ["target.txt"])


class StorageRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_storage_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_no_tmp_files(self):
        for root, _, files in os.walk(self.tmp):
            for filename in files:
                self.assertFalse(filename.endswith(".tmp"), f"leftover tmp in {root}: {filename}")

    def test_json_list_roundtrip(self):
        sl = storage.StorageList("notes", "json", path=self.tmp)
        sl.append({"a": 1})
        sl.save()
        sl2 = storage.StorageList("notes", "json", path=self.tmp)
        self.assertEqual(list(sl2), [{"a": 1}])
        self._assert_no_tmp_files()

    def test_msgpack_dict_roundtrip(self):
        sd = storage.StorageDict("index", "msgpack", path=self.tmp)
        sd["chat"] = {"id": "01ABC", "title": "héllo"}
        sd.save()
        sd2 = storage.StorageDict("index", "msgpack", path=self.tmp)
        self.assertEqual(dict(sd2), {"chat": {"id": "01ABC", "title": "héllo"}})
        self._assert_no_tmp_files()

    def test_yaml_dict_roundtrip(self):
        sd = storage.StorageDict("config", "yaml", path=self.tmp)
        sd["core"] = {"port": 3000}
        sd.save()
        sd2 = storage.StorageDict("config", "yaml", path=self.tmp)
        self.assertEqual(dict(sd2), {"core": {"port": 3000}})
        self._assert_no_tmp_files()

    def test_text_storage_roundtrip(self):
        st = storage.StorageText("prompt", path=self.tmp)
        st.set("system prompt")
        st2 = storage.StorageText("prompt", path=self.tmp)
        self.assertEqual(st2.get(), "system prompt")
        self._assert_no_tmp_files()

    def test_markdown_storage_roundtrip(self):
        sd = storage.StorageDict("ideas", "markdown", path=self.tmp)
        sd["openlumara/topic"] = "# note"
        sd.save()
        sd2 = storage.StorageDict("ideas", "markdown", path=self.tmp)
        self.assertEqual(sd2["openlumara"]["topic"], "# note")
        self._assert_no_tmp_files()


if __name__ == "__main__":
    unittest.main(verbosity=2)

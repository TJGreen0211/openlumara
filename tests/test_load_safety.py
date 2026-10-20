"""
Self-contained unit test for load() failure safety in core/storage.py (audit 2.2):
load() used to clear() before reading, so a corrupt/truncated/unreadable file
emptied the object in memory and the next save() (get() reloads on every
access) persisted the empty state - a two-step data wipe.

Now: read + parse into a temporary first; on read/parse failure the in-memory
copy is kept and logged, and a later save() restores the file.

Run:  venv\\Scripts\\python tests\\test_load_safety.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_core_mod = types.ModuleType("core")
_core_mod.debug = False
_core_mod.log = lambda category, msg: None
_core_mod.get_data_path = lambda subpath=None, user=None: tempfile.mkdtemp()
_core_mod.sandbox_path = lambda base, name: os.path.join(base, name)
sys.modules["core"] = _core_mod

_spec = importlib.util.spec_from_file_location(
    "ol_storage_safety_under_test", os.path.join(ROOT, "core", "storage.py"))
storage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(storage)


def _bump_mtime(path):
    stat = os.stat(path)
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))


class StorageListSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_loadsafe_")
        self.path = os.path.join(self.tmp, "notes.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_corrupt_file_keeps_memory_and_save_restores(self):
        sl = storage.StorageList("notes", "json", path=self.tmp)
        sl.append({"a": 1})
        sl.save()

        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        _bump_mtime(self.path)

        sl.get(0)  # triggers load(), which used to clear the list
        self.assertEqual(list(sl), [{"a": 1}])

        sl.save()
        sl2 = storage.StorageList("notes", "json", path=self.tmp)
        self.assertEqual(list(sl2), [{"a": 1}])

    def test_truncated_empty_file_keeps_memory(self):
        sl = storage.StorageList("notes", "json", path=self.tmp)
        sl.append({"a": 1})
        sl.save()

        with open(self.path, "w", encoding="utf-8") as f:
            f.write("")
        _bump_mtime(self.path)

        sl.get(0)
        self.assertEqual(list(sl), [{"a": 1}])

    def test_deleted_file_keeps_memory_and_save_recreates(self):
        sl = storage.StorageList("notes", "json", path=self.tmp)
        sl.append({"a": 1})
        sl.save()

        os.remove(self.path)

        sl.get(0)
        self.assertEqual(list(sl), [{"a": 1}])
        sl.save()
        self.assertTrue(os.path.exists(self.path))

    def test_valid_external_change_still_reloads(self):
        sl = storage.StorageList("notes", "json", path=self.tmp)
        sl.append({"a": 1})
        sl.save()

        with open(self.path, "w", encoding="utf-8") as f:
            f.write('[{"b": 2}]')
        _bump_mtime(self.path)

        sl.get(0)
        self.assertEqual(list(sl), [{"b": 2}])

    def test_empty_text_file_loads_empty(self):
        sl = storage.StorageList("lines", "text", path=self.tmp)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("")
        _bump_mtime(self.path)
        sl.load()
        self.assertEqual(list(sl), [])


class StorageDictSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_loadsafe_d_")
        self.path = os.path.join(self.tmp, "index.mp")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_truncated_msgpack_keeps_memory_and_save_restores(self):
        sd = storage.StorageDict("index", "msgpack", path=self.tmp)
        sd["chat"] = {"id": "01ABC"}
        sd.save()

        with open(self.path, "wb") as f:
            f.write(b"\x00\x01trunc")
        _bump_mtime(self.path)

        sd.get("chat")  # triggers load()
        self.assertEqual(dict(sd), {"chat": {"id": "01ABC"}})

        sd.save()
        sd2 = storage.StorageDict("index", "msgpack", path=self.tmp)
        self.assertEqual(dict(sd2), {"chat": {"id": "01ABC"}})

    def test_empty_yaml_document_loads_empty(self):
        path = os.path.join(self.tmp, "config.yml")
        sd = storage.StorageDict("config", "yaml", path=self.tmp)
        sd["core"] = {"port": 3000}
        sd.save()

        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        _bump_mtime(path)

        sd.get("core")
        self.assertEqual(dict(sd), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)

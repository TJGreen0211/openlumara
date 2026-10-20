"""
Self-contained unit test for chat metadata/title persistence (audit 2.4):
Chat.set() and metadata mutations used to be in-memory only, so chats.organize,
/rename, character switches etc. were silently lost unless a later message
happened to save the index. set() and the new update_metadata() now persist.

It does NOT import the real `core` package. Instead it injects lightweight
stand-ins into sys.modules (reusing the REAL core/storage.py), like
tests/test_html_preview.py.

Run:  venv\\Scripts\\python tests\\test_chat_set_persistence.py
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

DATA_DIR = {"path": None}

_core_mod = types.ModuleType("core")
_core_mod.debug = False
_core_mod.log = lambda category, msg: None
_core_mod.get_data_path = lambda subpath=None, user=None: (
    os.path.join(DATA_DIR["path"], subpath) if subpath else DATA_DIR["path"])
_core_mod.sandbox_path = lambda base, name: os.path.join(base, name)
_core_mod.config = types.SimpleNamespace(get=lambda *a, **k: "test-model")
sys.modules["core"] = _core_mod

_spec = importlib.util.spec_from_file_location(
    "ol_storage_chat_under_test", os.path.join(ROOT, "core", "storage.py"))
_storage_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_storage_mod)
_core_mod.storage = _storage_mod

_spec = importlib.util.spec_from_file_location(
    "ol_chat_under_test", os.path.join(ROOT, "core", "chat.py"))
chat_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chat_mod)


class _FakeChannel:
    name = "test"


def _make_chat():
    chat = chat_mod.Chat(_FakeChannel())
    chat.data.append({
        "id": "CHAT0001", "title": "New chat", "category": "general",
        "tags": [], "token_usage": 0, "metadata": {},
        "created": "", "updated": "",
    })
    chat.current = 0
    chat.data.save()
    return chat


async def _flush_index_save(chat):
    """wait for set()'s debounced index save to land"""
    task = chat._index_save_task
    if task:
        await task


def _reload_index():
    return _storage_mod.StorageList(
        os.path.join("chats", "test", "index"), "msgpack", path=DATA_DIR["path"])


class ChatSetPersistenceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        DATA_DIR["path"] = tempfile.mkdtemp(prefix="openlumara_chatset_")

    def tearDown(self):
        shutil.rmtree(DATA_DIR["path"], ignore_errors=True)

    async def test_set_persists_title(self):
        chat = _make_chat()

        await chat.set("title", "hello")
        await _flush_index_save(chat)

        self.assertEqual(_reload_index()[0]["title"], "hello")

    async def test_set_unchanged_value_skips_write(self):
        chat = _make_chat()
        before = os.stat(chat.data.path).st_mtime_ns

        await chat.set("title", "New chat")
        await _flush_index_save(chat)

        self.assertEqual(os.stat(chat.data.path).st_mtime_ns, before)

    async def test_set_unknown_key_still_raises(self):
        chat = _make_chat()
        with self.assertRaises(Exception):
            await chat.set("bogus", 1)

    async def test_set_with_explicit_index_persists(self):
        chat = _make_chat()
        chat.data.append({
            "id": "CHAT0002", "title": "Other", "category": "general",
            "tags": [], "token_usage": 0, "metadata": {},
            "created": "", "updated": "",
        })
        chat.data.save()

        await chat.set("category", "work", index=1)
        await _flush_index_save(chat)

        self.assertEqual(_reload_index()[1]["category"], "work")

    async def test_set_with_index_zero_and_no_current_chat(self):
        chat = _make_chat()
        chat.current = None

        await chat.set("title", "renamed", index=0)
        await _flush_index_save(chat)

        self.assertEqual(_reload_index()[0]["title"], "renamed")

    async def test_set_without_index_and_no_current_chat_raises(self):
        chat = _make_chat()
        chat.current = None
        with self.assertRaises(Exception):
            await chat.set("title", "x")

    async def test_back_to_back_sets_coalesce_into_one_write(self):
        chat = _make_chat()
        writes = []
        real_save = chat.data.save
        chat.data.save = lambda *a, **k: writes.append(1) or real_save(*a, **k)

        await chat.set("title", "t")
        await chat.set("category", "c")
        await chat.set("tags", ["x"])
        await _flush_index_save(chat)

        self.assertEqual(len(writes), 1)
        saved = _reload_index()[0]
        self.assertEqual((saved["title"], saved["category"], saved["tags"]), ("t", "c", ["x"]))

    async def test_chat_save_flushes_pending_set(self):
        chat = _make_chat()
        await chat.set("title", "flushed")

        await chat.save()

        self.assertIsNone(chat._index_save_task)
        self.assertEqual(_reload_index()[0]["title"], "flushed")

    def test_update_metadata_persists(self):
        chat = _make_chat()
        chat.update_metadata("character", "Pirouette")
        self.assertEqual(_reload_index()[0]["metadata"]["character"], "Pirouette")

    def test_update_metadata_creates_missing_dict_for_legacy_chat(self):
        chat = _make_chat()
        del chat.data[0]["metadata"]  # legacy chat predating the metadata key
        chat.data.save()

        chat.update_metadata("meeting_room", True, index=0)

        self.assertEqual(_reload_index()[0]["metadata"]["meeting_room"], True)

    def test_update_metadata_unchanged_skips_write(self):
        chat = _make_chat()
        chat.update_metadata("character", "X")
        before = os.stat(chat.data.path).st_mtime_ns

        chat.update_metadata("character", "X")

        self.assertEqual(os.stat(chat.data.path).st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)

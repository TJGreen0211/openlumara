"""
Self-contained unit test for core/messages.py save behavior (audit 2.3):
  - rapid adds coalesce into a single debounced disk write (one timestamp
    update / index rewrite, not one per message),
  - an explicit save() flushes the pending debounce immediately,
  - concurrent save() calls are serialized and leave a valid file,
  - history round-trips through the storage layer.

It does NOT import the real `core` package. Instead it injects lightweight
stand-ins into sys.modules (reusing the REAL core/storage.py), like
tests/test_html_preview.py.

Run:  venv\\Scripts\\python tests\\test_messages_save.py
"""
import asyncio
import importlib.util
import json
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
_core_mod.get_data_path = lambda subpath=None, user=None: DATA_DIR["path"]
_core_mod.sandbox_path = lambda base, name: os.path.join(base, name)
sys.modules["core"] = _core_mod

_spec = importlib.util.spec_from_file_location(
    "ol_storage_messages_under_test", os.path.join(ROOT, "core", "storage.py"))
_storage_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_storage_mod)
_core_mod.storage = _storage_mod

_spec = importlib.util.spec_from_file_location(
    "ol_messages_under_test", os.path.join(ROOT, "core", "messages.py"))
messages_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(messages_mod)


class _FakeManager:
    modules = {}


class _FakeChannel:
    name = "test"
    manager = _FakeManager()

    def _extract_content(self, message):
        return message.get("content", "")

    def log(self, *args, **kwargs):
        pass


class _FakeChat:
    path = os.path.join("chats", "test")

    def __init__(self):
        self._data = {"id": "TESTID1"}
        self._slot_dirty = False
        self.update_count = 0

    def get(self, key):
        return self._data.get(key)

    async def set(self, key, value):
        self._data[key] = value

    async def update_timestamp(self):
        self.update_count += 1

    async def save(self):
        pass


class MessagesSaveTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        DATA_DIR["path"] = tempfile.mkdtemp(prefix="openlumara_msgsave_")
        self.history_path = os.path.join(DATA_DIR["path"], "chats", "test", "history", "TESTID1.json")
        self.chat = _FakeChat()
        self.messages = messages_mod.Messages(_FakeChannel(), self.chat)

    def tearDown(self):
        shutil.rmtree(DATA_DIR["path"], ignore_errors=True)

    def _read_history(self):
        with open(self.history_path, encoding="utf-8") as f:
            return json.load(f)

    async def test_debounce_coalesces_rapid_adds(self):
        for i in range(5):
            await self.messages.add({"role": "user", "content": f"m{i}"})

        # debounce hasn't elapsed yet: disk still holds the initial empty list
        self.assertEqual(self._read_history(), [])

        await asyncio.sleep(self.messages.SAVE_DEBOUNCE_SECONDS + 0.15)

        history = self._read_history()
        self.assertEqual([m["content"] for m in history], ["m0", "m1", "m2", "m3", "m4"])
        # one coalesced save means one timestamp update (one index rewrite)
        self.assertEqual(self.chat.update_count, 1)

    async def test_explicit_save_flushes_pending_debounce(self):
        await self.messages.add({"role": "user", "content": "now"})
        await self.messages.save()

        self.assertEqual([m["content"] for m in self._read_history()], ["now"])
        self.assertEqual(self.chat.update_count, 1)

    async def test_concurrent_saves_stay_serialized_and_valid(self):
        for i in range(3):
            await self.messages.add({"role": "user", "content": f"c{i}"})

        await asyncio.gather(self.messages.save(), self.messages.save(), self.messages.save())

        history = self._read_history()
        self.assertEqual([m["content"] for m in history], ["c0", "c1", "c2"])

    async def test_save_writes_snapshot_not_live_list(self):
        await self.messages.add({"role": "assistant", "content": "before"})

        real_save = self.messages.data.save
        seen = {}

        def save_and_mutate(snapshot=None):
            # simulate the loop mutating history while the worker thread dumps
            self.messages.data.append({"role": "user", "content": "late"})
            self.messages.data[0]["content"] = "edited mid-save"
            seen["snapshot"] = snapshot
            return real_save(snapshot)

        self.messages.data.save = save_and_mutate
        await self.messages.save()

        self.assertIsNotNone(seen["snapshot"])
        self.assertEqual(self._read_history(), [{"role": "assistant", "content": "before", "_metadata": {}}])

    async def test_history_roundtrips_through_storage(self):
        await self.messages.add({"role": "user", "content": "round", "_metadata": {"x": 1}})
        await self.messages.save()

        reloaded = _storage_mod.StorageList(
            os.path.join("chats", "test", "history", "TESTID1"), "json", path=DATA_DIR["path"])
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["content"], "round")
        self.assertEqual(reloaded[0]["_metadata"]["x"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""
Self-contained tests for the on_chat_switch module hook (core/chat.py):
modules hear about every conversation the user leaves (switching chats,
starting a new one, /clear), but not startup and not a chat the user deleted.

Also covers the outgoing-chat bug it fixed: _set_current used to read the old
chat id from self.data[self.current], which after a delete shifts the indexes
points at a different chat (so deleting an earlier chat reset the current
chat's loaded tools and would have reported a bogus switch).

Uses the REAL core/storage.py, core/messages.py and core/chat.py with a fake
`core` package, so the app is never bootstrapped.

Run:  venv\\Scripts\\python tests\\test_chat_switch_hook.py
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
_core_mod.detail_error = lambda e: f"{type(e).__name__}: {e}"
_core_mod.get_data_path = lambda subpath=None, user=None: (
    os.path.join(DATA_DIR["path"], subpath) if subpath else DATA_DIR["path"])
_core_mod.sandbox_path = lambda base, name: os.path.join(base, name)
_core_mod.config = types.SimpleNamespace(get=lambda *a, **k: {"auto_resume_chats": False})
sys.modules["core"] = _core_mod


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel.split("/")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_core_mod.storage = _load("ol_storage_switch_under_test", "core/storage.py")
_core_mod.messages = _load("ol_messages_switch_under_test", "core/messages.py")
chat_mod = _load("ol_chat_switch_under_test", "core/chat.py")


class Recorder:
    def __init__(self, name="recorder"):
        self.name = name
        self.calls = []
        self.metadata = []

    async def on_chat_switch(self, old_chat_id, new_chat_id, old_messages, old_metadata):
        self.calls.append((old_chat_id, new_chat_id, [m["content"] for m in old_messages]))
        self.metadata.append(old_metadata)


class Exploder:
    name = "exploder"

    async def on_chat_switch(self, old_chat_id, new_chat_id, old_messages, old_metadata):
        raise RuntimeError("boom")


class Mutator:
    name = "mutator"

    async def on_chat_switch(self, old_chat_id, new_chat_id, old_messages, old_metadata):
        old_messages.clear()


class NoHook:
    name = "nohook"


class FakeToolLoader:
    def __init__(self):
        self.resets = 0

    def reset_for_new_chat(self):
        self.resets += 1

    def restore_chat_tools(self):
        pass


class FakeContext:
    async def get_total_tokens(self):
        return 0


class FakeChannel:
    name = "test"

    def __init__(self, modules):
        self.manager = types.SimpleNamespace(modules=modules)
        self.tool_loader = FakeToolLoader()
        self.context = FakeContext()
        self.logs = []

    def log(self, category, message):
        self.logs.append((category, message))

    def _extract_content(self, message):
        return message.get("content", "")

    async def save(self):
        pass


class ChatSwitchHookTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        DATA_DIR["path"] = tempfile.mkdtemp(prefix="openlumara_chatswitch_")
        self.rec = Recorder()
        self.channel = FakeChannel({"recorder": self.rec})
        self.chat = chat_mod.Chat(self.channel)

    async def asyncTearDown(self):
        # let debounced message/index saves land before deleting the folder
        await asyncio.sleep(0.4)
        shutil.rmtree(DATA_DIR["path"], ignore_errors=True)

    async def new_chat_with(self, *contents):
        chat_id = await self.chat.new()
        for c in contents:
            await self.chat.messages.add({"role": "user", "content": c})
        return chat_id

    async def test_startup_does_not_fire(self):
        await self.chat.autoload()  # creates the first chat
        self.assertEqual(self.rec.calls, [])

    async def test_new_chat_reports_the_chat_that_was_left(self):
        a = await self.new_chat_with("hi from A", "more A")
        self.rec.calls.clear()
        b = await self.chat.new()
        self.assertEqual(self.rec.calls, [(a, b, ["hi from A", "more A"])])

    async def test_switching_back_and_forth(self):
        a = await self.new_chat_with("in A")
        b = await self.new_chat_with("in B")
        self.rec.calls.clear()
        await self.chat.load(a)
        await self.chat.load(b)
        self.assertEqual(self.rec.calls, [(b, a, ["in B"]), (a, b, ["in A"])])

    async def test_loading_the_current_chat_does_not_fire(self):
        a = await self.new_chat_with("x")
        self.rec.calls.clear()
        await self.chat.load(a)
        self.assertEqual(self.rec.calls, [])

    async def test_clear_reports_the_cleared_conversation(self):
        a = await self.new_chat_with("one", "two")
        self.rec.calls.clear()
        await self.chat.clear()
        self.assertEqual(self.rec.calls, [(a, a, ["one", "two"])])
        self.assertEqual(list(self.chat.messages.data), [])

    async def test_clearing_an_empty_chat_does_not_fire(self):
        await self.chat.new()
        self.rec.calls.clear()
        await self.chat.clear()
        self.assertEqual(self.rec.calls, [])

    async def test_deleting_the_current_chat_does_not_hand_it_to_modules(self):
        a = await self.new_chat_with("keep")
        b = await self.new_chat_with("private, deleted")
        self.rec.calls.clear()
        resets = self.channel.tool_loader.resets
        await self.chat.delete(b)
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(self.chat.get("id"), a)
        self.assertEqual(self.chat.messages.chat_id, a)
        # a different chat is now loaded, so its tools must be reset/restored
        self.assertEqual(self.channel.tool_loader.resets, resets + 1)

    async def test_deleting_the_only_chat_does_not_fire(self):
        await self.new_chat_with("only one")
        only = self.chat.get("id")
        self.rec.calls.clear()
        await self.chat.delete(only)
        self.assertEqual(self.rec.calls, [])
        self.assertNotEqual(self.chat.get("id"), only)  # a fresh chat was created

    async def test_deleting_an_earlier_chat_is_not_a_switch(self):
        # regression: the index shift used to make the current chat look like a different one
        a = await self.new_chat_with("A")
        await self.new_chat_with("B")
        c = await self.new_chat_with("C")
        self.rec.calls.clear()
        resets = self.channel.tool_loader.resets
        await self.chat.delete(a)
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(self.chat.get("id"), c)
        self.assertEqual(self.channel.tool_loader.resets, resets)  # same chat, tools untouched

    async def test_deleting_a_later_chat_is_not_a_switch(self):
        a = await self.new_chat_with("A")
        b = await self.new_chat_with("B")
        await self.chat.load(a)
        self.rec.calls.clear()
        await self.chat.delete(b)
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(self.chat.get("id"), a)

    async def test_a_failing_hook_is_logged_and_does_not_block_others(self):
        self.channel.manager.modules = {"exploder": Exploder(), "nohook": NoHook(), "recorder": self.rec}
        a = await self.new_chat_with("A")
        self.rec.calls.clear()
        b = await self.chat.new()
        self.assertEqual(self.chat.get("id"), b)  # the switch still completed
        self.assertEqual(self.rec.calls, [(a, b, ["A"])])
        self.assertTrue(any("on_chat_switch" in msg and "boom" in msg for _, msg in self.channel.logs))

    async def test_quick_switch_back_keeps_unsaved_messages(self):
        # regression: saves are debounced, so reopening a chat before its pending
        # save landed loaded it from disk without its latest messages
        a = await self.new_chat_with("unsaved A")
        await self.chat.new()  # well inside the debounce window
        await self.chat.load(a)
        self.assertEqual([m["content"] for m in self.chat.messages.data], ["unsaved A"])

    async def test_switching_away_does_not_rewrite_an_unchanged_history(self):
        a = await self.new_chat_with("x")
        await self.chat.messages.save()
        path = self.chat.messages.data.path
        before = os.path.getmtime(path)
        await asyncio.sleep(0.05)
        await self.chat.new()
        await self.chat.load(a)
        self.assertEqual(os.path.getmtime(path), before)

    async def test_deleted_chat_is_not_written_back_by_a_pending_save(self):
        # regression: a debounced save still pending at delete time recreated the history file
        await self.new_chat_with("keep")
        b = await self.new_chat_with("delete me")
        path = self.chat.messages.data.path
        await self.chat.delete(b)
        await asyncio.sleep(self.chat.messages.SAVE_DEBOUNCE_SECONDS + 0.3)
        self.assertFalse(os.path.exists(path))

    async def test_deleting_another_chat_keeps_the_current_chats_pending_messages(self):
        a = await self.new_chat_with("A")
        b = await self.new_chat_with("B", "B unsaved")
        await self.chat.delete(a)
        await self.chat.new()
        await self.chat.load(b)
        self.assertEqual([m["content"] for m in self.chat.messages.data], ["B", "B unsaved"])

    async def test_hook_gets_the_outgoing_chats_metadata(self):
        await self.chat.new(metadata={"character": "pirate"})
        await self.chat.messages.add({"role": "user", "content": "arr"})
        await self.chat.new()
        self.assertEqual(self.rec.metadata[-1], {"character": "pirate"})
        # it's a copy: changing it doesn't touch the chat
        self.rec.metadata[-1]["character"] = "changed"
        await self.chat.clear()  # empty new chat: no event
        self.assertEqual(self.chat.data[0]["metadata"], {"character": "pirate"})

    async def test_flush_pending_saves_before_the_folder_is_deleted(self):
        # regression: deleting a user right after they chatted left debounced
        # index/history saves that then failed writing into the missing folder.
        # they are written right away instead (not dropped: a failed deletion loses nothing)
        logged = []
        await self.new_chat_with("unsaved")
        await self.chat.set("title", "renamed")  # schedules a debounced index save
        history_path = self.chat.messages.data.path
        await self.chat.flush_pending_saves()
        with open(history_path, encoding="utf-8") as f:
            self.assertEqual([m["content"] for m in json.load(f)], ["unsaved"])  # written, not dropped
        self.assertEqual(_core_mod.storage.StorageList(
            os.path.join("chats", "test", "index"), "msgpack", path=DATA_DIR["path"])[-1]["title"], "renamed")
        shutil.rmtree(DATA_DIR["path"])
        original_log, _core_mod.log = _core_mod.log, lambda category, msg: logged.append(msg)
        try:
            await asyncio.sleep(self.chat.messages.SAVE_DEBOUNCE_SECONDS + 0.3)
        finally:
            _core_mod.log = original_log
        self.assertEqual([m for m in logged if "error writing" in m], [])
        self.assertFalse(os.path.exists(DATA_DIR["path"]))

    async def test_hooks_get_a_copy_of_the_history(self):
        self.channel.manager.modules = {"mutator": Mutator(), "recorder": self.rec}
        a = await self.new_chat_with("one", "two")
        self.rec.calls.clear()
        await self.chat.new()
        self.assertEqual(self.rec.calls[0][2], [])  # the mutator emptied the shared list...
        await self.chat.load(a)
        self.assertEqual([m["content"] for m in self.chat.messages.data], ["one", "two"])  # ...not the chat


if __name__ == "__main__":
    unittest.main(verbosity=2)

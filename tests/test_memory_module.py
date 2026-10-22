"""
Self-contained tests for modules/memory.py: automatic extraction (on chat
switch + idle fallback), the memory tools, per-user isolation and cleanup.

Uses the REAL core/memory_store.py (sqlite + sqlite-vec) with a fake `core`:
a fake embedding engine (deterministic bag-of-words vectors) and a scripted
fake AI, so no model, network or app bootstrap is needed.

Run:  venv\\Scripts\\python tests\\test_memory_module.py
"""
import asyncio
import contextvars
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DATA_DIR = {"path": None}
LOGS = []
DIM = 64

# ---- fake core ----------------------------------------------------------------
core = types.ModuleType("core")
core.current_user = contextvars.ContextVar("current_user", default=None)
core.log = lambda category, msg: LOGS.append((category, msg))
core.detail_error = lambda e: f"{type(e).__name__}: {e}"


def _get_data_path(subpath=None, user=None):
    user = core.current_user.get() if user is None else user
    base = os.path.join(DATA_DIR["path"], user) if user else DATA_DIR["path"]
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, subpath) if subpath else base


core.get_data_path = _get_data_path
core.storage = types.SimpleNamespace(TEMPORARY=False)


class APIError:
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


core.api = types.SimpleNamespace(APIError=APIError)


class FakeModuleBase:
    settings = {}

    def __init__(self):
        self.channel = None
        self.manager = None
        self.config = None

    def log(self, category, message):
        LOGS.append((category, message))

    def result(self, data, success=True):
        return {"status": "success" if success else "error", "content": data}

    async def on_user_deleted(self, username):
        pass


def _command(name, help=None, send_to_ai=True):
    def decorator(func):
        func._is_command = True
        func._command_name = name
        return func
    return decorator


core.module = types.SimpleNamespace(Module=FakeModuleBase, command=_command)
MODEL_CONFIG = {"use_tools": True}
core.config = types.SimpleNamespace(get=lambda key, *a, **k: MODEL_CONFIG if key == "model" else {})
sys.modules["core"] = core


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel.split("/")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core.memory_store = _load("ol_memstore_for_module_test", "core/memory_store.py")
sys.modules["core.memory_store"] = core.memory_store


class EmbeddingsError(Exception):
    pass


class FakeEngine:
    """bag-of-words vectors: texts sharing words are close, like a (very dumb) real model"""
    def __init__(self):
        self.ready = True
        self.calls = 0
        self.error = None
        self.fail_loading = False
        self.load_attempts = 0

    @property
    def status(self):
        if self.ready:
            return "ready"
        return "failed" if self.error else "loading"

    def _vec(self, text):
        v = [0.0] * DIM
        for word in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    async def _embed(self, texts):
        if not self.ready:
            raise EmbeddingsError("not ready")
        self.calls += 1
        return [self._vec(t) for t in texts]

    async def embed_queries(self, texts):
        return await self._embed(texts)

    async def embed_passages(self, texts):
        return await self._embed(texts)

    async def ensure_ready(self):
        self.load_attempts += 1
        if self.fail_loading:
            self.ready, self.error = False, "huggingface unreachable"
            raise EmbeddingsError(self.error)
        self.ready, self.error = True, None
        return True


ENGINE = FakeEngine()
core.embeddings = types.SimpleNamespace(
    get=lambda: ENGINE, MODEL_ID="fake-model", MODEL_DIM=DIM, EmbeddingsError=EmbeddingsError)
sys.modules["core.embeddings"] = core.embeddings

mem_mod = _load("ol_memory_module_under_test", "modules/memory.py")


# ---- fakes for the module's surroundings ----------------------------------------
class FakeAPI:
    def __init__(self):
        self.replies = []
        self.prompts = []

    def queue(self, *operations_lists):
        for ops in operations_lists:
            self.replies.append(ops if isinstance(ops, (str, APIError)) else json.dumps({"operations": ops}))

    async def send(self, context, **kwargs):
        self.prompts.append(context[0]["content"])
        self.kwargs = kwargs
        reply = self.replies.pop(0) if self.replies else '{"operations": []}'
        if isinstance(reply, APIError):
            return reply
        return {"content": reply}


class FakeConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def make_module(**settings):
    values = {"auto_extract": True, "extract_from_character_chats": False, "idle_extract_minutes": 30,
              "core_memories_in_system_prompt": True, "max_core_memories": 20,
              "recall_relevant_memories": True, "recall_count": 5, "recall_in_character_chats": True,
              "insert_nudge_prompt": False}
    values.update(settings)
    module = mem_mod.Memory()
    module.config = FakeConfig(values)
    module.manager = types.SimpleNamespace(API=FakeAPI())
    return module


def msgs(*pairs):
    """msgs(("user", "hi"), ("assistant", "hello")) -> chat messages"""
    return [{"role": r, "content": c} for r, c in pairs]


class ModuleTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        DATA_DIR["path"] = tempfile.mkdtemp(prefix="openlumara_memmod_")
        LOGS.clear()
        ENGINE.ready, ENGINE.error, ENGINE.fail_loading, ENGINE.load_attempts = True, None, False, 0
        self.mod = make_module()
        with mock.patch.object(mem_mod.core.embeddings, "get", return_value=ENGINE):
            await self.mod.on_ready()
        await self.settle()
        self.api = self.mod.manager.API
        self.token = core.current_user.set("alice")

    async def asyncTearDown(self):
        core.current_user.reset(self.token)
        await self.settle()
        await self.mod.on_shutdown()
        shutil.rmtree(DATA_DIR["path"], ignore_errors=True)

    async def settle(self):
        """wait for the module's background tasks"""
        for _ in range(50):
            tasks = [t for t in self.mod._tasks if not t.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def switch(self, chat_id, messages, metadata=None):
        await self.mod.on_chat_switch(chat_id, "NEXT", messages, metadata or {})
        await self.settle()

    def store(self):
        return self.mod._store()


# ---- pure helpers -----------------------------------------------------------------
class HelperTests(unittest.TestCase):
    def test_transcript_skips_tools_commands_and_ghosts(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "I have a dog named Rex"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": '{"status": "success"}'},
            {"role": "assistant", "content": "Nice!"},
            {"role": "user", "content": "/help", "_metadata": {"is_cmd": True}},
            {"role": "assistant", "content": "ghost", "_metadata": {"ghost": True}},
            {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {}}]},
        ]
        chunks = mem_mod.build_transcript_chunks(messages)
        self.assertEqual(chunks, ["USER: I have a dog named Rex\nASSISTANT: Nice!\nUSER: look"])

    def test_transcript_truncates_and_chunks(self):
        long = "x" * 5000
        # the first line becomes "USER: " + 2000 chars + " [...]" = 2012 chars, so a
        # 2015 char budget can't also fit "USER: short"
        chunks = mem_mod.build_transcript_chunks(msgs(("user", long), ("user", "short")),
                                                 max_chars=2015, max_message_chars=2000)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].endswith("[...]"))
        self.assertLess(len(chunks[0]), 2100)

    def test_chunks_without_user_lines_are_dropped(self):
        self.assertEqual(mem_mod.build_transcript_chunks(msgs(("assistant", "hello there"))), [])

    def test_parse_operations_variants(self):
        ops = [{"op": "add", "content": "x"}]
        for text in (json.dumps({"operations": ops}),
                     "```json\n" + json.dumps({"operations": ops}) + "\n```",
                     "<think>let me think {not json}</think>" + json.dumps({"operations": ops}),
                     'Sure! {"operations": [{"op": "add", "content": "x",}]}',  # trailing comma
                     json.dumps(ops)):
            self.assertEqual(mem_mod.parse_operations(text), ops, text)
        self.assertEqual(mem_mod.parse_operations('{"operations": []}'), [])
        with self.assertRaises(ValueError):
            mem_mod.parse_operations("I couldn't find anything worth remembering.")
        self.assertEqual(len(mem_mod.parse_operations(json.dumps([{"op": "add"}] * 50))),
                         mem_mod.MAX_OPERATIONS_PER_CHUNK)

    def test_secret_filter(self):
        for secret in ("The user's api key is sk-abc123def456ghi789jkl",
                       "The user's card is 4111 1111 1111 1111",
                       "La contraseña es hunter2",
                       "token " + "a" * 40):
            self.assertTrue(mem_mod.contains_secret(secret), secret)
        for fine in ("The user has a dog named Rex", "The user was born in 1990",
                     "El usuario vive en Madrid", "The user's phone ends in 42"):
            self.assertFalse(mem_mod.contains_secret(fine), fine)

    def test_fuse_rankings_rewards_agreement(self):
        a, b, c = ({"id": x} for x in "abc")
        fused = mem_mod.fuse_rankings([(a, 1), (b, 1)], [(b, 0.9), (c, 0.8)])
        self.assertEqual([m["id"] for m, _ in fused], ["b", "a", "c"])


# ---- extraction ----------------------------------------------------------------------
class ExtractionTests(ModuleTestCase):
    async def test_switch_extracts_and_stores(self):
        self.api.queue([{"op": "add", "content": "The user has a dog named Rex", "category": "fact",
                         "importance": 4}])
        await self.switch("CHAT1", msgs(("user", "I have a dog named Rex"), ("assistant", "Cute!")))
        memories = self.store().list()
        self.assertEqual([m["content"] for m in memories], ["The user has a dog named Rex"])
        self.assertEqual((memories[0]["source"], memories[0]["source_chat"], memories[0]["importance"]),
                         ("extraction", "CHAT1", 4))
        self.assertEqual(self.store().missing_embeddings(), [])  # embedded right away
        self.assertIn("USER: I have a dog named Rex", self.api.prompts[0])
        # thinking is switched off for this side request, and no tools are offered
        self.assertEqual(self.api.kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"], False)
        self.assertFalse(self.api.kwargs["use_tools"])

    async def test_only_new_messages_are_processed(self):
        history = msgs(("user", "I live in Denver"), ("assistant", "ok"))
        await self.switch("CHAT1", history)
        await self.switch("CHAT1", history)  # nothing new: no ai call
        self.assertEqual(len(self.api.prompts), 1)
        history += msgs(("user", "I also play guitar"), ("assistant", "cool"))
        await self.switch("CHAT1", history)
        self.assertEqual(len(self.api.prompts), 2)
        self.assertIn("guitar", self.api.prompts[1])
        self.assertNotIn("Denver", self.api.prompts[1].split("Conversation excerpt:")[1])

    async def test_rewritten_history_starts_over(self):
        # regression: only the LAST message used to be fingerprinted, so editing an
        # earlier one went unnoticed whenever the final reply stayed the same ("ok")
        await self.switch("CHAT1", msgs(("user", "I live in Denver"), ("assistant", "ok")))
        await self.switch("CHAT1", msgs(("user", "I live in Boston"), ("assistant", "ok")))
        self.assertEqual(len(self.api.prompts), 2)
        self.assertIn("Boston", self.api.prompts[1])

    async def test_cleared_chat_then_new_conversation(self):
        first = msgs(("user", "I live in Denver"), ("assistant", "ok"), ("user", "and I'm a nurse"))
        await self.switch("CHAT1", first)
        await self.switch("CHAT1", msgs(("user", "my cat is Luna")))  # after /clear: shorter history
        self.assertIn("Luna", self.api.prompts[1])

    async def test_update_and_delete_use_the_refs_shown(self):
        store = self.store()
        denver = store.add("The user lives in Denver")
        tea = store.add("The user likes green tea")
        await self.mod._embed_missing(store)
        # the ai sees related memories as m1, m2...; find which ref each got
        self.api.queue([])
        await self.switch("CHAT0", msgs(("user", "I moved from Denver and stopped drinking green tea")))
        listing = self.api.prompts[0]
        ref = {m.group(2): m.group(1) for m in re.finditer(r"\[(m\d+)\] \(\w+\) (.+)", listing)}
        self.api.queue([{"op": "update", "ref": ref["The user lives in Denver"], "content": "The user lives in Boston"},
                        {"op": "delete", "ref": ref["The user likes green tea"]},
                        {"op": "update", "ref": "m99", "content": "invented ref is ignored"},
                        {"op": "delete", "id": denver["id"]}])  # raw ids are ignored too, only refs count
        await self.switch("CHAT1", msgs(("user", "I moved to Boston from Denver. no more green tea for me")))
        self.assertEqual(store.get(denver["id"])["content"], "The user lives in Boston")
        self.assertIsNone(store.get(tea["id"]))
        self.assertIsNotNone(store.get(tea["id"], include_deleted=True))  # soft delete: recoverable
        self.assertEqual(store.missing_embeddings(), [])  # the updated memory was re-embedded

    async def test_secrets_and_junk_are_not_stored(self):
        self.api.queue([{"op": "add", "content": "The user's password is hunter2"},
                        {"op": "add", "content": "   "},
                        {"op": "add", "content": "The user likes jazz", "category": "made-up-category"},
                        {"op": "frobnicate", "content": "?"}])
        await self.switch("CHAT1", msgs(("user", "my password is hunter2, and I love jazz")))
        memories = self.store().list()
        self.assertEqual([(m["content"], m["category"]) for m in memories], [("The user likes jazz", "fact")])

    async def test_exact_repeats_are_skipped(self):
        self.store().add("The user likes jazz")
        self.api.queue([{"op": "add", "content": "the user likes JAZZ."}])
        await self.switch("CHAT1", msgs(("user", "I like jazz")))
        self.assertEqual(self.store().count(), 1)

    async def test_character_chats_are_skipped_by_default(self):
        await self.switch("CHAT1", msgs(("user", "arr I be a pirate")), {"character": "pirate"})
        self.assertEqual(self.api.prompts, [])
        self.mod.config.values["extract_from_character_chats"] = True
        await self.switch("CHAT1", msgs(("user", "arr I be a pirate")), {"character": "pirate"})
        self.assertEqual(len(self.api.prompts), 1)

    async def test_auto_extract_off(self):
        self.mod.config.values["auto_extract"] = False
        await self.switch("CHAT1", msgs(("user", "I have a dog")))
        self.assertEqual(self.api.prompts, [])

    async def test_ai_failure_postpones_without_losing_progress(self):
        self.api.queue(APIError("server down"))
        history = msgs(("user", "I have a dog named Rex"))
        await self.switch("CHAT1", history)
        self.assertIsNone(self.store().get_processed("CHAT1"))
        self.assertTrue(any("postponed" in m and "server down" in m for _, m in LOGS))
        self.api.queue([{"op": "add", "content": "The user has a dog named Rex"}])
        await self.switch("CHAT1", history)  # retried next time
        self.assertEqual(self.store().count(), 1)

    async def test_unreadable_reply_is_skipped_not_retried_forever(self):
        self.api.queue("Sorry, I can't help with that.")
        await self.switch("CHAT1", msgs(("user", "I have a dog")))
        self.assertIsNotNone(self.store().get_processed("CHAT1"))
        self.assertTrue(any("couldn't read" in m for _, m in LOGS))

    async def test_embeddings_not_ready_postpones(self):
        ENGINE.ready = False
        await self.switch("CHAT1", msgs(("user", "I have a dog")))
        self.assertEqual(self.api.prompts, [])
        self.assertIsNone(self.store().get_processed("CHAT1"))

    async def test_long_chats_are_split_into_several_calls(self):
        history = []
        for i in range(30):
            history += msgs(("user", f"fact number {i} " + "y" * 600), ("assistant", "ok"))
        await self.switch("CHAT1", history)
        self.assertGreater(len(self.api.prompts), 1)

    async def test_users_are_isolated(self):
        self.api.queue([{"op": "add", "content": "Alice likes tea"}])
        await self.switch("CHAT1", msgs(("user", "I like tea")))
        bob = core.current_user.set("bob")
        try:
            self.api.queue([{"op": "add", "content": "Bob likes coffee"}])
            await self.switch("CHAT1", msgs(("user", "I like coffee")))
            self.assertEqual([m["content"] for m in self.store().list()], ["Bob likes coffee"])
        finally:
            core.current_user.reset(bob)
        self.assertEqual([m["content"] for m in self.store().list()], ["Alice likes tea"])
        self.assertTrue(os.path.isfile(os.path.join(DATA_DIR["path"], "alice", "memory.db")))
        self.assertTrue(os.path.isfile(os.path.join(DATA_DIR["path"], "bob", "memory.db")))


class ChangedRunsHelperTests(unittest.TestCase):
    def history(self):
        return msgs(("user", "u0"), ("assistant", "a1"), ("user", "u2"), ("assistant", "a3"),
                    ("user", "u4"), ("assistant", "a5"))

    def processed(self, messages):
        return {mem_mod.message_fingerprint(m) for m in messages}

    def contents(self, runs):
        return [[m["content"] for m in run] for run in runs]

    def test_never_processed_means_everything(self):
        h = self.history()
        self.assertEqual(self.contents(mem_mod.changed_runs(h, None)), [["u0", "a1", "u2", "a3", "u4", "a5"]])

    def test_nothing_changed(self):
        h = self.history()
        self.assertEqual(mem_mod.changed_runs(h, self.processed(h)), [])

    def test_new_messages_come_with_one_message_of_context(self):
        h = self.history()
        done = self.processed(h)
        h += msgs(("user", "u6"), ("assistant", "a7"))
        self.assertEqual(self.contents(mem_mod.changed_runs(h, done)), [["a5", "u6", "a7"]])

    def test_an_edit_far_up_reads_only_that_spot(self):
        h = self.history()
        done = self.processed(h)
        h[2] = {"role": "user", "content": "u2 edited"}
        self.assertEqual(self.contents(mem_mod.changed_runs(h, done)), [["a1", "u2 edited"]])

    def test_separate_changes_give_separate_runs(self):
        h = self.history()
        done = self.processed(h)
        h[2] = {"role": "user", "content": "u2 edited"}
        h += msgs(("user", "u6"))
        self.assertEqual(self.contents(mem_mod.changed_runs(h, done)), [["a1", "u2 edited"], ["a5", "u6"]])

    def test_assistant_only_changes_are_skipped(self):
        h = self.history()
        done = self.processed(h)
        h[5] = {"role": "assistant", "content": "a5 regenerated"}
        self.assertEqual(mem_mod.changed_runs(h, done), [])

    def test_commands_and_tool_messages_dont_count_as_user_changes(self):
        h = self.history()
        done = self.processed(h)
        h += [{"role": "user", "content": "/memory", "_metadata": {"is_cmd": True}},
              {"role": "tool", "content": "{}"}]
        self.assertEqual(mem_mod.changed_runs(h, done), [])

    def test_runs_are_joined_with_a_gap_marker_and_trimmed(self):
        runs = [msgs(("assistant", "a1"), ("user", "u2")), msgs(("assistant", "a5"), ("user", "u6"))]
        self.assertEqual(mem_mod.build_transcript_chunks(runs),
                         ["ASSISTANT: a1\nUSER: u2\n[...]\nASSISTANT: a5\nUSER: u6"])
        # a chunk boundary right at a gap never leaves a dangling marker
        chunks = mem_mod.build_transcript_chunks(runs, max_chars=25)
        self.assertTrue(all(not c.startswith("[...]") and not c.endswith("[...]") for c in chunks))
        self.assertEqual(sum(c.count("USER: ") for c in chunks), 2)


class IncrementalExtractionTests(ModuleTestCase):
    """finding 8: edits and regenerations only re-read what changed"""

    def long_chat(self, n=200):
        history = []
        for i in range(n):
            history += msgs(("user", f"message {i} about my day " + "z" * 300), ("assistant", f"reply {i}"))
        return history

    async def test_regenerating_a_reply_in_a_long_chat_costs_nothing(self):
        history = self.long_chat()
        await self.switch("CHAT1", history)
        first_calls = len(self.api.prompts)
        self.assertGreater(first_calls, 5)  # the first pass reads the whole chat
        history[-1] = {"role": "assistant", "content": "reply 199, regenerated"}
        await self.switch("CHAT1", history)
        self.assertEqual(len(self.api.prompts), first_calls)  # no ai call at all
        await self.switch("CHAT1", history)
        self.assertEqual(len(self.api.prompts), first_calls)  # and the new reply is recorded as processed

    async def test_editing_one_message_far_up_resends_only_that_message(self):
        history = self.long_chat()
        await self.switch("CHAT1", history)
        first_calls = len(self.api.prompts)
        history[10] = {"role": "user", "content": "message 5, edited: I just adopted a cat named Luna"}
        self.api.queue([{"op": "add", "content": "The user has a cat named Luna"}])
        await self.switch("CHAT1", history)
        self.assertEqual(len(self.api.prompts), first_calls + 1)
        excerpt = self.api.prompts[-1].split("Conversation excerpt:")[1].split("Respond with ONLY")[0]
        self.assertIn("cat named Luna", excerpt)
        self.assertIn("reply 4", excerpt)  # the message before it, as context
        self.assertNotIn("message 6", excerpt)
        self.assertLess(len(excerpt), 600)  # not the 70k-character chat
        self.assertEqual([m["content"] for m in self.store().list()], ["The user has a cat named Luna"])

    async def test_an_edit_and_new_messages_go_in_one_request(self):
        history = msgs(("user", "I live in Denver"), ("assistant", "ok"), ("user", "I'm a nurse"), ("assistant", "ok"))
        await self.switch("CHAT1", history)
        history[0] = {"role": "user", "content": "I live in Boston"}
        history += msgs(("user", "I have a dog"), ("assistant", "nice"))
        await self.switch("CHAT1", history)
        self.assertEqual(len(self.api.prompts), 2)
        self.assertIn("Boston", self.api.prompts[1])
        self.assertIn("[...]", self.api.prompts[1])
        self.assertIn("I have a dog", self.api.prompts[1])
        self.assertNotIn("I'm a nurse", self.api.prompts[1].split("Conversation excerpt:")[1])

    async def test_deleting_messages_needs_no_request(self):
        history = msgs(("user", "I live in Denver"), ("assistant", "ok"), ("user", "I'm a nurse"), ("assistant", "ok"))
        await self.switch("CHAT1", history)
        await self.switch("CHAT1", history[:2])
        self.assertEqual(len(self.api.prompts), 1)

    async def test_progress_is_kept_per_chat(self):
        history = msgs(("user", "I live in Denver"), ("assistant", "ok"))
        await self.switch("CHAT1", history)
        await self.switch("CHAT2", history)  # same messages, other chat: still read once there
        self.assertEqual(len(self.api.prompts), 2)


class IdleFallbackTests(ModuleTestCase):
    async def test_idle_chat_is_extracted_from_its_history_file(self):
        history_path = os.path.join(DATA_DIR["path"], "alice", "history.json")
        os.makedirs(os.path.dirname(history_path), exist_ok=True)
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(msgs(("user", "I'm training for a marathon")), f)
        chat = types.SimpleNamespace(
            messages=types.SimpleNamespace(data=types.SimpleNamespace(path=history_path), chat_id="CHAT1"),
            get=lambda key: {})
        self.mod.channel = types.SimpleNamespace(context=types.SimpleNamespace(chat=chat))
        await self.mod.on_assistant_message("reply")
        self.assertIn(("alice", "CHAT1"), self.mod._dirty)

        self.mod._sweep_idle_chats()  # not idle long enough yet
        await self.settle()
        self.assertEqual(self.api.prompts, [])

        self.mod._dirty[("alice", "CHAT1")]["at"] -= 31 * 60
        self.api.queue([{"op": "add", "content": "The user is training for a marathon"}])
        self.mod._sweep_idle_chats()
        await self.settle()
        self.assertEqual([m["content"] for m in self.store().list()], ["The user is training for a marathon"])
        self.assertNotIn(("alice", "CHAT1"), self.mod._dirty)

    async def test_switch_clears_the_idle_entry(self):
        self.mod._dirty[("alice", "CHAT1")] = {"at": 0, "path": "x", "metadata": {}}
        await self.switch("CHAT1", msgs(("user", "hi")))
        self.assertNotIn(("alice", "CHAT1"), self.mod._dirty)

    async def test_idle_extraction_off(self):
        self.mod.config.values["idle_extract_minutes"] = 0
        self.mod._dirty[("alice", "CHAT1")] = {"at": -10 ** 9, "path": "missing.json", "metadata": {}}
        self.mod._sweep_idle_chats()
        await self.settle()
        self.assertIn(("alice", "CHAT1"), self.mod._dirty)

    async def test_deleted_chat_file_is_ignored(self):
        self.mod._dirty[("alice", "CHAT1")] = {"at": -10 ** 9, "path": "does-not-exist.json", "metadata": {}}
        self.mod._sweep_idle_chats()
        await self.settle()
        self.assertEqual(self.api.prompts, [])


class ToolTests(ModuleTestCase):
    async def test_remember_search_update_forget(self):
        r = await self.mod.remember("The user's daughter is called Lucía", category="profile")
        self.assertEqual(r["status"], "success")
        memory_id = re.search(r"\[(\w+)\]", r["content"]).group(1)
        again = await self.mod.remember("the user's daughter is called lucía")
        self.assertIn("already remembered", again["content"])

        found = await self.mod.search("what is my daughter called")
        self.assertIn(memory_id, found["content"])

        up = await self.mod.update(memory_id, "The user's daughter is called Lucía and is six")
        self.assertIn("is six", up["content"])
        self.assertEqual(self.store().missing_embeddings(), [])

        self.assertEqual((await self.mod.forget(memory_id))["status"], "success")
        self.assertIn("No memories found", (await self.mod.search("daughter"))["content"])
        self.assertEqual((await self.mod.forget(memory_id))["status"], "error")

    async def test_pin_unpin(self):
        r = await self.mod.remember("The user likes jazz", category="fact")
        memory_id = re.search(r"\[(\w+)\]", r["content"]).group(1)
        self.assertNotIn("jazz", await self.mod.on_system_prompt())  # a plain fact isn't core
        await self.mod.pin(memory_id)
        self.assertIn("jazz", await self.mod.on_system_prompt())
        await self.mod.unpin(memory_id)
        self.assertNotIn("jazz", await self.mod.on_system_prompt())
        self.assertEqual((await self.mod.pin("NOPE"))["status"], "error")

    async def test_list_and_categories(self):
        await self.mod.remember("The user likes jazz", category="preference")
        await self.mod.remember("The user lives in Madrid", category="nonsense")  # falls back to fact
        self.assertIn("jazz", (await self.mod.list("preference"))["content"])
        self.assertIn("(fact) The user lives in Madrid", (await self.mod.list())["content"])
        self.assertEqual((await self.mod.list("gossip"))["status"], "error")

    async def test_search_while_model_loads(self):
        await self.mod.remember("The user likes jazz")
        ENGINE.ready = False
        r = await self.mod.search("jazz")
        self.assertEqual(r["status"], "error")
        self.assertIn("starting up", r["content"])

    async def test_remember_while_model_loads_is_embedded_later(self):
        ENGINE.ready = False
        await self.mod.remember("The user likes jazz")
        self.assertEqual(len(self.store().missing_embeddings()), 1)
        ENGINE.ready = True
        await self.mod._embed_missing(self.store())
        self.assertEqual(self.store().missing_embeddings(), [])


class FakeChatMessages:
    def __init__(self, chat_id, data):
        self.chat_id = chat_id
        self.data = data


def attach_chat(module, chat_id, messages):
    chat = types.SimpleNamespace(messages=FakeChatMessages(chat_id, messages), get=lambda key: {})
    module.channel = types.SimpleNamespace(context=types.SimpleNamespace(chat=chat))
    return chat


FILLER = ["The user watches football on Sundays", "The user drinks two coffees a day", "The user has three cousins",
          "The user once visited Iceland", "The user's flat has a balcony", "The user collects vinyl records",
          "The user bakes sourdough bread", "The user plays chess online", "The user owns a red bicycle",
          "The user is learning Italian"]


class GateHelperTests(unittest.TestCase):
    def test_contentless_messages(self):
        for text in ("hi", "thanks!", "ok", "Hola, ¿qué tal?", "gracias", "  ", "yes please", "what is it?"):
            self.assertTrue(mem_mod.is_contentless(text), text)
        for text in ("what's my dog called?", "¿dónde vive mi hermana?", "recommend a movie", "hi, how's Rex?"):
            self.assertFalse(mem_mod.is_contentless(text), text)

    def test_relevance_gate(self):
        mems = [{"id": f"m{i}"} for i in range(10)]
        sims = {f"m{i}": 0.70 + 0.001 * i for i in range(10)}
        sims["m0"] = 0.95  # stands out
        kept = mem_mod.relevance_gate(mems, keyword_ids={"m5"}, similarities=sims)
        self.assertEqual([m["id"] for m in kept], ["m0", "m5"])  # outlier + keyword match

    def test_relevance_gate_flat_scores_keep_nothing(self):
        mems = [{"id": f"m{i}"} for i in range(10)]
        sims = {f"m{i}": 0.80 for i in range(10)}
        self.assertEqual(mem_mod.relevance_gate(mems, set(), sims), [])

    def test_small_stores_skip_the_gate(self):
        mems = [{"id": "a"}, {"id": "b"}]
        self.assertEqual(mem_mod.relevance_gate(mems, set(), {"a": 0.8, "b": 0.8}), mems)


class RecallTests(ModuleTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for content in FILLER:
            await self.mod.remember(content)
        r = await self.mod.remember("The user has a golden retriever named Rex")
        self.rex = re.search(r"\[(\w+)\]", r["content"]).group(1)

    async def test_relevant_memory_is_recalled_for_the_latest_message(self):
        attach_chat(self.mod, "CHAT1", msgs(("user", "old message about chess"), ("assistant", "ok"),
                                            ("user", "what is my retriever called?")))
        prompt = await self.mod.on_end_prompt()
        self.assertIn("golden retriever named Rex", prompt)
        self.assertNotIn("chess", prompt)  # the earlier message isn't the query
        self.assertIn("only if they are actually relevant", prompt)
        self.assertEqual(self.store().get(self.rex)["use_count"], 1)

    async def test_contentless_message_recalls_nothing(self):
        attach_chat(self.mod, "CHAT1", msgs(("user", "thanks!")))
        self.assertIsNone(await self.mod.on_end_prompt())

    async def test_commands_are_not_queries(self):
        attach_chat(self.mod, "CHAT1", msgs(("user", "my retriever"))
                    + [{"role": "user", "content": "/memory", "_metadata": {"is_cmd": True}}])
        self.assertIn("Rex", await self.mod.on_end_prompt())

    async def test_recall_is_cached_per_message_and_invalidated_by_changes(self):
        attach_chat(self.mod, "CHAT1", msgs(("user", "what is my retriever called?")))
        await self.mod.on_end_prompt()
        calls = ENGINE.calls
        await self.mod.on_end_prompt()
        await self.mod.on_end_prompt()
        self.assertEqual(ENGINE.calls, calls)  # rebuilt prompts reuse the result
        self.assertEqual(self.store().get(self.rex)["use_count"], 1)
        await self.mod.update(self.rex, "The user has a golden retriever named Max")
        self.assertIn("named Max", await self.mod.on_end_prompt())  # memory changed: recomputed

    async def test_core_memories_are_not_repeated_in_recall(self):
        await self.mod.pin(self.rex)
        attach_chat(self.mod, "CHAT1", msgs(("user", "what is my retriever called?")))
        self.assertNotIn("Rex", await self.mod.on_end_prompt() or "")  # already in the system prompt
        self.assertIn("Rex", await self.mod.on_system_prompt())

    async def test_recall_can_be_turned_off(self):
        self.mod.config.values["recall_relevant_memories"] = False
        attach_chat(self.mod, "CHAT1", msgs(("user", "what is my retriever called?")))
        self.assertIsNone(await self.mod.on_end_prompt())

    async def test_no_recall_while_the_model_loads_and_no_stale_cache(self):
        attach_chat(self.mod, "CHAT1", msgs(("user", "what is my retriever called?")))
        ENGINE.ready = False
        self.assertIsNone(await self.mod.on_end_prompt())
        ENGINE.ready = True
        self.assertIn("Rex", await self.mod.on_end_prompt())

    async def test_recall_count(self):
        self.mod.config.values["recall_count"] = 1
        attach_chat(self.mod, "CHAT1", msgs(("user", "my retriever Rex and my vinyl records and chess")))
        prompt = await self.mod.on_end_prompt()
        self.assertEqual(len([line for line in prompt.splitlines() if line.startswith("[")]), 1)

    async def test_nudge_prompt_still_works(self):
        self.mod.config.values["insert_nudge_prompt"] = True
        attach_chat(self.mod, "CHAT1", msgs(("user", "hi")))
        self.assertIn("long-term memory", await self.mod.on_end_prompt())

    async def test_no_channel_no_recall(self):
        self.mod.channel = None
        self.assertIsNone(await self.mod.on_end_prompt())


class SystemPromptTests(ModuleTestCase):
    async def test_core_memories_order_and_filter(self):
        await self.mod.remember("The user likes jazz", category="preference")  # not core
        await self.mod.remember("The user is called Tom", category="profile")
        await self.mod.remember("Always answer in Spanish", category="instruction")
        pinned = await self.mod.remember("The user's wedding is on June 3", category="event")
        await self.mod.pin(re.search(r"\[(\w+)\]", pinned["content"]).group(1))
        prompt = await self.mod.on_system_prompt()
        self.assertNotIn("jazz", prompt)
        self.assertLess(prompt.index("wedding"), prompt.index("Tom"))  # pinned first
        self.assertIn("Spanish", prompt)

    async def test_core_memories_are_capped(self):
        for i in range(30):
            self.store().add(f"Profile fact number {i} " + "x" * 150, category="profile")
        self.mod._changed()
        prompt = await self.mod.on_system_prompt()
        lines = [line for line in prompt.splitlines() if line.startswith("[")]
        self.assertLessEqual(sum(len(line) for line in lines), mem_mod.MAX_CORE_PROMPT_CHARS + 400)
        self.mod.config.values["max_core_memories"] = 3
        lines = [line for line in (await self.mod.on_system_prompt()).splitlines() if line.startswith("[")]
        self.assertEqual(len(lines), 3)

    async def test_tool_guidance_only_with_tools(self):
        self.assertIn("memory_remember", await self.mod.on_system_prompt())
        MODEL_CONFIG["use_tools"] = False
        try:
            self.assertIsNone(await self.mod.on_system_prompt())  # no memories, no tools: nothing to say
        finally:
            MODEL_CONFIG["use_tools"] = True

    async def test_core_memories_setting(self):
        await self.mod.remember("The user is called Tom", category="profile")
        self.mod.config.values["core_memories_in_system_prompt"] = False
        self.assertNotIn("Tom", await self.mod.on_system_prompt())

    async def test_character_safe_follows_the_setting(self):
        self.assertTrue(self.mod.character_safe)
        self.mod.config.values["recall_in_character_chats"] = False
        self.assertFalse(self.mod.character_safe)


class CommandTests(ModuleTestCase):
    async def test_list_search_forget_undo(self):
        self.assertIn("Nothing is remembered", await self.mod.memory_command([]))
        r = await self.mod.remember("The user has a golden retriever named Rex")
        memory_id = re.search(r"\[(\w+)\]", r["content"]).group(1)
        self.assertIn("1 memories", await self.mod.memory_command(["list"]))
        self.assertIn("Rex", await self.mod.memory_command(["search", "golden", "retriever"]))
        self.assertIn("usage", await self.mod.memory_command(["search"]))
        self.assertIn("Forgotten", await self.mod.memory_command(["forget", memory_id]))
        self.assertIn("No memory", await self.mod.memory_command(["forget", memory_id]))
        self.assertIn("Restored", await self.mod.memory_command(["undo"]))
        self.assertIsNotNone(self.store().get(memory_id))
        self.assertEqual(self.store().missing_embeddings(), [])  # re-embedded on restore
        self.assertIn("nothing to bring back", await self.mod.memory_command(["undo"]))

    async def test_learn_runs_extraction_on_the_current_chat(self):
        attach_chat(self.mod, "CHAT1", msgs(("user", "I'm allergic to peanuts")))
        self.api.queue([{"op": "add", "content": "The user is allergic to peanuts", "category": "profile"}])
        self.assertIn("1 new", await self.mod.memory_command(["learn"]))
        self.assertIn("Nothing new", await self.mod.memory_command(["learn"]))  # already processed
        ENGINE.ready = False
        attach_chat(self.mod, "CHAT2", msgs(("user", "something else")))
        self.assertIn("Couldn't learn", await self.mod.memory_command(["learn"]))

    async def test_status_and_unknown(self):
        status = await self.mod.memory_command(["status"])
        self.assertIn("memories: 0", status)
        self.assertIn("memory model:", status)
        self.assertIn("unknown subcommand", await self.mod.memory_command(["frobnicate"]))


class ReviewFixTests(ModuleTestCase):
    """regressions for the code review findings (1, 4, 6, 7, 10)"""

    async def test_1_deleting_a_user_stops_their_running_extraction(self):
        gate = asyncio.Event()

        async def slow_send(context, **kwargs):
            await gate.wait()  # the ai is still thinking when the admin deletes the user
            return {"content": json.dumps({"operations": [{"op": "add", "content": "Alice likes tea"}]})}

        self.api.send = slow_send
        await self.mod.on_chat_switch("CHAT1", "NEXT", msgs(("user", "I like tea")), {})
        bob = core.current_user.set("bob")
        try:
            await self.mod.on_chat_switch("CHAT1", "NEXT", msgs(("user", "I like coffee")), {})  # bob's keeps going
        finally:
            core.current_user.reset(bob)
        await asyncio.sleep(0.05)
        alice_tasks = [t for t, u in self.mod._tasks.items() if u == "alice"]
        try:
            self.assertTrue(alice_tasks)
            await self.mod.on_before_user_delete("alice")
            self.assertTrue(all(t.cancelled() for t in alice_tasks))
            self.assertNotIn("alice", self.mod._stores)
            shutil.rmtree(os.path.join(DATA_DIR["path"], "alice"))  # would fail on windows with an open db
        finally:
            gate.set()  # never leave the fake ai blocked, even when an assertion fails
        await self.settle()
        self.assertFalse(os.path.exists(os.path.join(DATA_DIR["path"], "alice")))  # nothing recreated it
        bob = core.current_user.set("bob")
        try:
            self.assertEqual([m["content"] for m in self.store().list()], ["Alice likes tea"])  # bob's finished
        finally:
            core.current_user.reset(bob)

    async def test_4_failed_model_is_retried_with_backoff(self):
        ENGINE.fail_loading = True
        ENGINE.load_attempts = 0  # setup already ran the startup warm-up once
        await self.mod._warm_up()
        self.assertEqual(ENGINE.status, "failed")
        first_wait = self.mod._model_retry_delay
        self.assertIsNotNone(self.mod._model_retry_at)

        self.mod._retry_model_if_due()  # not due yet
        await self.settle()
        self.assertEqual(ENGINE.load_attempts, 1)

        self.mod._model_retry_at = 0  # due: still failing, so it backs off further
        self.mod._retry_model_if_due()
        await self.settle()
        self.assertEqual(ENGINE.load_attempts, 2)
        self.assertGreater(self.mod._model_retry_delay, first_wait)

        ENGINE.fail_loading = False  # huggingface is back
        self.mod._model_retry_at = 0
        self.mod._retry_model_if_due()
        await self.settle()
        self.assertEqual(ENGINE.status, "ready")
        self.assertIsNone(self.mod._model_retry_at)
        self.assertEqual(self.mod._model_retry_delay, mem_mod.MODEL_RETRY_FIRST_SECONDS)

    async def test_4_backoff_is_capped(self):
        ENGINE.fail_loading = True
        for _ in range(20):
            await self.mod._warm_up()
        self.assertEqual(self.mod._model_retry_delay, mem_mod.MODEL_RETRY_MAX_SECONDS)

    async def test_4_tools_say_failed_not_starting_up(self):
        await self.mod.remember("The user likes jazz")
        ENGINE.fail_loading = True
        await self.mod._warm_up()
        r = await self.mod.search("jazz")
        self.assertIn("couldn't be loaded", r["content"])
        self.assertIn("retried automatically", r["content"])
        self.assertNotIn("starting up", r["content"])
        self.assertIn("couldn't be loaded", await self.mod.memory_command(["search", "jazz"]))

    async def test_6_sqlite_errors_in_prompt_hooks_dont_propagate(self):
        import sqlite3
        await self.mod.remember("The user is called Tom", category="profile")
        attach_chat(self.mod, "CHAT1", msgs(("user", "what is my name?")))
        store = self.store()
        with mock.patch.object(store, "list", side_effect=sqlite3.OperationalError("database is locked")):
            system = await self.mod.on_system_prompt()  # must not raise
            end = await self.mod.on_end_prompt()
        self.assertNotIn("Tom", system)
        self.assertIn("memory_remember", system)  # the rest of the prompt still works
        self.assertIsNone(end)
        self.assertTrue(any("database is locked" in m for _, m in LOGS))
        self.assertIn("Tom", await self.mod.on_system_prompt())  # and it recovers on the next build

    async def test_6_closed_connection_in_recall_is_contained(self):
        import sqlite3
        attach_chat(self.mod, "CHAT1", msgs(("user", "what is my dog called?")))
        await self.mod.remember("The user has a dog named Rex")
        with mock.patch.object(self.store(), "vec_search",
                               side_effect=sqlite3.ProgrammingError("Cannot operate on a closed database.")):
            self.assertIsNone(await self.mod.on_end_prompt())
        self.assertIn("Rex", await self.mod.on_end_prompt())  # the failure wasn't cached

    async def test_7_duplicate_check_does_not_scan_every_memory(self):
        store = self.store()
        store.add("The user likes jazz")
        with mock.patch.object(store, "list", side_effect=AssertionError("full table scan")):
            memory, created = await self.mod._save_new(store, "the user likes JAZZ!")
            self.assertFalse(created)
            memory, created = await self.mod._save_new(store, "The user likes blues")
            self.assertTrue(created)

    async def test_10_user_deleted_clears_every_per_user_cache(self):
        attach_chat(self.mod, "CHAT1", msgs(("user", "my dog")))
        await self.mod.remember("The user has a dog named Rex")
        await self.mod.on_end_prompt()
        self.assertIn("alice", self.mod._revisions)
        self.assertIn("alice", self.mod._recall_cache)
        await self.mod.on_user_deleted("alice")
        for state in (self.mod._stores, self.mod._revisions, self.mod._recall_cache, self.mod._extract_locks):
            self.assertNotIn("alice", state)


class LifecycleTests(ModuleTestCase):
    async def test_before_user_delete_releases_the_database(self):
        self.store().add("The user likes jazz")
        user_dir = os.path.join(DATA_DIR["path"], "alice")
        await self.mod.on_before_user_delete("alice")
        self.assertNotIn("alice", self.mod._stores)
        shutil.rmtree(user_dir)  # would fail on windows if the sqlite file were still open
        self.assertFalse(os.path.exists(user_dir))

    async def test_user_deleted_drops_state(self):
        self.store()
        self.mod._dirty[("alice", "CHAT1")] = {"at": 0, "path": "x", "metadata": {}}
        self.mod._extract_locks["alice"] = asyncio.Lock()
        await self.mod.on_user_deleted("alice")
        self.assertNotIn("alice", self.mod._stores)
        self.assertEqual(self.mod._dirty, {})
        self.assertNotIn("alice", self.mod._extract_locks)

    async def test_temporary_mode_writes_no_database(self):
        with mock.patch.object(core.storage, "TEMPORARY", True):
            carol = core.current_user.set("carol")
            try:
                self.store().add("temp fact")
            finally:
                core.current_user.reset(carol)
        self.assertFalse(os.path.exists(os.path.join(DATA_DIR["path"], "carol", "memory.db")))


if __name__ == "__main__":
    unittest.main(verbosity=2)

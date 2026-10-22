import core
import core.embeddings
import core.memory_store
import asyncio
import hashlib
import json
import re
import time

import json_repair

# ------------------
# extraction tuning
# ------------------
# conversation text sent to the ai per extraction call
EXTRACT_CHUNK_CHARS = 8000
# one long message (a pasted file, a pdf) shouldn't crowd out the rest of the conversation
EXTRACT_MESSAGE_CHARS = 1500
# existing memories shown to the ai so it can update instead of duplicating
EXTRACT_RELATED_MEMORIES = 12
MAX_OPERATIONS_PER_CHUNK = 20
MAX_MEMORY_CHARS = 500
# skip a new memory that is a near-verbatim repeat of an existing one. measured on
# multilingual-e5-small (int8): paraphrases of one fact score 0.93-0.99, but
# different facts about different people also score up to 0.97 ("the user works
# as a nurse" vs "the user's mother works as a nurse" = 0.96), so anything lower
# would silently drop real memories. real deduplication is the ai's job (it sees
# the related memories); this only catches exact repeats
DUPLICATE_SIMILARITY = 0.985
# reciprocal rank fusion constant (merges keyword + vector rankings)
RRF_K = 60

# ------------------
# recall tuning (memories shown next to each user message)
# ------------------
# measured on multilingual-e5-small (int8) with 20 bilingual memories, 20 questions
# about them and 20 unrelated messages: raw similarity can't tell them apart
# (unrelated "gracias" scored 0.844, above the median relevant question's 0.809),
# so the gate is relative instead: how far a memory stands out from the user's
# other memories for this message, in standard deviations. z >= 1.0 kept 16/20
# of the right memories; unrelated messages still pass it, which is why the
# prompt tells the ai to ignore memories that aren't relevant. contentless
# messages ("hi", "gracias") are skipped before any search
RECALL_MIN_Z = 1.0
# memories compared for the z score (the vector index returns at most this many)
RECALL_POOL = 200
# below this many memories the z score is meaningless; just show the best matches
RECALL_MIN_MEMORIES_FOR_GATE = 8
# words that make a message "contentless" on top of the store's stopwords
GREETINGS = frozenset("""
hi hello hey thanks thank ok okay cool great nice yes yeah yep nope bye please sure lol
gracias hola vale sí bien buenas buenos dias días tardes noches adiós adios perfecto genial claro porfa
""".split())

CORE_CATEGORIES = ("profile", "instruction")

# a failed model download/load is retried with backoff: 1 min, 2, 4, ... up to 30 min
MODEL_RETRY_FIRST_SECONDS = 60
MODEL_RETRY_MAX_SECONDS = 1800
# size cap for the always-shown core memories (they sit in every system prompt)
MAX_CORE_PROMPT_CHARS = 2000

EXTRACTION_PROMPT = """You maintain the long-term memory of a personal assistant for one user. Read the conversation excerpt below and decide what is worth remembering about the USER for future conversations.

Remember: lasting facts about the user (identity, relationships, work, home, health, important dates, possessions), their preferences and opinions, ongoing goals or projects, and instructions the user wants the assistant to follow in future conversations.
Do NOT remember: small talk, one-off questions or tasks, instructions that only apply to the current message ("reply only 'ok'"), things only the assistant said, passing moods ("I'm tired today"), general knowledge, or secrets (passwords, API keys, card or ID numbers).

Rules:
- Each memory is one short, self-contained sentence about the user, in the third person.
- Write each memory in the language the user used for it, never translate: a fact the user said in Spanish becomes a Spanish memory ("El usuario vive en Madrid"), a fact said in English an English one ("The user lives in Madrid").
- If something updates or contradicts an existing memory, update that memory instead of adding a new one. Delete an existing memory only if the user made clear it is no longer true.
- Never add something an existing memory already says.
- If nothing is worth remembering, return an empty list.

Existing related memories:
{memories}

Conversation excerpt:
{transcript}

Respond with ONLY a JSON object and nothing else:
{{"operations": [
  {{"op": "add", "content": "...", "category": "profile|preference|fact|event|instruction", "importance": 1-5}},
  {{"op": "update", "ref": "m1", "content": "..."}},
  {{"op": "delete", "ref": "m2"}}
]}}"""

# obvious secrets never get stored, even if the ai tries to (api keys, long
# tokens, card numbers). a safety net, not a substitute for the prompt rule
_SECRET_RES = [
    re.compile(r"\b(sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_\-]{12,}"),
    re.compile(r"\b[A-Za-z0-9+/_\-]{32,}\b"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    re.compile(r"(?i)\b(password|passwd|contraseña|api[_ ]?key)\s*(is|es|:|=)\s*\S+"),
]


class ExtractionError(Exception):
    """extraction couldn't run (ai unreachable, embeddings unavailable); retried next time"""


def message_text(message: dict) -> str:
    """the plain text of a chat message (multimodal messages keep only their text parts)"""
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "").strip()


def message_fingerprint(message: dict) -> str:
    """short fingerprint of one message (role + text). the store keeps one per
    processed message, so only new or edited messages are ever read again"""
    return hashlib.sha256(f"{message.get('role')}\n{message_text(message)}".encode("utf-8")).hexdigest()[:16]


def is_conversation_message(message: dict) -> bool:
    """a real user/assistant message with text (not a tool call/result, command or ghost)"""
    if message.get("role") not in ("user", "assistant"):
        return False
    metadata = message.get("_metadata") or {}
    return not (metadata.get("is_cmd") or metadata.get("ghost")) and bool(message_text(message))


def changed_runs(messages: list, processed) -> list:
    """the stretches of a chat that need reading: every message whose fingerprint
    isn't in processed, plus the message before it as context, with touching
    stretches merged. processed=None (never processed) means the whole chat.

    a stretch only counts if a new/edited USER message is in it: extraction never
    stores what only the assistant said, so a regenerated reply alone costs nothing"""
    if processed is None:
        processed = set()
    changed = {i for i, m in enumerate(messages) if message_fingerprint(m) not in processed}
    wanted = changed | {i - 1 for i in changed if i > 0}

    runs, current, previous = [], [], None
    for i in sorted(wanted):
        if current and i != previous + 1:
            runs.append(current)
            current = []
        current.append(i)
        previous = i
    if current:
        runs.append(current)

    return [
        [messages[i] for i in run] for run in runs
        if any(i in changed and messages[i].get("role") == "user" and is_conversation_message(messages[i])
               for i in run)
    ]


def build_transcript_chunks(runs: list, max_chars: int = EXTRACT_CHUNK_CHARS,
                            max_message_chars: int = EXTRACT_MESSAGE_CHARS) -> list:
    """turn stretches of chat messages into 'USER: ...' / 'ASSISTANT: ...' transcript
    chunks for extraction ("[...]" marks a gap between stretches). tool calls/results,
    commands and ghost messages are left out, and chunks without a single user line
    are dropped (nothing to learn about the user). a plain list of messages counts
    as one stretch"""
    if runs and isinstance(runs[0], dict):
        runs = [runs]

    lines = []
    for run in runs:
        run_lines = []
        for message in run:
            if not is_conversation_message(message):
                continue
            text = message_text(message)
            if len(text) > max_message_chars:
                text = text[:max_message_chars] + " [...]"
            run_lines.append(f"{message['role'].upper()}: {text}")
        if run_lines and lines:
            lines.append("[...]")
        lines += run_lines

    chunks, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)

    tidy = []
    for chunk in chunks:
        # a gap marker means nothing at the very start or end of a chunk
        while chunk and chunk[0] == "[...]":
            chunk = chunk[1:]
        while chunk and chunk[-1] == "[...]":
            chunk = chunk[:-1]
        if any(line.startswith("USER: ") for line in chunk):
            tidy.append("\n".join(chunk))
    return tidy


def parse_operations(text: str) -> list:
    """pull the operations list out of the ai's reply. tolerates reasoning blocks,
    code fences and slightly broken json. raises ValueError if there's no json at all"""
    text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL)
    # the reply is either {"operations": [...]} or a bare [...] list: start from
    # whichever bracket comes first, or a bare list would parse as its first item
    candidates = [(text.find(o), o, c) for o, c in (("{", "}"), ("[", "]")) if text.find(o) != -1]
    if not candidates:
        raise ValueError("no json in the reply")
    start, _, closer = min(candidates)
    end = text.rfind(closer)
    if end <= start:
        raise ValueError("no json in the reply")
    raw = text[start:end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json_repair.loads(raw)

    if isinstance(data, dict):
        data = data.get("operations", [])
    if not isinstance(data, list):
        raise ValueError("operations is not a list")
    return [op for op in data if isinstance(op, dict)][:MAX_OPERATIONS_PER_CHUNK]


def contains_secret(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SECRET_RES)


def fuse_rankings(fts_hits: list, vec_hits: list, k: int = RRF_K) -> list:
    """merge keyword and vector results with reciprocal rank fusion.
    returns [(memory, score)] best first"""
    scores, memories = {}, {}
    for hits in (fts_hits, vec_hits):
        for rank, (memory, _) in enumerate(hits):
            scores[memory["id"]] = scores.get(memory["id"], 0.0) + 1.0 / (k + rank + 1)
            memories[memory["id"]] = memory
    return sorted(((memories[i], s) for i, s in scores.items()), key=lambda pair: pair[1], reverse=True)


def is_contentless(text: str) -> bool:
    """true for messages with nothing to recall against ("hi", "thanks!", "¿qué tal?")"""
    words = re.findall(r"\w+", str(text or "").lower())
    return not any(len(w) > 1 and w not in core.memory_store.STOPWORDS and w not in GREETINGS for w in words)


def relevance_gate(candidates: list, keyword_ids: set, similarities: list, min_z: float = RECALL_MIN_Z,
                   min_memories: int = RECALL_MIN_MEMORIES_FOR_GATE) -> list:
    """keep the candidates that are plausibly about this message: keyword matches
    on real content words always pass; vector matches must stand out from the
    rest of the user's memories by at least min_z standard deviations.
    similarities: {memory id: cosine} for the user's memories (up to RECALL_POOL)"""
    values = list(similarities.values())
    if len(values) < min_memories:
        return list(candidates)
    mean = sum(values) / len(values)
    std = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5 or 1e-9
    kept = []
    for memory in candidates:
        if memory["id"] in keyword_ids:
            kept.append(memory)
        elif memory["id"] in similarities and (similarities[memory["id"]] - mean) / std >= min_z:
            kept.append(memory)
    return kept


def format_memory(memory: dict) -> str:
    flags = memory["category"] + (", pinned" if memory.get("pinned") else "")
    return f"[{memory['id']}] ({flags}) {memory['content']}"


def _panel_item(memory: dict) -> dict:
    """the fields the webui memories panel needs"""
    return {key: memory[key] for key in ("id", "content", "category", "pinned", "updated_at")}


class Memory(core.module.Module):
    """Gives your AI a long-term memory of you that carries across chats and sessions. It picks up facts and preferences from your conversations automatically, and you can also ask it to remember or forget things."""

    dependencies = ["sqlite-vec>=0.1.6", "onnxruntime>=1.17", "tokenizers>=0.15", "numpy>=1.24"]

    settings = {
        "auto_extract": {
            "default": True,
            "description": "Automatically pick up things worth remembering about you (facts, preferences, instructions) when you leave a conversation: switching chats, starting a new chat, or clearing one. Uses one extra AI request per conversation."
        },
        "extract_from_character_chats": {
            "default": False,
            "description": "Also learn from chats where a character is active. Off by default, since roleplay usually isn't about you.",
            "depends": "auto_extract"
        },
        "idle_extract_minutes": {
            "default": 30,
            "min": 0,
            "description": "If you keep using the same chat without switching, learn from it after this many minutes of inactivity. 0 turns this off.",
            "depends": "auto_extract"
        },
        "core_memories_in_system_prompt": {
            "default": True,
            "description": "Always keep your most important memories in view of the AI: pinned memories, plus what it knows about who you are (profile) and how you want it to behave (instructions). You can ask the AI to pin or unpin a memory."
        },
        "max_core_memories": {
            "default": 20,
            "min": 1,
            "description": "How many core memories to keep in view at most (pinned first, then the most important).",
            "depends": "core_memories_in_system_prompt"
        },
        "recall_relevant_memories": {
            "default": True,
            "description": "Before each reply, look up memories related to your message and show them to the AI, so it remembers things about you without having to search."
        },
        "recall_count": {
            "default": 5,
            "min": 1,
            "max": 20,
            "description": "How many related memories to show at most per message.",
            "depends": "recall_relevant_memories"
        },
        "recall_in_character_chats": {
            "default": True,
            "description": "Keep using your memories while a character is active, so characters still know who you are."
        },
        "insert_nudge_prompt": {
            "default": False,
            "description": "Whether to put extra instructions in the end prompt (after message history) to help the AI autonomously use its memory tools, so that it remembers things without you having to explicitely ask it to."
        },
    }

    async def on_ready(self):
        # one sqlite store per user (None = global/cli), opened on first use
        self._stores = {}
        # serializes extraction per user, so quick back-and-forth switches queue up
        self._extract_locks = {}
        # (user, chat_id) -> {"at", "path", "metadata"}: chats with new messages, for the idle fallback
        self._dirty = {}
        # background task -> the user it works for (None = not user specific)
        self._tasks = {}
        # when to retry a failed model download/load, and how long to wait after that
        self._model_retry_at = None
        self._model_retry_delay = MODEL_RETRY_FIRST_SECONDS
        # user -> counter bumped on every memory change; part of the recall cache key
        self._revisions = {}
        # user -> ((chat_id, message hash, revision), prompt text): the end prompt is
        # built several times per turn (token counting etc), recall runs once
        self._recall_cache = {}
        # start the one-time model download now (server launch), not on first use
        self._spawn(self._warm_up())

    @property
    def character_safe(self):
        # keep recalling memories while a character is active (see core.module.Module.character_safe)
        return bool(self.config.get("recall_in_character_chats"))

    @character_safe.setter
    def character_safe(self, value):
        pass  # set by the base class; ours always follows the setting

    def _changed(self):
        """note that the current user's memories changed (invalidates cached recall)"""
        user = core.current_user.get()
        self._revisions[user] = self._revisions.get(user, 0) + 1

    async def on_shutdown(self):
        for task in list(getattr(self, "_tasks", {})):
            task.cancel()
        for store in getattr(self, "_stores", {}).values():
            store.close()
        self._stores = {}

    async def on_before_user_delete(self, username: str):
        # the user's data folder is about to be deleted. stop their extraction /
        # embedding work first: a task that kept running would reopen memory.db
        # (recreating the folder, or blocking its deletion on windows)
        tasks = [task for task, user in self._tasks.items() if user == username]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=10)
        self._drop_user(username)

    async def on_user_deleted(self, username: str):
        await super().on_user_deleted(username)
        self._drop_user(username)

    def _drop_user(self, username):
        """forget everything held for a user: their open store and per-user caches"""
        store = self._stores.pop(username, None)
        if store:
            store.close()
        self._extract_locks.pop(username, None)
        self._revisions.pop(username, None)
        self._recall_cache.pop(username, None)
        for key in [k for k in self._dirty if k[0] == username]:
            self._dirty.pop(key, None)

    # ------------------
    # internals
    # ------------------
    def _spawn(self, coro):
        """run a background task (it inherits the current user context)"""
        task = asyncio.create_task(coro)
        self._tasks[task] = core.current_user.get()
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self._tasks.pop(task, None)
        if not task.cancelled() and task.exception():
            self.log("module error", f"memory: background task failed: {core.detail_error(task.exception())}")

    async def _warm_up(self):
        try:
            await core.embeddings.get().ensure_ready()
        except core.embeddings.EmbeddingsError:
            # already logged. retry later with backoff (eg. huggingface was unreachable at startup)
            self._model_retry_at = time.monotonic() + self._model_retry_delay
            self.log("memory", f"retrying the memory model in {self._model_retry_delay // 60} minute(s)")
            self._model_retry_delay = min(self._model_retry_delay * 2, MODEL_RETRY_MAX_SECONDS)
            return
        self._model_retry_at = None
        self._model_retry_delay = MODEL_RETRY_FIRST_SECONDS
        for user in list(self._stores):
            token = core.current_user.set(user)
            try:
                self._spawn(self._embed_missing(self._stores[user]))
            finally:
                core.current_user.reset(token)

    def _store(self) -> core.memory_store.MemoryStore:
        """the current user's memory store (raises MemoryStoreError if it can't be opened)"""
        user = core.current_user.get()
        store = self._stores.get(user)
        if store is None:
            path = ":memory:" if core.storage.TEMPORARY else core.get_data_path("memory.db")
            store = core.memory_store.MemoryStore(path)
            store.configure_embeddings(core.embeddings.MODEL_ID, core.embeddings.MODEL_DIM)
            self._stores[user] = store
            if core.embeddings.get().ready:
                self._spawn(self._embed_missing(store))
        return store

    async def _run(self, fn, *args, **kwargs):
        """run a blocking store call off the event loop"""
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _embed_missing(self, store):
        """embed memories that don't have a vector yet (new, edited, restored, or saved
        while the model was still downloading)"""
        engine = core.embeddings.get()
        if not engine.ready:
            return 0
        done = 0
        while True:
            todo = await self._run(store.missing_embeddings, 32)
            if not todo:
                return done
            vectors = await engine.embed_passages([m["content"] for m in todo])
            for memory, vector in zip(todo, vectors):
                await self._run(store.set_embedding, memory["id"], vector)
            done += len(todo)
            if len(todo) < 32:
                return done

    async def _search(self, store, query: str, k: int = 8) -> list:
        """hybrid search: keyword (fts5) + meaning (embeddings), merged by rank.
        raises EmbeddingsError if the model isn't ready"""
        engine = core.embeddings.get()
        query_vector = (await engine.embed_queries([query]))[0]
        fts_hits = await self._run(store.fts_search, query, k * 3)
        vec_hits = await self._run(store.vec_search, query_vector, k * 3)
        return fuse_rankings(fts_hits, vec_hits)[:k]

    async def _save_new(self, store, content, category="fact", importance=3, pinned=False,
                        source="tool", source_chat=None):
        """add a memory unless it's a near-verbatim repeat. returns (memory, created)"""
        engine = core.embeddings.get()
        vector = (await engine.embed_passages([content]))[0] if engine.ready else None

        existing = await self._run(store.find_by_content, content)
        if existing:
            return existing, False
        if vector is not None:
            nearest = await self._run(store.vec_search, vector, 1)
            if nearest and nearest[0][1] >= DUPLICATE_SIMILARITY:
                return nearest[0][0], False

        memory = await self._run(store.add, content, category=category, importance=importance, pinned=pinned,
                                 source=source, source_chat=source_chat)
        self._changed()
        if vector is not None:
            await self._run(store.set_embedding, memory["id"], vector)
        return memory, True

    async def _update_content(self, store, memory_id, content):
        memory = await self._run(store.update, memory_id, content=content)
        self._changed()
        engine = core.embeddings.get()
        if memory and engine.ready:
            vector = (await engine.embed_passages([memory["content"]]))[0]
            await self._run(store.set_embedding, memory["id"], vector)
        return memory

    # ------------------
    # automatic extraction
    # ------------------
    async def on_chat_switch(self, old_chat_id, new_chat_id, old_messages, old_metadata):
        user = core.current_user.get()
        self._dirty.pop((user, old_chat_id), None)
        if not self.config.get("auto_extract"):
            return
        if old_metadata.get("character") and not self.config.get("extract_from_character_chats"):
            return
        # the switch waits for this hook, so the ai call runs in the background
        self._spawn(self._extract_chat(old_chat_id, old_messages))

    async def on_assistant_message(self, content):
        # remember which chat just got new messages, for the idle fallback
        if not self.config.get("auto_extract") or not self.channel:
            return
        try:
            chat = self.channel.context.chat
            entry = {"at": time.monotonic(), "path": chat.messages.data.path, "metadata": chat.get("metadata") or {}}
            self._dirty[(core.current_user.get(), chat.messages.chat_id)] = entry
        except Exception:
            pass

    async def on_background(self):
        while True:
            await asyncio.sleep(60)
            try:
                self._retry_model_if_due()
                self._sweep_idle_chats()
            except Exception as e:
                self.log("module error", f"memory: background check failed: {core.detail_error(e)}")

    def _retry_model_if_due(self):
        engine = core.embeddings.get()
        if engine.status != "failed" or self._model_retry_at is None or time.monotonic() < self._model_retry_at:
            return
        self._model_retry_at = None
        self._spawn(self._warm_up())

    def _model_unavailable_message(self) -> str:
        engine = core.embeddings.get()
        if engine.status == "failed":
            return (f"memory search is unavailable: the memory model couldn't be loaded ({engine.error}). "
                    "It is retried automatically in the background.")
        return "memory search is still starting up (the memory model is loading), try again shortly"

    def _sweep_idle_chats(self):
        minutes = self.config.get("idle_extract_minutes") or 0
        if not self.config.get("auto_extract") or minutes <= 0:
            return
        now = time.monotonic()
        for (user, chat_id), entry in list(self._dirty.items()):
            if now - entry["at"] < minutes * 60:
                continue
            self._dirty.pop((user, chat_id), None)
            if entry["metadata"].get("character") and not self.config.get("extract_from_character_chats"):
                continue
            token = core.current_user.set(user)
            try:
                self._spawn(self._extract_chat_file(chat_id, entry["path"]))
            finally:
                core.current_user.reset(token)

    async def _extract_chat_file(self, chat_id, path):
        try:
            with open(path, encoding="utf-8") as f:
                messages = json.load(f)
        except FileNotFoundError:
            return  # the chat was deleted meanwhile
        await self._extract_chat(chat_id, messages)

    async def _extract_chat(self, chat_id, messages):
        """learn from the messages added to a chat since the last extraction.
        returns (added, updated, deleted), or an error string if it was postponed"""
        user = core.current_user.get()
        lock = self._extract_locks.setdefault(user, asyncio.Lock())
        async with lock:
            try:
                return await self._extract_chat_locked(chat_id, messages)
            except (ExtractionError, core.embeddings.EmbeddingsError) as e:
                self.log("memory", f"extraction for chat {chat_id} postponed: {e}")
                return str(e)

    async def _extract_chat_locked(self, chat_id, messages):
        if not messages:
            return 0, 0, 0
        store = self._store()
        processed = await self._run(store.get_processed, chat_id)
        fingerprints = {message_fingerprint(m) for m in messages}
        if processed == fingerprints:
            return 0, 0, 0  # nothing new or edited

        # only the new/edited stretches are read (an edit or a regenerated reply in a
        # long chat doesn't resend the whole history)
        chunks = build_transcript_chunks(changed_runs(messages, processed))

        added = updated = deleted = 0
        if chunks:
            if not core.embeddings.get().ready:
                raise ExtractionError("the embedding model isn't ready yet")
            for chunk in chunks:
                ops = await self._ask_for_operations(store, chunk)
                if ops is None:
                    continue
                a, u, d = await self._apply_operations(store, ops, chat_id)
                added, updated, deleted = added + a, updated + u, deleted + d

        await self._run(store.set_processed, chat_id, fingerprints)
        if added or updated or deleted:
            self.log("memory", f"learned from chat {chat_id}: +{added} new, {updated} updated, {deleted} removed")
        return added, updated, deleted

    async def _ask_for_operations(self, store, transcript):
        """one ai call for one transcript chunk. returns the operations (with refs
        mapped to memory ids), or None if the reply couldn't be understood"""
        user_lines = "\n".join(line[6:] for line in transcript.splitlines() if line.startswith("USER: "))
        related = [m for m, _ in await self._search(store, user_lines[:2000], EXTRACT_RELATED_MEMORIES)]
        refs = {f"m{i + 1}": memory for i, memory in enumerate(related)}
        listing = "\n".join(f"[{ref}] ({m['category']}) {m['content']}" for ref, m in refs.items()) or "(none)"

        prompt = EXTRACTION_PROMPT.format(memories=listing, transcript=transcript)
        response = await self.manager.API.send(
            [{"role": "user", "content": prompt}],
            use_tools=False,
            temperature=0.2,
            # no reasoning: this is a small structured task and thinking only adds latency
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        if isinstance(response, core.api.APIError):
            raise ExtractionError(f"the AI request failed: {response}")

        try:
            ops = parse_operations(response.get("content", ""))
        except ValueError as e:
            self.log("memory", f"couldn't read the extraction reply ({e}), skipping that part of the chat")
            return None

        # the ai only ever sees refs (m1, m2...), never real ids. drop any id it
        # writes itself, so a made-up or guessed id can't touch an unrelated memory
        for op in ops:
            op.pop("id", None)
            if op.get("ref") in refs:
                op["id"] = refs[op["ref"]]["id"]
        return ops

    async def _apply_operations(self, store, ops, chat_id):
        added = updated = deleted = 0
        for op in ops:
            kind = str(op.get("op", "")).lower()
            content = str(op.get("content") or "").strip()[:MAX_MEMORY_CHARS]
            if kind in ("add", "update") and (not content or contains_secret(content)):
                continue
            try:
                if kind == "add":
                    category = op.get("category") if op.get("category") in core.memory_store.CATEGORIES else "fact"
                    _, created = await self._save_new(store, content, category=category,
                                                      importance=op.get("importance", 3),
                                                      source="extraction", source_chat=chat_id)
                    added += int(created)
                elif kind == "update" and op.get("id"):
                    if await self._update_content(store, op["id"], content):
                        updated += 1
                elif kind == "delete" and op.get("id"):
                    if await self._run(store.soft_delete, op["id"]):
                        self._changed()
                        deleted += 1
            except ValueError:
                continue
        return added, updated, deleted

    # ------------------
    # prompts
    # ------------------
    async def _core_memories(self, store) -> list:
        """the memories always kept in view: pinned ones first, then profile and
        instruction memories by importance. capped by count and by size"""
        limit = self.config.get("max_core_memories") or 20
        pinned = await self._run(store.list, pinned=True, limit=limit)
        rest = []
        for category in CORE_CATEGORIES:
            rest += await self._run(store.list, category=category, pinned=False, limit=limit)
        rest.sort(key=lambda m: (m["importance"], m["updated_at"]), reverse=True)

        chosen, size = [], 0
        for memory in (pinned + rest)[:limit]:
            size += len(memory["content"])
            if chosen and size > MAX_CORE_PROMPT_CHARS:
                break
            chosen.append(memory)
        return chosen

    async def on_system_prompt(self):
        sections = []
        if self.config.get("core_memories_in_system_prompt"):
            memories = await self._safely("loading core memories", self._core_memories_of_current_user, [])
            if memories:
                sections.append(
                    "These are things you remember about the user from earlier conversations. They are your own "
                    "memories: rely on them whenever they help, including when the user asks what you know or "
                    "remember about them. More memories may be given to you next to the user's messages. Don't bring "
                    "them up when they aren't relevant.\n"
                    + "\n".join(format_memory(m) for m in memories)
                )
        if core.config.get("model").get("use_tools", False):
            sections.append(
                "Things worth remembering about the user are saved automatically after each conversation. Use "
                "memory_remember when the user explicitly asks you to remember something, memory_forget when they "
                "ask you to forget something, and memory_search to look up something about them that you can't see."
            )
        sections.append(
            "Only claim to remember things about the user that are in your memories or in this conversation. "
            "Never invent past conversations or details the user hasn't told you."
        )
        return "\n\n".join(sections)

    async def _core_memories_of_current_user(self):
        return await self._core_memories(self._store())

    async def _safely(self, what, fn, fallback):
        """run a prompt-building step, turning ANY failure into the fallback. the
        manager disables a module for every user when one of its prompt hooks
        raises, so a passing sqlite hiccup (a closed connection, "database is
        locked") must only cost this one turn its memories"""
        try:
            return await fn()
        except core.embeddings.EmbeddingsError:
            return fallback  # model still loading or failed: expected, retried later
        except Exception as e:
            self.log("module error", f"memory: error while {what}: {core.detail_error(e)}")
            return fallback

    def _latest_user_message(self):
        """(chat id, text) of the latest user message in the current user's chat"""
        if not self.channel:
            return None, ""
        try:
            messages = self.channel.context.chat.messages
        except Exception:
            return None, ""
        if messages is None:
            return None, ""
        for message in reversed(messages.data):
            if message.get("role") == "user" and not (message.get("_metadata") or {}).get("is_cmd"):
                return messages.chat_id, message_text(message)
        return messages.chat_id, ""

    async def _recall(self, store, text: str, exclude_ids: set, k: int) -> list:
        """memories related to text, best first, that pass the relevance gate"""
        query_vector = (await core.embeddings.get().embed_queries([text]))[0]
        vec_hits = await self._run(store.vec_search, query_vector, RECALL_POOL)
        fts_hits = await self._run(store.fts_search, text, k * 3)
        similarities = {m["id"]: sim for m, sim in vec_hits}
        keyword_ids = {m["id"] for m, _ in fts_hits}
        fused = [m for m, _ in fuse_rankings(fts_hits, vec_hits[:k * 3]) if m["id"] not in exclude_ids]
        return relevance_gate(fused, keyword_ids, similarities)[:k]

    async def _recall_prompt(self):
        if not self.config.get("recall_relevant_memories"):
            return None
        chat_id, text = self._latest_user_message()
        if not text or is_contentless(text):
            return None

        user = core.current_user.get()
        k = self.config.get("recall_count") or 5
        key = (chat_id, hashlib.sha256(text.encode("utf-8")).hexdigest(), self._revisions.get(user, 0), k)
        cached = self._recall_cache.get(user)
        if cached and cached[0] == key:
            return cached[1]

        store = self._store()
        shown = set()
        if self.config.get("core_memories_in_system_prompt"):
            shown = {m["id"] for m in await self._core_memories(store)}
        memories = await self._recall(store, text, shown, k)

        prompt = None
        if memories:
            await self._run(store.touch, [m["id"] for m in memories])
            prompt = ("More things you remember about the user, in addition to the memories in your system prompt, "
                      "that may relate to their latest message. Use them only if they are actually relevant; "
                      "otherwise ignore them and don't mention them:\n"
                      + "\n".join(format_memory(m) for m in memories))
        self._recall_cache[user] = (key, prompt)
        return prompt

    async def on_end_prompt(self):
        sections = []
        # a failure isn't cached (the cache is only written on success), so the next build retries
        recalled = await self._safely("recalling memories", self._recall_prompt, None)
        if recalled:
            sections.append(recalled)
        if self.config.get("insert_nudge_prompt"):
            sections.append("You are responsible for managing your own long-term memory. You must proactively and autonomously decide to use the memory tools to maintain an accurate, up-to-date, and efficient record of the user, your own operational preferences, and important contextual facts. Do not wait for instructions to remember; if information is valuable for future interactions, store it immediately.")
        return "\n\n".join(sections) or None

    # ------------------
    # /memory command
    # ------------------
    @core.module.command("memory", send_to_ai=False, help={
        "": "shows everything that is remembered about you",
        "search <text>": "searches your memories",
        "forget <id>": "forgets a memory",
        "undo": "brings back the last forgotten memory",
        "learn": "learns from the current chat right now",
        "status": "shows the state of the memory system",
    })
    async def memory_command(self, args: list):
        sub = args[0].lower() if args else ""
        rest = " ".join(args[1:]).strip()
        try:
            store = self._store()
        except core.memory_store.MemoryStoreError as e:
            return f"The memory system is unavailable: {e}"

        if sub in ("", "list"):
            memories = await self._run(store.list, limit=200)
            if not memories:
                return "Nothing is remembered about you yet."
            return f"{len(memories)} memories:\n" + "\n".join(format_memory(m) for m in memories)

        if sub == "search":
            if not rest:
                return "usage: /memory search <text>"
            try:
                hits = await self._search(store, rest, 10)
            except core.embeddings.EmbeddingsError:
                return self._model_unavailable_message()
            return "\n".join(format_memory(m) for m, _ in hits) or f"No memories found for '{rest}'."

        if sub == "forget":
            if not rest:
                return "usage: /memory forget <id>"
            if not await self._run(store.soft_delete, rest):
                return "No memory with that id."
            self._changed()
            return "Forgotten. Use /memory undo to bring it back."

        if sub == "undo":
            memory = await self._run(store.last_deleted)
            if not memory:
                return "There's nothing to bring back."
            restored = await self._run(store.restore, memory["id"])
            self._changed()
            await self._embed_missing(store)
            return f"Restored: {format_memory(restored)}"

        if sub == "learn":
            chat = self.channel.context.chat if self.channel else None
            if chat is None or chat.messages is None:
                return "There's no chat to learn from."
            result = await self._extract_chat(chat.messages.chat_id, list(chat.messages.data))
            if isinstance(result, str):
                return f"Couldn't learn from this chat right now: {result}"
            added, updated, deleted = result
            if not (added or updated or deleted):
                return "Nothing new to remember from this chat."
            return f"Learned from this chat: {added} new, {updated} updated, {deleted} removed."

        if sub == "status":
            engine = core.embeddings.get()
            pending = sum(1 for key in self._dirty if key[0] == core.current_user.get())
            return (f"memories: {await self._run(store.count)}"
                    f" ({len(await self._run(store.list, pinned=True))} pinned)\n"
                    f"memory model: {engine.status}" + (f" ({engine.error})" if engine.error else "") + "\n"
                    f"automatic learning: {'on' if self.config.get('auto_extract') else 'off'}"
                    f" ({pending} chat(s) waiting for the idle check)\n"
                    f"recall: {'on' if self.config.get('recall_relevant_memories') else 'off'}")

        return "unknown subcommand, see /help memory"

    # ------------------
    # webui memories panel (channels/webui.py calls these for the logged-in user)
    # ------------------
    async def _panel_list(self) -> dict:
        store = self._store()
        engine = core.embeddings.get()
        memories = await self._run(store.list, limit=1000)
        return {
            "memories": [_panel_item(m) for m in memories],
            "categories": list(core.memory_store.CATEGORIES),
            # only worth showing while search is degraded; ready/idle say nothing
            "model_note": self._model_unavailable_message() if engine.status in ("downloading", "loading", "failed")
            else "",
        }

    async def _panel_search(self, query: str) -> list:
        """ids of memories that match query by keyword or clearly by meaning. the panel
        does its own substring filtering, so this only adds what that can't find"""
        store = self._store()
        fts_hits = await self._run(store.fts_search, query, 50)
        keyword_ids = [m["id"] for m, _ in fts_hits]
        try:
            query_vector = (await core.embeddings.get().embed_queries([query]))[0]
        except core.embeddings.EmbeddingsError:
            return keyword_ids  # model still loading: keyword matches only
        vec_hits = await self._run(store.vec_search, query_vector, RECALL_POOL)
        similarities = {m["id"]: sim for m, sim in vec_hits}
        if len(similarities) < RECALL_MIN_MEMORIES_FOR_GATE:
            # too few memories to tell a real match from noise
            return keyword_ids
        fused = [m for m, _ in fuse_rankings(fts_hits, vec_hits[:50])]
        return [m["id"] for m in relevance_gate(fused, set(keyword_ids), similarities)]

    async def _panel_add(self, content: str, category: str = "fact") -> dict:
        if category not in core.memory_store.CATEGORIES:
            category = "fact"
        memory, _ = await self._save_new(self._store(), content, category=category, source="user")
        return _panel_item(memory)

    async def _panel_update(self, memory_id: str, content=None, category=None, pinned=None) -> dict:
        store = self._store()
        memory = await self._run(store.get, memory_id)
        if memory is None:
            raise ValueError("No memory with that id")
        if category is not None or pinned is not None:
            memory = await self._run(store.update, memory_id, category=category, pinned=pinned)
            self._changed()
        if content is not None and content != memory["content"]:
            memory = await self._update_content(store, memory_id, content)
        return _panel_item(memory)

    async def _panel_forget(self, memory_id: str) -> bool:
        if not await self._run(self._store().soft_delete, memory_id):
            raise ValueError("No memory with that id")
        self._changed()
        return True

    async def _panel_restore(self, memory_id: str) -> dict:
        store = self._store()
        memory = await self._run(store.restore, memory_id)
        if memory is None:
            raise ValueError("That memory can't be brought back")
        self._changed()
        await self._embed_missing(store)
        return _panel_item(memory)

    # ------------------
    # tools
    # ------------------
    async def remember(self, content: str, category: str = "fact", pinned: bool = False):
        """Saves something to your long-term memory of the user, so you still know it in future chats. Only use it when the user explicitly asks you to remember something. Everything else worth knowing about the user is saved automatically after the conversation, so don't call this for facts the user merely mentions.

        Args:
            content: one short, self-contained sentence about the user, in the third person, written in the language the user used (never translate: Spanish facts as "El usuario...", English ones as "The user...")
            category: one of profile, preference, fact, event, instruction
            pinned: true to always keep this memory in view (only when the user asks for it to always be remembered)
        """
        if category not in core.memory_store.CATEGORIES:
            category = "fact"
        try:
            store = self._store()
            memory, created = await self._save_new(store, content, category=category, pinned=pinned)
        except (core.memory_store.MemoryStoreError, ValueError) as e:
            return self.result(f"couldn't save the memory: {e}", success=False)
        if not created:
            return self.result(f"already remembered: {format_memory(memory)}")
        return self.result(f"remembered: {format_memory(memory)}")

    async def search(self, query: str):
        """Searches your long-term memory of the user by meaning and keywords. Use it when you need to recall something about the user that isn't already in view.

        Args:
            query: what you're looking for, in any language
        """
        try:
            hits = await self._search(self._store(), query, 8)
        except core.embeddings.EmbeddingsError:
            return self.result(self._model_unavailable_message(), success=False)
        except core.memory_store.MemoryStoreError as e:
            return self.result(f"memory search failed: {e}", success=False)
        if not hits:
            return self.result(f"No memories found for '{query}'.")
        return self.result("\n".join(format_memory(m) for m, _ in hits))

    async def update(self, id: str, content: str):
        """Rewrites a memory that is outdated or wrong. ONLY use an id you can actually see; never make one up.

        Args:
            id: the memory's id
            content: the corrected memory, one short sentence
        """
        try:
            memory = await self._update_content(self._store(), id, content)
        except (core.memory_store.MemoryStoreError, ValueError) as e:
            return self.result(f"couldn't update the memory: {e}", success=False)
        if not memory:
            return self.result("memory with that id not found!", success=False)
        return self.result(f"updated: {format_memory(memory)}")

    async def forget(self, id: str):
        """Forgets a memory (when the user asks you to, or it's no longer true). ONLY use an id you can actually see.

        Args:
            id: the memory's id
        """
        try:
            ok = await self._run(self._store().soft_delete, id)
        except core.memory_store.MemoryStoreError as e:
            return self.result(f"couldn't forget the memory: {e}", success=False)
        if ok:
            self._changed()
        return self.result(f"memory {id} forgotten" if ok else "memory with that id not found!", success=ok)

    async def pin(self, id: str):
        """Pins a memory so it is always in view. Use sparingly, for the most important facts and instructions.

        Args:
            id: the memory's id
        """
        return await self._set_pinned(id, True)

    async def unpin(self, id: str):
        """Unpins a memory that no longer needs to be in view all the time.

        Args:
            id: the memory's id
        """
        return await self._set_pinned(id, False)

    async def _set_pinned(self, id, pinned):
        try:
            memory = await self._run(self._store().update, id, pinned=pinned)
        except core.memory_store.MemoryStoreError as e:
            return self.result(f"couldn't change the memory: {e}", success=False)
        if not memory:
            return self.result("memory with that id not found!", success=False)
        self._changed()
        return self.result(f"{'pinned' if pinned else 'unpinned'}: {format_memory(memory)}")

    async def list(self, category: str = ""):
        """Lists what you remember about the user, most important first.

        Args:
            category: optionally only one of profile, preference, fact, event, instruction
        """
        if category and category not in core.memory_store.CATEGORIES:
            return self.result(f"unknown category, use one of: {', '.join(core.memory_store.CATEGORIES)}",
                               success=False)
        try:
            memories = await self._run(self._store().list, category=category or None, limit=100)
        except core.memory_store.MemoryStoreError as e:
            return self.result(f"couldn't list memories: {e}", success=False)
        if not memories:
            return self.result("No memories yet.")
        return self.result("\n".join(format_memory(m) for m in memories))

"""
per-user long-term memory storage: one sqlite file per user (data/{username}/memory.db)

- memories: the source of truth (soft deletes via deleted_at)
- memories_fts: fts5 keyword index over content + tags, kept in sync by triggers
- memories_vec: sqlite-vec index of the embeddings (cosine), rowid = memories.pk.
  only created once configure_embeddings() knows the model + dimension
- extraction_state: per chat, the fingerprints of the messages already learned
  from, so automatic extraction only ever reads messages that are new or edited

sqlite-vec is required: if it can't be loaded the store refuses to open
(MemoryStoreError), there is no keyword-only fallback.

this file deliberately doesn't import core, so it can be unit tested on its own.
all methods are blocking; callers on the event loop should use asyncio.to_thread().
"""
import datetime
import json
import os
import re
import sqlite3
import threading

import ulid

SCHEMA_VERSION = 1

CATEGORIES = ("profile", "preference", "fact", "event", "instruction")
SOURCES = ("tool", "extraction", "user")

MAX_EMBED_DIM = 8192
MAX_FTS_TERMS = 32

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# function words that carry no meaning on their own. without these a spanish
# message like "¿qué tal la playa?" keyword-matches nearly every spanish memory
# through "la"/"el"/"de" (and english ones through "the"/"what"/"is")
STOPWORDS = frozenset("""
a about after again all also am an and any are as at be because been before being but by can could did do
does doing down for from had has have having he her here hers him his how i if in into is it its just me
more most my no nor not now of off on once only or other our out over own same she should so some such
than that the their them then there these they this those through to too under until up very was we were
what when where which while who whom why will with would you your yours
al algo como con cual cuando de del desde donde el ella ellas ellos en era es esa ese eso esta este esto
estoy fue ha hay la las le les lo los mas me mi mis muy no nos o para pero por que qué quien se si sin sobre
son su sus tal te tu tus un una uno unos y ya yo cómo cuál cuándo dónde quién
""".split())

_COLUMNS = (
    "pk, id, content, category, tags, pinned, importance, source, source_chat, "
    "created_at, updated_at, last_used, use_count, deleted_at"
)


def content_key(text: str) -> str:
    """normalized form of a memory's text (lowercase words only), so exact repeats
    can be found with one indexed lookup ("The user likes JAZZ." == "the user likes jazz")"""
    return " ".join(_TOKEN_RE.findall(str(text or "").lower()))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    pk          INTEGER PRIMARY KEY,
    id          TEXT UNIQUE NOT NULL,
    content     TEXT NOT NULL,
    content_key TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'fact',
    tags        TEXT NOT NULL DEFAULT '',
    pinned      INTEGER NOT NULL DEFAULT 0,
    importance  INTEGER NOT NULL DEFAULT 3,
    source      TEXT NOT NULL DEFAULT 'tool',
    source_chat TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    last_used   TEXT,
    use_count   INTEGER NOT NULL DEFAULT 0,
    deleted_at  TEXT
);

CREATE INDEX IF NOT EXISTS memories_live ON memories(deleted_at, category);
CREATE INDEX IF NOT EXISTS memories_content_key ON memories(content_key);

-- no porter stemmer: it's english-only and mangles other languages (we have
-- spanish speaking users). remove_diacritics makes "cafe" match "café"
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, tags,
    content='memories', content_rowid='pk',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.pk, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags) VALUES ('delete', old.pk, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content, tags ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags) VALUES ('delete', old.pk, old.content, old.tags);
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.pk, new.content, new.tags);
END;

-- message_hashes: json list of short per-message fingerprints
CREATE TABLE IF NOT EXISTS extraction_state (
    chat_id        TEXT PRIMARY KEY,
    message_hashes TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
"""


class MemoryStoreError(Exception):
    """the store can't be opened or used (missing sqlite-vec, newer schema, bad embedding, ...)"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def fts_query(text: str):
    """turn arbitrary user text into a safe fts5 query: every word becomes a
    quoted term, OR'd together. quoting means punctuation, operators (NEAR, -,
    *, ^) and stray quotes can never produce an fts5 syntax error.
    returns None if the text has no searchable words"""
    terms = []
    for token in _TOKEN_RE.findall(str(text or "").lower()):
        # single letters are noise in both english and spanish ("what's" -> "s",
        # "a", "y", "o") and match nearly everything; single digits are kept
        if len(token) == 1 and not token.isdigit():
            continue
        if token in STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
        if len(terms) >= MAX_FTS_TERMS:
            break
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def _normalize_tags(tags):
    """tags are stored as one space separated string (indexed by fts5).
    multi-word tags get joined with underscores so they stay one tag"""
    if not tags:
        return ""
    if isinstance(tags, str):
        tags = [tags]
    seen = []
    for tag in tags:
        tag = "_".join(str(tag).strip().lower().split())
        if tag and tag not in seen:
            seen.append(tag)
    return " ".join(seen)


def _check_category(category):
    if category not in CATEGORIES:
        raise ValueError(f"unknown memory category '{category}' (expected one of: {', '.join(CATEGORIES)})")
    return category


def _clamp_importance(importance):
    try:
        importance = int(importance)
    except (TypeError, ValueError):
        importance = 3
    return max(1, min(5, importance))


def _row_to_dict(row):
    return {
        "id": row["id"],
        "content": row["content"],
        "category": row["category"],
        "tags": row["tags"].split() if row["tags"] else [],
        "pinned": bool(row["pinned"]),
        "importance": row["importance"],
        "source": row["source"],
        "source_chat": row["source_chat"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_used": row["last_used"],
        "use_count": row["use_count"],
        "deleted_at": row["deleted_at"],
    }


class MemoryStore:
    def __init__(self, path: str):
        """open (or create) a memory database. path ':memory:' gives a throwaway
        in-memory store (used for --tmp mode)"""
        self.path = path
        self._lock = threading.RLock()

        try:
            import sqlite_vec
        except ImportError as e:
            raise MemoryStoreError("the sqlite-vec package is not installed") from e
        self._serialize = sqlite_vec.serialize_float32

        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

        try:
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
        except (AttributeError, sqlite3.Error) as e:
            # AttributeError: python builds without extension loading support
            self._conn.close()
            raise MemoryStoreError(f"could not load the sqlite-vec extension: {e}") from e

        # rollback journal (not WAL): auto_backup zips the raw files in data/,
        # and a WAL database keeps recent commits in a separate -wal file
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=FULL")

        try:
            self._init_schema()
        except Exception:
            self._conn.close()
            raise

    # ------------------
    # setup
    # ------------------
    def _init_schema(self):
        with self._lock:
            version = None
            has_meta = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if has_meta:
                row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                version = int(row["value"]) if row else None

            if version is not None and version > SCHEMA_VERSION:
                raise MemoryStoreError(
                    f"memory database {self.path} has schema version {version}, "
                    f"but this version of openlumara only understands up to {SCHEMA_VERSION}"
                )

            # future migrations go here: `if version == 1: ...migrate to 2...`
            self._conn.executescript(_SCHEMA)
            with self._conn:
                self._set_meta("schema_version", str(SCHEMA_VERSION))

    def _get_meta(self, key):
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key, value):
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------
    # embeddings config
    # ------------------
    @property
    def embed_model(self):
        with self._lock:
            return self._get_meta("embed_model")

    @property
    def embed_dim(self):
        with self._lock:
            dim = self._get_meta("embed_dim")
            return int(dim) if dim else None

    def _vec_table_exists(self):
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='memories_vec'"
        ).fetchone() is not None

    def configure_embeddings(self, model: str, dim: int) -> bool:
        """make sure the vector index matches the embedding model in use.

        vectors from different models (or sizes) can't be compared, so when the
        model or dimension changes the whole index is dropped and rebuilt empty.
        returns True if that happened (every memory then needs re-embedding,
        see missing_embeddings()), False if the index was already up to date"""
        if not model:
            raise ValueError("embedding model name is required")
        try:
            dim = int(dim)
        except (TypeError, ValueError):
            raise ValueError(f"invalid embedding dimension: {dim!r}")
        if not 1 <= dim <= MAX_EMBED_DIM:
            raise ValueError(f"invalid embedding dimension: {dim}")

        with self._lock:
            if (self._vec_table_exists() and self._get_meta("embed_model") == model
                    and self._get_meta("embed_dim") == str(dim)):
                return False

            with self._conn:
                self._conn.execute("DROP TABLE IF EXISTS memories_vec")
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE memories_vec USING vec0(embedding float[{dim}] distance_metric=cosine)"
                )
                self._set_meta("embed_model", model)
                self._set_meta("embed_dim", str(dim))
            return True

    def _require_vec(self):
        if not self._vec_table_exists():
            raise MemoryStoreError("embeddings are not configured yet (call configure_embeddings first)")

    def _check_vector(self, vector):
        dim = self.embed_dim
        if vector is None or len(vector) != dim:
            got = None if vector is None else len(vector)
            raise MemoryStoreError(f"embedding has dimension {got}, expected {dim}")
        return self._serialize([float(x) for x in vector])

    # ------------------
    # memories
    # ------------------
    def _get_row(self, id, include_deleted=False):
        sql = f"SELECT {_COLUMNS} FROM memories WHERE id=?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        return self._conn.execute(sql, (str(id).strip(),)).fetchone()

    def add(self, content: str, category: str = "fact", tags=None, pinned: bool = False,
            importance: int = 3, source: str = "tool", source_chat: str = None) -> dict:
        """store a new memory and return it"""
        content = str(content or "").strip()
        if not content:
            raise ValueError("memory content can't be empty")
        _check_category(category)
        if source not in SOURCES:
            raise ValueError(f"unknown memory source '{source}'")

        now = _now()
        memory_id = str(ulid.ULID())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memories(id, content, content_key, category, tags, pinned, importance, source, "
                "source_chat, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, content, content_key(content), category, _normalize_tags(tags), int(bool(pinned)),
                 _clamp_importance(importance), source, source_chat, now, now)
            )
            return _row_to_dict(self._get_row(memory_id))

    def find_by_content(self, content: str):
        """the live memory whose text equals content after normalization, or None"""
        key = content_key(content)
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM memories WHERE content_key=? AND deleted_at IS NULL LIMIT 1", (key,)
            ).fetchone()
            return _row_to_dict(row) if row else None

    def get(self, id: str, include_deleted: bool = False):
        """get one memory by id, or None"""
        with self._lock:
            row = self._get_row(id, include_deleted)
            return _row_to_dict(row) if row else None

    def update(self, id: str, content: str = None, category: str = None, tags=None,
               pinned: bool = None, importance: int = None):
        """change fields of a live memory. returns the updated memory, or None if
        it doesn't exist. changing the content drops its embedding (it's stale
        now), so it shows up in missing_embeddings() until re-embedded"""
        fields = {}
        if content is not None:
            content = str(content).strip()
            if not content:
                raise ValueError("memory content can't be empty")
            fields["content"] = content
            fields["content_key"] = content_key(content)
        if category is not None:
            fields["category"] = _check_category(category)
        if tags is not None:
            fields["tags"] = _normalize_tags(tags)
        if pinned is not None:
            fields["pinned"] = int(bool(pinned))
        if importance is not None:
            fields["importance"] = _clamp_importance(importance)

        with self._lock:
            row = self._get_row(id)
            if row is None:
                return None
            if not fields:
                return _row_to_dict(row)

            fields["updated_at"] = _now()
            assignments = ", ".join(f"{k}=?" for k in fields)
            with self._conn:
                self._conn.execute(f"UPDATE memories SET {assignments} WHERE pk=?", (*fields.values(), row["pk"]))
                if "content" in fields and fields["content"] != row["content"] and self._vec_table_exists():
                    self._conn.execute("DELETE FROM memories_vec WHERE rowid=?", (row["pk"],))
            return _row_to_dict(self._get_row(id))

    def soft_delete(self, id: str) -> bool:
        """hide a memory from everything (recall, search, lists) but keep it so it
        can be restored. its embedding is dropped and recreated on restore"""
        with self._lock:
            row = self._get_row(id)
            if row is None:
                return False
            with self._conn:
                self._conn.execute("UPDATE memories SET deleted_at=? WHERE pk=?", (_now(), row["pk"]))
                if self._vec_table_exists():
                    self._conn.execute("DELETE FROM memories_vec WHERE rowid=?", (row["pk"],))
            return True

    def restore(self, id: str):
        """undo a soft delete. returns the restored memory or None"""
        with self._lock:
            row = self._get_row(id, include_deleted=True)
            if row is None or row["deleted_at"] is None:
                return None
            with self._conn:
                self._conn.execute(
                    "UPDATE memories SET deleted_at=NULL, updated_at=? WHERE pk=?", (_now(), row["pk"])
                )
            return _row_to_dict(self._get_row(id))

    def last_deleted(self):
        """the most recently soft-deleted memory (for undo), or None"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM memories WHERE deleted_at IS NOT NULL "
                "ORDER BY deleted_at DESC, pk DESC LIMIT 1"
            ).fetchone()
            return _row_to_dict(row) if row else None

    def purge(self, id: str) -> bool:
        """permanently delete a memory (live or soft-deleted). can't be undone"""
        with self._lock:
            row = self._get_row(id, include_deleted=True)
            if row is None:
                return False
            with self._conn:
                if self._vec_table_exists():
                    self._conn.execute("DELETE FROM memories_vec WHERE rowid=?", (row["pk"],))
                self._conn.execute("DELETE FROM memories WHERE pk=?", (row["pk"],))
            return True

    def list(self, category: str = None, pinned: bool = None, include_deleted: bool = False,
             limit: int = None) -> list:
        """list memories, most important first (then most recently updated)"""
        where, params = [], []
        if not include_deleted:
            where.append("deleted_at IS NULL")
        if category is not None:
            where.append("category=?")
            params.append(_check_category(category))
        if pinned is not None:
            where.append("pinned=?")
            params.append(int(bool(pinned)))

        sql = f"SELECT {_COLUMNS} FROM memories"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY pinned DESC, importance DESC, updated_at DESC, pk DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        with self._lock:
            return [_row_to_dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def count(self, include_deleted: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM memories"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        with self._lock:
            return self._conn.execute(sql).fetchone()[0]

    def touch(self, ids):
        """mark memories as used (they were recalled into a prompt)"""
        ids = [str(i).strip() for i in (ids or []) if i]
        if not ids:
            return
        now = _now()
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE memories SET last_used=?, use_count=use_count+1 WHERE id=? AND deleted_at IS NULL",
                [(now, i) for i in ids]
            )

    # ------------------
    # search
    # ------------------
    def fts_search(self, query: str, k: int = 30) -> list:
        """keyword search (bm25 ranked) over live memories.
        returns [(memory, score)] best first; a higher score is a better match"""
        match = fts_query(query)
        if match is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join('m.' + c.strip() for c in _COLUMNS.split(','))}, "
                "bm25(memories_fts) AS rank "
                "FROM memories_fts JOIN memories m ON m.pk = memories_fts.rowid "
                "WHERE memories_fts MATCH ? AND m.deleted_at IS NULL "
                "ORDER BY rank LIMIT ?",
                (match, int(k))
            ).fetchall()
        # bm25() is lower-is-better (negative), flip it so callers can treat higher as better
        return [(_row_to_dict(r), -r["rank"]) for r in rows]

    def vec_search(self, vector, k: int = 30) -> list:
        """nearest-neighbour search over live memories by embedding.
        returns [(memory, similarity)] best first, similarity = 1 - cosine distance"""
        with self._lock:
            self._require_vec()
            blob = self._check_vector(vector)
            # soft-deleted memories have no vector, so every hit is live
            rows = self._conn.execute(
                f"SELECT {', '.join('m.' + c.strip() for c in _COLUMNS.split(','))}, v.distance "
                "FROM (SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k = ?) v "
                "JOIN memories m ON m.pk = v.rowid "
                "WHERE m.deleted_at IS NULL ORDER BY v.distance",
                (blob, int(k))
            ).fetchall()
        return [(_row_to_dict(r), 1.0 - r["distance"]) for r in rows]

    def set_embedding(self, id: str, vector) -> bool:
        """store (or replace) the embedding of a live memory"""
        with self._lock:
            self._require_vec()
            blob = self._check_vector(vector)
            row = self._get_row(id)
            if row is None:
                return False
            with self._conn:
                self._conn.execute("DELETE FROM memories_vec WHERE rowid=?", (row["pk"],))
                self._conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (row["pk"], blob))
            return True

    def missing_embeddings(self, limit: int = 100) -> list:
        """live memories that have no embedding yet (new, edited, restored, or
        everything after the embedding model changed)"""
        with self._lock:
            if not self._vec_table_exists():
                sql = f"SELECT {_COLUMNS} FROM memories WHERE deleted_at IS NULL ORDER BY pk LIMIT ?"
            else:
                sql = (f"SELECT {_COLUMNS} FROM memories WHERE deleted_at IS NULL "
                       "AND pk NOT IN (SELECT rowid FROM memories_vec) ORDER BY pk LIMIT ?")
            return [_row_to_dict(r) for r in self._conn.execute(sql, (int(limit),)).fetchall()]

    # ------------------
    # extraction progress
    # ------------------
    def get_processed(self, chat_id: str):
        """fingerprints of a chat's messages that were already learned from, as a
        set, or None if this chat was never processed"""
        with self._lock:
            row = self._conn.execute(
                "SELECT message_hashes FROM extraction_state WHERE chat_id=?", (chat_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return set(json.loads(row["message_hashes"]))
        except (ValueError, TypeError):
            return set()  # unreadable record: treat everything as new

    def set_processed(self, chat_id: str, fingerprints):
        """record the fingerprints of all of a chat's current messages as processed"""
        payload = json.dumps(sorted(set(fingerprints)))
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO extraction_state(chat_id, message_hashes, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET message_hashes=excluded.message_hashes, "
                "updated_at=excluded.updated_at",
                (chat_id, payload, _now())
            )

    def delete_processed(self, chat_id: str):
        """forget a chat's extraction progress (eg. the chat was deleted)"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM extraction_state WHERE chat_id=?", (chat_id,))

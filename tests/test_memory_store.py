"""
Self-contained tests for core/memory_store.py (per-user sqlite memory storage:
fts5 keyword index + sqlite-vec embeddings + extraction progress).

memory_store doesn't import core, so it's loaded straight from its file and
the app is never bootstrapped. sqlite-vec is a hard requirement of the store,
so these tests need it installed (venv\\Scripts\\pip install sqlite-vec).

Run:  venv\\Scripts\\python tests\\test_memory_store.py
"""
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location(
    "ol_memory_store_under_test", os.path.join(ROOT, "core", "memory_store.py"))
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)

DIM = 4


def unit(*xs):
    n = sum(x * x for x in xs) ** 0.5
    return [x / n for x in xs]


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_memstore_")
        self.path = os.path.join(self.tmp, "alice", "memory.db")
        self.store = ms.MemoryStore(self.path)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reopen(self):
        self.store.close()
        self.store = ms.MemoryStore(self.path)


class SchemaTests(StoreTestCase):
    def test_creates_file_and_parent_dirs(self):
        self.assertTrue(os.path.isfile(self.path))

    def test_schema_version_recorded(self):
        self.assertEqual(self.store._get_meta("schema_version"), str(ms.SCHEMA_VERSION))

    def test_data_survives_reopen(self):
        m = self.store.add("likes green tea", category="preference", tags=["drinks"])
        self.reopen()
        self.assertEqual(self.store.get(m["id"])["content"], "likes green tea")
        self.assertEqual(self.store.fts_search("tea")[0][0]["id"], m["id"])

    def test_newer_schema_is_refused(self):
        with self.store._conn:
            self.store._set_meta("schema_version", str(ms.SCHEMA_VERSION + 1))
        self.store.close()
        with self.assertRaises(ms.MemoryStoreError):
            ms.MemoryStore(self.path)
        self.store = ms.MemoryStore(":memory:")  # so tearDown has something to close

    def test_rollback_journal_not_wal(self):
        mode = self.store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "delete")

    def test_in_memory_store_writes_nothing(self):
        mem = ms.MemoryStore(":memory:")
        try:
            mem.add("temporary fact")
            self.assertEqual(mem.count(), 1)
        finally:
            mem.close()
        self.assertFalse(os.path.exists(os.path.join(os.getcwd(), ":memory:")))

    def test_missing_sqlite_vec_is_a_hard_failure(self):
        with mock.patch.dict(sys.modules, {"sqlite_vec": None}):
            with self.assertRaises(ms.MemoryStoreError):
                ms.MemoryStore(":memory:")

    def test_extension_loading_unsupported_is_a_hard_failure(self):
        real_connect = sqlite3.connect

        class NoExtConn:
            def __init__(self, *a, **kw):
                self._c = real_connect(*a, **kw)

            def enable_load_extension(self, flag):
                raise AttributeError("enable_load_extension")

            def __getattr__(self, name):
                return getattr(self._c, name)

        with mock.patch.object(ms.sqlite3, "connect", NoExtConn):
            with self.assertRaises(ms.MemoryStoreError):
                ms.MemoryStore(":memory:")


class CrudTests(StoreTestCase):
    def test_add_and_get(self):
        m = self.store.add("  works as a nurse  ", category="profile", tags=["Job", "job", "night shift"],
                           pinned=True, importance=5, source="extraction", source_chat="CHAT1")
        got = self.store.get(m["id"])
        self.assertEqual(got["content"], "works as a nurse")
        self.assertEqual(got["category"], "profile")
        self.assertEqual(got["tags"], ["job", "night_shift"])
        self.assertTrue(got["pinned"])
        self.assertEqual(got["importance"], 5)
        self.assertEqual(got["source"], "extraction")
        self.assertEqual(got["source_chat"], "CHAT1")
        self.assertIsNone(got["deleted_at"])
        self.assertEqual(got["use_count"], 0)

    def test_ids_are_unique_ulids(self):
        ids = {self.store.add(f"fact {i}")["id"] for i in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(len(i) == 26 for i in ids))

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.store.add("   ")
        with self.assertRaises(ValueError):
            self.store.add("x", category="gossip")
        with self.assertRaises(ValueError):
            self.store.add("x", source="nowhere")

    def test_importance_is_clamped(self):
        self.assertEqual(self.store.add("a", importance=99)["importance"], 5)
        self.assertEqual(self.store.add("b", importance=-3)["importance"], 1)
        self.assertEqual(self.store.add("c", importance="lots")["importance"], 3)

    def test_get_unknown_and_whitespace_id(self):
        m = self.store.add("x")
        self.assertIsNone(self.store.get("NOPE"))
        self.assertEqual(self.store.get(f"  {m['id']} ")["id"], m["id"])

    def test_update_fields(self):
        m = self.store.add("lives in Boston", category="fact")
        before = m["updated_at"]
        up = self.store.update(m["id"], content="lives in Denver", category="profile", tags=["home"],
                               pinned=True, importance=4)
        self.assertEqual(up["content"], "lives in Denver")
        self.assertEqual(up["category"], "profile")
        self.assertEqual(up["tags"], ["home"])
        self.assertTrue(up["pinned"])
        self.assertEqual(up["importance"], 4)
        self.assertGreaterEqual(up["updated_at"], before)

    def test_update_unknown_or_deleted_returns_none(self):
        self.assertIsNone(self.store.update("NOPE", content="x"))
        m = self.store.add("x")
        self.store.soft_delete(m["id"])
        self.assertIsNone(self.store.update(m["id"], content="y"))

    def test_update_validation(self):
        m = self.store.add("x")
        with self.assertRaises(ValueError):
            self.store.update(m["id"], content="  ")
        with self.assertRaises(ValueError):
            self.store.update(m["id"], category="gossip")

    def test_update_with_no_fields_is_noop(self):
        m = self.store.add("x")
        self.assertEqual(self.store.update(m["id"])["updated_at"], m["updated_at"])

    def test_list_ordering_and_filters(self):
        low = self.store.add("low", importance=1)
        high = self.store.add("high", importance=5)
        pinned = self.store.add("pinned", importance=1, pinned=True)
        pref = self.store.add("pref", category="preference")
        self.assertEqual([m["id"] for m in self.store.list()][:2], [pinned["id"], high["id"]])
        self.assertEqual([m["id"] for m in self.store.list(category="preference")], [pref["id"]])
        self.assertEqual([m["id"] for m in self.store.list(pinned=True)], [pinned["id"]])
        self.assertEqual(len(self.store.list(limit=2)), 2)
        self.assertIn(low["id"], [m["id"] for m in self.store.list(pinned=False)])
        with self.assertRaises(ValueError):
            self.store.list(category="gossip")

    def test_find_by_content(self):
        m = self.store.add("The user likes Jazz.")
        self.assertEqual(self.store.find_by_content("the user likes JAZZ")["id"], m["id"])
        self.assertIsNone(self.store.find_by_content("the user likes jazz music"))
        self.assertIsNone(self.store.find_by_content("  !! "))
        # follows edits, ignores deleted memories
        self.store.update(m["id"], content="The user likes blues")
        self.assertIsNone(self.store.find_by_content("the user likes jazz"))
        self.assertEqual(self.store.find_by_content("THE USER LIKES BLUES")["id"], m["id"])
        self.store.soft_delete(m["id"])
        self.assertIsNone(self.store.find_by_content("the user likes blues"))

    def test_find_by_content_uses_the_index(self):
        plan = " ".join(row["detail"] for row in self.store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM memories WHERE content_key=? AND deleted_at IS NULL", ("x",)))
        self.assertIn("memories_content_key", plan)

    def test_touch(self):
        m = self.store.add("x")
        self.store.touch([m["id"], m["id"], "NOPE", None])
        got = self.store.get(m["id"])
        self.assertEqual(got["use_count"], 2)
        self.assertIsNotNone(got["last_used"])
        self.store.touch([])  # no-op


class DeleteTests(StoreTestCase):
    def test_soft_delete_hides_everywhere(self):
        m = self.store.add("secret hobby is knitting", tags=["hobby"])
        self.assertTrue(self.store.soft_delete(m["id"]))
        self.assertIsNone(self.store.get(m["id"]))
        self.assertIsNotNone(self.store.get(m["id"], include_deleted=True)["deleted_at"])
        self.assertEqual(self.store.list(), [])
        self.assertEqual(len(self.store.list(include_deleted=True)), 1)
        self.assertEqual(self.store.fts_search("knitting"), [])
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.count(include_deleted=True), 1)
        self.assertFalse(self.store.soft_delete(m["id"]))  # already gone

    def test_restore_and_last_deleted(self):
        a = self.store.add("alpha")
        b = self.store.add("bravo")
        self.store.soft_delete(a["id"])
        self.store.soft_delete(b["id"])
        self.assertEqual(self.store.last_deleted()["id"], b["id"])
        self.assertEqual(self.store.restore(b["id"])["id"], b["id"])
        self.assertEqual(self.store.last_deleted()["id"], a["id"])
        self.assertIsNone(self.store.restore(b["id"]))  # not deleted any more
        self.assertIsNone(self.store.restore("NOPE"))
        self.assertEqual(self.store.fts_search("bravo")[0][0]["id"], b["id"])

    def test_purge_is_permanent(self):
        m = self.store.add("forget me completely")
        self.store.soft_delete(m["id"])
        self.assertTrue(self.store.purge(m["id"]))
        self.assertIsNone(self.store.get(m["id"], include_deleted=True))
        self.assertEqual(self.store.count(include_deleted=True), 0)
        self.assertFalse(self.store.purge(m["id"]))
        # nothing left in the fts index either
        n = self.store._conn.execute("SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'forget'").fetchone()
        self.assertEqual(n[0], 0)


class FtsTests(StoreTestCase):
    def test_accents_and_case_insensitive(self):
        m = self.store.add("Le encanta el café de Colombia y la música clásica", tags=["Gustos"])
        for q in ("cafe", "CAFÉ", "musica", "clasica", "gustos"):
            hits = self.store.fts_search(q)
            self.assertEqual([h[0]["id"] for h in hits], [m["id"]], q)

    def test_searches_tags(self):
        m = self.store.add("has a golden retriever", tags=["pets"])
        self.assertEqual(self.store.fts_search("pets")[0][0]["id"], m["id"])

    def test_more_matching_terms_rank_higher(self):
        weak = self.store.add("the user likes dogs")
        strong = self.store.add("dog named Rex, a golden retriever dog")
        hits = self.store.fts_search("Rex golden retriever")
        self.assertEqual(hits[0][0]["id"], strong["id"])
        self.assertNotIn(weak["id"], [h[0]["id"] for h in hits])
        self.assertTrue(all(isinstance(h[1], float) for h in hits))

    def test_update_reindexes(self):
        m = self.store.add("lives in Boston", tags=["home"])
        self.store.update(m["id"], content="lives in Denver", tags=["city"])
        self.assertEqual(self.store.fts_search("boston"), [])
        self.assertEqual(self.store.fts_search("home"), [])
        self.assertEqual(self.store.fts_search("denver")[0][0]["id"], m["id"])
        self.assertEqual(self.store.fts_search("city")[0][0]["id"], m["id"])

    def test_hostile_queries_never_raise(self):
        self.store.add('he said "hi" -- NEAR(a b) * ^ OR AND')
        for q in ['"', '""', "*", "NEAR(", ")", "a OR", "-x", "'; DROP TABLE memories; --",
                  "col:val", "^start", "a AND NOT b", "{x}", "\\", "\x00", "🙂 emoji"]:
            self.store.fts_search(q)  # must not raise
        self.assertEqual(self.store.count(), 1)

    def test_empty_queries(self):
        self.store.add("x")
        for q in ("", "   ", "!!!", None):
            self.assertEqual(self.store.fts_search(q), [])

    def test_k_limit(self):
        for i in range(10):
            self.store.add(f"apple number {i}")
        self.assertEqual(len(self.store.fts_search("apple", k=3)), 3)

    def test_single_letters_are_not_search_terms(self):
        self.store.add("The user's sister Sarah lives in Denver")
        self.store.add("El usuario es alérgico a los cacahuetes")
        self.assertEqual(self.store.fts_search("what's my dog's name"), [])
        self.assertEqual(self.store.fts_search("recommend me a coffee"), [])
        self.assertEqual(ms.fts_query("a y o i 5 s"), '"5"')

    def test_stopwords_are_not_search_terms(self):
        self.store.add("El usuario vive en Madrid, en el barrio de Chamberí")
        self.store.add("The user has a dog")
        self.assertEqual(self.store.fts_search("¿qué tal la playa?"), [])
        self.assertEqual(self.store.fts_search("what is the weather"), [])
        self.assertEqual(len(self.store.fts_search("barrio de Madrid")), 1)
        self.assertIsNone(ms.fts_query("what is the"))

    def test_fts_query_builder(self):
        self.assertEqual(ms.fts_query("Dog dog CAT"), '"dog" OR "cat"')
        self.assertIsNone(ms.fts_query("?!"))
        self.assertEqual(len(ms.fts_query(" ".join(f"w{i}" for i in range(100))).split(" OR ")), ms.MAX_FTS_TERMS)


class VectorTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.store.configure_embeddings("test-model", DIM))

    def test_configure_is_idempotent_and_persists(self):
        self.assertFalse(self.store.configure_embeddings("test-model", DIM))
        self.reopen()
        self.assertEqual(self.store.embed_model, "test-model")
        self.assertEqual(self.store.embed_dim, DIM)
        self.assertFalse(self.store.configure_embeddings("test-model", DIM))

    def test_model_or_dim_change_rebuilds_index(self):
        m = self.store.add("x")
        self.store.set_embedding(m["id"], unit(1, 0, 0, 0))
        self.assertEqual(self.store.missing_embeddings(), [])
        self.assertTrue(self.store.configure_embeddings("other-model", DIM))
        self.assertEqual([x["id"] for x in self.store.missing_embeddings()], [m["id"]])
        self.assertTrue(self.store.configure_embeddings("other-model", 8))
        self.assertEqual(self.store.embed_dim, 8)

    def test_configure_validation(self):
        for model, dim in (("", 4), ("m", 0), ("m", -1), ("m", "abc"), ("m", ms.MAX_EMBED_DIM + 1)):
            with self.assertRaises(ValueError):
                self.store.configure_embeddings(model, dim)

    def test_nearest_neighbours(self):
        dog = self.store.add("has a dog named Rex")
        cat = self.store.add("allergic to cats")
        job = self.store.add("works as a nurse")
        self.store.set_embedding(dog["id"], unit(1, 0.1, 0, 0))
        self.store.set_embedding(cat["id"], unit(0.6, 0.8, 0, 0))
        self.store.set_embedding(job["id"], unit(0, 0, 1, 0))
        hits = self.store.vec_search(unit(1, 0, 0, 0), k=3)
        self.assertEqual([h[0]["id"] for h in hits], [dog["id"], cat["id"], job["id"]])
        self.assertAlmostEqual(hits[0][1], 0.995, places=2)
        self.assertAlmostEqual(hits[2][1], 0.0, places=3)
        self.assertEqual(len(self.store.vec_search(unit(1, 0, 0, 0), k=1)), 1)

    def test_deleted_memories_leave_the_index(self):
        a = self.store.add("a")
        b = self.store.add("b")
        self.store.set_embedding(a["id"], unit(1, 0, 0, 0))
        self.store.set_embedding(b["id"], unit(0, 1, 0, 0))
        self.store.soft_delete(a["id"])
        self.assertEqual([h[0]["id"] for h in self.store.vec_search(unit(1, 0, 0, 0))], [b["id"]])
        # restored memories need a fresh embedding
        self.store.restore(a["id"])
        self.assertEqual([x["id"] for x in self.store.missing_embeddings()], [a["id"]])
        self.store.purge(b["id"])
        self.assertEqual(self.store.vec_search(unit(0, 1, 0, 0)), [])

    def test_content_change_invalidates_embedding_but_metadata_change_does_not(self):
        m = self.store.add("lives in Boston")
        self.store.set_embedding(m["id"], unit(1, 0, 0, 0))
        self.store.update(m["id"], pinned=True, tags=["home"], importance=5)
        self.assertEqual(self.store.missing_embeddings(), [])
        self.store.update(m["id"], content="lives in Boston")  # same text
        self.assertEqual(self.store.missing_embeddings(), [])
        self.store.update(m["id"], content="lives in Denver")
        self.assertEqual([x["id"] for x in self.store.missing_embeddings()], [m["id"]])

    def test_set_embedding_replaces(self):
        m = self.store.add("x")
        self.store.set_embedding(m["id"], unit(1, 0, 0, 0))
        self.store.set_embedding(m["id"], unit(0, 0, 0, 1))
        hits = self.store.vec_search(unit(0, 0, 0, 1))
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0][1], 1.0, places=4)

    def test_wrong_dimension_raises(self):
        m = self.store.add("x")
        with self.assertRaises(ms.MemoryStoreError):
            self.store.set_embedding(m["id"], [1.0, 0.0])
        with self.assertRaises(ms.MemoryStoreError):
            self.store.vec_search([1.0] * (DIM + 1))
        with self.assertRaises(ms.MemoryStoreError):
            self.store.set_embedding(m["id"], None)

    def test_set_embedding_unknown_or_deleted(self):
        self.assertFalse(self.store.set_embedding("NOPE", unit(1, 0, 0, 0)))
        m = self.store.add("x")
        self.store.soft_delete(m["id"])
        self.assertFalse(self.store.set_embedding(m["id"], unit(1, 0, 0, 0)))

    def test_missing_embeddings_limit_and_order(self):
        ids = [self.store.add(f"m{i}")["id"] for i in range(5)]
        self.assertEqual([x["id"] for x in self.store.missing_embeddings(limit=3)], ids[:3])


class UnconfiguredVectorTests(StoreTestCase):
    def test_vector_ops_need_configuration(self):
        m = self.store.add("x")
        with self.assertRaises(ms.MemoryStoreError):
            self.store.vec_search([1.0, 0.0, 0.0, 0.0])
        with self.assertRaises(ms.MemoryStoreError):
            self.store.set_embedding(m["id"], [1.0, 0.0, 0.0, 0.0])
        # everything counts as missing until the index exists
        self.assertEqual([x["id"] for x in self.store.missing_embeddings()], [m["id"]])
        self.assertIsNone(self.store.embed_model)
        self.assertIsNone(self.store.embed_dim)
        # and the non-vector paths work without it
        self.store.update(m["id"], content="y")
        self.store.soft_delete(m["id"])
        self.store.purge(m["id"])


class ProcessedTests(StoreTestCase):
    def test_get_set_overwrite_delete(self):
        self.assertIsNone(self.store.get_processed("CHAT1"))  # never processed
        self.store.set_processed("CHAT1", ["aaa", "bbb", "aaa"])
        self.assertEqual(self.store.get_processed("CHAT1"), {"aaa", "bbb"})
        self.store.set_processed("CHAT1", [])
        self.assertEqual(self.store.get_processed("CHAT1"), set())  # processed, but empty
        self.store.set_processed("CHAT1", {"ccc"})
        self.assertEqual(self.store.get_processed("CHAT1"), {"ccc"})
        self.store.delete_processed("CHAT1")
        self.assertIsNone(self.store.get_processed("CHAT1"))

    def test_chats_are_independent_and_survive_reopen(self):
        self.store.set_processed("CHAT1", ["a"])
        self.store.set_processed("CHAT2", ["b"])
        self.reopen()
        self.assertEqual(self.store.get_processed("CHAT1"), {"a"})
        self.assertEqual(self.store.get_processed("CHAT2"), {"b"})

    def test_unreadable_record_counts_as_nothing_processed(self):
        self.store.set_processed("CHAT1", ["a"])
        with self.store._conn:
            self.store._conn.execute("UPDATE extraction_state SET message_hashes='not json'")
        self.assertEqual(self.store.get_processed("CHAT1"), set())


class IsolationAndConcurrencyTests(StoreTestCase):
    def test_per_user_files_are_independent(self):
        bob = ms.MemoryStore(os.path.join(self.tmp, "bob", "memory.db"))
        try:
            self.store.add("alice likes tea")
            bob.add("bob likes coffee")
            self.assertEqual([m["content"] for m in self.store.list()], ["alice likes tea"])
            self.assertEqual([m["content"] for m in bob.list()], ["bob likes coffee"])
            self.assertEqual(bob.fts_search("tea"), [])
        finally:
            bob.close()

    def test_concurrent_writes_from_threads(self):
        self.store.configure_embeddings("test-model", DIM)
        errors = []

        def worker(n):
            try:
                for i in range(25):
                    m = self.store.add(f"thread {n} fact {i}", tags=[f"t{n}"])
                    self.store.set_embedding(m["id"], unit(n + 1, i + 1, 1, 1))
                    self.store.fts_search(f"fact {i}")
                    self.store.touch([m["id"]])
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.store.count(), 200)
        self.assertEqual(self.store.missing_embeddings(), [])

    def test_close_is_idempotent(self):
        self.store.close()
        self.store.close()
        self.store = ms.MemoryStore(self.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""
Self-contained tests for core/embeddings.py (in-process multilingual-e5-small
via onnxruntime, downloaded to data/embeddings/ and verified by sha256).

A fake `core` module is injected so the app is never bootstrapped. Downloads
are faked by patching _fetch, and inference by a fake tokenizer/session, so
the unit tests need no network and no model.

RealModelTest runs the actual model only if it is already downloaded (start the
app once, or set OL_EMBED_MODEL_DIR to a folder holding model_qint8.onnx +
tokenizer.json); otherwise it is skipped.

Run:  venv\\Scripts\\python tests\\test_embeddings.py
"""
import asyncio
import hashlib
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

LOGS = []
DATA_ROOT = tempfile.mkdtemp(prefix="openlumara_embed_")

_core_mod = types.ModuleType("core")
_core_mod.log = lambda category, msg: LOGS.append((category, msg))
_core_mod.detail_error = lambda e: f"{type(e).__name__}: {e}"


def _fake_data_path(subpath=None, user=None):
    base = os.path.join(DATA_ROOT, user) if user else DATA_ROOT
    return os.path.join(base, subpath) if subpath else base


_core_mod.get_data_path = _fake_data_path
sys.modules["core"] = _core_mod

_spec = importlib.util.spec_from_file_location("ol_embeddings_under_test", os.path.join(ROOT, "core", "embeddings.py"))
emb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emb)
ONNX = emb.ONNX_FILE


def run(coro):
    return asyncio.run(coro)


def fake_files(contents: dict):
    """MODEL_FILES entries for tiny fake payloads"""
    return {name: (f"repo/{name}", len(data), hashlib.sha256(data).hexdigest()) for name, data in contents.items()}


class DownloadTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(emb.model_dir(), ignore_errors=True)
        self.payloads = {ONNX: b"onnx-bytes" * 100, "tokenizer.json": b'{"tok": 1}'}
        self.files_patch = mock.patch.object(emb, "MODEL_FILES", fake_files(self.payloads))
        self.files_patch.start()
        self.addCleanup(self.files_patch.stop)
        self.fetched = []

    def fake_fetch(self, overrides=None):
        overrides = overrides or {}

        def _fetch(url, tmp_path, name, expected_size):
            self.fetched.append((url, name))
            with open(tmp_path, "wb") as f:
                f.write(overrides.get(name, self.payloads[name]))
        return _fetch

    def test_model_dir_is_global_not_per_user(self):
        self.assertEqual(emb.model_dir(), os.path.join(DATA_ROOT, "embeddings", "multilingual-e5-small"))
        self.assertTrue(os.path.isdir(emb.model_dir()))

    def test_install_downloads_and_verifies(self):
        with mock.patch.object(emb, "_fetch", self.fake_fetch()):
            emb._install()
        self.assertTrue(emb.is_installed())
        self.assertEqual({n for _, n in self.fetched}, {ONNX, "tokenizer.json"})
        # pinned revision in the url
        self.assertTrue(all(emb.MODEL_REVISION in url for url, _ in self.fetched))
        with open(os.path.join(emb.model_dir(), ONNX), "rb") as f:
            self.assertEqual(f.read(), self.payloads[ONNX])
        self.assertEqual([f for f in os.listdir(emb.model_dir()) if f.endswith(".part")], [])

    def test_install_skips_files_already_present(self):
        with mock.patch.object(emb, "_fetch", self.fake_fetch()):
            emb._install()
            self.fetched.clear()
            emb._install()
        self.assertEqual(self.fetched, [])

    def test_only_missing_files_are_downloaded(self):
        with mock.patch.object(emb, "_fetch", self.fake_fetch()):
            emb._install()
            os.remove(os.path.join(emb.model_dir(), "tokenizer.json"))
            self.fetched.clear()
            emb._install()
        self.assertEqual([n for _, n in self.fetched], ["tokenizer.json"])

    def test_checksum_mismatch_is_rejected_and_cleaned_up(self):
        bad = b"X" * len(self.payloads[ONNX])  # right size, wrong content
        with mock.patch.object(emb, "_fetch", self.fake_fetch({ONNX: bad})):
            with self.assertRaises(emb.EmbeddingsError) as ctx:
                emb._install()
        self.assertIn("checksum", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(emb.model_dir(), ONNX)))
        self.assertFalse(os.path.exists(os.path.join(emb.model_dir(), ONNX + ".part")))

    def test_size_mismatch_is_rejected(self):
        with mock.patch.object(emb, "_fetch", self.fake_fetch({"tokenizer.json": b"short"})):
            with self.assertRaises(emb.EmbeddingsError):
                emb._install()
        self.assertFalse(emb._installed("tokenizer.json"))

    def test_network_error_leaves_no_partial_file(self):
        def broken(url, tmp_path, name, expected_size):
            with open(tmp_path, "wb") as f:
                f.write(b"half")
            raise OSError("connection reset")

        with mock.patch.object(emb, "_fetch", broken):
            with self.assertRaises(OSError):
                emb._install()
        self.assertEqual(os.listdir(emb.model_dir()), [])

    def test_model_id_names_the_quantization(self):
        # vectors from fp32 and int8 differ slightly, so stores must see them as different models
        self.assertEqual(emb.MODEL_ID, "intfloat/multilingual-e5-small@qint8")
        self.assertEqual(emb.ONNX_FILE, "model_qint8.onnx")
        self.assertIn("qint8", emb.MODEL_FILES[emb.ONNX_FILE][0])

    def test_wrong_size_existing_file_counts_as_missing(self):
        with open(os.path.join(emb.model_dir(), ONNX), "wb") as f:
            f.write(b"truncated")
        self.assertFalse(emb._installed(ONNX))


class FakeEncoding:
    def __init__(self, ids, mask):
        self.ids, self.attention_mask = ids, mask


class FakeTokenizer:
    """token count = word count, padded to the longest text in the batch"""
    def __init__(self):
        self.seen = []

    def encode_batch(self, texts):
        self.seen.append(list(texts))
        lengths = [len(t.split()) for t in texts]
        width = max(lengths)
        return [FakeEncoding(list(range(1, n + 1)) + [0] * (width - n), [1] * n + [0] * (width - n))
                for n in lengths]


class FakeSession:
    """hidden state of token i = [i, 1, 0, ...]; padding positions get a huge
    value so any pooling bug that includes them shows up"""
    def __init__(self):
        self.calls = 0

    def run(self, _outputs, feeds):
        import numpy as np
        self.calls += 1
        ids = feeds["input_ids"]
        assert "token_type_ids" in feeds and (feeds["token_type_ids"] == 0).all()
        hidden = np.zeros(ids.shape + (emb.MODEL_DIM,), dtype=np.float32)
        hidden[:, :, 0] = ids
        hidden[:, :, 1] = 1.0
        hidden[feeds["attention_mask"] == 0] = 1e6
        return [hidden]


class EngineTests(unittest.TestCase):
    def make_ready_engine(self):
        engine = emb.Embedder()
        engine._tokenizer = FakeTokenizer()
        engine._session = FakeSession()
        engine._input_names = ("input_ids", "attention_mask", "token_type_ids")
        engine.status = "ready"
        return engine

    def test_not_ready_raises(self):
        engine = emb.Embedder()
        with self.assertRaises(emb.EmbeddingsError) as ctx:
            run(engine.embed_queries(["hi"]))
        self.assertIn("not ready", str(ctx.exception))
        engine.status, engine.error = "failed", "boom"
        with self.assertRaises(emb.EmbeddingsError) as ctx:
            run(engine.embed_passages(["hi"]))
        self.assertIn("boom", str(ctx.exception))

    def test_prefixes(self):
        engine = self.make_ready_engine()
        run(engine.embed_queries(["  where is my dog  "]))
        run(engine.embed_passages(["has a dog"]))
        self.assertEqual(engine._tokenizer.seen, [["query: where is my dog"], ["passage: has a dog"]])

    def test_mean_pooling_ignores_padding_and_normalizes(self):
        import numpy as np
        engine = self.make_ready_engine()
        # with the "passage:" prefix: 2 tokens -> mean id 1.5 (padded to 4, the
        # padding must not leak in) ; 4 tokens -> mean id 2.5
        a, b = run(engine.embed_passages(["one", "one two three"]))
        self.assertEqual(len(a), emb.MODEL_DIM)
        for vec, mean_id in ((a, 1.5), (b, 2.5)):
            expected = np.zeros(emb.MODEL_DIM)
            expected[0], expected[1] = mean_id, 1.0
            expected /= np.linalg.norm(expected)
            np.testing.assert_allclose(vec, expected, atol=1e-6)
            self.assertAlmostEqual(float(np.linalg.norm(vec)), 1.0, places=5)

    def test_batching(self):
        engine = self.make_ready_engine()
        texts = [f"text {i}" for i in range(emb.BATCH_SIZE * 2 + 3)]
        vectors = run(engine.embed_passages(texts))
        self.assertEqual(len(vectors), len(texts))
        self.assertEqual(engine._session.calls, 3)

    def test_empty_and_bad_input(self):
        engine = self.make_ready_engine()
        self.assertEqual(run(engine.embed_queries([])), [])
        with self.assertRaises(TypeError):
            run(engine.embed_queries("a bare string"))

    def test_inference_errors_are_wrapped(self):
        engine = self.make_ready_engine()
        engine._session.run = mock.Mock(side_effect=RuntimeError("onnx exploded"))
        with self.assertRaises(emb.EmbeddingsError):
            run(engine.embed_queries(["x"]))


class EnsureReadyTests(unittest.TestCase):
    def test_installs_once_under_concurrency_then_ready(self):
        engine = emb.Embedder()
        installs, loads = [], []

        with mock.patch.object(emb, "is_installed", side_effect=lambda: bool(installs)), \
                mock.patch.object(emb, "_install", side_effect=lambda: installs.append(1)), \
                mock.patch.object(engine, "_load", side_effect=lambda: loads.append(1)):
            async def go():
                return await asyncio.gather(*(engine.ensure_ready() for _ in range(5)))
            self.assertEqual(run(go()), [True] * 5)

        self.assertEqual((len(installs), len(loads)), (1, 1))
        self.assertTrue(engine.ready)
        self.assertIsNone(engine.error)

    def test_failure_is_reported_then_retry_succeeds(self):
        engine = emb.Embedder()
        attempts = []

        def flaky_install():
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("no internet")

        with mock.patch.object(emb, "is_installed", return_value=False), \
                mock.patch.object(emb, "_install", side_effect=flaky_install), \
                mock.patch.object(engine, "_load"):
            with self.assertRaises(emb.EmbeddingsError):
                run(engine.ensure_ready())
            self.assertEqual(engine.status, "failed")
            self.assertIn("no internet", engine.error)
            self.assertTrue(run(engine.ensure_ready()))
        self.assertTrue(engine.ready)

    def test_survives_a_new_event_loop(self):
        # an in-process restart runs a fresh asyncio loop; the install lock must not be stale
        engine = emb.Embedder()

        async def hold_lock_forever():
            await engine._install_lock().acquire()  # bound to loop #1, never released

        with mock.patch.object(emb, "is_installed", return_value=True), mock.patch.object(engine, "_load"):
            run(hold_lock_forever())
            self.assertTrue(run(asyncio.wait_for(engine.ensure_ready(), timeout=5)))  # loop #2

    def test_skips_download_when_installed(self):
        engine = emb.Embedder()
        with mock.patch.object(emb, "is_installed", return_value=True), \
                mock.patch.object(emb, "_install") as install, mock.patch.object(engine, "_load"):
            run(engine.ensure_ready())
        install.assert_not_called()

    def test_get_returns_singleton(self):
        self.assertIs(emb.get(), emb.get())


def _real_model_dir():
    override = os.environ.get("OL_EMBED_MODEL_DIR")
    if override:
        return override
    return os.path.join(ROOT, "data", "embeddings", "multilingual-e5-small")


@unittest.skipUnless(
    all(os.path.isfile(os.path.join(_real_model_dir(), f)) for f in (emb.ONNX_FILE, "tokenizer.json")),
    "real model not downloaded (start the app once, or set OL_EMBED_MODEL_DIR)")
class RealModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = emb.Embedder()
        with mock.patch.object(emb, "model_dir", _real_model_dir):
            run(cls.engine.ensure_ready())

    def test_bilingual_recall(self):
        import numpy as np
        memories = [
            "The user has a golden retriever named Rex",
            "El usuario trabaja como enfermera en un hospital",
            "Al usuario le encanta el café colombiano",
        ]
        docs = np.array(run(self.engine.embed_passages(memories)))
        # (query, expected memory, max rank allowed). same-language queries must
        # rank the right memory first. cross-language ones only need it in the
        # top 2: e5-small's cross-language scores are near-ties (the int8 model
        # puts "¿cómo se llama mi perro?" 0.001 behind the spanish coffee memory),
        # a trade-off accepted for the int8 model's ram/speed on small hosts.
        # recall injects the top 5, so a near-miss at #2 still reaches the ai
        queries = [
            ("what is my dog called?", 0, 1),
            ("¿cómo se llama mi perro?", 0, 2),
            ("what do I do for a living?", 1, 2),
            ("¿qué café me gusta?", 2, 1),
        ]
        q = np.array(run(self.engine.embed_queries([text for text, _, _ in queries])))
        self.assertEqual(docs.shape, (3, emb.MODEL_DIM))
        np.testing.assert_allclose(np.linalg.norm(docs, axis=1), 1.0, atol=1e-5)
        for (text, expected, max_rank), vec in zip(queries, q):
            ranking = list(np.argsort(-(docs @ vec)))
            self.assertLess(ranking.index(expected), max_rank, text)

    def test_long_text_is_truncated_not_rejected(self):
        vec = run(self.engine.embed_passages(["palabra " * 5000]))[0]
        self.assertEqual(len(vec), emb.MODEL_DIM)


def tearDownModule():
    shutil.rmtree(DATA_ROOT, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

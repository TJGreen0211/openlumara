"""
text embeddings for openlumara (powers semantic recall in the memory module)

runs intfloat/multilingual-e5-small in-process on the CPU with onnxruntime:
no server, works offline once downloaded, and handles english + spanish (and
~100 other languages) in the same vector space, so a spanish question can find
a memory that was stored in english.

we use the int8-quantized onnx export, not the fp32 one: it is built for small
hosts (the main deployment is a 1 vCPU / 2 GB droplet). measured against fp32 on
a mixed english/spanish recall set: vectors 99.4%+ identical, 13/20 vs 14/20
correct top matches, but roughly half the ram (~415 MB vs ~745 MB, peak ~470 MB
vs ~1.1 GB) and 2-6x faster on a single core. it runs on any x86-64 cpu; the
"avx512_vnni" in the filename only means it's fastest on cpus that have it.

the model (~135 MB) is downloaded once into data/embeddings/ and shared by all
users. it is pinned to a fixed huggingface revision and every file is checked
against its sha256 before use. the download is started when the memory module
starts (ensure_ready()), not on first use.

e5 models are trained with "query: " / "passage: " prefixes. search text must go
through embed_queries() and stored text through embed_passages(), otherwise
recall quality drops noticeably.

onnxruntime, tokenizers and numpy are imported lazily, so importing this file
never fails; the memory module declares them as dependencies.
"""
import asyncio
import hashlib
import os
import threading
import time

import httpx

import core

MODEL_NAME = "intfloat/multilingual-e5-small"
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_DIM = 384
MODEL_DIR_NAME = "multilingual-e5-small"

# identifies the exact vectors this engine produces. stores record it (see
# MemoryStore.configure_embeddings) so a change of model OR quantization
# rebuilds their vector index instead of mixing incompatible vectors
MODEL_ID = f"{MODEL_NAME}@qint8"

ONNX_FILE = "model_qint8.onnx"

# local filename: (path inside the huggingface repo, size in bytes, sha256)
MODEL_FILES = {
    ONNX_FILE: (
        "onnx/model_qint8_avx512_vnni.onnx", 118346824,
        "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88",
    ),
    "tokenizer.json": (
        "tokenizer.json", 17082730,
        "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    ),
}
FILE_URL = "https://huggingface.co/{repo}/resolve/{revision}/{path}"

QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
MAX_TOKENS = 512
BATCH_SIZE = 16


class EmbeddingsError(Exception):
    """the embedding model isn't available (still downloading, failed) or embedding failed"""


def _log(msg: str):
    core.log("embeddings", msg)


def model_dir() -> str:
    # user="" (not None) forces the GLOBAL data path, same as stt: the model is
    # shared by everyone, so it must not be downloaded once per user
    path = core.get_data_path(os.path.join("embeddings", MODEL_DIR_NAME), user="")
    os.makedirs(path, exist_ok=True)
    return path


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(url: str, tmp_path: str, name: str, expected_size: int):
    """stream url into tmp_path, logging progress every ~10%"""
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0),
                      headers={"User-Agent": "openlumara-embeddings"}) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0) or 0) or expected_size
        # small files (the tokenizer) download in seconds, no need to narrate them
        if total < (50 << 20):
            total = 0
        next_report = 0.1
        written = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)
                written += len(chunk)
                if total and written / total >= next_report:
                    _log(f"downloading {name}: {written >> 20} / {total >> 20} MB")
                    next_report += 0.1


def _download(filename: str):
    """download one model file (via a .part file) and verify it. raises on failure
    and never leaves a partial or unverified file behind"""
    repo_path, size, sha = MODEL_FILES[filename]
    dest = os.path.join(model_dir(), filename)
    tmp = dest + ".part"
    url = FILE_URL.format(repo=MODEL_NAME, revision=MODEL_REVISION, path=repo_path)
    try:
        _fetch(url, tmp, filename, size)
        actual_size = os.path.getsize(tmp)
        if actual_size != size:
            raise EmbeddingsError(f"{filename} download is {actual_size} bytes, expected {size}")
        actual_sha = _sha256(tmp)
        if actual_sha != sha:
            raise EmbeddingsError(f"{filename} failed its checksum (got {actual_sha[:12]}..., expected {sha[:12]}...)")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _installed(filename: str) -> bool:
    # size check only: hashing the model on every startup is too slow. files are
    # verified by sha256 when downloaded and only ever moved into place afterwards
    path = os.path.join(model_dir(), filename)
    return os.path.isfile(path) and os.path.getsize(path) == MODEL_FILES[filename][1]


def is_installed() -> bool:
    return all(_installed(name) for name in MODEL_FILES)


def _install():
    """download whatever model files are missing (blocking, runs in a thread)"""
    missing = [name for name in MODEL_FILES if not _installed(name)]
    if not missing:
        return
    total_mb = sum(MODEL_FILES[name][1] for name in missing) >> 20
    _log(f"downloading embedding model {MODEL_NAME} ({total_mb} MB, one time)...")
    for name in missing:
        _download(name)
    _log("embedding model downloaded")


class Embedder:
    """the shared embedding engine. one instance for the whole process (see get())"""

    def __init__(self):
        # idle -> downloading -> loading -> ready, or failed (retried by the next ensure_ready())
        self.status = "idle"
        self.error = None
        self._tokenizer = None
        self._session = None
        self._input_names = ()
        self._lock = None
        self._lock_loop = None
        # onnxruntime sessions are thread safe, but running several batches at
        # once just fights over the same cores and slows down the ai server
        self._run_lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def _install_lock(self):
        # an asyncio.Lock is bound to the loop it was first used on, and an
        # in-process restart (manager.restart) runs a brand new event loop
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def ensure_ready(self) -> bool:
        """download (if needed) and load the model. safe to call concurrently and
        repeatedly; after a failure, calling it again retries.
        raises EmbeddingsError if the model can't be made ready"""
        if self.ready:
            return True
        async with self._install_lock():
            if self.ready:
                return True
            started = time.monotonic()
            try:
                if not is_installed():
                    self.status = "downloading"
                    await asyncio.to_thread(_install)
                self.status = "loading"
                await asyncio.to_thread(self._load)
            except Exception as e:
                self.status = "failed"
                self.error = core.detail_error(e)
                _log(f"embedding model unavailable: {self.error}")
                raise EmbeddingsError(self.error) from e

            self.status = "ready"
            self.error = None
            _log(f"embedding model ready ({MODEL_NAME}, {time.monotonic() - started:.1f}s)")
            return True

    def _load(self):
        import onnxruntime
        import tokenizers

        directory = model_dir()
        tokenizer = tokenizers.Tokenizer.from_file(os.path.join(directory, "tokenizer.json"))
        tokenizer.enable_truncation(max_length=MAX_TOKENS)
        pad_id = tokenizer.token_to_id("<pad>")
        tokenizer.enable_padding(pad_id=pad_id if pad_id is not None else 1, pad_token="<pad>")

        options = onnxruntime.SessionOptions()
        # leave cores free for the rest of the app (and a local ai server)
        options.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        options.inter_op_num_threads = 1
        session = onnxruntime.InferenceSession(
            os.path.join(directory, ONNX_FILE), sess_options=options, providers=["CPUExecutionProvider"]
        )

        self._input_names = tuple(i.name for i in session.get_inputs())
        self._tokenizer = tokenizer
        self._session = session

    def _embed_sync(self, texts: list) -> list:
        import numpy as np

        vectors = []
        with self._run_lock:
            for start in range(0, len(texts), BATCH_SIZE):
                batch = self._tokenizer.encode_batch(texts[start:start + BATCH_SIZE])
                ids = np.array([e.ids for e in batch], dtype=np.int64)
                mask = np.array([e.attention_mask for e in batch], dtype=np.int64)
                feeds = {"input_ids": ids, "attention_mask": mask}
                if "token_type_ids" in self._input_names:
                    feeds["token_type_ids"] = np.zeros_like(ids)

                hidden = self._session.run(None, feeds)[0]  # (batch, tokens, dim)

                # mean pooling over the real tokens only (padding is masked out)
                weights = mask[:, :, None].astype(np.float32)
                pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
                # unit length, so cosine similarity is a plain dot product
                pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
                vectors.extend(pooled.astype(np.float32).tolist())
        return vectors

    async def _embed(self, texts, prefix: str) -> list:
        if not self.ready:
            if self.status == "failed":
                raise EmbeddingsError(f"the embedding model failed to load: {self.error}")
            raise EmbeddingsError(f"the embedding model is not ready yet ({self.status})")
        if isinstance(texts, str):
            raise TypeError("pass a list of strings")
        texts = [prefix + str(t or "").strip() for t in texts]
        if not texts:
            return []
        try:
            return await asyncio.to_thread(self._embed_sync, texts)
        except Exception as e:
            raise EmbeddingsError(f"embedding failed: {e}") from e

    async def embed_queries(self, texts: list) -> list:
        """embed search text (eg. the user's message). returns one 384-d unit vector per text"""
        return await self._embed(texts, QUERY_PREFIX)

    async def embed_passages(self, texts: list) -> list:
        """embed text to be stored and searched later (memories). one 384-d unit vector per text"""
        return await self._embed(texts, PASSAGE_PREFIX)


_engine = Embedder()


def get() -> Embedder:
    """the process-wide embedding engine"""
    return _engine

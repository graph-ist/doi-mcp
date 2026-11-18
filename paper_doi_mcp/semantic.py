"""Semantic layer of the corpus: sentence embeddings for the passages in corpus.sqlite, so that
search_everything(semantic=True) finds a passage by meaning and across languages (a query in
Italian reaches a German page). The model runs locally with ONNX Runtime; nothing leaves the
machine.

Default model: multilingual-e5-small (Xenova's ONNX export, quantized: ~120 MB, 384 dimensions),
which covers Italian, English and German. It needs the optional dependencies
    uv sync --extra semantic        (numpy, onnxruntime, tokenizers)
and the model files in SEMANTIC_MODEL_DIR (default STATE_DIRECTORY/models/multilingual-e5-small):
    semantic.py download            fetches model_quantized.onnx and tokenizer.json from Hugging Face
    semantic.py embed [--minutes N] embeds the passages not yet embedded (the weekly job does it too)
    semantic.py status
    semantic.py download-reranker   the optional cross-encoder for search_everything(rerank=True)
Vectors are stored as float32 blobs and compared with numpy (exact cosine over all passages: a
few milliseconds for tens of thousands of passages, no extension to load). The first indexing on
a Raspberry Pi 4 takes hours for a large corpus; it is incremental and resumable.
"""
import hashlib
import json
import math
import os
import re
import struct
import sys
import time
from .paths import state_dir

STATE = state_dir()
DEFAULT_MODEL = "multilingual-e5-small"
MODEL_DIR = os.environ.get("SEMANTIC_MODEL_DIR", os.path.join(STATE, "models", DEFAULT_MODEL))
MODEL_FILES = {
    "model_quantized.onnx": "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx",
    "tokenizer.json": "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/tokenizer.json",
}
MAX_TOKENS = 512


def pack(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


class HashEmbedder:
    """Deterministic bag-of-words hashing, for tests and for machines without the model: it
    exercises the whole pipeline but it is lexical, not semantic (no cross-language matches)."""
    name = "hash-64"
    dim = 64

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            for w in re.findall(r"\w+", t.lower()):
                h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=4).digest(), "big")
                vec[h % self.dim] += 1.0 if (h >> 31) else -1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


class OnnxEmbedder:
    """multilingual-e5-small through onnxruntime: mean pooling over the attention mask, L2
    normalised, with the 'query: ' / 'passage: ' prefixes the model was trained with."""

    def __init__(self, model_dir: str = MODEL_DIR, threads: int | None = None):
        import numpy as np  # noqa: F401 - fail early when the extra is missing
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.model_dir = model_dir
        self.name = os.path.basename(model_dir.rstrip("/")) or DEFAULT_MODEL
        self.tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding()
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(os.path.join(model_dir, "model_quantized.onnx"), opts,
                                            providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.dim = None

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        import numpy as np
        prefix = "query: " if kind == "query" else "passage: "
        enc = self.tokenizer.encode_batch([prefix + t for t in texts])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feed)[0]  # (batch, tokens, dim)
        m = mask[:, :, None].astype(hidden.dtype)
        pooled = (hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
        pooled = pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9)
        self.dim = pooled.shape[1]
        return pooled.astype(np.float32).tolist()


# --- reranking ------------------------------------------------------------------------------------
# A cross-encoder reads query and passage together and scores their relevance: more precise than
# comparing two embeddings, and much slower (one model run per passage), so search_everything uses
# it only on request, on the top candidates, within a time budget. Multilingual MS MARCO model,
# ONNX export from the cross-encoder organisation on Hugging Face. The file names are those the
# organisation publishes for its ONNX exports; check them on the model page before downloading.
RERANK_MODEL = "mmarco-mMiniLMv2-L12-H384-v1"
RERANK_DIR = os.environ.get("SEMANTIC_RERANK_MODEL_DIR", os.path.join(STATE, "models", RERANK_MODEL))
RERANK_FILES = {
    "model.onnx": f"https://huggingface.co/cross-encoder/{RERANK_MODEL}/resolve/main/onnx/model.onnx",
    "tokenizer.json": f"https://huggingface.co/cross-encoder/{RERANK_MODEL}/resolve/main/tokenizer.json",
}


class OverlapReranker:
    """Share of query words found in the passage, for tests and machines without the model."""
    name = "overlap"

    def score(self, query: str, texts: list[str]) -> list[float]:
        q = set(re.findall(r"\w+", query.lower()))
        return [len(q & set(re.findall(r"\w+", t.lower()))) / (len(q) or 1) for t in texts]


class OnnxReranker:
    """Cross-encoder through onnxruntime: (query, passage) pairs, one relevance logit each. Uses
    model_quantized.onnx when present (smaller, faster on a Raspberry Pi), else model.onnx."""

    def __init__(self, model_dir: str = RERANK_DIR, threads: int | None = None):
        import numpy as np  # noqa: F401
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.name = os.path.basename(model_dir.rstrip("/")) or RERANK_MODEL
        self.tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding()
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        quant = os.path.join(model_dir, "model_quantized.onnx")
        self.session = ort.InferenceSession(quant if os.path.exists(quant) else os.path.join(model_dir, "model.onnx"),
                                            opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def score(self, query: str, texts: list[str]) -> list[float]:
        import numpy as np
        enc = self.tokenizer.encode_batch([(query, t) for t in texts])
        feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
        logits = self.session.run(None, feed)[0]
        return [float(x) for x in np.asarray(logits).reshape(len(texts), -1)[:, 0]]


def rerank_available(model_dir: str = RERANK_DIR) -> tuple[bool, str]:
    have = [f for f in ("model_quantized.onnx", "model.onnx") if os.path.exists(os.path.join(model_dir, f))]
    if not have or not os.path.exists(os.path.join(model_dir, "tokenizer.json")):
        return False, f"reranker files missing in {model_dir}: model.onnx and tokenizer.json (run: semantic.py download-reranker)"
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError as e:
        return False, f"optional dependency missing ({e.name}): install with the 'semantic' extra"
    return True, "ok"


_reranker = None


def load_reranker(model_dir: str = RERANK_DIR):
    global _reranker
    if _reranker is None and rerank_available(model_dir)[0]:
        _reranker = OnnxReranker(model_dir)
    return _reranker


def available(model_dir: str = MODEL_DIR) -> tuple[bool, str]:
    """Whether the semantic layer can run here, and why not when it cannot."""
    missing = [f for f in MODEL_FILES if not os.path.exists(os.path.join(model_dir, f))]
    if missing:
        return False, f"model files missing in {model_dir}: {', '.join(missing)} (run: semantic.py download)"
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError as e:
        return False, f"optional dependency missing ({e.name}): install with the 'semantic' extra"
    return True, "ok"


_embedder = None


def load_embedder(model_dir: str = MODEL_DIR):
    """The process-wide embedder, or None with the reason stored for the caller."""
    global _embedder
    if _embedder is None:
        ok, _ = available(model_dir)
        if ok:
            _embedder = OnnxEmbedder(model_dir)
    return _embedder


def download(model_dir: str = MODEL_DIR, fetch=None, files: dict | None = None) -> dict:
    """Fetch the model files once. The user is expected to check the Hugging Face page and the
    file sizes: this only copies what the URLs serve."""
    import urllib.request
    os.makedirs(model_dir, exist_ok=True)
    out = {}
    for name, url in (files or MODEL_FILES).items():
        path = os.path.join(model_dir, name)
        if os.path.exists(path):
            out[name] = "present"
            continue
        if fetch:
            data = fetch(url)
        else:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "paper-doi-mcp"}), timeout=600) as r:
                data = r.read()
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        out[name] = f"downloaded ({len(data) // 1024} KiB)"
    return {"model_dir": model_dir, "files": out}


def main() -> None:
    from . import corpus
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "download":
        print(json.dumps(download(), indent=1))
        return
    if cmd == "download-reranker":
        print(json.dumps(download(RERANK_DIR, files=RERANK_FILES), indent=1))
        return
    db = corpus.connect()
    try:
        if cmd == "embed":
            minutes = float(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else 60.0
            emb = load_embedder()
            if emb is None:
                raise SystemExit(available()[1])
            corpus.sync(db)
            print(json.dumps(corpus.embed_pending(db, emb, budget_seconds=minutes * 60)))
        else:
            ok, why = available()
            rok, rwhy = rerank_available()
            print(json.dumps({"available": ok, "detail": why, "model_dir": MODEL_DIR,
                              "reranker": {"available": rok, "detail": rwhy, "model_dir": RERANK_DIR}, **corpus.stats(db)}, indent=1))
    finally:
        db.close()


if __name__ == "__main__":
    main()

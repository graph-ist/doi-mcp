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


def download(model_dir: str = MODEL_DIR, fetch=None) -> dict:
    """Fetch the model files once. The user is expected to check the Hugging Face page and the
    file sizes: this only copies what the URLs serve."""
    import urllib.request
    os.makedirs(model_dir, exist_ok=True)
    out = {}
    for name, url in MODEL_FILES.items():
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
            print(json.dumps({"available": ok, "detail": why, "model_dir": MODEL_DIR, **corpus.stats(db)}, indent=1))
    finally:
        db.close()


if __name__ == "__main__":
    main()

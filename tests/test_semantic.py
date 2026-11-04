import asyncio
import json
import os

import pytest

import corpus
import knowledge
import semantic
import server as s


@pytest.fixture
def kdir(tmp_path, monkeypatch):
    d = tmp_path / "knowledge"
    (d / "fulltext").mkdir(parents=True)
    (d / "notes_text.json").write_text(json.dumps({"schema_version": 1, "generated": "x", "notes": [
        {"path": "Zettelkasten/Theses/Affordances.md", "title": "Affordances", "text": "#### Position\nAffordances are relational properties of the agent-environment system.\n"},
        {"path": "Zettelkasten/Reading Notes/Gestalt.md", "title": "Gestalt", "text": "#### Thesis\nWahrnehmung ist Gestaltbildung.\n"}]}))
    (d / "fulltext" / "ZKEY0001.json").write_text(json.dumps({"item": "ZKEY0001", "title": "Ecological approach", "pages": [
        {"page": "127", "text": "The affordances of the environment are what it offers the animal."}], "annotations": []}))
    monkeypatch.setattr(knowledge, "KNOWLEDGE_DIR", str(d))
    monkeypatch.setattr(corpus, "DB_PATH", str(tmp_path / "corpus.sqlite"))
    monkeypatch.setattr(s, "SEMANTIC_EMBEDDER", None)
    return d


def test_embeddings_follow_passages_and_semantic_search_ranks_by_similarity(kdir):
    emb = semantic.HashEmbedder()
    db = corpus.connect()
    corpus.sync(db)
    assert corpus.stats(db)["pending_embeddings"] == 3
    first = corpus.embed_pending(db, emb, budget_seconds=10, batch=2)
    assert first["embedded_now"] == 3 and first["pending"] == 0 and first["model"] == "hash-64"
    assert corpus.stats(db)["embedded"] == 3 and corpus.stats(db)["embedding_model"] == "hash-64"
    hits = corpus.semantic_search(db, emb, "affordances environment animal", limit=3)
    # the hash embedder is bag-of-words: the page and the note share the words, so both rank high
    assert "ZKEY0001" in [h.get("zotero_key") for h in hits[:2]] and hits[0]["match"] == "semantic"
    assert hits[0]["score"] >= hits[1]["score"] >= hits[2]["score"]
    assert corpus.semantic_search(db, emb, "affordances", scope="notes")[0]["note"].endswith("Affordances.md")
    assert corpus.semantic_search(db, emb, "affordances", ref="ZKEY0001")[0]["locator"] == "p. 127"
    # a re-indexed source loses its vectors and gets them back on the next pass
    os.utime(kdir / "fulltext" / "ZKEY0001.json", None)
    corpus.sync(db)
    assert corpus.stats(db)["pending_embeddings"] == 1
    assert corpus.embed_pending(db, emb, budget_seconds=10)["embedded_now"] == 1
    # the time budget stops the pass; a zero budget embeds nothing
    (kdir / "fulltext" / "ZKEY0002.json").write_text(json.dumps({"item": "ZKEY0002", "title": "T", "pages": [{"page": "1", "text": "more"}], "annotations": []}))
    corpus.sync(db)
    assert corpus.embed_pending(db, emb, budget_seconds=0)["embedded_now"] == 0
    assert semantic.unpack(semantic.pack([0.5, -1.0])) == [0.5, -1.0]


def test_search_everything_semantic_flag(kdir, monkeypatch):
    unavailable = asyncio.run(s.search_everything("affordances", semantic=True))
    assert unavailable["status"] == "semantic_unavailable" and "model files missing" in unavailable["detail"]
    monkeypatch.setattr(s, "SEMANTIC_EMBEDDER", semantic.HashEmbedder())
    out = asyncio.run(s.search_everything("affordances environment", semantic=True, limit=2))
    assert out["status"] == "ok" and out["semantic"] and out["embedding_progress"]["embedded_now"] == 3
    assert out["results"][0]["match"] == "semantic" and out["index"]["pending_embeddings"] == 0
    lexical = asyncio.run(s.search_everything("affordances environment"))
    assert lexical["semantic"] is False and "embedding_progress" not in lexical


def test_model_download_and_availability(tmp_path):
    model_dir = str(tmp_path / "model")
    ok, why = semantic.available(model_dir)
    assert not ok and "model files missing" in why
    out = semantic.download(model_dir, fetch=lambda url: b"x" * 10)
    assert set(out["files"]) == {"model_quantized.onnx", "tokenizer.json"} and os.path.exists(os.path.join(model_dir, "tokenizer.json"))
    assert semantic.download(model_dir, fetch=lambda url: b"")["files"]["tokenizer.json"] == "present"
    ok, why = semantic.available(model_dir)
    assert ok or "optional dependency missing" in why

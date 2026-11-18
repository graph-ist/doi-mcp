import asyncio
import json
import os

import pytest

from paper_doi_mcp import corpus
from paper_doi_mcp import knowledge
from paper_doi_mcp import semantic
from paper_doi_mcp import server as s


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


# --- library items, hybrid ranking, reranking ---------------------------------------------------

def _library(kdir, items):
    (kdir / "library.json").write_text(json.dumps({"schema_version": 1, "generated": "x", "library_version": 1,
                                                    "items": items, "collections": []}))


ITEMS = [{"key": "ZKEY0001", "title": "Ecological approach", "creators": ["Gibson"], "year": 1979, "tags": ["perception"],
          "abstract": "Affordances are what the environment offers the animal."},
         {"key": "ZKEY0009", "title": "Sciences of the artificial", "creators": ["Simon"], "year": 1969, "tags": [],
          "abstract": None}]


def test_library_items_are_passages_tracked_one_by_one(kdir):
    _library(kdir, ITEMS)
    db = corpus.connect()
    out = corpus.sync(db)
    assert out["items_reindexed"] == 2 and out["items"] == 2 and corpus.stats(db)["passages"]["items"] == 2
    hit = corpus.search(db, "artificial", scope="items")[0]
    assert hit["zotero_key"] == "ZKEY0009" and hit["open"] == "zotero://select/library/items/ZKEY0009" and hit["locator"] == "1969"
    assert corpus.library_item_text(ITEMS[0]) == ("Ecological approach\nGibson (1979)\n"
                                                  "Affordances are what the environment offers the animal.\nTags: perception")
    emb = semantic.HashEmbedder()
    corpus.embed_pending(db, emb, budget_seconds=10)
    # the snapshot is rewritten after every Zotero write: only the item whose text changed is redone
    _library(kdir, [ITEMS[0], {**ITEMS[1], "abstract": "Design as a science of the artificial."}])
    os.utime(kdir / "library.json", (1, 1))
    assert corpus.sync(db)["items_reindexed"] == 1 and corpus.stats(db)["pending_embeddings"] == 1
    _library(kdir, [ITEMS[1]])
    os.utime(kdir / "library.json", (2, 2))
    assert corpus.sync(db)["items_removed"] == 1 and not corpus.search(db, "Gibson", scope="items")
    os.remove(kdir / "library.json")
    assert corpus.sync(db)["items"] == 0 and "items" not in corpus.stats(db)["passages"]
    assert corpus.stats(db)["sources"] == 2  # notes_text.json and the fulltext file, not library items


def test_hybrid_fuses_both_rankings(kdir):
    _library(kdir, ITEMS)
    db = corpus.connect()
    corpus.sync(db)
    emb = semantic.HashEmbedder()
    corpus.embed_pending(db, emb, budget_seconds=10)
    hits, notes = corpus.hybrid_search(db, emb, "affordances environment animal", limit=5)
    assert notes == [] and hits[0]["match"] == "both" and "_id" in hits[0]
    assert hits[0]["score"] >= hits[-1]["score"] and "cosine" in hits[0]
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    only_sem, notes = corpus.hybrid_search(db, emb, "AND", limit=3)  # a bare operator: no lexical query
    assert notes[0].startswith("lexical part skipped") and all(h["match"] == "semantic" for h in only_sem)


def test_rerank_reorders_within_budget(kdir):
    db = corpus.connect()
    corpus.sync(db)
    hits = corpus.search(db, "affordances OR Wahrnehmung", limit=10)
    info = corpus.rerank(db, semantic.OverlapReranker(), "Wahrnehmung Gestaltbildung", hits)
    assert info["reranked"] == len(hits) and hits[0]["note"].endswith("Gestalt.md")
    assert hits[0]["rerank_score"] >= hits[-1]["rerank_score"]
    hits2 = corpus.search(db, "affordances OR Wahrnehmung", limit=10)
    order = [h["_id"] for h in hits2]
    assert corpus.rerank(db, semantic.OverlapReranker(), "x", hits2, budget_seconds=0)["reranked"] == 0
    assert [h["_id"] for h in hits2] == order


def test_search_everything_modes_and_rerank(kdir, monkeypatch):
    _library(kdir, ITEMS)
    fallback = asyncio.run(s.search_everything("affordances", mode="hybrid"))
    assert fallback["mode"] == "lexical" and "semantic part unavailable" in fallback["notes"][0]
    assert all("_id" not in h for h in fallback["results"])
    monkeypatch.setattr(s, "SEMANTIC_EMBEDDER", semantic.HashEmbedder())
    hyb = asyncio.run(s.search_everything("affordances environment", mode="hybrid", limit=3))
    assert hyb["mode"] == "hybrid" and hyb["results"][0]["match"] in ("both", "lexical", "semantic")
    assert hyb["returned"] == 3 and all("_id" not in h for h in hyb["results"])
    items = asyncio.run(s.search_everything("artificial", scope="items"))
    assert [h["zotero_key"] for h in items["results"]] == ["ZKEY0009"]
    no_rr = asyncio.run(s.search_everything("affordances", rerank=True))
    assert no_rr["rerank"]["status"] == "unavailable" and "reranker files missing" in no_rr["rerank"]["detail"]
    monkeypatch.setattr(s, "RERANKER", semantic.OverlapReranker())
    rr = asyncio.run(s.search_everything("affordances OR Wahrnehmung", rerank=True, limit=2))
    assert rr["rerank"]["status"] == "ok" and rr["rerank"]["model"] == "overlap" and rr["returned"] == 2
    assert rr["results"][0]["rerank_score"] >= rr["results"][1]["rerank_score"]

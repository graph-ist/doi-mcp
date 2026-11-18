import json
import os

import pytest

from paper_doi_mcp import corpus
from paper_doi_mcp import knowledge


@pytest.fixture
def kdir(tmp_path, monkeypatch):
    d = tmp_path / "knowledge"
    (d / "fulltext").mkdir(parents=True)
    (d / "notes_text.json").write_text(json.dumps({"schema_version": 1, "generated": "2026-11-04T00:00:00Z", "notes": [
        {"path": "Zettelkasten/Reading Notes/Designerly ways.md", "title": "Designerly ways",
         "text": "#### Thesis\nDesign has its own ways of knowing, distinct from the sciences and the humanities.\n\n#### My Observations\nCross reads Schön charitably.\n"},
        {"path": "Zettelkasten/Theses/Affordances.md", "title": "Affordances", "text": "#### Position\nAffordances are relational, not properties of the artefact.\n"}]}))
    (d / "fulltext" / "ZKEY0001.json").write_text(json.dumps({"item": "ZKEY0001", "title": "Designerly ways of knowing", "pages": [
        {"page": "221", "text": "Designerly ways of knowing rest on the manipulation of non-verbal codes."},
        {"page": "222", "text": "The Gestaltung of artefacts: Entwurf und Erkenntnis."}],
        "annotations": [{"key": "A1", "type": "highlight", "page": "221", "text": "non-verbal codes", "comment": "key claim"}]}))
    monkeypatch.setattr(knowledge, "KNOWLEDGE_DIR", str(d))
    monkeypatch.setattr(corpus, "DB_PATH", str(tmp_path / "corpus.sqlite"))
    return d


def test_index_follows_the_files_and_search_locates_passages(kdir):
    db = corpus.connect()
    assert corpus.sync(db) == {"reindexed": 2, "removed": 0, "sources": 2, "items_reindexed": 0, "items_removed": 0, "items": 0}
    assert corpus.sync(db)["reindexed"] == 0
    hits = corpus.search(db, "non-verbal codes")
    assert {h["scope"] for h in hits} == {"library", "annotations"}
    lib = next(h for h in hits if h["scope"] == "library")
    assert lib["zotero_key"] == "ZKEY0001" and lib["locator"] == "p. 221" and lib["open"].endswith("?page=221")
    notes = corpus.search(db, "ways of knowing", scope="notes")
    assert notes[0]["note"].endswith("Designerly ways.md") and notes[0]["locator"] == "Thesis"
    # diacritics ignored, phrases and NOT honoured, prefix search
    assert corpus.search(db, "Gestaltung")[0]["locator"] == "p. 222"
    assert corpus.search(db, '"relational, not properties"')[0]["scope"] == "notes"
    assert all(h["scope"] != "notes" for h in corpus.search(db, "knowing NOT sciences"))
    assert corpus.search(db, "Entw*")[0]["zotero_key"] == "ZKEY0001"
    assert corpus.search(db, "knowing", ref="ZKEY0001") and not corpus.search(db, "knowing", ref="NOPE0000")
    assert isinstance(corpus.search(db, '"unbalanced'), list)  # punctuation is sanitised, never a parser error
    # a removed file leaves the index
    os.remove(kdir / "fulltext" / "ZKEY0001.json")
    assert corpus.sync(db)["removed"] == 1 and not corpus.search(db, "non-verbal")
    assert corpus.stats(db)["passages"] == {"notes": 3}


def test_search_everything_tool(kdir):
    import asyncio
    from paper_doi_mcp import server as s
    out = asyncio.run(s.search_everything("Schön", scope="notes"))
    assert out["status"] == "ok" and out["results"][0]["locator"] == "My Observations" and out["index"]["sources"] == 2
    assert asyncio.run(s.search_everything("x", scope="all", limit=500))["returned"] == 0


def test_chunking_keeps_paragraphs():
    text = "\n\n".join(f"paragraph {i} " + "word " * 100 for i in range(5))
    parts = corpus.chunks(text, 1200)
    assert len(parts) >= 3 and all(len(p) <= 1300 for p in parts) and parts[0].startswith("paragraph 0")

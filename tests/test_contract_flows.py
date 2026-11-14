"""The three servers meet in KNOWLEDGE_DIR. These tests run the cross-server flows on the files
the other two servers' own tests produce (tests/fixtures/contract, copied from the Zotero MCP
and the Obsidian MCP: regenerate there with UPDATE_CONTRACT=1 when a format changes), so a
drift in a snapshot format fails here before it fails on the Pi."""
import asyncio
import json
import os
import shutil

import pytest

from paper_doi_mcp import corpus
from paper_doi_mcp import knowledge
from paper_doi_mcp import maps
from paper_doi_mcp import server as s

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "contract")


@pytest.fixture
def contract(tmp_path, monkeypatch):
    d = tmp_path / "knowledge"
    shutil.copytree(FIXTURES, d)
    monkeypatch.setattr(knowledge, "KNOWLEDGE_DIR", str(d))
    monkeypatch.setattr(corpus, "DB_PATH", str(tmp_path / "corpus.sqlite"))
    monkeypatch.setattr(maps, "DB_PATH", str(tmp_path / "maps.sqlite"))
    knowledge._cache.clear()
    return d


def test_fixtures_carry_the_schema_versions_this_reader_supports(contract):
    for name in ("library.json", "notes.json", "notes_text.json"):
        data = json.load(open(contract / name, encoding="utf-8"))
        assert data["schema_version"] == knowledge.SUPPORTED_SCHEMA.get(name, knowledge.SUPPORTED_SCHEMA["notes.json"])
    assert "problems" not in knowledge.status()


def test_reading_note_flow_links_item_note_and_corpus(contract):
    """add-to-library → reading-note → maps: the item has a note, the note's text is searchable,
    the map node shows both, and the draft resolves the citekey."""
    nodes = [{"id": "W1", "doi": "10.1016/0142-694x(82)90040-0", "title": "Designerly ways of knowing", "first_author": "Nigel Cross", "year": 1982},
             {"id": "B:schon-1983-reflective-practitioner", "doi": None, "title": "The reflective practitioner", "first_author": "Schön", "year": 1983},
             {"id": "P:EP3500000", "doi": None, "title": "Foldable display device", "first_author": "ACME", "year": 2017}]
    knowledge.annotate(nodes)
    assert [n["library_match"] for n in nodes] == ["doi", "title", "patent"]
    assert nodes[0]["notes"][0]["path"].endswith("Designerly ways of knowing.md") and nodes[0]["verified"]["status"] == "match"
    assert nodes[0]["zotero_tags"] == ["cited/thesis", "core/seminal"]
    hits = asyncio.run(s.search_everything("non-verbal codes"))["results"]
    assert {h["scope"] for h in hits} == {"library", "annotations"} and hits[0]["zotero_key"] == "ZKEY0001"
    notes = asyncio.run(s.search_everything("charitably", scope="notes"))["results"]
    assert notes[0]["locator"] == "My Observations" and notes[0]["note"].endswith("Designerly ways of knowing.md")
    draft = asyncio.run(s.check_draft("Design has its own epistemology [@cross1982designerly, p. 221] and [@schon1983]."))
    assert draft["resolved"]["cross1982designerly"]["verified"] == "match" and draft["resolved"]["schon1983"]["resolved_by"] == "surname_year"
    assert draft["unverified"][0]["key"] == "ZKEY0002" and draft["bibliography_keys"] == ["ZKEY0001", "ZKEY0002"]


def test_coverage_and_map_seeds_from_the_contract_library(contract, monkeypatch):
    cov = asyncio.run(s.coverage_report(project="thesis"))
    assert cov["items"] == 2 and cov["with_note"] == 1 and [r["key"] for r in cov["without_note"]] == ["ZKEY0002"]
    assert cov["notes_without_item"] == [] and cov["notes_with_missing_item"] == []

    async def not_in_openalex(surname, title, budget=None):
        return None

    async def seed(x):
        return {"id": "W1", "doi": "10.1016/0142-694x(82)90040-0", "title": "Designerly ways of knowing", "first_author": "Nigel Cross",
                "year": 1982, "cited_by": 100, "field": None, "refs": []}
    monkeypatch.setattr(s, "_resolve_work", not_in_openalex)
    monkeypatch.setattr(s, "_cg_seed", seed)
    asyncio.run(s.map_create("Contract"))
    out = asyncio.run(s.map_add_seeds("Contract", zotero_tag="cited/thesis"))
    assert set(out["seeds"]) == {"W1", "Z:ZKEY0002"}
    works = {w["id"]: w for w in asyncio.run(s.map_get("Contract"))["works"]}
    assert works["W1"]["in_library"] and works["W1"]["library_match"] == "doi" and works["Z:ZKEY0002"]["library_match"] == "key"
    assert works["W1"]["kind"] == "work" and works["Z:ZKEY0002"]["kind"] == "book"

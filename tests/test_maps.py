import asyncio

import pytest

import maps
import server as s


def node(i, refs=(), doi=None, cited=0):
    return {"id": i, "doi": doi, "title": f"T{i}", "first_author": "A", "year": 2000, "cited_by": cited,
            "field": None, "refs": list(refs)}


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(maps, "DB_PATH", str(tmp_path / "maps.sqlite"))
    works = {"W1": node("W1", ["W3", "W4"], "10.1234/one"), "W2": node("W2", ["W3", "W5"]),
             "W3": node("W3", ["W9"], cited=50), "W4": node("W4", cited=5), "W5": node("W5", cited=500),
             "W6": node("W6", ["W1", "W2", "W3"])}

    async def seed(x):
        x = s._oa_id(x) if x.upper().startswith("W") else {"10.1234/one": "W1"}.get(x)
        if x not in works:
            raise ValueError("not found")
        return works[x]

    async def expand(mode, seeds, per_seed):
        if mode == "references":
            return [(works[r], f"references:{sd['id']}") for sd in seeds for r in sd["refs"]], []
        return [(works["W6"], f"citing:{sd['id']}") for sd in seeds], []

    monkeypatch.setattr(s, "_cg_seed", seed)
    monkeypatch.setattr(s, "_expand", expand)
    return works


def run(c):
    return asyncio.run(c)


def test_map_lifecycle(db):
    out = run(s.map_create("Design theory", ["10.1234/one", "W2", "W404"]))
    assert out["status"] == "ok" and set(out["seeds"]) == {"W1", "W2"} and out["source_errors"]
    assert run(s.map_create("design theory"))["status"] == "exists"
    out = run(s.map_expand("Design theory"))
    assert out["added"] == 4 and out["works_in_map"] == 6
    # W3 is cited by both seeds and by W6: first suggestion
    sug = run(s.map_suggest("Design theory"))["suggestions"]
    assert sug[0]["id"] == "W3" and sug[0]["seeds_linked"] == 2
    assert set(sug[0]["found_by"]) == {"references:W1", "references:W2"}
    assert run(s.map_mark("Design theory", ["W3", "W404"], "dismissed", "off topic"))["not_in_map"] == ["W404"]
    assert "W3" not in [x["id"] for x in run(s.map_suggest("Design theory"))["suggestions"]]
    got = run(s.map_get("Design theory"))
    assert {"W1", "W3"} <= {w["id"] for w in got["works"]} and ["W1", "W3"] in got["citation_edges"]
    assert next(w for w in got["works"] if w["id"] == "W3")["note"] == "off topic"
    listed = run(s.map_list())["maps"][0]
    assert listed["seeds"] == 2 and listed["works"] == 6 and listed["dismissed"] == 1


def test_candidate_promoted_to_seed_keeps_state(db):
    run(s.map_create("M", ["W1"]))
    run(s.map_expand("M"))
    run(s.map_mark("M", ["W3"], "read"))
    assert run(s.map_add_seeds("M", ["W3"]))["seeds"] == {"W3": "promoted"}
    w3 = next(w for w in run(s.map_get("M"))["works"] if w["id"] == "W3")
    assert w3["role"] == "seed" and w3["state"] == "read" and "seed" in w3["found_by"]


def test_map_errors(db):
    assert run(s.map_expand("nope"))["status"] == "not_found"
    assert run(s.map_create("bad/name"))["status"] == "invalid_input"
    run(s.map_create("Empty"))
    assert run(s.map_expand("Empty"))["status"] == "no_seeds"
    assert run(s.map_expand("Empty", ["sideways"]))["status"] == "invalid_input"


@pytest.fixture
def snapshot(monkeypatch, tmp_path):
    import json, knowledge
    d = tmp_path / "knowledge"; d.mkdir()
    (d / "library.json").write_text(json.dumps({"generated": "2026-11-01T12:00:00Z", "items": [
        {"key": "ZKEY0001", "doi": "10.1234/one", "title": "One", "tags": ["cited/x", "core/seminal", "sub/1"], "collections": ["Design › Theory"]},
        {"key": "ZKEY0002", "doi": None, "title": "Book without DOI", "tags": ["cited/x"], "collections": []}]}))
    (d / "notes.json").write_text(json.dumps({"generated": "2026-11-01T12:00:00Z", "notes": [
        {"title": "One note", "path": "Zettelkasten/Summaries/One note.md", "zotero_keys": ["ZKEY0001"]}]}))
    monkeypatch.setattr(knowledge, "KNOWLEDGE_DIR", str(d))
    knowledge._cache.clear()


def test_zotero_tag_seeds_and_bridges(db, snapshot):
    run(s.map_create("Z"))
    out = run(s.map_add_seeds("Z", zotero_tag="cited/x"))
    assert out["seeds"] == {"W1": "added"} and "1 items have no DOI" in out["warnings"][0]
    w1 = next(w for w in run(s.map_get("Z"))["works"] if w["id"] == "W1")
    assert w1["in_library"] and w1["zotero_key"] == "ZKEY0001" and w1["notes"][0]["title"] == "One note"
    assert w1["zotero_tags"] == ["cited/x", "core/seminal"]
    assert run(s.map_add_seeds("Z", zotero_collection="theory"))["seeds"] == {"W1": "updated"}


def test_refs_fallback_and_warning(db, monkeypatch, snapshot):
    db["W7"] = node("W7", [], "10.1234/book")
    db["W8"] = node("W8", [])

    async def s2_refs(doi):
        return ["W3", "W4"] if doi == "10.1234/book" else []
    monkeypatch.setattr(s, "_s2_reference_ids", s2_refs)
    run(s.map_create("B"))
    out = run(s.map_add_seeds("B", ["W7", "W8"]))
    assert out["references_from_semantic_scholar"] == ["W7"]
    assert len(out["warnings"]) == 1 and "W8" in out["warnings"][0]


def test_rename_remove_delete(db, snapshot):
    run(s.map_create("Old", ["W1"]))
    run(s.map_expand("Old"))
    assert run(s.map_rename("Old", "New", "desc"))["map"] == "New"
    assert run(s.map_remove("New", ["W3", "W404"]))["removed"] == 1
    assert "W3" not in {w["id"] for w in run(s.map_get("New"))["works"]}
    assert run(s.map_delete("New", "new"))["status"] == "not_confirmed"
    assert run(s.map_delete("New", "New"))["status"] == "ok"
    assert run(s.map_get("New"))["status"] == "not_found"


def test_cocited_total_breaks_ties():
    nodes = {k: {"id": k, "role": "candidate", "refs": []} for k in ("S", "A", "B", "C", "X", "Y")}
    nodes["S"]["role"] = "seed"
    nodes["X"]["refs"] = ["A", "B"]
    nodes["Y"]["refs"] = ["A", "C"]
    s._map_view(nodes)
    assert nodes["A"]["cocited_total"] == 2 and nodes["B"]["cocited_total"] == 1 and nodes["S"]["cocited_total"] == 0


def test_export_markdown(db, snapshot):
    run(s.map_create("E", ["W1"]))
    run(s.map_expand("E"))
    run(s.map_mark("E", ["W3"], "to_read", "check ch. 2"))
    md = run(s.map_export("E"))["markdown"]
    assert "## Seeds" in md and "## To read" in md and "check ch. 2" in md
    assert "[Zotero](zotero://select/library/items/ZKEY0001)" in md and "[[One note]]" in md


def test_watch_adds_new_citing(db, monkeypatch, snapshot):
    run(s.map_create("W", ["W1"]))
    calls = []

    async def oa_get(path, **params):
        calls.append(params["filter"])
        return {"results": [{"id": "https://openalex.org/W99", "doi": None, "title": "Fresh", "authorships": [],
                             "publication_year": 2026, "cited_by_count": 0, "referenced_works": []}]}
    monkeypatch.setattr(s, "_oa_get", oa_get)
    out = run(s.map_watch("W"))
    assert [n["id"] for n in out["new"]] == ["W99"] and calls[0].startswith("cites:W1,from_created_date:")
    assert run(s.map_watch("W"))["new"] == []


def test_full_map_keeps_most_linked(db, monkeypatch):
    monkeypatch.setattr(s, "MAP_MAX_NODES", 4)
    run(s.map_create("Cap", ["W1", "W2"]))
    out = run(s.map_expand("Cap", ["references"]))
    # W3 is cited by both seeds; W4 and W5 by one each, W5 more cited: W3 and W5 enter
    ids = {w["id"] for w in run(s.map_get("Cap"))["works"]}
    assert out["skipped_map_full"] == 1 and ids == {"W1", "W2", "W3", "W5"}

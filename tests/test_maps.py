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

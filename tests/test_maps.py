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
            return [(works[r], f"references:{sd['id']}") for sd in seeds for r in sd["refs"] if r in works], []
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
        {"key": "ZKEY0002", "doi": None, "title": "Book without DOI", "creators": ["Writer"], "year": 1999, "tags": ["cited/x"], "collections": []},
        {"key": "ZKEY0003", "doi": None, "title": "Designerly ways of knowing", "creators": ["Cross", "Other"], "year": 1982,
         "tags": ["sub/330304-design-theory"], "collections": [], "verified": {"date": "2026-11-04", "status": "match", "detail": "verify_book verified"}},
        {"key": "ZKEY0004", "doi": None, "title": "Foldable display device", "creators": ["Lee"], "year": 2017,
         "patent_number": "EP3500000B1", "tags": [], "collections": []}]}))
    (d / "notes.json").write_text(json.dumps({"generated": "2026-11-01T12:00:00Z", "notes": [
        {"title": "One note", "path": "Zettelkasten/Summaries/One note.md", "zotero_keys": ["ZKEY0001"]}]}))
    monkeypatch.setattr(knowledge, "KNOWLEDGE_DIR", str(d))
    knowledge._cache.clear()

    async def not_in_openalex(surname, title, budget=None):
        return None
    monkeypatch.setattr(s, "_resolve_work", not_in_openalex)
    return d


def test_zotero_tag_seeds_and_bridges(db, snapshot):
    run(s.map_create("Z"))
    out = run(s.map_add_seeds("Z", zotero_tag="cited/x"))
    # the book without DOI is not in OpenAlex: it enters as a Zotero node, its bibliography not extracted yet
    assert out["seeds"] == {"W1": "added", "Z:ZKEY0002": "added"}
    assert any("ZKEY0002" in w and "extract_references" in w for w in out["warnings"])
    z = next(w for w in run(s.map_get("Z"))["works"] if w["id"] == "Z:ZKEY0002")
    assert z["in_library"] and z["zotero_key"] == "ZKEY0002" and z["title"] == "Book without DOI"
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
    async def batch_none(key, values):
        return [], []
    monkeypatch.setattr(s, "_oa_batch", batch_none)
    out = run(s.map_refresh("W", new_citing=True))
    assert [n["id"] for n in out["new"]] == ["W99"] and calls[0].startswith("cites:W1,from_created_date:")
    assert run(s.map_refresh("W", new_citing=True))["new"] == []


def test_full_map_keeps_most_linked(db, monkeypatch):
    monkeypatch.setattr(s, "MAP_MAX_NODES", 4)
    run(s.map_create("Cap", ["W1", "W2"]))
    out = run(s.map_expand("Cap", ["references"]))
    # W3 is cited by both seeds; W4 and W5 by one each, W5 more cited: W3 and W5 enter
    ids = {w["id"] for w in run(s.map_get("Cap"))["works"]}
    assert out["skipped"] == 1 and ids == {"W1", "W2", "W3", "W5"}


def test_duplicates_merged_and_prune(db, monkeypatch):
    db["W3"]["title"] = "Resilience and stability of ecological systems"
    db["W5"] = {**node("W5", ["W9"], cited=10), "title": "Resilience and Stability of Ecological Systems", "year": 2001}
    db["W3"]["year"] = 2000
    run(s.map_create("D", ["W1", "W2"]))
    out = run(s.map_expand("D", ["references"]))
    assert out["duplicates_merged"] and out["duplicates_merged"][0]["kept"] == "W3"
    ids = {w["id"] for w in run(s.map_get("D"))["works"]}
    assert "W5" not in ids and "W3" in ids
    run(s.map_mark("D", ["W4"], "dismissed"))
    pr = run(s.map_prune("D", keep=0))
    left = {w["id"]: w for w in run(s.map_get("D"))["works"]}
    assert set(left) == {"W1", "W2", "W4"} and pr["removed"] == 1


def test_max_new_and_expand_from_candidate(db):
    run(s.map_create("N", ["W1", "W2"]))
    out = run(s.map_expand("N", ["references"], max_new=1))
    assert out["added"] == 1 and out["skipped"] >= 1
    ids = {w["id"] for w in run(s.map_get("N"))["works"]}
    assert "W3" in ids  # linked to both seeds: enters first
    out = run(s.map_expand("N", ["citing"], seeds=["W3"]))
    assert out["started_from"] == ["W3"]


def test_momentum():
    import datetime
    y = datetime.datetime.now(datetime.timezone.utc).year
    assert s._momentum([{"year": y, "cited_by_count": 50}, {"year": y - 1, "cited_by_count": 3}, {"year": y - 3, "cited_by_count": 2}, {"year": y - 4, "cited_by_count": 9}]) == 5
    assert s._momentum(None) is None


def test_refresh_keeps_state(db, monkeypatch):
    run(s.map_create("R", ["W1"]))
    run(s.map_expand("R", ["references"]))
    run(s.map_mark("R", ["W3"], "read"))

    async def batch(key, ids):
        return [{**db[i], "cited_by": 999, "momentum": 42} for i in ids if i in db], []
    monkeypatch.setattr(s, "_oa_batch", batch)
    out = run(s.map_refresh("R"))
    w3 = next(w for w in run(s.map_get("R"))["works"] if w["id"] == "W3")
    assert out["refreshed"] == 3 and w3["cited_by"] == 999 and w3["momentum"] == 42 and w3["state"] == "read"


def test_pdf_references_book_nodes_and_editions(db, snapshot, monkeypatch):
    import json
    refs = snapshot / "references"; refs.mkdir()
    (refs / "ZKEY0002.json").write_text(json.dumps({"entries": [
        "Smith, Ann. 1990. Known Work About Things. London: Verso.",
        "Doe, John. 1975. A Lost Book on Design. Paris: Minuit.",
        "Roe, Rita. 1980. Another Lost Book Here. Paris: Minuit."]}))
    calls = []

    async def resolve(surname, title, budget=None):
        calls.append(title)
        return dict(db["W3"]) if surname == "Smith" else None
    monkeypatch.setattr(s, "_resolve_work", resolve)
    run(s.map_create("P"))
    out = run(s.map_add_seeds("P", zotero_items=["ZKEY0002"]))
    assert out["pdf_references"]["Z:ZKEY0002"] == {"entries": 3, "openalex": 1, "book_nodes": 2, "unparsed": 0, "pending": 0}
    book = next(w for w in run(s.map_get("P"))["works"] if w["id"] == "Z:ZKEY0002")
    assert book["refs_source"] == "zotero_pdf"
    # resolutions are cached: a second time nothing is looked up again
    n = len(calls)
    run(s.map_add_seeds("P", zotero_items=["ZKEY0002"]))
    assert len(calls) == n
    # a book node cited by only one work in the map stays out; W1 also citing it brings it in
    run(s.map_expand("P"))
    ids = {w["id"] for w in run(s.map_get("P"))["works"]}
    assert "W3" in ids and not any(i.startswith("B:") for i in ids)
    m, conn = s._map_db()
    mid = m.map_row(conn, "P")["id"]
    db["W1"]["refs"] = ["B:doe-1975-lost-book-design"]
    run(s.map_add_seeds("P", ["W1"]))
    run(s.map_expand("P"))
    got = run(s.map_get("P"))
    assert "B:doe-1975-lost-book-design" in {w["id"] for w in got["works"]}
    assert ["W1", "B:doe-1975-lost-book-design"] in got["citation_edges"]


def test_parse_ref_styles():
    assert s._parse_ref("Adams, Carol. 1990. The Sexual Politics of Meat. New York: Continuum.") == \
        ("Adams", 1990, "The Sexual Politics of Meat")
    assert s._parse_ref("Alexander, Christopher, Notes on the Synthesis of Form, Cambridge, MA: Harvard University Press, 1964.") == \
        ("Alexander", 1964, "Notes on the Synthesis of Form")
    assert s._parse_ref('Becker, Howard S. 1972. "A School is a Lousy Place." In American Behavioral Scientist 16.')[2] == \
        "A School is a Lousy Place"


def test_merge_editions_remaps_references(db):
    m, conn = s._map_db()
    mid = m.create(conn, "E")
    a = {**node("W50", [], cited=900), "title": "Science and the Modern World", "first_author": "Alfred North Whitehead", "year": 1926}
    b = {**node("W51", [], cited=40), "title": "Science and the Modern World", "first_author": "A. N. Whitehead", "year": 1967}
    c = node("W52", ["W51"])
    with conn:
        for n_, role in ((a, "candidate"), (b, "candidate"), (c, "seed")):
            m.upsert(conn, mid, n_, role, "test")
    merged = s._merge_duplicates(m, conn, mid)
    assert merged and merged[0]["kept"] == "W50"
    assert m.nodes(conn, mid)["W52"]["refs"] == ["W50"]


def test_resolve_crossref_first_and_budget(monkeypatch):
    calls = []

    async def crossref(c, title, author):
        return [{"doi": "10.1/x", "title": "Homo Sacer", "subtitle": "Sovereign Power and Bare Life", "authors": ["Agamben"]}]

    async def oa_get(path, **params):
        calls.append(path)
        if path.startswith("works/doi:"):
            return {"id": "https://openalex.org/W9", "title": "Homo Sacer", "referenced_works": []}
        return {"results": []}
    async def s2(path, params=None, body=None):
        return {"status": "ok", "data": {"data": []}}
    monkeypatch.setattr(s, "_crossref", crossref)
    monkeypatch.setattr(s, "_oa_get", oa_get)
    monkeypatch.setattr(s, "_s2_api", s2)
    budget = {"searches": 0, "max": 1}
    assert run(s._resolve_work("Agamben", "Homo Sacer: Sovereign Power and Bare Life", budget))["id"] == "W9"
    assert budget["searches"] == 0 and calls == ["works/doi:10.1/x"]  # free path, no search
    assert run(s._resolve_work("Nobody", "Unknown Title Here", budget)) is None  # one paid search
    with pytest.raises(s.BudgetExhausted):
        run(s._resolve_work("Nobody", "Another Unknown Title", budget))


def test_snapshot_schema_version_is_checked(snapshot):
    import json, knowledge
    assert knowledge.library() is not None and knowledge.status()["library_snapshot"] == "2026-11-01T12:00:00Z"
    # a newer schema than this reader understands is refused, and map_get says why
    lib = json.loads((snapshot / "library.json").read_text())
    (snapshot / "library.json").write_text(json.dumps({**lib, "schema_version": knowledge.SUPPORTED_SCHEMA["library.json"] + 1}))
    knowledge._cache.clear()
    assert knowledge.library() is None and knowledge.by_doi() == {}
    st = knowledge.status()
    assert st["library_snapshot"] is None and "schema_version" in st["problems"]["library.json"]
    # the current version loads again
    (snapshot / "library.json").write_text(json.dumps({**lib, "schema_version": 1}))
    knowledge._cache.clear()
    assert knowledge.library() is not None and "problems" not in knowledge.status()


def test_bridge_matches_by_title_patent_and_shows_verification(snapshot):
    import knowledge
    nodes = [
        # OpenAlex has a DOI for the book, the Zotero record has none: matched by title, author and year
        {"id": "W10", "doi": "10.5555/oa-book", "title": "Designerly Ways of Knowing", "first_author": "Nigel Cross", "year": 1982},
        # same title, other author: no match
        {"id": "W11", "doi": None, "title": "Designerly ways of knowing", "first_author": "Schön", "year": 1982},
        # a bibliography stub from a PDF, matched by title
        {"id": "B:cross-1982-designerly-ways-knowing", "doi": None, "title": "Designerly ways of knowing", "first_author": "Cross", "year": 1983},
        # a patent node matched by publication number, kind code ignored
        {"id": "P:EP3500000", "doi": None, "title": "Foldable display device", "first_author": "ACME", "year": 2017},
        {"id": "P:US1", "doi": None, "title": "Other", "first_author": "X", "year": 2000},
    ]
    knowledge.annotate(nodes)
    assert [n["library_match"] for n in nodes] == ["title", None, "title", "patent", None]
    assert nodes[0]["zotero_key"] == "ZKEY0003" and nodes[0]["verified"]["status"] == "match"
    assert nodes[3]["zotero_key"] == "ZKEY0004" and nodes[1]["in_library"] is False


def test_patents_in_maps(db, snapshot, monkeypatch):
    async def fake_patent_get(number, resolve_literature=0, citing=True):
        if number == "bad":
            raise ValueError("not a patent publication number")
        return {"status": "ok", "number": "EP3500000", "title": "Foldable display device", "applicants": ["ACME GMBH"],
                "priority": "2017-06-01", "published": "2020-01-15", "cited_by_total": 2,
                "url": "https://worldwide.espacenet.com/patent/search?q=pn%3DEP3500000",
                "cites_literature": [{"text": "LEE J: ...", "doi": "10.1234/one"}, {"text": "KHALILBEIGI M: FoldMe", "doi": None}]}

    async def fake_oa_batch(filter_key, values):
        assert filter_key == "doi" and values == ["10.1234/one"]
        return [db["W1"]], []
    monkeypatch.setattr(s, "patent_get", fake_patent_get)
    monkeypatch.setattr(s, "_oa_batch", fake_oa_batch)
    run(s.map_create("P", ["W2"]))
    out = run(s.map_add_patents("P", ["EP3500000", "bad"]))
    assert out["literature_added"] == 1 and out["patents"]["P:EP3500000"]["result"] == "added"
    assert out["patents"]["bad"]["status"] == "invalid_input" and out["unresolved"][0]["text"].startswith("KHALILBEIGI")
    run(s.map_expand("P"))
    got = run(s.map_get("P"))
    p = next(w for w in got["works"] if w["id"] == "P:EP3500000")
    assert p["kind"] == "patent" and p["year"] == 2017 and p["in_library"] and p["zotero_key"] == "ZKEY0004"
    assert ["P:EP3500000", "W1"] in got["patent_edges"] and not any("P:" in e[0] for e in got["citation_edges"])
    # the patent cites W1 and W1 cites W3: no co-citation or coupling may involve the patent
    assert not any("P:EP3500000" in pair[:2] for pair in got["cocitation"] + got["coupling"])
    w1 = next(w for w in got["works"] if w["id"] == "W1")
    assert "patent:P:EP3500000" in w1["found_by"] and w1["kind"] == "work"
    text = run(s.map_export("P", states=["new"]))["markdown"]
    assert "[Espacenet](https://worldwide.espacenet.com/patent/search?q=pn%3DEP3500000)" in text
    with pytest.raises(ValueError):
        run(s.map_add_patents("P", []))

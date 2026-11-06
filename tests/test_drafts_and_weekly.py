import asyncio
import json
import os

import httpx
import pytest

import knowledge
import server as s


@pytest.fixture
def snap(tmp_path, monkeypatch):
    d = tmp_path / "knowledge"
    d.mkdir()
    (d / "library.json").write_text(json.dumps({"schema_version": 1, "generated": "2026-11-04T00:00:00Z", "items": [
        {"key": "ZKEY0001", "doi": "10.1016/0142-694x(82)90040-0", "title": "Designerly ways of knowing", "creators": ["Cross"], "year": 1982,
         "tags": ["cited/thesis", "sub/330304-design-theory"], "collections": ["Design › Theory"], "attachments": [{"key": "ATT1", "contentType": "application/pdf"}],
         "citekey": "cross1982designerly", "verified": {"date": "2026-11-04", "status": "match", "detail": "verify_doi registered_match"}},
        {"key": "ZKEY0002", "doi": None, "title": "The reflective practitioner", "creators": ["Schön"], "year": 1983, "tags": ["cited/thesis"], "collections": [], "attachments": []},
        {"key": "ZKEY0003", "doi": None, "title": "Educating the reflective practitioner", "creators": ["Schön"], "year": 1983, "tags": [], "collections": [], "attachments": []},
        {"key": "ZKEY0004", "doi": "10.5555/old", "title": "An old paper", "creators": ["Norman"], "year": 1988, "tags": ["cited/thesis"], "collections": [], "attachments": [],
         "verified": None}]}))
    (d / "notes.json").write_text(json.dumps({"schema_version": 1, "generated": "2026-11-04T00:00:00Z", "notes": [
        {"path": "Zettelkasten/Reading Notes/Designerly ways.md", "title": "Designerly ways", "folder": "Zettelkasten/Reading Notes", "zotero_keys": ["ZKEY0001"], "kind": None},
        {"path": "Zettelkasten/Reading Notes/Orphan.md", "title": "Orphan", "folder": "Zettelkasten/Reading Notes", "zotero_keys": [], "kind": None},
        {"path": "Zettelkasten/Reading Notes/Gone.md", "title": "Gone", "folder": "Zettelkasten/Reading Notes", "zotero_keys": ["DEADBEEF"], "kind": None},
        {"path": "Zettelkasten/Theses/T.md", "title": "T", "folder": "Zettelkasten/Theses", "zotero_keys": [], "kind": "thesis"}]}))
    monkeypatch.setattr(knowledge, "KNOWLEDGE_DIR", str(d))
    knowledge._cache.clear()
    return d


def test_check_draft_resolves_keys_and_flags_what_is_missing(snap):
    text = ("Cross argues that design has its own epistemology [@cross1982designerly, p. 221]. Norman disagrees [@norman1988]. "
            "Schön is ambiguous here [@schon1983]. Nobody wrote this [@nobody2001]. "
            'As Cross puts it, "designerly ways of knowing rest on the manipulation of non-verbal codes" [@cross1982designerly]. '
            "See also @ZKEY0001 [p. 3] and -@norman1988.")
    out = asyncio.run(s.check_draft(text))
    assert out["status"] == "ok" and out["citations"] == 7 and out["distinct_keys"] == 5
    assert out["resolved"]["cross1982designerly"]["resolved_by"] == "citekey" and out["resolved"]["ZKEY0001"]["resolved_by"] == "item_key"
    assert out["resolved"]["norman1988"]["resolved_by"] == "surname_year" and out["resolved"]["norman1988"]["verified"] is None
    assert out["ambiguous"] == ["schon1983"] and out["unresolved"] == ["nobody2001"]
    assert [u["citekey"] for u in out["unverified"]] == ["norman1988"]
    assert len(out["quotes_without_page"]) == 1 and out["quotes_without_page"][0]["citation"] == "cross1982designerly"
    assert out["bibliography_keys"] == ["ZKEY0001", "ZKEY0004"]
    assert asyncio.run(s.check_draft("no citations here"))["citations"] == 0


def test_coverage_report(snap):
    out = asyncio.run(s.coverage_report(project="thesis"))
    assert out["status"] == "ok" and out["items"] == 3 and out["with_note"] == 1
    assert {r["key"] for r in out["without_note"]} == {"ZKEY0002", "ZKEY0004"}
    assert {r["key"] for r in out["without_pdf"]} == {"ZKEY0002", "ZKEY0004"}
    assert {r["key"] for r in out["unverified"]} == {"ZKEY0002", "ZKEY0004"}
    assert out["notes_without_item"] == ["Zettelkasten/Reading Notes/Orphan.md"]
    assert out["notes_with_missing_item"] == [{"note": "Zettelkasten/Reading Notes/Gone.md", "zotero_key": "DEADBEEF"}]
    whole = asyncio.run(s.coverage_report())
    assert whole["items"] == 4 and whole["projects"] == {"thesis": 3} and whole["anzsrc"] == {"330304-design-theory": 1}
    assert whole["years"] == {"1982": 1, "1983": 2, "1988": 1} and whole["notes_total"] == 4 and whole["items_total"] == 4


def test_weekly_job_writes_a_report_and_survives_a_failing_step(snap, tmp_path, monkeypatch):
    import weekly
    monkeypatch.setattr(weekly, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(weekly, "LAST", str(tmp_path / "state" / "weekly-last.json"))
    monkeypatch.setattr(weekly, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(weekly, "BACKUP_KEEP", 1)

    async def maps():
        return {"maps": [{"name": "Design"}]}

    async def refresh(name, new_citing=False, per_seed=25):
        return {"status": "ok", "new_citing": 3, "retracted": 0}

    async def retractions(dois=None, map=None):
        raise RuntimeError("Crossref down")
    monkeypatch.setattr(s, "map_list", maps)
    monkeypatch.setattr(s, "map_refresh", refresh)
    monkeypatch.setattr(s, "check_retractions", retractions)
    out = asyncio.run(weekly.run(s, knowledge, do_backup=True, do_harvest=False))
    report = open(out["report"], encoding="utf-8").read()
    assert out["report"].startswith(str(snap / "reports" / "weekly-")) and "**Design**: 3 new citing" in report
    assert "Crossref down" in report and "**thesis**: 3 items, 1 with a note" in report and "to read or to write up" in report
    assert "note whose item is gone: [[Gone]]" in report
    assert any(f.endswith("knowledge.tar.gz") for f in os.listdir(tmp_path / "backups"))
    last = json.load(open(tmp_path / "state" / "weekly-last.json"))
    assert last["retractions"]["error"].startswith("RuntimeError")
    # the tool returns it
    monkeypatch.setattr(weekly, "LAST", str(tmp_path / "state" / "weekly-last.json"))
    got = asyncio.run(s.weekly_report())
    assert got["status"] == "ok" and got["markdown"] == report
    # a second run rotates the backups
    asyncio.run(weekly.run(s, knowledge, do_backup=True, do_harvest=False))
    assert sum(f.endswith("knowledge.tar.gz") for f in os.listdir(tmp_path / "backups")) == 1


def test_http_auth_is_bearer_only_and_status_is_loopback(snap, tmp_path, monkeypatch):
    import maps, corpus, philarchive, weekly
    monkeypatch.setattr(maps, "DB_PATH", str(tmp_path / "maps.sqlite"))
    monkeypatch.setattr(corpus, "DB_PATH", str(tmp_path / "corpus.sqlite"))
    monkeypatch.setattr(philarchive, "DB_PATH", str(tmp_path / "philarchive.sqlite"))
    monkeypatch.setattr(weekly, "LAST", str(tmp_path / "nope.json"))

    async def inner(scope, receive, send):
        from starlette.responses import JSONResponse
        await JSONResponse({"ok": True})(scope, receive, send)
    token = "t" * 32
    app = s._TokenAuth(inner, token)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            assert (await c.get("/mcp?token=" + token)).status_code == 401
            assert (await c.get("/mcp", headers={"Authorization": f"Bearer {token}"})).status_code == 200
            monkeypatch.setattr(s, "_ALLOW_QUERY_TOKEN", True)  # Claude web: the token in the URL
            assert (await c.get("/mcp?token=" + token)).status_code == 200
            assert (await c.get("/mcp?token=wrong")).status_code == 401
            monkeypatch.setattr(s, "_ALLOW_QUERY_TOKEN", False)
            st = await c.get("/status")
            assert st.status_code == 200
            body = st.json()
            assert body["service"] == "paper-doi-mcp" and body["maps"] == 0 and body["weekly"] is None
            assert body["snapshots"]["library_snapshot"] and "passages" in body["corpus"] and body["philarchive"]["records"] == 0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 1)), base_url="http://host") as c:
            assert (await c.get("/status")).status_code == 404
    asyncio.run(scenario())


def test_search_register_and_search_log(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "SEARCH_LOG", str(tmp_path / "searches.jsonl"))

    async def fake_openalex(query, year_from, year_to, doc_types, limit, page, scope, oa, ex, fill):
        return s._ok("OpenAlex", query, scope, None, page, limit, 42, [])

    async def fake_dnb(query, year_from, year_to, limit, page, scope):
        raise RuntimeError("DNB down")
    monkeypatch.setattr(s, "_search_openalex", fake_openalex)
    monkeypatch.setitem(s._ADAPTERS, "dnb", fake_dnb)
    asyncio.run(s.search_literature("affordance theory", year_from=2015))
    asyncio.run(s.search_literature("affordance theory", database="openalex,dnb"))
    asyncio.run(s.search_literature("embodied interaction"))
    entries = s._read_search_log()
    assert [e["kind"] for e in entries] == ["search_literature"] * 3
    assert entries[0]["hits"] == {"OpenAlex": 42} and entries[0]["year_from"] == 2015 and entries[0]["errors"] == 0
    assert entries[1]["databases"] == ["openalex", "dnb"] and entries[1]["errors"] == 1
    out = asyncio.run(s.search_log("Affordance Theory"))
    assert len(out["same"]) == 2 and out["summary"].startswith("searched 2 time(s) before, last on")
    assert asyncio.run(s.search_log("theory of affordances"))["similar"] == [] and \
        asyncio.run(s.search_log("affordance theory and design"))["similar"]
    assert asyncio.run(s.search_log("nothing like this"))["summary"] == "not searched before"
    assert asyncio.run(s.search_log())["total_in_period"] == 3 and asyncio.run(s.search_log(days=0))["total_in_period"] == 3


def test_weekly_report_lists_searches_and_notifies(snap, tmp_path, monkeypatch):
    import weekly
    monkeypatch.setattr(weekly, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(weekly, "LAST", str(tmp_path / "state" / "weekly-last.json"))
    monkeypatch.setattr(s, "SEARCH_LOG", str(tmp_path / "searches.jsonl"))
    s._log_event("search_literature", query="affordance theory", databases=["openalex"], hits={"OpenAlex": 42}, returned=25, errors=0)
    s._log_event("map_create", map="Design", seeds=3)

    async def maps():
        return {"maps": []}

    async def retractions(dois=None, map=None):
        return {"notices": [], "not_checked": []}
    monkeypatch.setattr(s, "map_list", maps)
    monkeypatch.setattr(s, "check_retractions", retractions)
    sent = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=20):
        sent["url"], sent["body"], sent["headers"] = req.full_url, req.data.decode(), dict(req.headers)
        return _Resp()
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example/topic")
    monkeypatch.setenv("NTFY_TOKEN", "tk")
    monkeypatch.setattr(weekly.urllib.request, "urlopen", fake_urlopen)
    out = asyncio.run(weekly.run(s, knowledge, do_backup=False, do_harvest=False))
    report = open(out["report"], encoding="utf-8").read()
    assert "## Searches this week" in report and "`affordance theory` on openalex: 42 hit(s)" in report and "map **Design** created (3 seeds)" in report
    assert out["notify"]["status"] == "sent" and sent["url"] == "https://ntfy.example/topic"
    assert sent["headers"]["Title"].startswith("Weekly report") and sent["headers"]["Authorization"] == "Bearer tk"
    assert "0 new citing work(s), 0 retraction notice(s)" in sent["body"]
    monkeypatch.delenv("NTFY_URL")
    assert asyncio.run(weekly.run(s, knowledge, do_backup=False, do_harvest=False))["notify"] == {"status": "disabled"}

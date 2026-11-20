import asyncio

import pytest

from paper_doi_mcp import server as s

TEXT = """Affordances were introduced by Gibson (1979) and reinterpreted later (Norman, 2013).
Think-aloud is limited (Ericsson & Simon, 1993; Fox et al., 2011), see also Gaver (1991, p. 80).
A sequential account (Gaver, 1992) and a disputed one (Bianchi, 2020) follow. Rossi and Conti (2021a)
disagree, as do Rossi and Conti (2021b)."""

REFS = """Gibson, J. J. (1979). The ecological approach to visual perception. Houghton Mifflin.
Norman, D. A. (2013). The design of everyday things (Rev. ed.). Basic Books.
Ericsson, K. A., & Simon, H. A. (1993). Protocol analysis. MIT Press.
Fox, M. C., Ericsson, K. A., & Best, R. (2011). Do procedures for verbal reporting of thinking have to be reactive? Psychological Bulletin, 137(2), 316-344.
Gaver, W. W. (1991). Technology affordances. In Proceedings of CHI '91 (pp. 79-84). ACM.
Rossi, A., & Conti, L. (2021a). First paper. Design Studies, 72, 1-10.
Rossi, A., & Conti, L. (2021b). Second paper. Design Issues, 37(1), 11-20.
Schön, D. A. (1983). The reflective practitioner. Basic Books.
Verdi, P. (2005). One. Journal A, 1, 1-2.
Verdi, P. (2005). Two. Journal B, 2, 3-4."""


def test_cross_check():
    out = asyncio.run(s.check_citations(TEXT, REFS))
    assert out["status"] == "ok" and out["citations_found"] == 9 and out["references_found"] == 10
    assert [c["names"] for c in out["cited_not_in_references"]] == ["Bianchi"]
    assert out["year_mismatches"][0]["names"] == "Gaver" and out["year_mismatches"][0]["reference_years"] == ["1991"]
    assert any("Schön" in e for e in out["references_not_cited"])
    assert any("Verdi" in e for e in out["references_not_cited"])
    assert out["ambiguous"] and all("Verdi" in e for e in out["ambiguous"][0])
    assert not any("Rossi" in e for e in out["references_not_cited"])


def test_requires_both_inputs():
    with pytest.raises(ValueError):
        asyncio.run(s.check_citations("", REFS))


DNB_XML = """<?xml version="1.0"?>
<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">
<numberOfRecords>2</numberOfRecords><records>
<record><recordData><dc xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>Sein und Zeit</dc:title><dc:creator>Heidegger, Martin</dc:creator><dc:date>2006</dc:date>
<dc:publisher>Tübingen : Niemeyer</dc:publisher><dc:identifier>978-3-484-70153-6</dc:identifier>
<dc:identifier>https://d-nb.info/978123456</dc:identifier><dc:language>ger</dc:language><dc:subject>Ontologie</dc:subject><dc:type>Text</dc:type>
</dc></recordData></record>
<record><recordData><dc xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>Sein und Zeit</dc:title><dc:creator>Heidegger, Martin</dc:creator><dc:date>1927</dc:date>
</dc></recordData></record>
</records></searchRetrieveResponse>"""


def test_dnb_search_adapter(monkeypatch):
    import httpx
    seen = {}

    async def handle(request):
        seen.update(dict(request.url.params))
        assert request.url.host == "services.dnb.de"
        return httpx.Response(200, content=DNB_XML.encode("utf-8"))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            return await s.search_literature('"Sein und Zeit"', year_from=2000, database="dnb", limit=5)

    out = asyncio.run(run())
    assert seen["query"] == 'tit="Sein und Zeit"' and seen["recordSchema"] == "oai_dc"
    assert out["status"] == "ok"
    recs = out["results"]
    assert len(recs) == 1 and recs[0]["year"] == 2006 and recs[0]["venue"] == "Niemeyer"
    assert recs[0]["note"] == "ISBN 978-3-484-70153-6" and recs[0]["keywords"] == ["Ontologie"]
    assert out["log"]["total_hits"] == 2 and out["log"]["dropped_by_year"] == 1


def test_dnb_is_left_alone_after_403_or_429(monkeypatch):
    import httpx
    calls = []
    status = {"code": 429}
    monkeypatch.setattr(s, "_dnb_pause", {"until": 0.0, "status": None})

    async def handle(request):
        calls.append(request)
        if status["code"] == 200:
            return httpx.Response(200, content=DNB_XML.encode("utf-8"))
        return httpx.Response(status["code"], text="Forbidden")

    async def search():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            return await s.search_literature('"Sein und Zeit"', database="dnb", limit=5)

    first = asyncio.run(search())
    assert first["status"] == "error" and first["http_status"] == 429 and len(calls) == 1
    status["code"] = 200  # the block is lifted, but the server keeps away from the DNB
    second = asyncio.run(search())
    assert second["status"] == "error" and "paused for" in second["detail"] and "HTTP 429" in second["detail"]
    assert len(calls) == 1
    s._dnb_pause["until"] = 0.0  # pause over
    assert asyncio.run(search())["status"] == "ok" and len(calls) == 2


@pytest.mark.parametrize("query,expected", [
    ('design ethics', 'tit=design AND tit=ethics'),
    ('design ethics NOT "user study"', 'tit=design AND tit=ethics NOT tit="user study"'),
    ('(affordance OR affordances) AND design', '( tit=affordance OR tit=affordances ) AND tit=design'),
    ('design (ethics OR morality)', 'tit=design AND ( tit=ethics OR tit=morality )'),
])
def test_cql_has_explicit_conjunctions(query, expected):
    assert s._cql(query, "tit") == expected

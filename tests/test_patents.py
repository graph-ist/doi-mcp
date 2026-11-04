import asyncio
import xml.etree.ElementTree as ET

import pytest

import patents
import server as s

XML = """<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns:ops="http://ops.epo.org" xmlns="http://www.epo.org/exchange">
 <ops:biblio-search total-result-count="2"><ops:search-result><exchange-documents>
  <exchange-document system="ops.epo.org" family-id="123" country="EP" doc-number="3500000" kind="A1">
   <bibliographic-data>
    <publication-reference><document-id document-id-type="docdb"><country>EP</country><doc-number>3500000</doc-number><kind>A1</kind><date>20190626</date></document-id></publication-reference>
    <classifications-ipcr><classification-ipcr><text>G06F   1/16        20060101AFI20190101BHEP</text></classification-ipcr></classifications-ipcr>
    <priority-claims><priority-claim><document-id><date>20171220</date></document-id></priority-claim></priority-claims>
    <parties><applicants><applicant data-format="epodoc"><applicant-name><name>SAMSUNG ELECTRONICS CO LTD</name></applicant-name></applicant>
     <applicant data-format="original"><applicant-name><name>Samsung Electronics Co., Ltd.</name></applicant-name></applicant></applicants>
     <inventors><inventor data-format="epodoc"><inventor-name><name>KIM JIN [KR]</name></inventor-name></inventor></inventors></parties>
    <invention-title lang="de">Faltbare Anzeige</invention-title><invention-title lang="en">Foldable display device</invention-title>
    <references-cited>
     <citation cited-phase="search"><patcit num="1"><document-id document-id-type="docdb"><country>US</country><doc-number>2015123456</doc-number><kind>A1</kind></document-id>
       <document-id document-id-type="epodoc"><doc-number>US2015123456</doc-number></document-id></patcit></citation>
     <citation cited-phase="search"><nplcit num="2"><text>LEE J ET AL: "Towards more paper-like input: flexible input devices for foldable interaction styles", PROC. UIST 2008, 19 October 2008, pages 1-10, XP058000000, DOI: 10.1145/1449715.1449754</text></nplcit></citation>
     <citation cited-phase="examination"><category>X</category><nplcit num="3"><text>KHALILBEIGI M ET AL: "FoldMe: interacting with double-sided foldable displays", TEI 2012, 19 February 2012, XP055000000</text></nplcit></citation>
    </references-cited>
   </bibliographic-data>
   <abstract lang="en"><p>A foldable display device includes a hinge.</p></abstract>
  </exchange-document>
 </exchange-documents></ops:search-result></ops:biblio-search>
</ops:world-patent-data>"""


def test_parse_document():
    root = ET.fromstring(XML)
    assert patents.total(root) == 2
    (d,) = patents.documents(root)
    assert d["number"] == "EP3500000" and d["title"] == "Foldable display device"
    assert d["applicants"] == ["SAMSUNG ELECTRONICS CO LTD"] and d["inventors"] == ["KIM JIN [KR]"]
    assert d["published"] == "2019-06-26" and d["priority"] == "2017-12-20" and d["ipc"] == ["G06F 1/16"]
    assert d["cites_patents"] == ["US2015123456"] and len(d["cites_literature"]) == 2
    assert d["cites_literature"][1]["phase"] == "examination" and d["cites_literature"][1]["category"] == "X"
    assert d["cites_literature"][0]["category"] is None


def test_parse_npl_and_numbers():
    f = patents.parse_npl('LEE J ET AL: "Towards more paper-like input", PROC. UIST 2008, 19 October 2008, DOI: 10.1145/1449715.1449754')
    assert f == {"doi": "10.1145/1449715.1449754", "title": "Towards more paper-like input", "first_author": "Lee", "year": 2008}
    assert patents.number("EP 3 500 000 A1") == "EP3500000" and patents.number("us2019/123456") == "US2019123456"
    with pytest.raises(ValueError):
        patents.number("foldable")
    assert patents.cql("foldable display") == 'ta all "foldable display"'
    assert patents.cql("pa=samsung and ic=G06F1/16") == "pa=samsung and ic=G06F1/16"


def test_patent_get_resolves_literature(monkeypatch):
    calls = []

    async def fake_get(c, path, params=None):
        calls.append((path, params))
        return ET.fromstring(XML)

    async def fake_crossref(c, title, author):
        return [s._cand("crossref", "10.1145/2148131.2148148",
                        "FoldMe: interacting with double-sided foldable displays",
                        ["Khalilbeigi", "Lissermann"], 2012),
                s._cand("crossref", "10.1145/9999", "FoldMe: a poster", ["Rossi"], 2012)]

    monkeypatch.setattr(patents, "get", fake_get)
    monkeypatch.setattr(s, "_crossref", fake_crossref)
    out = asyncio.run(s.patent_get("EP3500000"))
    assert out["status"] == "ok" and out["literature_with_doi"] == 1 and out["literature_with_candidate"] == 1
    first, second = out["cites_literature"]
    assert first["doi"] == "10.1145/1449715.1449754" and first["doi_candidate"] is None
    assert second["doi"] is None and second["doi_candidate"]["doi"] == "10.1145/2148131.2148148"
    assert second["doi_candidate"]["author_ok"] is True and second["doi_candidate"]["title_score"] >= s.TITLE_THRESHOLD
    assert out["cited_by_total"] == 2 and calls[1][1]["q"] == "ct=EP3500000"


def test_patent_get_rejects_weak_candidates_and_respects_budget(monkeypatch):
    calls = []

    async def fake_get(c, path, params=None):
        return ET.fromstring(XML)

    async def fake_crossref(c, title, author):
        calls.append(title)
        return [s._cand("crossref", "10.1145/1", "FoldMe: interacting with foldable displays and more things", ["Verdi"], 2012)]

    monkeypatch.setattr(patents, "get", fake_get)
    monkeypatch.setattr(s, "_crossref", fake_crossref)
    out = asyncio.run(s.patent_get("EP3500000", citing=False))
    assert out["cites_literature"][1]["doi_candidate"] is None and len(calls) == 1
    out = asyncio.run(s.patent_get("EP3500000", resolve_literature=0, citing=False))
    assert len(calls) == 1 and "cited_by" not in out


def test_patent_get_invalid_number_raises_for_the_wrapper():
    with pytest.raises(ValueError):
        asyncio.run(s.patent_get("foldable"))


def test_not_configured(monkeypatch):
    monkeypatch.setattr(patents, "KEY", "")
    patents._token["value"] = None
    out = asyncio.run(s.patent_search("foldable display"))
    assert out["status"] == "not_configured"


def test_npl_surname_handles_both_name_orders():
    assert patents.npl_surname('LEE J ET AL: "Towards more paper-like input", PROC. UIST 2008') == "Lee"
    assert patents.npl_surname('JOHN SMITH: "A foldable display", 2019') == "Smith"
    assert patents.npl_surname('Maria Rossi, Luca Bianchi: "Flexible", 2020') == "Rossi"
    assert patents.npl_surname('VAN DER LINDEN J: "x"') == "Van Der Linden"
    assert patents.npl_surname('SMITH J.: "y"') == "Smith" and patents.npl_surname(': "no author"') is None
    assert patents.parse_npl('JOHN SMITH: "A foldable display device", 2019')["first_author"] == "Smith"

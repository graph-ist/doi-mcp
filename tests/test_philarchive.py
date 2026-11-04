import asyncio

import pytest

import philarchive
import server as s

PAGE1 = """<?xml version="1.0"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>
<record><header><identifier>oai:philarchive.org/rec/ABC1</identifier><datestamp>2026-10-01</datestamp></header>
<metadata><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>Affordances as relations: a phenomenological account</dc:title><dc:creator>Rossi, Maria</dc:creator>
<dc:description>Affordances are relational and the artefact's properties underdetermine them.</dc:description>
<dc:subject>Philosophy of Mind</dc:subject><dc:date>2025</dc:date><dc:identifier>https://philarchive.org/rec/ABC1</dc:identifier>
<dc:identifier>doi:10.5555/aff</dc:identifier><dc:language>en</dc:language></oai_dc:dc></metadata></record>
<record><header status="deleted"><identifier>oai:philarchive.org/rec/GONE</identifier><datestamp>2026-10-02</datestamp></header></record>
<resumptionToken>tok1</resumptionToken></ListRecords></OAI-PMH>"""
PAGE2 = """<?xml version="1.0"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>
<record><header><identifier>oai:philarchive.org/rec/DEF2</identifier><datestamp>2026-10-03</datestamp></header>
<metadata><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>Design ethics and agency</dc:title><dc:creator>Bianchi, Luca</dc:creator><dc:date>2019-05-01</dc:date>
<dc:identifier>https://philarchive.org/rec/DEF2</dc:identifier></oai_dc:dc></metadata></record>
</ListRecords></OAI-PMH>"""


@pytest.fixture
def pdb(tmp_path, monkeypatch):
    monkeypatch.setattr(philarchive, "DB_PATH", str(tmp_path / "philarchive.sqlite"))
    return philarchive.connect()


def test_harvest_follows_resumption_tokens_and_is_incremental(pdb):
    calls = []

    def fetch(params):
        calls.append(params)
        return PAGE2 if params.get("resumptionToken") == "tok1" else PAGE1
    out = philarchive.harvest(pdb, fetch=fetch)
    assert out["records"] == 2 and out["pages"] == 2 and out["full"] and calls[0]["metadataPrefix"] == "oai_dc"
    assert calls[1] == {"verb": "ListRecords", "resumptionToken": "tok1"}
    info = philarchive.count(pdb)
    assert info["records"] == 2 and info["last_harvest"]
    calls.clear()
    philarchive.harvest(pdb, fetch=lambda p: (calls.append(p), b"<OAI-PMH><error code='noRecordsMatch'/></OAI-PMH>")[1])
    assert calls[0]["from"] == info["last_harvest"][:10]
    rows, total = philarchive.search(pdb, "affordances", None, None, 10, 1)
    assert total == 1 and rows[0]["doi"] == "10.5555/aff" and rows[0]["year"] == 2025
    assert philarchive.search(pdb, "agency", 2020, None, 10, 1)[1] == 0
    assert philarchive.search(pdb, "Bianchi", None, None, 10, 1, scope="fulltext")[1] == 1
    assert philarchive.search(pdb, "Bianchi", None, None, 10, 1)[1] == 0


def test_search_literature_uses_the_local_copy(pdb):
    assert "philarchive" in s.DATABASES
    out = asyncio.run(s.search_literature("affordances", database="philarchive"))
    assert out["status"] == "error" and "empty" in out["detail"]
    philarchive.harvest(pdb, fetch=lambda p: PAGE2 if p.get("resumptionToken") else PAGE1)
    out = asyncio.run(s.search_literature("affordances", database="philarchive"))
    assert out["status"] == "ok" and out["results"][0]["doi"] == "10.5555/aff" and out["results"][0]["found_in"] == ["philarchive"]
    assert out["log"]["records_in_copy"] == 2 and out["results"][0]["abstract"].startswith("Affordances are relational")

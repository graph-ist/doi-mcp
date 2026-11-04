"""Patents from EPO Open Patent Services (OPS 3.2): search, bibliographic data and the
literature a patent cites ("non-patent literature", NPL), so patents can sit next to papers.

Credentials: EPO_OPS_KEY and EPO_OPS_SECRET (developers.epo.org, app with "OPS v3.2"; free up
to 4 GB a week). Responses are XML; this module only parses them, the server wires the tools.
"""
import base64
import os
import re
import time
import xml.etree.ElementTree as ET

import httpx

BASE = "https://ops.epo.org/3.2"
NS = {"ops": "http://ops.epo.org", "ex": "http://www.epo.org/exchange"}
KEY, SECRET = os.environ.get("EPO_OPS_KEY", ""), os.environ.get("EPO_OPS_SECRET", "")
_token: dict = {"value": None, "until": 0.0}


class NotConfigured(RuntimeError):
    pass


def configured() -> bool:
    return bool(KEY and SECRET)


async def _access_token(c: httpx.AsyncClient) -> str:
    if _token["value"] and time.time() < _token["until"]:
        return _token["value"]
    if not configured():
        raise NotConfigured("EPO OPS credentials missing: set EPO_OPS_KEY and EPO_OPS_SECRET in /etc/doi-mcp/env")
    basic = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    r = await c.post(f"{BASE}/auth/accesstoken", data={"grant_type": "client_credentials"},
                     headers={"Authorization": f"Basic {basic}"})
    r.raise_for_status()
    d = r.json()
    _token.update(value=d["access_token"], until=time.time() + int(d.get("expires_in", 1200)) - 60)
    return _token["value"]


async def get(c: httpx.AsyncClient, path: str, params: dict | None = None) -> ET.Element | None:
    """GET an OPS resource as XML; None when OPS has no result (404)."""
    for attempt in (0, 1):
        tok = await _access_token(c)
        r = await c.get(f"{BASE}/rest-services/{path}", params=params,
                        headers={"Authorization": f"Bearer {tok}", "Accept": "application/xml"})
        if r.status_code == 400 and "invalid_access_token" in r.text and not attempt:
            _token["value"] = None
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return ET.fromstring(r.content)
    return None


def cql(query: str) -> str:
    """Free words become a title/abstract search; text with '=' is taken as OPS CQL."""
    q = query.strip()
    if "=" in q or re.search(r"\b(all|any|within)\b", q):
        return q
    return f'ta all "{q.replace(chr(34), "")}"'


def number(text: str) -> str:
    """'EP 1 000 000 A1', 'EP1000000' -> 'EP1000000' (epodoc, kind code dropped)."""
    t = re.sub(r"[\s.,/-]", "", text.upper())
    m = re.fullmatch(r"([A-Z]{2})(\d+)([A-Z]\d?)?", t)
    if not m:
        raise ValueError(f"not a patent publication number: {text!r} (e.g. EP1000000, US2019123456A1)")
    return m.group(1) + m.group(2)


def _txt(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    x = el.find(path, NS)
    return " ".join("".join(x.itertext()).split()) if x is not None else None


def _docid(el: ET.Element) -> dict:
    d = el.find("ex:document-id", NS)
    if d is None:
        return {}
    country, num, kind, date = (_txt(d, f"ex:{k}") for k in ("country", "doc-number", "kind", "date"))
    return {"number": f"{country or ''}{num or ''}", "kind": kind, "date": date}


def parse_document(doc: ET.Element) -> dict:
    """One exchange-document: number, title (English first), applicants, inventors, dates,
    classes, abstract, patent citations and NPL citations (raw text)."""
    bib = doc.find("ex:bibliographic-data", NS)
    titles = {t.get("lang"): " ".join("".join(t.itertext()).split()) for t in bib.findall("ex:invention-title", NS)} if bib is not None else {}
    names = lambda role: list(dict.fromkeys(
        " ".join("".join(n.itertext()).split()).rstrip(",")
        for n in bib.findall(f"ex:parties/ex:{role}s/ex:{role}[@data-format='epodoc']/ex:{role}-name/ex:name", NS))) if bib is not None else []
    pub = bib.find("ex:publication-reference", NS) if bib is not None else None
    pubid = _docid(pub) if pub is not None else {}
    prio = [_txt(p, "ex:document-id/ex:date") for p in bib.findall("ex:priority-claims/ex:priority-claim", NS)] if bib is not None else []
    ipc = [" ".join((c.text or "").split()[:2]) for c in bib.findall("ex:classifications-ipcr/ex:classification-ipcr/ex:text", NS)] if bib is not None else []
    patcit, npl = [], []
    if bib is not None:
        for cit in bib.findall("ex:references-cited/ex:citation", NS):
            p = cit.find("ex:patcit", NS)
            if p is not None:
                ids = [_docid_el(x) for x in p.findall("ex:document-id", NS)]
                best = next((i for i in ids if i["type"] == "epodoc"), ids[0] if ids else None)
                if best and best["number"]:
                    patcit.append(best["number"])
            n = cit.find("ex:nplcit", NS)
            if n is not None:
                t = _txt(n, "ex:text")
                if t:
                    npl.append({"text": t, "phase": cit.get("cited-phase")})
    abstract = next((" ".join("".join(a.itertext()).split()) for a in doc.findall("ex:abstract", NS) if a.get("lang") == "en"), None) \
        or next((" ".join("".join(a.itertext()).split()) for a in doc.findall("ex:abstract", NS)), None)
    return {"number": f"{doc.get('country', '')}{doc.get('doc-number', '')}", "kind": doc.get("kind"),
            "family_id": doc.get("family-id"),
            "title": titles.get("en") or next(iter(titles.values()), None),
            "applicants": names("applicant"), "inventors": names("inventor"),
            "published": _date(pubid.get("date")), "priority": _date(min((p for p in prio if p), default=None)),
            "ipc": list(dict.fromkeys(ipc)), "abstract": abstract,
            "cites_patents": list(dict.fromkeys(patcit)), "cites_literature": npl}


def _docid_el(d: ET.Element) -> dict:
    country, num, kind = (_txt(d, f"ex:{k}") for k in ("country", "doc-number", "kind"))
    n = num or ""
    if d.get("document-id-type") == "docdb":
        n = f"{country or ''}{n}"
    return {"type": d.get("document-id-type"), "number": n}


def _date(d: str | None) -> str | None:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) == 8 else d


def documents(root: ET.Element | None) -> list[dict]:
    if root is None:
        return []
    return [parse_document(d) for d in root.iter(f"{{{NS['ex']}}}exchange-document")
            if d.get("status") != "not found"]


def total(root: ET.Element | None) -> int:
    if root is None:
        return 0
    s = root.find(".//ops:biblio-search", NS)
    return int(s.get("total-result-count", 0)) if s is not None else 0


DOI = re.compile(r"\b10\.\d{4,9}/[^\s,;\"'<>]+", re.I)


def parse_npl(text: str) -> dict:
    """Fields of an NPL citation as EPO search reports write it, e.g.
    'SMITH J ET AL: "A foldable display", PROC. CHI 2019, 1 May 2019, pages 1-10, XP058..., DOI: 10.1145/...'."""
    doi = DOI.search(text)
    title = re.search(r"[\"“]([^\"”]{8,300})[\"”]", text)
    year = re.findall(r"\b(19[5-9]\d|20[0-3]\d)\b", text)
    author = re.match(r"\s*([A-ZÀ-Þ][A-Za-zÀ-ÿ'\-]+)", text)
    return {"doi": doi.group(0).rstrip(".)]") if doi else None, "title": title.group(1).strip(" ,.") if title else None,
            "first_author": author.group(1).title() if author else None, "year": int(year[0]) if year else None}

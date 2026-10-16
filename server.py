"""MCP server: resolve and verify DOIs for cited papers.

Sources: Crossref (authoritative for DOI registration), Semantic Scholar, OpenAlex.
Env (optional):
  CONTACT_EMAIL            used for Crossref/OpenAlex polite pool
  SEMANTIC_SCHOLAR_API_KEY raises Semantic Scholar rate limits
"""

import asyncio
import html
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from mcp.server.fastmcp import FastMCP

VERSION = "0.2.0"
mcp = FastMCP("paper-doi")

EMAIL = os.environ.get("CONTACT_EMAIL", "")
S2_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
UA = f"paper-doi-mcp/{VERSION}" + (f" (mailto:{EMAIL})" if EMAIL else "")
TITLE_THRESHOLD = 0.88
# Titles shorter than this identify little on their own, so a match also needs the author
SHORT_TITLE_TOKENS = 4
BOOK_TYPES = {"book", "monograph", "edited-book", "reference-book", "book-set"}
# Crossref/OpenAlex types that can share a title with a work without being that work
NON_WORK_TYPES = {"peer-review", "component", "grant", "erratum", "retraction", "paratext"}
DERIVATIVE_RE = re.compile(
    r"^\s*(?:correction|corrigendum|erratum|addendum|retraction(?:\s+(?:note|notice))?|"
    r"notice of retraction|expression of concern|decision letter|author response|"
    r"reply|response|comment|review)(?:\s+(?:to|for|on|of))?\s*[:\"'“‘«]", re.I)
RETRACTED_PREFIX_RE = re.compile(r"^\s*(?:retracted|withdrawn)(?:\s+article)?\s*:\s*", re.I)
RETRACTION_TYPES = {"retraction", "removal", "withdrawal"}
NAME_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "du", "le",
                  "la", "dos", "das", "ten", "ter", "zu", "al", "el", "bin", "ibn"}

DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)


def _norm(s: str | None) -> str:
    # Drop accents but keep non-Latin letters, so Cyrillic or CJK titles don't normalize to ""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[\W_]+", " ", s.casefold()).split())


def _clean_title(s: str | None) -> str:
    return RETRACTED_PREFIX_RE.sub("", _strip_html(s) or "")


def _title_sim(cited: str, title: str | None, subtitle: str | None = None) -> tuple[float, int]:
    """Best similarity over subtitle variants, and the token length of the shorter compared form."""
    c, t = _clean_title(cited), _clean_title(title)
    pairs = [(c, t), (c, t.split(":")[0]), (c.split(":")[0], t)]
    if subtitle:
        pairs.append((c, f"{t} {_clean_title(subtitle)}"))
    best = (0.0, 0)
    for a, b in pairs:
        a, b = _norm(a), _norm(b)
        s = SequenceMatcher(None, a, b).ratio() if a and b else 0.0
        if s > best[0]:
            best = (s, min(len(a.split()), len(b.split())))
    return round(best[0], 3), best[1]


def _name_tokens(name: str) -> set[str]:
    return {t for t in _norm(name).split() if len(t) > 1}


def _surname(author: str) -> set[str]:
    """Surname tokens of a cited author: 'Fox, M.' and 'Mark Fox' both give {'fox'}."""
    part = author.split(",")[0] if "," in author else (author.split() or [""])[-1]
    return _name_tokens(part) - NAME_PARTICLES


def _surname_query(author: str) -> str:
    return max(_surname(author), key=len, default="")


def _author_ok(author: str | None, authors: list[str]) -> bool | None:
    """Whether the cited surname is among the record's name tokens; None if undecidable."""
    want = _surname(author) if author else set()
    if not want or not authors:
        return None
    return bool(want & set().union(*map(_name_tokens, authors)))


def _clean_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    s = doi.strip()
    if re.match(r"https?://", s, re.I):
        s = unquote(urlsplit(s).path)
    m = DOI_RE.search(s)
    if not m:
        return None
    d = m.group(0)
    # Trailing punctuation belongs to the surrounding text; brackets only when unbalanced
    while d and (d[-1] in ".,;:'\"]" or (d[-1] == ")" and d.count(")") > d.count("("))
                 or (d[-1] == ">" and d.count(">") > d.count("<"))):
        d = d[:-1]
    return d.lower()


def _doi_path(d: str) -> str:
    return quote(d, safe="/")


def _err(name: str, e: BaseException) -> str:
    return f"{name}: {type(e).__name__}" + (f": {e}" if str(e) else "")


class _PoliteTransport(httpx.AsyncHTTPTransport):
    """Caps concurrency per host and spaces requests where the API's terms require it."""
    CONCURRENCY = {"api.semanticscholar.org": 1, "export.arxiv.org": 1}
    MIN_INTERVAL = {"api.semanticscholar.org": 1.0, "export.arxiv.org": 3.0}

    def __init__(self, **kw):
        super().__init__(**kw)
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._last: dict[str, float] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        sem = self._sems.setdefault(host, asyncio.Semaphore(self.CONCURRENCY.get(host, 5)))
        async with sem:
            wait = self._last.get(host, 0.0) + self.MIN_INTERVAL.get(host, 0.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()
            return await super().handle_async_request(request)


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30, headers={"User-Agent": UA},
                                    transport=_PoliteTransport())
    return _client


_WORK_CACHE: dict[str, tuple[float, int, dict | None]] = {}


async def _crossref_work(d: str) -> tuple[int, dict | None]:
    """Crossref record for a DOI, cached for an hour; returns (HTTP status, message)."""
    hit = _WORK_CACHE.get(d)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1], hit[2]
    r = await _http().get(f"https://api.crossref.org/works/{_doi_path(d)}",
                          params={"mailto": EMAIL} if EMAIL else {})
    msg = r.json()["message"] if r.status_code == 200 else None
    if r.status_code in (200, 404):
        if len(_WORK_CACHE) >= 1000:
            _WORK_CACHE.pop(next(iter(_WORK_CACHE)))
        _WORK_CACHE[d] = (time.monotonic(), r.status_code, msg)
    return r.status_code, msg


def _cand(source: str, doi, title, authors, year, venue=None, **extra) -> dict[str, Any] | None:
    doi = _clean_doi(doi)
    if not doi or not title:
        return None
    return {"source": source, "doi": doi, "title": _strip_html(title), "authors": authors,
            "year": year, "venue": venue, **extra}


async def _crossref(c: httpx.AsyncClient, title: str, author: str | None) -> list[dict]:
    q = f"{title} {author}" if author else title
    params = {"query.bibliographic": q, "rows": 5,
              "select": "DOI,title,subtitle,author,issued,container-title,type"}
    if EMAIL:
        params["mailto"] = EMAIL
    r = await c.get("https://api.crossref.org/works", params=params)
    r.raise_for_status()
    out = []
    for it in r.json()["message"]["items"]:
        year = (it.get("issued", {}).get("date-parts") or [[None]])[0][0]
        authors = [a.get("family", a.get("name", "")) for a in it.get("author", [])]
        out.append(_cand("crossref", it.get("DOI"), (it.get("title") or [""])[0],
                         authors, year, (it.get("container-title") or [None])[0],
                         subtitle=(it.get("subtitle") or [None])[0], type=it.get("type")))
    return out


async def _semantic_scholar(c: httpx.AsyncClient, title: str) -> list[dict]:
    r = await _s2(c, "GET", "https://api.semanticscholar.org/graph/v1/paper/search/match",
                  params={"query": title, "fields": "title,year,authors,externalIds,venue"})
    if r.status_code == 404:
        return []
    r.raise_for_status()
    out = []
    for it in r.json().get("data", []):
        doi = (it.get("externalIds") or {}).get("DOI")
        authors = [a["name"] for a in it.get("authors", []) if a.get("name")]
        out.append(_cand("semantic_scholar", doi, it.get("title"), authors,
                         it.get("year"), it.get("venue") or None))
    return out


async def _openalex(c: httpx.AsyncClient, title: str) -> list[dict]:
    params = {"search": _norm(title), "per-page": 5,
              "select": "doi,title,publication_year,authorships,primary_location,type"}
    if EMAIL:
        params["mailto"] = EMAIL
    r = await c.get("https://api.openalex.org/works", params=params)
    r.raise_for_status()
    out = []
    for it in r.json().get("results", []):
        authors = [a["author"]["display_name"]
                   for a in it.get("authorships", []) if a.get("author", {}).get("display_name")]
        src = ((it.get("primary_location") or {}).get("source") or {}).get("display_name")
        out.append(_cand("openalex", it.get("doi"), it.get("title"), authors,
                         it.get("publication_year"), src, type=it.get("type")))
    return out


async def _crossref_type(doi: str) -> str | None:
    _, m = await _crossref_work(doi)
    return m.get("type") if m else None


async def _open_library(c: httpx.AsyncClient, title: str, author: str | None) -> list[dict]:
    params = {"title": title, "limit": 3, "fields": "title,author_name,first_publish_year,isbn,edition_count"}
    if author:
        params["author"] = author
    r = await c.get("https://openlibrary.org/search.json", params=params)
    r.raise_for_status()
    return [
        {"title": d.get("title"), "authors": d.get("author_name", []),
         "first_publish_year": d.get("first_publish_year"),
         "edition_count": d.get("edition_count"), "isbn_sample": (d.get("isbn") or [])[:3]}
        for d in r.json().get("docs", [])
    ]


def _assess(cand: dict, title: str, author: str | None, year: int | None) -> dict:
    score, tokens = _title_sim(title, cand["title"], cand.get("subtitle"))
    year_ok = abs(year - int(cand["year"])) <= 1 if year and cand.get("year") else None
    return {"score": score, "title_tokens": tokens, "year_ok": year_ok,
            "author_ok": _author_ok(author, cand["authors"])}


def _eligible(m: dict) -> bool:
    if m["score"] < TITLE_THRESHOLD or m["author_ok"] is False or m["year_ok"] is False:
        return False
    return m["title_tokens"] >= SHORT_TITLE_TOKENS or m["author_ok"] is True


def _is_derivative(cand: dict, cited: str) -> bool:
    """Errata, retraction notices, peer reviews and replies that carry the work's title."""
    if cand.get("type") in NON_WORK_TYPES:
        return True
    return bool(DERIVATIVE_RE.match(cand["title"] or "")) and not DERIVATIVE_RE.match(cited)


@mcp.tool()
async def find_doi(title: str, first_author: str | None = None, year: int | None = None,
                   is_book: bool = False) -> dict:
    """Find the DOI of a paper from its title (optionally first author surname and year).

    Queries Crossref, Semantic Scholar and OpenAlex in parallel and ranks candidates by
    title similarity (subtitle variants tolerated). Status 'match' requires title similarity
    above threshold, no author conflict and a year within 1 when given; titles under four
    words also need first_author to confirm. Otherwise 'no_confident_match': attach no DOI.
    Errata, retraction notices, peer reviews and replies are excluded and counted in
    'excluded_derivatives'. 'sources' shows which indexes hold the DOI; they derive DOIs
    largely from Crossref, so agreement is not independent confirmation.

    Set is_book=True for monographs. Book DOIs are edition-specific and many books have
    none, so the year must match exactly (else status 'edition_mismatch', no DOI), and
    Open Library edition/ISBN data is returned in 'book_info' as a fallback identifier.
    """
    names = ["crossref", "semantic_scholar", "openalex"]
    c = _http()
    jobs = [_crossref(c, title, first_author), _semantic_scholar(c, title), _openalex(c, title)]
    if is_book:
        names.append("open_library")
        jobs.append(_open_library(c, title, first_author))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    errors, cands, book_info, excluded = [], [], [], 0
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            errors.append(_err(name, res))
        elif name == "open_library":
            book_info = res
        else:
            for x in filter(None, res):
                if _is_derivative(x, title):
                    excluded += 1
                else:
                    cands.append(x)

    merged: dict[str, dict] = {}
    for cd in cands:
        a = _assess(cd, title, first_author, year)
        m = merged.setdefault(cd["doi"], {**cd, "score": -1.0, "title_tokens": 0,
                                          "year_ok": None, "author_ok": None, "sources": []})
        m["sources"].append(cd["source"])
        if (_eligible(a), a["score"]) > (_eligible(m), m["score"]):
            m.update({k: v for k, v in cd.items() if k != "source"}, **a)
    ranked = sorted(merged.values(),
                    key=lambda m: (_eligible(m), m["score"], len(m["sources"])), reverse=True)
    for m in ranked:
        m.pop("source", None)
        m["url"] = f"https://doi.org/{m['doi']}"

    if is_book:
        # Same-title records are often reviews or chapters; keep only book-level Crossref types
        ranked = ranked[:8]
        types = await asyncio.gather(*(_crossref_type(m["doi"]) for m in ranked),
                                     return_exceptions=True)
        for m, t in zip(ranked, types):
            m["type"] = t if isinstance(t, str) else None
        ranked = [m for m in ranked if m["type"] in BOOK_TYPES]
    ranked = ranked[:3]
    best = ranked[0] if ranked and _eligible(ranked[0]) else None
    status = "match" if best else "no_confident_match"
    if is_book and best and year and best["year"] and int(best["year"]) != year:
        status = "edition_mismatch"
    out = {"status": status, "best": best if status == "match" else None,
           "candidates": ranked, "excluded_derivatives": excluded, "source_errors": errors}
    notes = []
    top = ranked[0] if ranked else None
    if (top and not best and top["score"] >= TITLE_THRESHOLD and top["author_ok"] is None
            and top["title_tokens"] < SHORT_TITLE_TOKENS):
        notes.append("Short title: pass first_author so the match can be confirmed.")
    if is_book:
        out["book_info"] = book_info
        if status != "match":
            notes.append("No DOI for the cited edition. Cite without DOI (ISBN optional); "
                         "candidates, if any, belong to other editions or chapters.")
    if notes:
        out["note"] = " ".join(notes)
    return out


def _biblio_norm(s) -> str:
    s = re.sub(r"[\u2010-\u2015\s]+", "-", str(s)).strip("-").lower()
    # Expand abbreviated page ranges: 123-45 -> 123-145
    m = re.fullmatch(r"(\d+)-(\d+)", s)
    if m and len(m[2]) < len(m[1]):
        s = f"{m[1]}-{m[1][:len(m[1]) - len(m[2])]}{m[2]}"
    return s


@mcp.tool()
async def verify_doi(doi: str, expected_title: str | None = None,
                     expected_first_author: str | None = None, expected_year: int | None = None,
                     expected_volume: str | None = None, expected_issue: str | None = None,
                     expected_pages: str | None = None) -> dict:
    """Check that a DOI is registered in Crossref and return its metadata.

    If expected_title is given, also returns a title-similarity score, which detects
    DOIs that exist but belong to a different paper; status 'registered_match' or
    'registered_title_mismatch'. If expected_first_author is given and absent from the
    record's authors, status is 'registered_author_mismatch'. Volume, issue, pages and year
    are returned from the source; expected values are compared with it
    ('bibliographic_checks': match | mismatch | not_in_source per field; year matches the
    issued, print or online year). Cite the source values whenever they differ.
    'is_retracted' and 'updates' report retractions, corrections and expressions of concern
    registered in Crossref (including Retraction Watch data): a retracted work may still
    be cited, but the reference must say so.
    """
    d = _clean_doi(doi)
    if not d:
        return {"status": "invalid_format", "doi": doi}
    try:
        code, m = await _crossref_work(d)
        if code == 404:
            # Non-Crossref DOIs (e.g. DataCite, arXiv) resolve via doi.org
            h = await _http().head(f"https://doi.org/{_doi_path(d)}", follow_redirects=False)
            return {"status": "registered_non_crossref" if h.status_code in (301, 302, 303)
                    else "not_found", "doi": d}
    except httpx.HTTPError as e:
        return {"status": "error", "doi": d, "detail": _err("crossref", e)}
    if code != 200 or m is None:
        return {"status": "error", "doi": d, "http_status": code}
    raw_title = (m.get("title") or [""])[0]
    subtitle = (m.get("subtitle") or [None])[0]
    years = {k: (m.get(k, {}).get("date-parts") or [[None]])[0][0]
             for k in ("issued", "published-print", "published-online")}
    updates = [{"type": u.get("type"), "doi": u.get("DOI"), "source": u.get("source"),
                "date": ((u.get("updated") or {}).get("date-time") or "")[:10] or None}
               for u in m.get("updated-by", [])]
    out = {
        "status": "registered", "doi": d, "url": f"https://doi.org/{d}",
        "title": _strip_html(raw_title), "subtitle": _strip_html(subtitle),
        "authors": [a.get("family", a.get("name", "")) for a in m.get("author", [])],
        "year": years["issued"], "year_print": years["published-print"],
        "year_online": years["published-online"],
        "venue": (m.get("container-title") or [None])[0], "type": m.get("type"),
        "volume": m.get("volume"), "issue": m.get("issue"),
        "pages": m.get("page") or m.get("article-number"),
        "publisher": m.get("publisher"), "edition": m.get("edition-number"),
        "isbn": m.get("ISBN"),
        "is_retracted": any(u["type"] in RETRACTION_TYPES for u in updates)
                        or bool(RETRACTED_PREFIX_RE.match(raw_title)),
        "updates": updates,
    }
    expected = {"volume": expected_volume, "issue": expected_issue, "pages": expected_pages}
    checks = {}
    for field, exp in expected.items():
        if exp is None:
            continue
        src = out[field]
        checks[field] = ("not_in_source" if not src
                         else "match" if _biblio_norm(src) == _biblio_norm(exp) else "mismatch")
    if expected_year:
        known = {y for y in years.values() if y}
        checks["year"] = ("not_in_source" if not known
                          else "match" if expected_year in known else "mismatch")
    author_ok = _author_ok(expected_first_author, out["authors"])
    if expected_first_author:
        checks["first_author"] = {True: "match", False: "mismatch", None: "not_in_source"}[author_ok]
    if checks:
        out["bibliographic_checks"] = checks
    if expected_title:
        sim, _ = _title_sim(expected_title, raw_title, subtitle)
        out["title_similarity"] = sim
        out["status"] = "registered_match" if sim >= TITLE_THRESHOLD else "registered_title_mismatch"
    if author_ok is False and out["status"] != "registered_title_mismatch":
        out["status"] = "registered_author_mismatch"
    return out


def _book_rec(source, title, authors, publishers, years, isbns, doi=None) -> dict:
    return {"source": source, "title": title or "", "authors": authors or [],
            "publishers": publishers or [], "years": [int(y) for y in years or []],
            "isbns": [re.sub(r"[^0-9Xx]", "", i).upper() for i in isbns or []], "doi": doi}


async def _book_open_library(c: httpx.AsyncClient, title: str, author: str | None) -> list[dict]:
    q = f'title:"{_norm(title)}"' + (f" AND author:{_surname_query(author)}" if author else "")
    r = await c.get("https://openlibrary.org/search.json", params={
        "q": q, "limit": 5,
        "fields": "key,title,author_name,editions,editions.title,editions.publisher,"
                  "editions.publish_year,editions.isbn"})
    r.raise_for_status()
    return [_book_rec("open_library", e.get("title") or w.get("title"), w.get("author_name"),
                      e.get("publisher"), e.get("publish_year"), e.get("isbn"))
            for w in r.json().get("docs", []) for e in w.get("editions", {}).get("docs", [])]


async def _book_crossref(c: httpx.AsyncClient, title: str, author: str | None) -> list[dict]:
    params = {"query.bibliographic": f"{title} {author or ''}".strip(), "rows": 5,
              "filter": "type:book,type:monograph,type:edited-book",
              "select": "DOI,title,author,editor,issued,publisher,ISBN"}
    if EMAIL:
        params["mailto"] = EMAIL
    r = await c.get("https://api.crossref.org/works", params=params)
    r.raise_for_status()
    out = []
    for it in r.json()["message"]["items"]:
        people = [p.get("family", p.get("name", "")) for p in it.get("author", []) + it.get("editor", [])]
        year = (it.get("issued", {}).get("date-parts") or [[None]])[0][0]
        out.append(_book_rec("crossref", (it.get("title") or [""])[0], people,
                             [it["publisher"]] if it.get("publisher") else [],
                             [year] if year else [], it.get("ISBN"), it.get("DOI")))
    return out


async def _book_dnb(c: httpx.AsyncClient, title: str, author: str | None) -> list[dict]:
    # Deutsche Nationalbibliothek SRU; covers the German-language market, incl. translations
    query = f'tit="{_norm(title)}"' + (f' AND per={_surname_query(author)}' if author else "")
    r = await c.get("https://services.dnb.de/sru/dnb", params={
        "version": "1.1", "operation": "searchRetrieve", "query": query,
        "recordSchema": "oai_dc", "maximumRecords": 5})
    r.raise_for_status()
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    out = []
    for dc in ET.fromstring(r.content).iter("{http://www.openarchives.org/OAI/2.0/oai_dc/}dc"):
        txt = lambda tag: [e.text or "" for e in dc.findall(f"dc:{tag}", ns)]
        pubs = [p.split(" : ")[-1].strip() for p in txt("publisher")]
        years = [y for y in (re.match(r"\d{4}", d) for d in txt("date")) if y]
        isbns = [m.group(0) for i in txt("identifier")
                 if (m := re.search(r"\d[\d-]{8,}[\dXx]", i))]
        out.append(_book_rec("dnb", (txt("title") or [""])[0], txt("creator"), pubs,
                             [y.group(0) for y in years], isbns))
    return out


@mcp.tool()
async def verify_book(title: str, first_author: str | None = None, year: int | None = None,
                      publisher: str | None = None, isbn: str | None = None) -> dict:
    """Verify a book edition (year, publisher, ISBN) against three catalogues.

    Sources: Open Library (crowd-sourced, partly imported from library and retail feeds),
    Crossref (publisher-deposited, DOI books only) and the Deutsche Nationalbibliothek
    (DNB, German-language market). The catalogues can share upstream publisher metadata,
    so agreement is corroboration, not proof. A record counts as matching when title,
    author and every supplied field (year, publisher, ISBN) agree; missing data in the
    record counts against it. Status: 'verified' (matched by at least
    two sources), 'single_source' (one source only: weaker, say so), 'partial' (the work
    exists but no record matches year/publisher: use 'editions' to correct the citation),
    'not_found'. Edition statements ('2nd ed.') and page counts are not checked.
    """
    names = ["open_library", "crossref", "dnb"]
    c = _http()
    results = await asyncio.gather(
        _book_open_library(c, title, first_author), _book_crossref(c, title, first_author),
        _book_dnb(c, title, first_author), return_exceptions=True)
    wanted_isbn = re.sub(r"[^0-9Xx]", "", isbn).upper() if isbn else None
    wanted_pub = _norm(publisher) if publisher else ""
    nt = _norm(_clean_title(title))
    errors, title_hits, matched = [], [], []
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            errors.append(_err(name, res))
            continue
        for rec in res:
            rt = _norm(_clean_title(rec["title"]))
            contained = len(nt.split()) >= SHORT_TITLE_TOKENS and f" {nt} " in f" {rt} "
            if _title_sim(title, rec["title"])[0] < TITLE_THRESHOLD and not contained:
                continue
            if first_author and not _author_ok(first_author, rec["authors"]):
                continue
            pubs = [p for p in map(_norm, rec["publishers"]) if p]
            rec["year_ok"] = (year in rec["years"]) if year else None
            rec["publisher_ok"] = (any(wanted_pub in p or p in wanted_pub for p in pubs)
                                   if wanted_pub else None)
            rec["isbn_ok"] = (wanted_isbn in rec["isbns"]) if wanted_isbn else None
            title_hits.append(rec)
            if rec["year_ok"] is not False and rec["publisher_ok"] is not False \
                    and rec["isbn_ok"] is not False:
                matched.append(rec)
    confirmed_by = sorted({m["source"] for m in matched})
    status = ("verified" if len(confirmed_by) >= 2 else "single_source" if confirmed_by
              else "partial" if title_hits else "not_found")
    best = None
    if matched:
        best = {"title": matched[0]["title"], "publishers": sorted({p for m in matched for p in m["publishers"]}),
                "years": sorted({y for m in matched for y in m["years"]}),
                "isbns": sorted({i for m in matched for i in m["isbns"]})[:6],
                "doi": next((m["doi"] for m in matched if m["doi"]), None),
                "confirmed_by": confirmed_by}
    others = [{k: r[k] for k in ("source", "title", "publishers", "years", "isbns")}
              for r in title_hits if r not in matched][:6]
    return {"status": status, "best": best, "editions": others, "source_errors": errors}


@mcp.tool()
async def format_apa(doi: str) -> dict:
    """Return the APA-formatted reference for a DOI (Crossref/doi.org content negotiation)."""
    d = _clean_doi(doi)
    if not d:
        return {"status": "invalid_format", "doi": doi}
    try:
        r = await _http().get(f"https://doi.org/{_doi_path(d)}", follow_redirects=True,
                              headers={"Accept": "text/x-bibliography; style=apa"})
    except httpx.HTTPError as e:
        return {"status": "error", "doi": d, "detail": _err("doi.org", e)}
    if r.status_code != 200:
        return {"status": "error", "http_status": r.status_code, "doi": d}
    if not r.headers.get("content-type", "").startswith("text/x-bibliography"):
        # Registration agency without content negotiation: doi.org fell through to the landing page
        return {"status": "unsupported", "doi": d,
                "detail": "no APA rendering available for this DOI; format it manually"}
    return {"status": "ok", "doi": d, "apa": r.text.strip()}


OA_SELECT = ("id,doi,title,publication_year,type,authorships,primary_location,"
             "abstract_inverted_index,keywords,topics,cited_by_count,is_retracted,"
             "open_access,language")


def _oa_abstract(inv: dict | None) -> str | None:
    if not inv:
        return None
    words = {p: w for w, ps in inv.items() for p in ps}
    return " ".join(words[i] for i in sorted(words))


def _oa_record(it: dict) -> dict:
    loc = it.get("primary_location") or {}
    src = loc.get("source") or {}
    return {
        "doi": _clean_doi(it.get("doi")),
        "title": it.get("title"),
        "authors": [a["author"]["display_name"] for a in it.get("authorships", [])
                    if a.get("author", {}).get("display_name")],
        "year": it.get("publication_year"),
        "venue": src.get("display_name"),
        "venue_type": src.get("type"),
        "type": it.get("type"),
        "language": it.get("language"),
        "abstract": _oa_abstract(it.get("abstract_inverted_index")),
        "abstract_source": "openalex" if it.get("abstract_inverted_index") else None,
        "keywords": [k["display_name"] for k in it.get("keywords", [])],
        "topics": [t["display_name"] for t in it.get("topics", [])[:3]],
        "cited_by_count": it.get("cited_by_count"),
        "is_retracted": it.get("is_retracted"),
        "is_open_access": (it.get("open_access") or {}).get("is_oa"),
        "oa_url": (it.get("open_access") or {}).get("oa_url"),
        "openalex_id": it.get("id"),
    }


async def _s2(c: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
    headers = {"x-api-key": S2_KEY} if S2_KEY else {}
    for attempt in range(4):
        r = await c.request(method, url, headers=headers, **kw)
        if r.status_code != 429 or attempt == 3:
            return r
        # Unauthenticated traffic shares one global pool; back off, but never block for long
        try:
            wait = float(r.headers.get("Retry-After", ""))
        except ValueError:
            wait = 2.0 ** attempt
        await asyncio.sleep(min(wait, 10.0))
    return r


async def _fill_abstracts_s2(c: httpx.AsyncClient, records: list[dict]) -> str | None:
    """Fill missing abstracts and TL;DRs from Semantic Scholar in one batch request."""
    todo = [r for r in records if r["doi"]]
    if not todo:
        return None
    resp = await _s2(c, "POST", "https://api.semanticscholar.org/graph/v1/paper/batch",
                     params={"fields": "abstract,tldr"},
                     json={"ids": [f"DOI:{r['doi']}" for r in todo]})
    if resp.status_code != 200:
        return f"semantic_scholar: HTTP {resp.status_code}"
    for rec, hit in zip(todo, resp.json()):
        if not hit:
            continue
        if not rec["abstract"] and hit.get("abstract"):
            rec["abstract"], rec["abstract_source"] = hit["abstract"], "semantic_scholar"
        rec["tldr"] = (hit.get("tldr") or {}).get("text")
    return None


ARXIV_NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom",
            "o": "http://a9.com/-/spec/opensearch/1.1/"}
ARXIV_ID_RE = re.compile(r"^(?:arxiv:)?((?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)$", re.I)


def _arxiv_query(query: str, scope: str, year_from: int | None, year_to: int | None) -> str:
    # arXiv field prefixes bind to one term, so expand every term and map NOT to ANDNOT
    field = ("ti", "abs") if scope == "title_abstract" else ("all",)
    out = []
    for tok in re.findall(r'"[^"]+"|[()]|[^\s()]+', query):
        if tok in ("AND", "OR", "(", ")"):
            out.append(tok)
        elif tok == "NOT":
            out.append("ANDNOT")
        else:
            out.append("(" + " OR ".join(f"{f}:{tok}" for f in field) + ")")
    q = " ".join(out)
    if year_from or year_to:
        lo, hi = f"{year_from or 1991}01010000", f"{year_to or 2100}12312359"
        q = f"({q}) AND submittedDate:[{lo} TO {hi}]"
    return q


def _arxiv_records(xml_bytes: bytes) -> tuple[list[dict], int]:
    root = ET.fromstring(xml_bytes)
    total = int(root.findtext("o:totalResults", "0", ARXIV_NS))
    out = []
    for e in root.findall("a:entry", ARXIV_NS):
        txt = lambda tag, ns="a": " ".join((e.findtext(f"{ns}:{tag}", "", ARXIV_NS) or "").split())
        aid = re.sub(r"v\d+$", "", txt("id").rsplit("/abs/", 1)[-1])
        cats = [c.get("term") for c in e.findall("a:category", ARXIV_NS)]
        pdf = next((l.get("href") for l in e.findall("a:link", ARXIV_NS)
                    if l.get("title") == "pdf"), None)
        out.append({
            "doi": _clean_doi(txt("doi", "x")), "arxiv_id": aid, "title": txt("title"),
            "authors": [" ".join(a.findtext("a:name", "", ARXIV_NS).split())
                        for a in e.findall("a:author", ARXIV_NS)],
            "year": int(txt("published")[:4]) if txt("published") else None,
            "venue": txt("journal_ref", "x") or None, "venue_type": "preprint", "type": "preprint",
            "language": None, "abstract": txt("summary") or None, "abstract_source": "arxiv",
            "keywords": [], "topics": cats, "cited_by_count": None, "is_retracted": None,
            "is_open_access": True, "oa_url": pdf, "openalex_id": None,
            "note": "doi, when present, is the published version; arXiv holds no author keywords",
        })
    return out, total


async def _search_arxiv(query, year_from, year_to, limit, page, scope) -> dict:
    sq = _arxiv_query(query, scope, year_from, year_to)
    r = await _http().get("https://export.arxiv.org/api/query", params={
        "search_query": sq, "start": (page - 1) * limit, "max_results": limit,
        "sortBy": "relevance", "sortOrder": "descending"})
    if r.status_code != 200:
        return {"status": "error", "http_status": r.status_code, "detail": r.text[:300]}
    records, total = _arxiv_records(r.content)
    return {
        "status": "ok",
        "log": {"database": "arXiv", "query": query, "scope": scope, "filters": sq,
                "page": page, "per_page": limit, "total_hits": total, "returned": len(records),
                "with_abstract": sum(1 for x in records if x["abstract"]),
                "executed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "results": records, "source_errors": [],
    }


def _expand(query: str, fmt) -> str:
    """Apply fmt to every search term, keeping Boolean operators and parentheses."""
    return " ".join(t if t in ("AND", "OR", "NOT", "(", ")") else fmt(t)
                    for t in re.findall(r'"[^"]+"|[()]|[^\s()]+', query))


def _plain(query: str) -> str:
    """Strip Boolean syntax for engines that only take free text."""
    return " ".join(t.strip('"') for t in re.findall(r'"[^"]+"|[^\s()]+', query)
                    if t not in ("AND", "OR", "NOT"))


def _strip_html(s: str | None) -> str | None:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split()) or None


def _rec(source: str, **kw) -> dict:
    base = {"doi": None, "arxiv_id": None, "title": None, "authors": [], "year": None,
            "venue": None, "venue_type": None, "type": None, "language": None,
            "abstract": None, "abstract_source": None, "keywords": [], "topics": [],
            "cited_by_count": None, "is_retracted": None, "is_open_access": None,
            "oa_url": None, "openalex_id": None}
    base.update(kw)
    base["abstract_source"] = source if base["abstract"] else None
    return base


def _ok(db: str, query, scope, filters, page, limit, total, records, **extra) -> dict:
    return {"status": "ok",
            "log": {"database": db, "query": query, "scope": scope, "filters": filters,
                    "page": page, "per_page": limit, "total_hits": total,
                    "returned": len(records),
                    "with_abstract": sum(1 for x in records if x["abstract"]),
                    "executed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    **extra},
            "results": records, "source_errors": []}


def _year(v) -> int | None:
    m = re.match(r"\d{4}", str(v or ""))
    return int(m.group(0)) if m else None


async def _get_json(url: str, params: dict) -> tuple[dict | None, dict | None]:
    r = await _http().get(url, params=params)
    if r.status_code != 200:
        return None, {"status": "error", "http_status": r.status_code, "detail": r.text[:300]}
    return r.json(), None


async def _search_europepmc(query, year_from, year_to, limit, page, scope) -> dict:
    # Life sciences, psychology, cognitive science, health; also indexes many preprints (PPR)
    fmt = (lambda t: f"(TITLE:{t} OR ABSTRACT:{t})") if scope == "title_abstract" else (lambda t: t)
    q = _expand(query, fmt)
    if year_from or year_to:
        q = f"({q}) AND PUB_YEAR:[{year_from or 1900} TO {year_to or 2100}]"
    d, err = await _get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search", {
        "query": q, "format": "json", "resultType": "core", "pageSize": limit, "page": page})
    if err:
        return err
    recs = []
    for it in d["resultList"]["result"]:
        recs.append(_rec(
            "europepmc", doi=_clean_doi(it.get("doi")), title=(it.get("title") or "").rstrip("."),
            authors=[a.strip() for a in (it.get("authorString") or "").rstrip(".").split(", ")
                     if a.strip()],
            year=_year(it.get("pubYear")), venue=it.get("journalTitle"),
            venue_type="preprint" if it.get("source") == "PPR" else None,
            type=((it.get("pubTypeList") or {}).get("pubType") or [None])[0],
            abstract=_strip_html(it.get("abstractText")),
            keywords=(it.get("keywordList") or {}).get("keyword", []),
            cited_by_count=it.get("citedByCount"), is_open_access=it.get("isOpenAccess") == "Y"))
    return _ok("Europe PMC", query, scope, q, page, limit, d["hitCount"], recs)


async def _search_doaj(query, year_from, year_to, limit, page, scope) -> dict:
    # Open-access journals across all disciplines, with abstracts and author keywords
    fmt = ((lambda t: f"(bibjson.title:{t} OR bibjson.abstract:{t})") if scope == "title_abstract"
           else (lambda t: t))
    q = _expand(query, fmt)
    if year_from or year_to:
        q = f"({q}) AND bibjson.year:[{year_from or 1900} TO {year_to or 2100}]"
    d, err = await _get_json("https://doaj.org/api/search/articles/" + quote(q, safe=""),
                             {"pageSize": limit, "page": page})
    if err:
        return err
    recs = []
    for it in d["results"]:
        b = it["bibjson"]
        doi = next((i["id"] for i in b.get("identifier", []) if i.get("type") == "doi"), None)
        recs.append(_rec(
            "doaj", doi=_clean_doi(doi), title=b.get("title"),
            authors=[a["name"] for a in b.get("author", []) if a.get("name")],
            year=_year(b.get("year")), venue=(b.get("journal") or {}).get("title"),
            venue_type="journal", type="article", abstract=_strip_html(b.get("abstract")),
            keywords=b.get("keywords", []), topics=[s["term"] for s in b.get("subject", [])][:3],
            is_open_access=True,
            oa_url=next((l["url"] for l in b.get("link", []) if l.get("type") == "fulltext"), None)))
    return _ok("DOAJ", query, scope, q, page, limit, d["total"], recs)


async def _search_hal(query, year_from, year_to, limit, page, scope) -> dict:
    # French national open archive: humanities, social sciences, HCI; many non-English records
    fmt = ((lambda t: f"(title_t:{t} OR abstract_t:{t})") if scope == "title_abstract"
           else (lambda t: t))
    q = _expand(query, fmt)
    params = {"q": q, "wt": "json", "rows": limit, "start": (page - 1) * limit,
              "fl": "title_s,abstract_s,keyword_s,doiId_s,producedDateY_i,authFullName_s,"
                    "docType_s,uri_s,journalTitle_s,language_s,openAccess_bool"}
    if year_from or year_to:
        params["fq"] = f"producedDateY_i:[{year_from or 1900} TO {year_to or 2100}]"
    d, err = await _get_json("https://api.archives-ouvertes.fr/search/", params)
    if err:
        return err
    first = lambda v: (v[0] if isinstance(v, list) and v else v) or None
    recs = [_rec("hal", doi=_clean_doi(it.get("doiId_s")), title=first(it.get("title_s")),
                 authors=it.get("authFullName_s", []), year=it.get("producedDateY_i"),
                 venue=it.get("journalTitle_s"), type=it.get("docType_s"),
                 language=first(it.get("language_s")), abstract=first(it.get("abstract_s")),
                 keywords=it.get("keyword_s", []), is_open_access=it.get("openAccess_bool"),
                 oa_url=it.get("uri_s")) for it in d["response"]["docs"]]
    return _ok("HAL", query, scope, f"{q}" + (f" | fq {params['fq']}" if "fq" in params else ""),
               page, limit, d["response"]["numFound"], recs)


async def _search_zenodo(query, year_from, year_to, limit, page, scope) -> dict:
    # Repository for preprints, working papers, conference papers; mixed quality, no peer review
    fmt = ((lambda t: f"(title:{t} OR description:{t})") if scope == "title_abstract"
           else (lambda t: t))
    q = _expand(query, fmt)
    if year_from or year_to:
        q = (f"({q}) AND publication_date:[{year_from or 1900}-01-01 TO "
             f"{year_to or 2100}-12-31]")
    d, err = await _get_json("https://zenodo.org/api/records", {
        "q": q, "size": limit, "page": page, "type": "publication", "sort": "bestmatch"})
    if err:
        return err
    recs = []
    for it in d["hits"]["hits"]:
        m = it["metadata"]
        recs.append(_rec(
            "zenodo", doi=_clean_doi(it.get("doi") or m.get("doi")), title=m.get("title"),
            authors=[c["name"] for c in m.get("creators", []) if c.get("name")],
            year=_year(m.get("publication_date")),
            venue=(m.get("journal") or {}).get("title") or m.get("conference_title"),
            venue_type="repository", type=(m.get("resource_type") or {}).get("subtype"),
            abstract=_strip_html(m.get("description")), keywords=m.get("keywords", []),
            is_open_access=m.get("access_right") == "open", oa_url=(it.get("links") or {}).get("html")))
    return _ok("Zenodo", query, scope, q, page, limit, d["hits"]["total"], recs,
               note="records are self-deposited and not peer-reviewed by Zenodo")


async def _search_osf(query, year_from, year_to, limit, page, scope) -> dict:
    # OSF preprint servers (PsyArXiv, SocArXiv, MetaArXiv, ...); the API only does a title
    # substring match, so the first term or phrase of the query is used
    phrase = re.search(r'"([^"]+)"', query)
    term = phrase.group(1) if phrase else (_plain(query).split(" ") or [""])[0]
    params = {"filter[title]": term, "page[size]": min(limit, 100), "page": page}
    if year_from:
        params["filter[date_published][gte]"] = f"{year_from}-01-01"
    if year_to:
        params["filter[date_published][lte]"] = f"{year_to}-12-31"
    d, err = await _get_json("https://api.osf.io/v2/preprints/", params)
    if err:
        return err
    recs = []
    for it in d["data"]:
        a = it["attributes"]
        prov = ((it.get("relationships") or {}).get("provider") or {}).get("data", {}).get("id")
        recs.append(_rec(
            "osf", doi=_clean_doi(a.get("doi")), title=a.get("title"), year=_year(a.get("date_published")),
            venue=prov, venue_type="preprint", type="preprint", abstract=a.get("description"),
            keywords=a.get("tags", []),
            topics=[s["text"] for chain in a.get("subjects", []) for s in chain][:3],
            is_open_access=True, oa_url=(it.get("links") or {}).get("html")))
    return _ok("OSF Preprints", query, "title_only", params["filter[title]"], page, limit,
               (d.get("links", {}).get("meta") or {}).get("total"), recs,
               note="title substring match on the first term/phrase only; Boolean syntax "
                    "ignored; authors not retrieved: screen results against the full query")


async def _search_s2(query, year_from, year_to, limit, page, scope) -> dict:
    # Broad index with TL;DRs; free-text relevance search without Boolean operators
    params = {"query": _plain(query), "limit": min(limit, 100), "offset": (page - 1) * limit,
              "fields": "title,abstract,year,venue,externalIds,citationCount,authors,"
                        "fieldsOfStudy,tldr,openAccessPdf,publicationTypes"}
    if year_from or year_to:
        params["year"] = f"{year_from or ''}-{year_to or ''}"
    r = await _s2(_http(), "GET", "https://api.semanticscholar.org/graph/v1/paper/search",
                  params=params)
    if r.status_code != 200:
        return {"status": "error", "http_status": r.status_code, "detail": r.text[:200]}
    d = r.json()
    recs = []
    for it in d.get("data", []):
        ids = it.get("externalIds") or {}
        rec = _rec("semantic_scholar", doi=_clean_doi(ids.get("DOI")), arxiv_id=ids.get("ArXiv"),
                   title=it.get("title"), authors=[a["name"] for a in it.get("authors", []) if a.get("name")],
                   year=it.get("year"), venue=it.get("venue") or None,
                   type=(it.get("publicationTypes") or [None])[0], abstract=it.get("abstract"),
                   topics=it.get("fieldsOfStudy") or [], cited_by_count=it.get("citationCount"),
                   oa_url=(it.get("openAccessPdf") or {}).get("url") or None)
        rec["tldr"] = (it.get("tldr") or {}).get("text")
        recs.append(rec)
    return _ok("Semantic Scholar", query, "relevance", params["query"], page, limit,
               d.get("total"), recs, note="no Boolean syntax; no author keywords")


DATABASES = ["openalex", "europepmc", "semanticscholar", "hal", "doaj", "arxiv", "osf", "zenodo"]


def _surname_set(authors: list[str]) -> set[str]:
    """Surnames from heterogeneous formats: 'Mark C. Fox', 'Fox, M.', 'Fox MC'."""
    out = set()
    for a in authors or []:
        a = unicodedata.normalize("NFKD", a).encode("ascii", "ignore").decode()
        if "," in a:
            toks = _norm(a.split(",")[0]).split()
        else:
            toks = re.findall(r"[A-Za-z'\-]+", a)
            while len(toks) > 1 and toks[-1].isupper() and len(toks[-1]) <= 3:
                toks.pop()
            toks = _norm(" ".join(toks[-1:])).split()
        if toks:
            out.add(toks[-1])
    return out


def _same_work(a: dict, b: dict) -> str | None:
    """Decide whether two records with the same title are one work; return the basis or None."""
    pre = lambda r: (r.get("venue_type") in ("preprint", "repository") or r.get("type") == "preprint"
                     or any(d in ("arxiv", "osf", "zenodo") for d in r.get("found_in", [])))
    # a preprint can predate its published version by years; two articles rarely differ by more than 1
    tol = 3 if pre(a) or pre(b) else 1
    if a.get("year") and b.get("year") and abs(int(a["year"]) - int(b["year"])) > tol:
        return None
    sa, sb = _surname_set(a.get("authors")), _surname_set(b.get("authors"))
    if sa and sb:
        return "title+authors+year" if len(sa & sb) >= min(2, len(sa), len(sb)) else None
    return "title+year (authors unavailable)"
_ADAPTERS = {"europepmc": _search_europepmc, "doaj": _search_doaj, "hal": _search_hal,
             "zenodo": _search_zenodo, "osf": _search_osf, "semanticscholar": _search_s2,
             "arxiv": _search_arxiv}


async def _search_openalex(query, year_from, year_to, doc_types, limit, page, scope,
                           open_access_only, exclude_retracted, fill_missing_abstracts) -> dict:
    filters = []
    if year_from and year_to:
        filters.append(f"publication_year:{year_from}-{year_to}")
    elif year_from:
        filters.append(f"publication_year:>{year_from - 1}")
    elif year_to:
        filters.append(f"publication_year:<{year_to + 1}")
    if doc_types:
        filters.append("type:" + "|".join(doc_types))
    if open_access_only:
        filters.append("is_oa:true")
    if exclude_retracted:
        filters.append("is_retracted:false")
    params: dict[str, Any] = {"per-page": limit, "page": page, "select": OA_SELECT}
    if scope == "title_abstract":
        # Commas separate OpenAlex filters, so a comma inside the query breaks the request
        filters.append(f"title_and_abstract.search:{query.replace(',', ' ')}")
    else:
        params["search"] = query
    params["filter"] = ",".join(filters)
    if EMAIL:
        params["mailto"] = EMAIL
    c = _http()
    r = await c.get("https://api.openalex.org/works", params=params)
    if r.status_code == 400:
        return {"status": "invalid_query", "detail": r.text[:300]}
    r.raise_for_status()
    data = r.json()
    records = [_oa_record(it) for it in data["results"]]
    error = await _fill_abstracts_s2(c, records) if fill_missing_abstracts else None
    return {
        "status": "ok",
        "log": {"database": "OpenAlex", "query": query, "scope": scope,
                "filters": params["filter"], "page": page, "per_page": limit,
                "total_hits": data["meta"]["count"], "returned": len(records),
                "with_abstract": sum(1 for x in records if x["abstract"]),
                "executed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "results": records, "source_errors": [error] if error else [],
    }


@mcp.tool()
async def search_literature(query: str, year_from: int | None = None, year_to: int | None = None,
                            doc_types: list[str] | None = None, limit: int = 25, page: int = 1,
                            scope: str = "title_abstract", open_access_only: bool = False,
                            exclude_retracted: bool = True, fill_missing_abstracts: bool = True,
                            database: str = "openalex") -> dict:
    """Search the literature and return records with abstracts and keywords.

    database: one name, a comma-separated list, or 'all'. Available:
    openalex (default; journals, books, chapters, many preprints; best abstracts and
    keywords), europepmc (life sciences, psychology, health, preprints), semanticscholar
    (broad, TL;DRs; free-text only; rate-limited without API key), hal (French open
    archive; humanities, social sciences, HCI), doaj (open-access journals, all fields),
    arxiv (physics, maths, computer science incl. HCI and AI; no author keywords),
    osf (PsyArXiv, SocArXiv and other OSF preprint servers; title-only search),
    zenodo (self-deposited papers, not peer-reviewed). Philosophy is thinly covered by
    all of these; PhilPapers has no accessible API.
    With several databases, results are merged: records with the same DOI or arXiv id are
    one work; records with the same normalized title are merged only if first-author
    surnames overlap (min(2, n) shared) and years are within 1 (3 if either is a preprint),
    or if one side has no authors ('dedup_basis' says which rule applied, and
    'log.dedup_summary' counts merges and same-title records kept separate). A preprint and
    its published version with different DOIs thus collapse into one record, while generic
    titles by different authors stay apart. 'found_in' lists the databases and
    'log.databases' holds one log per database. A failing database is reported in
    source_errors and does not abort the rest.

    query supports Boolean syntax (AND, OR, NOT in capitals, "exact phrases", parentheses)
    except where noted. scope: 'title_abstract' (default, reproducible) or 'fulltext'
    (OpenAlex: indexed full text; others: all searchable fields). doc_types, open_access_only,
    exclude_retracted and fill_missing_abstracts apply to OpenAlex only; doc_types uses
    OpenAlex types ('article', 'review', 'book', 'book-chapter', 'preprint', 'dissertation');
    conference papers are typed 'article' with venue_type 'conference'. Missing OpenAlex
    abstracts are filled from Semantic Scholar when it holds them. Other databases do not
    apply the OpenAlex-only filters: their log lists them under 'ignored_filters', and their
    results may include retracted or off-type records. The 'log' records query,
    filters, source, date and hit counts for reporting; this tool only retrieves, it does
    not judge relevance. Per-database hit counts cannot be added up as unique papers, and
    deduplication covers only the current page.
    """
    limit = max(1, min(limit, 50))
    names = DATABASES if database.strip() == "all" else [
        d.strip().lower() for d in database.split(",") if d.strip()]
    bad = [n for n in names if n not in DATABASES]
    if bad or not names:
        return {"status": "invalid_database", "detail": f"unknown {bad}; use {DATABASES} or 'all'"}

    def run(n):
        if n == "openalex":
            return _search_openalex(query, year_from, year_to, doc_types, limit, page, scope,
                                    open_access_only, exclude_retracted, fill_missing_abstracts)
        return _ADAPTERS[n](query, year_from, year_to, limit, page, scope)

    outs = await asyncio.gather(*(run(n) for n in names), return_exceptions=True)
    openalex_only = [f for f, on in (("doc_types", doc_types), ("open_access_only", open_access_only),
                                     ("exclude_retracted", exclude_retracted)) if on]
    for n, r in zip(names, outs):
        if n != "openalex" and openalex_only and isinstance(r, dict) and r.get("status") == "ok":
            r["log"]["ignored_filters"] = openalex_only
    if len(names) == 1:
        r = outs[0]
        if isinstance(r, Exception):
            return {"status": "error", "detail": _err(names[0], r)}
        for rec in r.get("results", []):
            rec["found_in"] = names
        return r

    merged, by_key, by_title, logs, errors, before = [], {}, {}, [], [], 0
    stats: dict[str, int] = {}
    for n, r in zip(names, outs):
        if isinstance(r, Exception) or r.get("status") != "ok":
            detail = _err(n, r) if isinstance(r, Exception) else f"{n}: " + str(
                r.get("detail") or r.get("http_status") or r.get("status"))
            errors.append(detail[:250])
            logs.append({"database": n, "status": "error"})
            continue
        logs.append(r["log"])
        errors += r.get("source_errors", [])
        for rec in r["results"]:
            before += 1
            rec["found_in"] = [n]
            strong = [(rec["doi"], "doi") if rec.get("doi") else None,
                      (f"arxiv:{rec['arxiv_id']}", "arxiv_id") if rec.get("arxiv_id") else None]
            strong = [s for s in strong if s]
            hit, basis = next(((by_key[k], b) for k, b in strong if k in by_key), (None, None))
            tk = _norm(rec["title"])[:90] if rec.get("title") else None
            if not hit and tk:
                for cand in by_title.get(tk, []):
                    basis = _same_work(cand, rec)
                    if basis:
                        hit = cand
                        break
                else:
                    if tk in by_title:
                        stats["same_title_kept_separate"] = stats.get("same_title_kept_separate", 0) + 1
            if hit:
                stats[basis] = stats.get(basis, 0) + 1
                if n not in hit["found_in"]:
                    hit["found_in"].append(n)
                if basis not in hit.setdefault("dedup_basis", []):
                    hit["dedup_basis"].append(basis)
                for f in ("abstract", "doi", "arxiv_id", "oa_url", "tldr", "cited_by_count",
                          "abstract_source"):
                    if not hit.get(f) and rec.get(f):
                        hit[f] = rec[f]
                hit["keywords"] += [k for k in rec["keywords"] if k not in hit["keywords"]]
            else:
                hit = rec
                merged.append(rec)
                if tk:
                    by_title.setdefault(tk, []).append(rec)
            for k, _ in strong:
                by_key.setdefault(k, hit)
    return {
        "status": "ok",
        "log": {"query": query, "scope": scope, "page": page, "per_page": limit,
                "databases": logs, "records_before_dedup": before,
                "records_after_dedup": len(merged), "dedup_scope": "this page only",
                "dedup_summary": stats,
                "with_abstract": sum(1 for x in merged if x["abstract"]),
                "executed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "results": merged, "source_errors": errors,
    }


@mcp.tool()
async def get_metadata(identifier: str) -> dict:
    """Return abstract, keywords, topics, citation counts and open-access data for a work.

    identifier: a DOI or an arXiv identifier ('2401.01234', 'arXiv:2401.01234v2'). Combines
    OpenAlex (abstract, author keywords, topics, retraction flag) with Semantic Scholar
    (TL;DR, fields of study, influential citations, open-access PDF); arXiv identifiers
    are resolved on arXiv and return its record. Fields that a source does not hold are
    null; abstract_source tells which source supplied it.
    """
    c = _http()
    m = ARXIV_ID_RE.match(identifier.strip())
    if m:
        r = await c.get("https://export.arxiv.org/api/query", params={"id_list": m.group(1)})
        if r.status_code != 200:
            return {"status": "error", "http_status": r.status_code}
        recs, _ = _arxiv_records(r.content)
        recs = [x for x in recs if x["title"] and x["title"] != "Error"]
        return {"status": "ok", **recs[0], "source_errors": []} if recs else {
            "status": "not_found", "arxiv_id": m.group(1)}
    d = _clean_doi(identifier)
    if not d:
        return {"status": "invalid_format", "identifier": identifier}
    params = {"select": OA_SELECT}
    if EMAIL:
        params["mailto"] = EMAIL
    oa, s2 = await asyncio.gather(
        c.get(f"https://api.openalex.org/works/doi:{_doi_path(d)}", params=params),
        _s2(c, "GET", f"https://api.semanticscholar.org/graph/v1/paper/DOI:{_doi_path(d)}",
            params={"fields": "abstract,tldr,fieldsOfStudy,citationCount,"
                              "influentialCitationCount,openAccessPdf"}),
        return_exceptions=True)
    errors, out = [], {"status": "not_found", "doi": d}
    if not isinstance(oa, Exception) and oa.status_code == 200:
        out = {"status": "ok", **_oa_record(oa.json())}
    elif isinstance(oa, Exception) or oa.status_code != 404:
        errors.append(_err("openalex", oa) if isinstance(oa, Exception)
                      else f"openalex: HTTP {oa.status_code}")
    if not isinstance(s2, Exception) and s2.status_code == 200:
        h = s2.json()
        out.setdefault("doi", d)
        out["status"] = "ok"
        if not out.get("abstract") and h.get("abstract"):
            out["abstract"], out["abstract_source"] = h["abstract"], "semantic_scholar"
        out["tldr"] = (h.get("tldr") or {}).get("text")
        out["fields_of_study"] = h.get("fieldsOfStudy")
        out["s2_citation_count"] = h.get("citationCount")
        out["s2_influential_citation_count"] = h.get("influentialCitationCount")
        out["s2_open_access_pdf"] = (h.get("openAccessPdf") or {}).get("url") or None
    elif isinstance(s2, Exception) or s2.status_code != 404:
        errors.append(_err("semantic_scholar", s2) if isinstance(s2, Exception)
                      else f"semantic_scholar: HTTP {s2.status_code}")
    out["source_errors"] = errors
    return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

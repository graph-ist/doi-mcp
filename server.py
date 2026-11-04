"""MCP server: verify citations (DOIs, book editions), search the literature, map citations.

Sources: Crossref, OpenAlex, Semantic Scholar, Open Library, Google Books, DNB, ORCID,
doi.org and open repositories (Europe PMC, HAL, DOAJ, arXiv, OSF, Zenodo). See README.md.
Env (optional):
  CONTACT_EMAIL            used for Crossref/OpenAlex polite pool
  GOOGLE_BOOKS_API_KEY     sent as x-goog-api-key; raises the Google Books quota
  ORCID_CLIENT_ID          with ORCID_CLIENT_SECRET, gets a /read-public token for the ORCID API
  ORCID_CLIENT_SECRET      never sent to anything but orcid.org/oauth/token
    OPENALEX_API_KEY         authenticates OpenAlex requests via api_key
    OPENALEX_ENABLE_WRITES   enables confirmed OpenAlex account writes; default false
    SEMANTIC_SCHOLAR_API_KEY authenticates requests; 1 request/s across all endpoints
  MCP_TRANSPORT            'stdio' (default) or 'http' (streamable HTTP on 127.0.0.1)
  PORT                     HTTP port, default 3002
  MCP_AUTH_TOKEN           required in http mode, as Bearer header or ?token= query
  MCP_ALLOWED_HOSTS        comma-separated public host names allowed behind a proxy
"""

import asyncio
import functools
import hmac
import html
import json
import sys
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from matching import (  # one implementation of "is this record the cited work?", shared with the Zotero MCP
    TITLE_THRESHOLD, SHORT_TITLE_TOKENS, NON_WORK_TYPES, DERIVATIVE_RE, RETRACTED_PREFIX_RE, NAME_PARTICLES,
    REF_STOP as _REF_STOP, REF_YEAR as _REF_YEAR,
    strip_html as _strip_html, norm as _norm, clean_title as _clean_title, title_sim as _title_sim,
    name_tokens as _name_tokens, surname as _surname, surname_query as _surname_query, author_ok as _author_ok,
    assess as _assess, eligible as _eligible, is_derivative as _is_derivative,
    ascii_fold as _ascii, title_words as _title_words, parse_ref as _parse_ref, ref_key as _ref_key,
    title_match as _title_match)
BOOK_TYPES = {"book", "monograph", "edited-book", "reference-book", "book-set"}
RETRACTION_TYPES = {"retraction", "removal", "withdrawal"}
from typing import Annotated, Any, Literal, get_args
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

VERSION = "0.8.1"
_PUBLIC_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
MAPS_PAGE = "https://claude.ai/artifact/34ALXDYU74t27u6qmYnfUT"
INSTRUCTIONS = (
    "Citation verification: find_doi then verify_doi (verify_book for editions); format_apa "
    "or format_reference only after verification; check_citations cross-checks in-text "
    "citations with a reference list offline. Literature search: search_literature; "
    "paper_table for details on a list of ids. Persistent literature maps (map_* tools) are "
    "viewed by the user on the 'Literature maps' page, the only map page: " + MAPS_PAGE +
    " . Give that link whenever a map is created, expanded or asked for. Raw API passthroughs "
    "(openalex_*, semantic_scholar*, orcid, google_books) return native JSON: keep per_page "
    "and limit small and use select/fields where the API offers them. search_everything looks "
    "through the user's own notes, PDF texts and highlights (not the web); check_draft checks a "
    "manuscript's [@citekey] citations against the Zotero library; coverage_report says what a "
    "project still lacks (notes, PDFs, verification).")
mcp = FastMCP("paper-doi", instructions=INSTRUCTIONS, stateless_http=True, transport_security=TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *_PUBLIC_HOSTS],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                     *(f"https://{h}" for h in _PUBLIC_HOSTS)]))

# The gateway layer (raw passthroughs to OpenAlex, Semantic Scholar, Google Books, ORCID)
# is registered only when ENABLE_GATEWAY=true: it adds many tool definitions to every
# session and returns native JSON without the checks the verification tools apply.
GATEWAY_ENABLED = os.environ.get("ENABLE_GATEWAY", "").strip().lower() == "true"
GATEWAY_TOOLS: list[str] = []


def _tool(fn=None, *, gateway: bool = False, **kw):
    """Register an MCP tool. Every tool answers with a dict carrying 'status': a ValueError
    raised by input validation becomes {'status': 'invalid_input', 'detail': ...} instead of
    a protocol error. The undecorated coroutine is returned, so direct calls keep raising."""
    def deco(f):
        @functools.wraps(f)
        async def wrapped(*args, **kwargs):
            try:
                return await f(*args, **kwargs)
            except ValueError as e:
                return {"status": "invalid_input", "detail": str(e)}
        if gateway:
            GATEWAY_TOOLS.append(f.__name__)
        if not gateway or GATEWAY_ENABLED:
            mcp.tool(**kw)(wrapped)
        return f
    return deco(fn) if fn is not None else deco

EMAIL = os.environ.get("CONTACT_EMAIL", "")
OA_KEY = os.environ.get("OPENALEX_API_KEY", "")
OA_WRITES_ENABLED = os.environ.get("OPENALEX_ENABLE_WRITES", "").strip().lower() == "true"
S2_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
GB_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")
ORCID_CLIENT_ID = os.environ.get("ORCID_CLIENT_ID", "")
ORCID_CLIENT_SECRET = os.environ.get("ORCID_CLIENT_SECRET", "")
IA_ACCESS = os.environ.get("IA_ACCESS_KEY", "")
IA_SECRET = os.environ.get("IA_SECRET_KEY", "")
UA = f"paper-doi-mcp/{VERSION}" + (f" (mailto:{EMAIL})" if EMAIL else "")
DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)
# Indexes that largely derive their records from Crossref count as one source of evidence
SOURCE_GROUPS = {"openalex": "crossref", "semantic_scholar": "crossref", "semanticscholar": "crossref",
                 # Both are fed largely by the same publisher and retail feeds
                 "open_library": "book_feeds", "google_books": "book_feeds"}


def _independent(sources: list[str]) -> int:
    return len({SOURCE_GROUPS.get(x, x) for x in sources})


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


def _clean_orcid(value: str | None) -> str | None:
    """Canonical ORCID iD from a bare iD or orcid.org URL; None if malformed or checksum fails."""
    s = re.sub(r"^https?://(?:www\.)?orcid\.org/", "", (value or "").strip(), flags=re.I).upper()
    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", s):
        return None
    total = 0
    for ch in s.replace("-", "")[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return s if s[-1] == ("X" if check == 10 else str(check)) else None


def _oa_redact(detail: str) -> str:
    if OA_KEY:
        for secret in sorted({OA_KEY, quote(OA_KEY, safe=""), quote_plus(OA_KEY)},
                             key=len, reverse=True):
            detail = detail.replace(secret, "[redacted]")
    return detail


def _err(name: str, e: BaseException) -> str:
    detail = _oa_redact(str(e))
    return f"{name}: {type(e).__name__}" + (f": {detail}" if detail else "")


class _PoliteTransport(httpx.AsyncHTTPTransport):
    """Caps concurrency per host; intervals cover all endpoints, including retries."""
    # Crossref: polite pool allows a few concurrent requests; beyond that it answers 429
    CONCURRENCY = {"api.semanticscholar.org": 1, "export.arxiv.org": 1,
                   "api.openalex.org": 1, "user.openalex.org": 1, "api.crossref.org": 2, "archive.org": 2,
                   "ops.epo.org": 1}
    MIN_INTERVAL = {"api.semanticscholar.org": 1.0, "export.arxiv.org": 3.0,
                    "api.openalex.org": 1.0, "user.openalex.org": 1.0, "api.crossref.org": 0.12,
                    "ops.epo.org": 1.0}

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
    for attempt in range(4):
        r = await _http().get(f"https://api.crossref.org/works/{_doi_path(d)}",
                              params={"mailto": EMAIL} if EMAIL else {})
        if r.status_code != 429 or attempt == 3:
            break
        await asyncio.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 10))
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


async def _oa_request(c: httpx.AsyncClient, path: str, params: dict,
                      method: str = "GET", body: Any = None,
                      user_api: bool = False) -> httpx.Response:
    params = dict(params)
    if EMAIL and not user_api:
        params["mailto"] = EMAIL
    if OA_KEY and not user_api:
        params["api_key"] = OA_KEY
    headers = {"Authorization": f"Bearer {OA_KEY}"} if user_api and OA_KEY else {}
    host = "user.openalex.org" if user_api else "api.openalex.org"
    retryable = method == "GET" or (method == "POST" and path in {
        "", "query", "text", "text/topics", "text/keywords"})
    for attempt in range(4):
        response = await c.request(method, f"https://{host}/{path}", params=params,
                                   headers=headers, follow_redirects=False,
                                   **({"json": body} if body is not None else {}))
        response.request.url = response.request.url.copy_remove_param("api_key")
        response.request.headers.pop("Authorization", None)
        if response.status_code != 429 or not retryable or attempt == 3:
            return response
        try:
            wait = max(0.0, float(response.headers.get("Retry-After", "")))
        except ValueError:
            wait = 2.0 ** attempt
        if wait > 60:  # daily budget used up (Retry-After = hours until reset): retrying is useless
            return response
        await response.aclose()
        await asyncio.sleep(min(wait, 10.0))
    return response


def _oa_public_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[redacted]" if key.lower() in {"api_key", "access_token", "authorization"}
                else _oa_public_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_oa_public_data(item) for item in value]
    return _oa_redact(value) if isinstance(value, str) else value


async def _oa_api(path: str, params: dict | None = None, method: str = "GET",
                  body: Any = None, user_api: bool = False) -> dict:
    if "api_key" in (params or {}):
        raise ValueError("Configure OPENALEX_API_KEY in the server environment, not params")
    try:
        response = await _oa_request(_http(), path, params or {}, method, body, user_api)
    except httpx.HTTPError as error:
        return {"status": "error", "source": "openalex", "detail": _err("openalex", error)}
    if response.status_code == 204:
        return {"status": "ok", "http_status": 204, "data": None}
    content_type = response.headers.get("content-type", "").split(";")[0]
    if content_type in {"text/csv", "text/plain", "text/markdown"} and response.is_success:
        return {"status": "ok", "http_status": response.status_code,
                "content_type": content_type, "data": _oa_redact(response.text),
                "next_cursor": response.headers.get("x-next-cursor")}
    try:
        data = _oa_public_data(response.json())
    except ValueError:
        return {"status": "error", "http_status": response.status_code,
                "detail": "OpenAlex returned a non-JSON response"}
    return {"status": "ok" if response.is_success else "error",
            "http_status": response.status_code, "data": data}


_OA_AUTOCOMPLETE = ("works", "authors", "sources", "institutions", "topics", "keywords",
                    "concepts", "publishers", "funders")
OpenAlexEntity = Literal[
    "works", "authors", "sources", "institutions", "publishers", "funders", "awards",
    "locations", "raw-affiliation-strings", "topics", "subfields", "fields", "domains",
    "keywords", "sdgs", "study-designs", "concepts", "work-types", "source-types",
    "institution-types", "countries", "continents", "languages", "licenses", "indexes",
    "source-lists",
]
_OA_ENTITIES = set(get_args(OpenAlexEntity))
_OA_LIST_ONLY = {"raw-affiliation-strings"}


def _oa_segment(value: str) -> str:
    if not value.strip() or value.strip() in {".", ".."}:
        raise ValueError("Provide a non-empty identifier")
    return quote(value.strip(), safe="")


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=True))
async def openalex_entity(entity: OpenAlexEntity, entity_id: str | None = None,
                          autocomplete: str | None = None,
                          params: dict[str, str | int | float | bool] | None = None) -> dict:
    """Read OpenAlex entity lists or a single entity, with native query parameters.

    Covers works, authors, sources, institutions, publishers, funders, awards,
    locations, raw-affiliation-strings, topics, subfields, fields, domains, keywords,
    sdgs, study-designs, concepts (deprecated), and vocabulary endpoints.
    Omit entity_id for a list; provide an OpenAlex ID or supported external ID for
    details (DOI/PMID for works, ORCID for authors, ROR for institutions, ISSN for
    sources). autocomplete=<text> instead queries GET /autocomplete/{entity} (typeahead
    matches and identifier hints; works, authors, sources, institutions, topics, keywords,
    concepts, publishers, funders only). Identifiers are URL-encoded, never a destination URL.
    raw-affiliation-strings has no single-entity identifier; use its list filters.
    Lists default to per_page=25 and are long: pass select (e.g. 'id,title,publication_year')
    to keep the response readable.

    params forwards native filter, search, sort, group_by, select, page, per_page,
    cursor, sample, seed, corpus and other API-supported options without rewriting.
    Endpoint-specific validity is checked by OpenAlex. For deep pagination start
    with cursor='*', then pass data.meta.next_cursor. Each call fetches one page.
    Response data preserves metadata, groups, nested fields and request cost; API
    credentials are redacted. Uses OPENALEX_API_KEY and CONTACT_EMAIL automatically.
    Requests can consume your OpenAlex budget; no bulk auto-pagination or downloads.
    """
    if entity not in _OA_ENTITIES:
        raise ValueError(f"Unknown entity '{entity}'; use one of {', '.join(_OA_ENTITIES)}")
    path = entity
    if autocomplete is not None:
        if entity_id is not None:
            raise ValueError("autocomplete and entity_id are alternatives; pass one")
        if entity not in _OA_AUTOCOMPLETE:
            raise ValueError(f"autocomplete supports only {', '.join(_OA_AUTOCOMPLETE)}")
        if not autocomplete.strip() or "q" in (params or {}):
            raise ValueError("autocomplete must be non-empty text; do not pass params.q")
        return await _oa_api(f"autocomplete/{entity}", {**(params or {}), "q": autocomplete})
    if entity_id is not None:
        if entity in _OA_LIST_ONLY:
            raise ValueError(f"{entity} only supports list queries")
        path += "/" + _oa_segment(entity_id)
    elif entity not in _OA_LIST_ONLY:
        params = {"per_page": 25, **(params or {})}
    return await _oa_api(path, params)


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=True))
async def openalex_text(title: str, abstract: str = "",
                        target: Literal["all", "topics", "keywords"] = "all",
                        method: Literal["GET", "POST"] = "POST", version: int = 2) -> dict:
    """Tag text via /text, /text/topics or /text/keywords using GET or POST.

    title and abstract combined must be 20-2000 characters. The text is sent to
    OpenAlex, so obtain permission before sending confidential unpublished material.
    Costs $0.01/request per current API documentation; limit 1 request/s.
    version=2 uses the current classifier; version=1 is available until 2027-01-13.
    Returns original topics, keywords, scores and hierarchy without normalizing.
    """
    if not 20 <= len(title) + len(abstract) <= 2000 or not title.strip():
        raise ValueError("Provide a title and 20-2000 characters of combined title/abstract")
    if target not in {"all", "topics", "keywords"} or method not in {"GET", "POST"}:
        raise ValueError("Invalid text target or method")
    if version not in {1, 2}:
        raise ValueError("version must be 1 or 2")
    path = "text" + (f"/{target}" if target != "all" else "")
    content = {"title": title, "abstract": abstract}
    if method == "GET":
        return await _oa_api(path, {**content, "version": version})
    return await _oa_api(path, {"version": version}, "POST", content)


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=True))
async def openalex_query(
    query: str | dict, query_format: Literal["oql", "oqo"] = "oql",
    method: Literal["GET", "POST"] = "POST",
    params: dict[str, str | int | bool] | None = None,
) -> dict:
    """Execute an OpenAlex OQL query (string) or OQO query (JSON object); one page per call.

    Consumes the account's query budget. To translate, validate or inspect the language
    without executing, use openalex_query_inspect; for classic REST filters use
    openalex_entity. params holds view options (sort, select, page, per_page, cursor,
    rerank, format); not the query itself. Returns meta.next_cursor, diagnostics, costs.
    """
    params = _oa_query_params(params)
    _oa_query_check(query, query_format)
    if method == "POST":
        return await _oa_api("", params, "POST", {query_format: query})
    encoded = json.dumps(query) if isinstance(query, dict) else query
    return await _oa_api("", {**params, query_format: encoded})


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=True))
async def openalex_query_inspect(
    action: Literal["translate", "validate", "context", "properties", "spec"],
    query: str | dict | None = None, query_format: Literal["oql", "oqo", "oxurl"] = "oql",
    entity: OpenAlexEntity | None = None, artifact: str | None = None,
    params: dict[str, str | int | bool] | None = None,
) -> dict:
    """Inspect the OpenAlex query language without executing a search (free).

    translate: query in oql, oqo or oxurl (a classic REST URL) to the other forms, with
    validation. validate: an oql string. context: parse context of an oql string at
    params.pos. properties: the queryable columns, for all entities or for entity.
    spec: the language specification, or one artifact of it (e.g. 'oqo').
    To run a query use openalex_query.
    """
    params = _oa_query_params(params)
    if entity is not None and action != "properties":
        raise ValueError("entity is only accepted by action='properties'")
    if artifact is not None and action != "spec":
        raise ValueError("artifact is only accepted by action='spec'")
    if action in {"properties", "spec"}:
        if query is not None:
            raise ValueError(f"action='{action}' takes no query")
        path = "properties" if action == "properties" else "query/spec"
        suffix = entity if action == "properties" else artifact
        return await _oa_api(path + ("/" + _oa_segment(suffix) if suffix else ""), params)
    if query is None or (isinstance(query, str) and not query.strip()):
        raise ValueError(f"action='{action}' requires query")
    if action in {"validate", "context"}:
        if query_format != "oql" or not isinstance(query, str):
            raise ValueError(f"action='{action}' requires an OQL string (query_format='oql')")
        if action == "context" and "pos" not in params:
            raise ValueError("action='context' requires params.pos (cursor position)")
        return await _oa_api("validate" if action == "validate" else "parse-context",
                             {**params, "q": query})
    _oa_query_check(query, query_format)
    return await _oa_api("query", params, "POST", {query_format: query})


def _oa_query_params(params: dict | None) -> dict:
    params = dict(params or {})
    if bad := {"oql", "oqo", "oxurl", "q"} & params.keys():
        raise ValueError(f"Pass the query in the query argument, not params.{bad.pop()}")
    return params


def _oa_query_check(query: Any, query_format: str) -> None:
    if query_format == "oqo" and not isinstance(query, dict):
        raise ValueError("query_format='oqo' requires query to be a JSON object")
    if query_format != "oqo" and not isinstance(query, str):
        raise ValueError(f"query_format='{query_format}' requires query to be a string")
    if isinstance(query, str) and not query.strip():
        raise ValueError("query must not be empty")


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=True))
async def openalex_status(action: Literal["rate_limit", "changefiles"] = "rate_limit",
                          date: str | None = None) -> dict:
    """Read /rate-limit (credentials redacted) or /changefiles[/{date}].

    changefiles lists available dates or returns metadata/download links for YYYY-MM-DD;
    requires a paid OpenAlex plan. Does not download any files. date is only valid
    for changefiles. Uses the configured key and the shared 1 request/s limiter.
    """
    if action == "rate_limit" and date is None:
        return await _oa_api("rate-limit")
    if action != "changefiles":
        raise ValueError("date is only supported for changefiles")
    if date is not None:
        datetime.strptime(date, "%Y-%m-%d")
    return await _oa_api("changefiles" + ("/" + _oa_segment(date) if date else ""))


_OA_ACCOUNT_READ_ROUTES = {
    "collections": "collections", "collection": "collections/{collection_id}",
    "members": "collections/{collection_id}/members",
    "imports": "collections/{collection_id}/imports",
    "import": "collections/{collection_id}/imports/{import_id}",
    "saved_searches": "saved-searches", "saved_search": "saved-searches/{saved_search_id}",
}
_OA_ACCOUNT_WRITE_ROUTES = {
    "create_collection": ("POST", "collections"),
    "update_collection": ("PATCH", "collections/{collection_id}"),
    "delete_collection": ("DELETE", "collections/{collection_id}"),
    "add_members": ("POST", "collections/{collection_id}/members"),
    "remove_members": ("DELETE", "collections/{collection_id}/members"),
    "remove_member": ("DELETE", "collections/{collection_id}/members/{member_id}"),
    "import_collection": ("POST", "collections/{collection_id}/imports"),
    "create_saved_search": ("POST", "saved-searches"),
    "update_saved_search": ("PATCH", "saved-searches/{saved_search_id}"),
    "delete_saved_search": ("DELETE", "saved-searches/{saved_search_id}"),
    "curate": ("POST", "curations"),
}


def _oa_account_path(template: str, identifiers: dict[str, str | None]) -> str:
    required = set(re.findall(r"\{(\w+)\}", template))
    if any(value is not None and name not in required for name, value in identifiers.items()):
        raise ValueError("An identifier was provided that this action does not use")
    encoded = {}
    for name in required:
        value = identifiers.get(name)
        if value is None:
            raise ValueError(f"{name} is required")
        encoded[name] = _oa_segment(value)
    return template.format_map(encoded)


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=True))
async def openalex_account_read(
    action: Literal["collections", "collection", "members", "imports", "import",
                    "saved_searches", "saved_search"],
    collection_id: str | None = None, import_id: str | None = None,
    saved_search_id: str | None = None,
    params: dict[str, str | int | bool] | None = None,
) -> dict:
    """Read collections, members, import jobs and saved searches from OpenAlex.

    collections: GET /collections (public plus yours); collection: /collections/{id}.
    members: /collections/{id}/members; accepts page/per_page (max 1000), cursor,
    or format=csv for all members (potentially large, explicitly requested only).
    imports: /collections/{id}/imports; import adds /{import_id} for job progress.
    saved_searches: /saved-searches; saved_search adds /{saved_search_id}.
    params forwards native filter/search/sort/group_by/select/page/per_page/cursor
    on collections, and has_alert/page/per_page on saved_searches. Endpoint-specific
    validation is delegated to OpenAlex. Returns one page, preserving cursors and codes.
    Private resources require a PERSONAL OPENALEX_API_KEY; organization keys cannot
    own collections or saved searches. These reads do not modify your account.
    """
    if action not in _OA_ACCOUNT_READ_ROUTES:
        raise ValueError("Unknown account read action")
    path = _oa_account_path(_OA_ACCOUNT_READ_ROUTES[action], {
        "collection_id": collection_id, "import_id": import_id,
        "saved_search_id": saved_search_id})
    return await _oa_api(path, params)


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False))
async def openalex_account_write(
    action: Literal["create_collection", "update_collection", "delete_collection",
                    "add_members", "remove_members", "remove_member", "import_collection",
                    "create_saved_search", "update_saved_search", "delete_saved_search", "curate"],
    body: dict | list[dict] | None = None, collection_id: str | None = None,
    member_id: str | None = None, saved_search_id: str | None = None,
    params: dict[str, str | int | bool] | None = None, confirm: bool = False,
) -> dict:
    """Modify OpenAlex account data. Defaults to preview ONLY; never silently confirm.

    Show the exact operation and body and obtain the user's explicit approval BEFORE
    calling with confirm=true. Needs a PERSONAL OPENALEX_API_KEY with the right ownership
    or curator permissions; writes are never retried.
    Collections: create_collection (body display_name, entity_type, optional description,
    access, member_ids up to 10000, or copy_of), update_collection, delete_collection
    (permanent, members included), add_members (body.member_ids), remove_members
    (params.member_ids comma-string, max 100, no body), remove_member (member_id),
    import_collection (exactly one of body.query, body.oql, body.copy_of; runs server-side,
    CONSUMES QUERY CREDITS, up to 1000000 members; progress via openalex_account_read).
    Saved searches: create_saved_search (body.name, body.url, optional description and
    alert={frequency: daily|weekly|monthly}, which sends emails), update_saved_search
    (alert=null disables emails), delete_saved_search.
    curate: POST corrections to user.openalex.org (Bearer); body is one object or a list,
    each with entity, entity_id, property, action, value (authorships, display_name,
    full_name, orcid). Submit only corrections the user has reviewed: they affect public
    records. The API validates permissions and syntax.
    """
    if action not in _OA_ACCOUNT_WRITE_ROUTES:
        raise ValueError("Unknown account write action")
    method, template = _OA_ACCOUNT_WRITE_ROUTES[action]
    path = _oa_account_path(template, {"collection_id": collection_id, "member_id": member_id,
                                      "saved_search_id": saved_search_id})
    if "api_key" in (params or {}):
        raise ValueError("Configure the API key in the server environment")
    if method == "DELETE" and body is not None:
        raise ValueError("DELETE actions do not accept a body")
    if method != "DELETE" and not body:
        raise ValueError("This action requires a non-empty body")
    if isinstance(body, list) and action != "curate":
        raise ValueError("Only curate accepts an array of objects")
    if action == "remove_members" and not (params or {}).get("member_ids"):
        raise ValueError("remove_members requires params.member_ids")
    if confirm and not OA_WRITES_ENABLED:
        raise ValueError("OpenAlex account writes are disabled; set OPENALEX_ENABLE_WRITES=true")
    if not confirm:
        return {"status": "confirmation_required", "action": action, "method": method,
                "host": "user.openalex.org" if action == "curate" else "api.openalex.org",
                "path": path, "body": _oa_public_data(body), "params": _oa_public_data(params or {})}
    if not OA_KEY:
        raise ValueError("OPENALEX_API_KEY is required for account writes")
    return await _oa_api(path, params, method, body, user_api=action == "curate")


async def _openalex(c: httpx.AsyncClient, title: str) -> list[dict]:
    params = {"search": _norm(title), "per-page": 5,
              "select": "doi,title,publication_year,authorships,primary_location,type"}
    r = await _oa_request(c, "works", params)
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


@_tool()
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
    Open Library and Google Books edition/ISBN data is returned in 'book_info' and
    'book_info_google_books' as fallback identifiers.
    """
    names = ["crossref", "semantic_scholar", "openalex"]
    c = _http()
    jobs = [_crossref(c, title, first_author), _semantic_scholar(c, title), _openalex(c, title)]
    if is_book:
        names += ["open_library", "google_books"]
        jobs += [_open_library(c, title, first_author), _book_google_books(c, title, first_author)]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    errors, cands, book_info, gb_info, excluded = [], [], [], [], 0
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            errors.append(_err(name, res))
        elif name == "open_library":
            book_info = res
        elif name == "google_books":
            gb_info = res
        else:
            for x in filter(None, res):
                if _is_derivative(x, title):
                    excluded += 1
                else:
                    cands.append(x)

    merged: dict[str, dict] = {}
    for cd in cands:
        # Books: the year decides the edition, not the match; checked exactly below
        a = _assess(cd, title, first_author, None if is_book else year)
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
    if best:
        competing = [m["doi"] for m in ranked[1:] if _eligible(m)]
        out["agreement"] = {"independent_sources": _independent(best["sources"]),
                            "competing_dois": competing}
        if competing:
            notes.append("Other DOIs also fit the title (often a preprint or another version): "
                         "confirm which one is cited.")
    top = ranked[0] if ranked else None
    if (top and not best and top["score"] >= TITLE_THRESHOLD and top["author_ok"] is None
            and top["title_tokens"] < SHORT_TITLE_TOKENS):
        notes.append("Short title: pass first_author so the match can be confirmed.")
    if is_book:
        out["book_info"] = book_info
        out["book_info_google_books"] = [
            {k: r[k] for k in ("title", "authors", "publishers", "years", "isbns")}
            for r in gb_info
            if _title_sim(title, r["title"])[0] >= TITLE_THRESHOLD
            and _author_ok(first_author, r["authors"]) is not False][:3]
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


async def _doi_agency(d: str) -> str | None:
    """Registration agency from doi.org/doiRA (Crossref, DataCite, mEDRA, JaLC...); None if unregistered."""
    r = await _http().get(f"https://doi.org/doiRA/{_doi_path(d)}")
    r.raise_for_status()
    data = r.json()
    return data[0].get("RA") if data else None


def _datacite_as_crossref(a: dict) -> dict:
    """DataCite attributes in the Crossref message shape, so verify_doi has one comparison path."""
    titles = a.get("titles") or []
    main = next((t["title"] for t in titles if not t.get("titleType")),
                titles[0]["title"] if titles else "")
    sub = next((t["title"] for t in titles if t.get("titleType") == "Subtitle"), None)
    cont = a.get("container") or {}
    pages = "-".join(p for p in (cont.get("firstPage"), cont.get("lastPage")) if p) or None
    pub = a.get("publisher")
    return {"title": [main], "subtitle": [sub] if sub else [],
            "author": [{"family": c.get("familyName") or c.get("name", ""),
                        "ORCID": next((n.get("nameIdentifier") for n in c.get("nameIdentifiers") or []
                                       if n.get("nameIdentifierScheme") == "ORCID"), None)}
                       for c in a.get("creators") or []],
            "issued": {"date-parts": [[a.get("publicationYear")]]},
            "container-title": [cont["title"]] if cont.get("title") else [],
            "type": (a.get("types") or {}).get("resourceTypeGeneral"),
            "volume": cont.get("volume"), "issue": cont.get("issue"), "page": pages,
            "publisher": pub.get("name") if isinstance(pub, dict) else pub, "updated-by": []}


@_tool()
async def verify_doi(doi: str, expected_title: str | None = None,
                     expected_first_author: str | None = None, expected_year: int | None = None,
                     expected_volume: str | None = None, expected_issue: str | None = None,
                     expected_pages: str | None = None,
                     expected_orcid: str | None = None) -> dict:
    """Check that a DOI is registered and return its metadata from its registration agency.

    Crossref DOIs are read from Crossref. When Crossref does not know the DOI, doi.org/doiRA
    names the agency: DataCite DOIs (Zenodo, arXiv, OSF, datasets) are read from DataCite and
    go through the same checks. Other agencies (mEDRA, JaLC...) return status
    'registered_unsupported_agency': registered, but title, authors and year are unverified.
    'registration_agency' is reported for every registered DOI.

    If expected_title is given, also returns a title-similarity score, which detects
    DOIs that exist but belong to a different paper; status 'registered_match' or
    'registered_title_mismatch'. If expected_first_author is given and absent from the
    record's authors, status is 'registered_author_mismatch'. Volume, issue, pages and year
    are returned from the source; expected values are compared with it
    ('bibliographic_checks': match | mismatch | not_in_source per field; year matches the
    issued, print or online year). Cite the source values whenever they differ.
    expected_orcid (iD or orcid.org URL) is checked against the ORCID iDs deposited for the
    authors ('author_orcids'): 'match'; 'mismatch' only when every author has an iD and none
    is the expected one; otherwise 'not_in_source', since deposit is optional. A malformed
    iD returns status 'invalid_orcid'. For unsupported agencies it is reported as unchecked.
    'is_retracted' and 'updates' report retractions, corrections and expressions of concern
    registered in Crossref (including Retraction Watch data): a retracted work may still
    be cited, but the reference must say so. DataCite has no retraction source: for those
    DOIs 'is_retracted' is null (unknown), never false, unless the title says RETRACTED.
    """
    d = _clean_doi(doi)
    if not d:
        return {"status": "invalid_format", "doi": doi}
    want_orcid = _clean_orcid(expected_orcid) if expected_orcid else None
    if expected_orcid and not want_orcid:
        return {"status": "invalid_orcid", "orcid": expected_orcid}
    agency = "Crossref"
    try:
        code, m = await _crossref_work(d)
        if code == 404:
            agency = await _doi_agency(d)
            if agency is None:
                return {"status": "not_found", "doi": d}
            if agency != "DataCite":
                return {"status": "registered_unsupported_agency", "doi": d,
                        "registration_agency": agency,
                        "detail": f"registered with {agency}: no supported metadata API, "
                                  "so title, authors and year are unverified",
                        **({"unchecked": ["orcid"]} if want_orcid else {})}
            r = await _http().get(f"https://api.datacite.org/dois/{_doi_path(d)}")
            code, m = r.status_code, None
            if code == 200:
                m = _datacite_as_crossref(r.json()["data"]["attributes"])
    except (httpx.HTTPError, ValueError, KeyError) as e:
        return {"status": "error", "doi": d, "detail": _err(agency or "doi.org", e)}
    if code != 200 or m is None:
        return {"status": "error", "doi": d, "registration_agency": agency, "http_status": code}
    raw_title = (m.get("title") or [""])[0]
    subtitle = (m.get("subtitle") or [None])[0]
    years = {k: (m.get(k, {}).get("date-parts") or [[None]])[0][0]
             for k in ("issued", "published-print", "published-online")}
    updates = [{"type": u.get("type"), "doi": u.get("DOI"), "source": u.get("source"),
                "date": ((u.get("updated") or {}).get("date-time") or "")[:10] or None}
               for u in m.get("updated-by", [])]
    out = {
        "status": "registered", "doi": d, "url": f"https://doi.org/{d}",
        "registration_agency": agency,
        "title": _strip_html(raw_title), "subtitle": _strip_html(subtitle),
        "authors": [a.get("family", a.get("name", "")) for a in m.get("author", [])],
        "year": years["issued"], "year_print": years["published-print"],
        "year_online": years["published-online"],
        "venue": (m.get("container-title") or [None])[0], "type": m.get("type"),
        "volume": m.get("volume"), "issue": m.get("issue"),
        "pages": m.get("page") or m.get("article-number"),
        "publisher": m.get("publisher"), "edition": m.get("edition-number"),
        "isbn": m.get("ISBN"),
        "author_orcids": sorted({o for a in m.get("author", []) if (o := _clean_orcid(a.get("ORCID")))}),
        "is_retracted": any(u["type"] in RETRACTION_TYPES for u in updates)
                        or bool(RETRACTED_PREFIX_RE.match(raw_title)),
        "updates": updates,
    }
    if agency != "Crossref" and not out["is_retracted"]:
        out["is_retracted"] = None
    expected = {"volume": expected_volume, "issue": expected_issue, "pages": expected_pages}
    checks = {}
    for field, exp in expected.items():
        if exp is None:
            continue
        src = out[field]
        checks[field] = ("not_in_source" if not src
                         else "match" if _biblio_norm(src) == _biblio_norm(exp) else "mismatch")
    if want_orcid:
        authors = m.get("author", [])
        all_have = bool(authors) and all(_clean_orcid(a.get("ORCID")) for a in authors)
        checks["orcid"] = ("match" if want_orcid in out["author_orcids"]
                           else "mismatch" if all_have else "not_in_source")
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


async def _gb_request(c: httpx.AsyncClient, path: str, params: dict) -> httpx.Response:
    headers = {"x-goog-api-key": GB_KEY} if GB_KEY else {}
    return await c.get(f"https://www.googleapis.com/books/v1/{path}", params=params, headers=headers)


async def _book_google_books(c: httpx.AsyncClient, title: str, author: str | None) -> list[dict]:
    q = f'intitle:"{_norm(title)}"' + (f" inauthor:{_surname_query(author)}" if author else "")
    r = await _gb_request(c, "volumes", {"q": q, "maxResults": 10, "printType": "books"})
    r.raise_for_status()
    out = []
    for it in r.json().get("items", []):
        v = it.get("volumeInfo", {})
        year = re.match(r"\d{4}", v.get("publishedDate") or "")
        isbns = [i["identifier"] for i in v.get("industryIdentifiers", [])
                 if str(i.get("type", "")).startswith("ISBN") and i.get("identifier")]
        out.append(_book_rec("google_books", v.get("title"), v.get("authors"),
                             [v["publisher"]] if v.get("publisher") else [],
                             [year.group(0)] if year else [], isbns))
    return out


def _book_conflicts(matched: list[dict]) -> list[dict]:
    """Fields where two sources, each with a value, report nothing in common."""
    per_source: dict[str, list[dict]] = {}
    for rec in matched:
        per_source.setdefault(rec["source"], []).append(rec)
    fields = {
        "year": ({s: sorted({y for r in recs for y in r["years"]}) for s, recs in per_source.items()},
                 lambda a, b: a == b),
        "publisher": ({s: sorted({p for r in recs for p in map(_norm, r["publishers"]) if p})
                       for s, recs in per_source.items()}, lambda a, b: a in b or b in a)}
    out = []
    for field, (values, same) in fields.items():
        have = {s: v for s, v in values.items() if v}
        srcs = sorted(have)
        bad = {s for i, a in enumerate(srcs) for b in srcs[i + 1:]
               if not any(same(x, y) for x in have[a] for y in have[b]) for s in (a, b)}
        if bad:
            out.append({"field": field, "values": {s: have[s] for s in sorted(bad)}})
    return out


@_tool()
async def verify_book(title: str, first_author: str | None = None, year: int | None = None,
                      publisher: str | None = None, isbn: str | None = None) -> dict:
    """Verify a book edition (year, publisher, ISBN) against four catalogues.

    Sources: Open Library (crowd-sourced, partly imported from library and retail feeds),
    Crossref (publisher-deposited, DOI books only), Google Books (aggregated publisher and
    library feeds) and the Deutsche Nationalbibliothek
    (DNB, German-language market). The catalogues can share upstream publisher metadata,
    so agreement is corroboration, not proof. A record counts as matching when title,
    author and every supplied field (year, publisher, ISBN) agree; missing data in the
    record counts against it. Status: 'verified' (matched by at least
    two sources), 'single_source' (one source only: weaker, say so), 'partial' (the work
    exists but no record matches year/publisher: use 'editions' to correct the citation),
    'not_found'. Edition statements ('2nd ed.') and page counts are not checked.
    'agreement' counts independent catalogue groups (Open Library and Google Books share
    feeds and count as one) and flags, without excluding anything, year or publisher values
    that two confirming sources report with no overlap: level 'high' (2+ groups, no
    conflict), 'medium' (2+ groups with a conflict), 'low' (one group) or 'none'.
    """
    names = ["open_library", "crossref", "google_books", "dnb"]
    c = _http()
    results = await asyncio.gather(
        _book_open_library(c, title, first_author), _book_crossref(c, title, first_author),
        _book_google_books(c, title, first_author),
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
    conflicts = _book_conflicts(matched)
    groups = _independent(confirmed_by)
    level = ("none" if not groups else "low" if groups == 1
             else "medium" if conflicts else "high")
    out = {"status": status, "best": best, "editions": others, "source_errors": errors,
           "agreement": {"level": level, "independent_sources": groups,
                         "conflicts": conflicts}}
    if conflicts:
        out["note"] = ("Sources disagree on " + ", ".join(c["field"] for c in conflicts) +
                       ": possibly different editions, or an imprint and its parent "
                       "publisher; check before citing.")
    return out


async def _json_api(source: str, request) -> dict:
    """Run one GET and wrap it as {status, http_status, data}, keeping the API's own errors."""
    try:
        response = await request
    except httpx.HTTPError as error:
        return {"status": "error", "source": source, "detail": _err(source, error)}
    try:
        data = response.json()
    except ValueError:
        return {"status": "error", "http_status": response.status_code,
                "detail": f"{source} returned a non-JSON response"}
    return {"status": "ok" if response.is_success else "error",
            "http_status": response.status_code, "data": data}


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=True))
async def google_books(action: Literal["search", "volume"], query: str | None = None,
                       volume_id: str | None = None,
                       params: dict[str, str | int] | None = None) -> dict:
    """Search Google Books or read one volume (title, authors, publisher, date, ISBNs).

    search: GET /volumes with query as q. q accepts the native keywords intitle:, inauthor:,
    inpublisher:, subject:, isbn:, lccn:, oclc: and quoted phrases.
    volume: GET /volumes/{volume_id}.
    params forwards native options (maxResults up to 40, startIndex, orderBy, printType,
    langRestrict, filter, projection) unchanged; do not pass q or key there. Results are
    restricted by the server's location. Uses GOOGLE_BOOKS_API_KEY when set. The record
    shows how Google Books lists an edition: to check a citation use verify_book, which
    compares it with other catalogues. Returns status, http_status and the original JSON.
    """
    if action not in {"search", "volume"} or {"q", "key"} & set(params or {}):
        raise ValueError("Invalid action; q and key cannot be passed in params")
    if action == "search":
        if volume_id is not None or not query or not query.strip():
            raise ValueError("search requires query and does not use volume_id")
        return await _json_api("google_books", _gb_request(
            _http(), "volumes", {**(params or {}), "q": query}))
    if query is not None:
        raise ValueError("volume requires volume_id and does not use query")
    return await _json_api("google_books", _gb_request(
        _http(), "volumes/" + _path_segment(volume_id, "volume_id"), params or {}))


_orcid_token: tuple[float, str] | None = None
_orcid_lock = asyncio.Lock()


async def _orcid_bearer(refresh: bool = False) -> str:
    global _orcid_token
    async with _orcid_lock:
        if refresh or not _orcid_token or _orcid_token[0] <= time.monotonic():
            _orcid_token = None
            r = await _http().post("https://orcid.org/oauth/token",
                                   headers={"Accept": "application/json"}, data={
                "client_id": ORCID_CLIENT_ID, "client_secret": ORCID_CLIENT_SECRET,
                "grant_type": "client_credentials", "scope": "/read-public"})
            r.raise_for_status()
            try:
                body = r.json()
                token = body["access_token"]
                ttl = max(float(body.get("expires_in") or 3600) - 60, 0.0)
            except (ValueError, KeyError, TypeError):
                raise httpx.HTTPError("ORCID token response is malformed") from None
            _orcid_token = (time.monotonic() + ttl, token)
        return _orcid_token[1]


async def _orcid_get(path: str, params: dict) -> httpx.Response:
    """GET on the public API, with a cached /read-public token when client credentials are set."""
    url = f"https://pub.orcid.org/v3.0/{path}"
    if not (ORCID_CLIENT_ID and ORCID_CLIENT_SECRET):
        return await _http().get(url, params=params, headers={"Accept": "application/json"})
    for refresh in (False, True):
        headers = {"Accept": "application/json",
                   "Authorization": f"Bearer {await _orcid_bearer(refresh)}"}
        r = await _http().get(url, params=params, headers=headers)
        if r.status_code != 401:
            break
    return r


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=True))
async def orcid(action: Literal["search", "person", "works"], orcid_id: str | None = None,
                query: str | None = None,
                params: dict[str, str | int] | None = None) -> dict:
    """Search the ORCID registry or read the public data of one record.

    search: GET /expanded-search with query as q (Solr syntax: family-name:Rossi AND
    given-names:Anna, affiliation-org-name:"Politecnico di Milano", doi-self:"10.x/y").
    Returns ORCID iD, names and institutions of each match. params forwards start and rows
    (rows defaults to 10; the public API caps results at 10,000). Name matches are
    candidates, not identity: confirm with institution or works before assigning an iD.
    person: GET /{orcid_id}/person (names, other names, keywords, external identifiers).
    works: GET /{orcid_id}/works (work summaries grouped by identifier, DOIs included).
    orcid_id accepts the bare iD or an orcid.org URL; the checksum is verified before any
    request. Only data the researcher made public is returned. Works without credentials;
    with ORCID_CLIENT_ID and ORCID_CLIENT_SECRET it authenticates with a /read-public token.
    Returns status, http_status and the original JSON.
    """
    if "q" in (params or {}):
        raise ValueError("Pass the search text as query, not params.q")
    if action == "search":
        if orcid_id is not None or not query or not query.strip():
            raise ValueError("search requires query and does not use orcid_id")
        path, p = "expanded-search/", {"rows": 10, **(params or {}), "q": query}
    elif action in {"person", "works"}:
        clean = _clean_orcid(orcid_id)
        if query is not None or not clean:
            raise ValueError(f"{action} requires a valid orcid_id and does not use query")
        path, p = f"{clean}/{action}", params or {}
    else:
        raise ValueError("Invalid action")
    return await _json_api("orcid", _orcid_get(path, p))


TITLE_SMALL_WORDS = {
    "about", "above", "after", "against", "among", "around", "before", "between", "beyond",
    "from", "into", "over", "than", "that", "their", "these", "this", "those", "through",
    "toward", "towards", "under", "until", "upon", "versus", "when", "where", "which", "while",
    "with", "within", "without"}


def _title_case_suspected(title: str | None) -> bool | None:
    """True if the title looks title-cased, False if sentence-cased, None if too short to judge.

    Counts capitalised words among those a sentence-case title would normally lower:
    skips the first word and the first after a colon, acronyms, words with inner capitals,
    short words and prepositions. Proper nouns inflate the count, so it is only a flag.
    """
    words = (title or "").split()
    eligible = capitalised = 0
    at_start = True
    for w in words:
        core = w.strip("\"'\u201c\u201d\u2018\u2019()[]{}.,;:!?")
        skip, at_start = at_start, w.endswith((":", "?", "!", "."))
        if (skip or len(core) < 4 or not core[0].isalpha() or core.isupper()
                or any(ch.isupper() for ch in core[1:]) or core.lower() in TITLE_SMALL_WORDS):
            continue
        eligible += 1
        capitalised += core[0].isupper()
    if eligible < 3:
        return None
    return capitalised / eligible >= 0.8


async def _csl_json(d: str) -> dict | None:
    try:
        r = await _http().get(f"https://doi.org/{_doi_path(d)}", follow_redirects=True,
                              headers={"Accept": "application/vnd.citationstyles.csl+json"})
        data = r.json() if r.status_code == 200 and "json" in r.headers.get("content-type", "") else None
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@_tool()
async def format_apa(doi: str) -> dict:
    """APA reference for a DOI from doi.org content negotiation, plus the fields behind it.

    The text reproduces the metadata as the registration agency holds it: capitalisation,
    given names and pages are as deposited, and it is NOT checked against the cited work
    (run verify_doi first; on a mismatch compose the reference from the verified values).
    'title_case_suspected' is true when the title looks title-cased, but APA wants sentence
    case for article and chapter titles. It is a flag, not a conversion: lower the title
    by hand, keeping proper nouns and acronyms. null means too short to judge or no record.
    'fields' holds the structured record (authors, year, container, volume, issue, page).
    """
    d = _clean_doi(doi)
    if not d:
        return {"status": "invalid_format", "doi": doi}
    r, csl = await asyncio.gather(
        _http().get(f"https://doi.org/{_doi_path(d)}", follow_redirects=True,
                    headers={"Accept": "text/x-bibliography; style=apa"}),
        _csl_json(d), return_exceptions=True)
    if isinstance(r, httpx.HTTPError):
        return {"status": "error", "doi": d, "detail": _err("doi.org", r)}
    if isinstance(r, BaseException):
        raise r
    if isinstance(csl, BaseException):
        raise csl
    if r.status_code != 200:
        return {"status": "error", "http_status": r.status_code, "doi": d}
    if not r.headers.get("content-type", "").startswith("text/x-bibliography"):
        # Registration agency without content negotiation: doi.org fell through to the landing page
        return {"status": "unsupported", "doi": d,
                "detail": "no APA rendering available for this DOI; format it manually"}
    out = {"status": "ok", "doi": d, "apa": r.text.strip(), "title_case_suspected": None}
    if csl:
        title = csl.get("title")
        title = title if isinstance(title, str) else None
        issued = ((csl.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        out["title_case_suspected"] = _title_case_suspected(title)
        out["fields"] = {
            "title": title,
            "authors": [{"family": a.get("family") or a.get("literal") or a.get("name"),
                         "given": a.get("given")} for a in csl.get("author") or []],
            "year": issued, "container": csl.get("container-title"),
            "volume": csl.get("volume"), "issue": csl.get("issue"), "page": csl.get("page"),
            "publisher": csl.get("publisher"), "type": csl.get("type")}
        if out["title_case_suspected"]:
            out["note"] = ("The title looks title-cased; APA requires sentence case for article "
                           "and chapter titles. Lower it by hand, keeping proper nouns and acronyms.")
    return out


REF_FORMATS = {"bibtex": "application/x-bibtex", "ris": "application/x-research-info-systems",
               "csl_json": "application/vnd.citationstyles.csl+json"}
CSL_STYLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def format_reference(doi: str, format: str = "bibtex", locale: str = "en-US") -> dict:
    """A DOI's reference in another format, from doi.org content negotiation: 'bibtex', 'ris',
    'csl_json', or the name of any CSL style ('chicago-author-date', 'modern-language-association',
    'harvard-cite-them-right', 'ieee', 'vancouver'...; see the CSL style repository). For APA use
    format_apa, which also checks the title case. Same caveat: the text reproduces the metadata as
    deposited, it is not checked against the cited work (verify_doi first)."""
    d = _clean_doi(doi)
    if not d:
        return {"status": "invalid_format", "doi": doi}
    fmt = format.strip().lower()
    if fmt in REF_FORMATS:
        accept = REF_FORMATS[fmt]
    elif CSL_STYLE_RE.fullmatch(fmt) and re.fullmatch(r"[A-Za-z]{2}(-[A-Za-z]{2})?", locale):
        accept = f"text/x-bibliography; style={fmt}; locale={locale}"
    else:
        return {"status": "invalid_format", "format": format,
                "detail": "use bibtex, ris, csl_json or a CSL style name in lowercase with hyphens"}
    try:
        r = await _http().get(f"https://doi.org/{_doi_path(d)}", follow_redirects=True,
                              headers={"Accept": accept})
    except httpx.HTTPError as e:
        return {"status": "error", "doi": d, "detail": _err("doi.org", e)}
    if r.status_code == 404:
        return {"status": "not_found", "doi": d}
    if r.status_code != 200:
        return {"status": "error", "http_status": r.status_code, "doi": d,
                "detail": "unknown CSL style?" if accept.startswith("text/x-bibliography") else None}
    if r.headers.get("content-type", "").startswith("text/html"):
        return {"status": "unsupported", "doi": d,
                "detail": "this registration agency offers no such format; format it manually"}
    body = r.text.strip()
    return {"status": "ok", "doi": d, "format": fmt,
            "reference": json.loads(body) if fmt == "csl_json" else body}


_CITE_YEAR = r"(?:\d{4}[a-z]?|n\.d\.|in press)"
_CITE_NAME = r"[A-ZÀ-ÖØ-Þ][\w'’\-]+(?:\s(?:van|von|der|den|de|del|della|di|da|du|le|la|dos|ten|ter)\s[A-ZÀ-ÖØ-Þ][\w'’\-]+)?"
_PAREN_CITE_RE = re.compile(r"\(([^()]*?\b" + _CITE_YEAR + r"[^()]*)\)")
_PAREN_ITEM_RE = re.compile(
    r"(?:(?:see|cf\.|e\.g\.,?|also)\s+)?(" + _CITE_NAME + r"(?:\s*(?:,|&|and)\s*" + _CITE_NAME +
    r")*(?:\s+et\s+al\.)?)\s*,\s*(" + _CITE_YEAR + r"(?:\s*,\s*" + _CITE_YEAR + r")*)")
_NARRATIVE_RE = re.compile(
    r"\b(" + _CITE_NAME + r"(?:\s*(?:,|and|&)\s*" + _CITE_NAME + r")*(?:\s+et\s+al\.)?)\s*\((" +
    _CITE_YEAR + r"(?:\s*,\s*" + _CITE_YEAR + r")*)(?:,\s*(?:p|pp|chap|para)\.[^)]*)?\)")
_REF_YEAR_RE = re.compile(r"\((\d{4})[a-z]?(?:,[^)]*)?\)|\((n\.d\.)\)")


def _cite_key(names: str, year: str) -> tuple[str, str]:
    first = re.split(r"\s*(?:,|&|and)\s*|\s+et\s+al\.", names.strip())[0]
    return _norm(first.split()[-1]) if first else "", year.strip()


def _parse_citations(text: str) -> list[dict]:
    found = []
    for m in _PAREN_CITE_RE.finditer(text):
        for item in m.group(1).split(";"):
            for im in _PAREN_ITEM_RE.finditer(item):
                for year in im.group(2).split(","):
                    found.append({"names": im.group(1).strip(), "year": year.strip(),
                                  "form": "parenthetical", "text": im.group(0).strip()})
    for m in _NARRATIVE_RE.finditer(text):
        for year in m.group(2).split(","):
            found.append({"names": m.group(1).strip(), "year": year.strip(),
                          "form": "narrative", "text": m.group(0).strip()})
    return found


def _parse_references(references: str) -> list[dict]:
    out = []
    for entry in re.split(r"\n\s*\n|\n(?=\S)", references.strip()):
        entry = " ".join(entry.split())
        if not entry:
            continue
        ym = _REF_YEAR_RE.search(entry)
        year = (ym.group(1) or ym.group(2)) if ym else None
        head = entry[:ym.start()] if ym else entry
        first = head.split(",")[0].strip()
        surname = first.split()[-1] if first else ""
        out.append({"entry": entry, "first_author": surname, "year": year,
                    "suffix": (entry[ym.start() + 5] if ym and ym.group(1)
                               and entry[ym.start() + 5:ym.start() + 6].isalpha() else "")})
    return out


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def check_citations(text: str, references: str) -> dict:
    """Cross-check APA author-date citations in a text against its reference list, offline.

    Parses parenthetical citations ((Norman, 2013), (Gibson, 1979; Gaver, 1991),
    (Ericsson et al., 1993)) and narrative ones (Norman (2013), Fox and Best (2011)), and
    reference entries (one per line or paragraph: first author surname before the first
    comma, year in parentheses). Reports 'cited_not_in_references', 'references_not_cited',
    'year_mismatches' (same first author, other year: a likely typo) and 'ambiguous'
    (two entries share author and year without a/b suffixes). Matching is by first-author
    surname and year only, so a hit is not a verified reference: run find_doi and verify_doi
    on the entries. Does not parse numeric or footnote citation styles.
    """
    if not text.strip() or not references.strip():
        raise ValueError("text and references must both be non-empty")
    cites = _parse_citations(text)
    refs = _parse_references(references)
    ref_keys: dict[tuple[str, str], list[dict]] = {}
    for r in refs:
        ref_keys.setdefault((_norm(r["first_author"]), (r["year"] or "") + r["suffix"]), []).append(r)
    by_author: dict[str, set[str]] = {}
    for (a, y), _ in ref_keys.items():
        by_author.setdefault(a, set()).add(y)
    missing, used, mismatches = [], set(), []
    for c in cites:
        key = _cite_key(c["names"], c["year"])
        if key in ref_keys:
            used.add(key)
        elif key[0] in by_author:
            mismatches.append({**c, "reference_years": sorted(by_author[key[0]])})
        else:
            missing.append(c)
    uncited = [r["entry"] for (k, rs) in ref_keys.items() if k not in used for r in rs]
    ambiguous = [[r["entry"] for r in rs] for rs in ref_keys.values() if len(rs) > 1]
    return {"status": "ok", "citations_found": len(cites), "references_found": len(refs),
            "cited_not_in_references": missing, "references_not_cited": uncited,
            "year_mismatches": mismatches, "ambiguous": ambiguous,
            "note": "matched by first-author surname and year only; verify entries with "
                    "find_doi/verify_doi"}


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def check_retractions(dois: list[str] | None = None, map: str | None = None) -> dict:
    """Retractions, expressions of concern and corrections, from Crossref (includes Retraction
    Watch), for a list of DOIs (up to 1000; e.g. a whole Zotero library) or, with map, for every
    work with a DOI in a literature map, where retracted works are then flagged. A retracted
    work may still be cited, but the text must say so. Only works with a notice are listed;
    DataCite DOIs have no retraction source and are reported as 'not_checked'."""
    if (dois is None) == (map is None):
        raise ValueError("pass either dois (a list) or map (a map name), not both")
    if map is not None:
        return await _check_map_retractions(map)
    if len(dois) > 1000:
        return {"status": "invalid_input", "detail": "at most 1000 DOIs per call"}
    cleaned = {}
    for raw in dois:
        d = _clean_doi(raw)
        if d:
            cleaned.setdefault(d, raw)
    invalid = [raw for raw in dois if not _clean_doi(raw)]
    gate = asyncio.Semaphore(2)

    async def one(d: str):
        async with gate:
            try:
                return d, *await _crossref_work(d)
            except httpx.HTTPError as e:
                return d, 0, {"error": _err("Crossref", e)}

    flagged, not_checked, errors = [], [], []
    for d, code, m in await asyncio.gather(*(one(d) for d in cleaned)):
        if code == 404:
            not_checked.append(d)
            continue
        if code != 200 or not m or "error" in m:
            errors.append({"doi": d, "http_status": code or None})
            continue
        raw_title = (m.get("title") or [""])[0]
        updates = [{"type": u.get("type"), "notice_doi": u.get("DOI"), "source": u.get("source"),
                    "date": ((u.get("updated") or {}).get("date-time") or "")[:10] or None}
                   for u in m.get("updated-by", [])]
        retracted = any(u["type"] in RETRACTION_TYPES for u in updates) or bool(RETRACTED_PREFIX_RE.match(raw_title))
        if retracted or updates:
            flagged.append({"doi": d, "title": _clean_title(raw_title)[:200], "is_retracted": retracted,
                            "authors": [a.get("family", a.get("name", "")) for a in m.get("author", [])][:3],
                            "updates": updates})
    flagged.sort(key=lambda f: (not f["is_retracted"], f["doi"]))
    return {"status": "ok", "checked": len(cleaned) - len(not_checked) - len(errors),
            "retracted": sum(f["is_retracted"] for f in flagged),
            "with_notices": flagged, "not_checked": not_checked, "errors": errors, "invalid": invalid}


async def _check_map_retractions(map: str) -> dict:
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    nodes = maps.nodes(db, m["id"])
    by_doi = {n["doi"]: k for k, n in nodes.items() if n.get("doi")}
    out = await check_retractions(list(by_doi))
    with db:
        for f in out.get("with_notices", []):
            if f["is_retracted"] and f["doi"] in by_doi:
                db.execute("UPDATE nodes SET is_retracted = 1 WHERE map_id = ? AND work_id = ?",
                           (m["id"], by_doi[f["doi"]]))
    return {**out, "map": m["name"]}


IA_FIELDS = ["identifier", "title", "creator", "year", "date", "publisher", "language", "isbn",
             "collection", "lending___status", "access-restricted-item", "imagecount"]


def _ia_terms(value: str) -> str:
    """Words of a field value as a Lucene group, with query syntax removed."""
    words = re.findall(r"[\w'-]+", value, re.UNICODE)
    return "(" + " ".join(words) + ")" if words else ""


def _ia_access(doc: dict) -> str:
    if str(doc.get("access-restricted-item")).lower() != "true":
        return "public"
    status = doc.get("lending___status") or ""
    status = " ".join(status) if isinstance(status, list) else status
    return "print_disabled_only" if status == "is_printdisabled" else "borrowable"


# The patent tools exist only when the EPO OPS keys are configured: without them they could
# only fail, and each tool definition costs context in every conversation.
EPO_CONFIGURED = bool(os.environ.get("EPO_OPS_KEY") and os.environ.get("EPO_OPS_SECRET"))
_patent_tool = _tool if EPO_CONFIGURED else (lambda **kw: (lambda fn: fn))


def _patent_brief(p: dict) -> dict:
    return {k: p[k] for k in ("number", "kind", "title", "applicants", "published", "priority", "ipc")} | {
        "abstract": (p["abstract"] or "")[:400] or None, "cites_literature": len(p["cites_literature"]),
        "url": f"https://worldwide.espacenet.com/patent/search?q=pn%3D{p['number']}"}


def _patent_error(e: Exception) -> dict:
    import patents
    if isinstance(e, patents.NotConfigured):
        return {"status": "not_configured", "detail": str(e)}
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 403:
        return {"status": "error", "http_status": 403,
                "detail": "EPO OPS refused the request: fair-use throttling (retry in a minute), "
                          "weekly quota used, or app not enabled for OPS v3.2"}
    return {"status": "error", "detail": _err("EPO OPS", e)}


@_patent_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def patent_search(query: str, limit: int = 25, page: int = 1) -> dict:
    """Search patents worldwide (EPO Open Patent Services, ~140 million documents). Free words
    search title and abstract; OPS CQL is accepted as is, e.g. 'ta all "foldable display" and
    pa=samsung and pd>=2018', 'ic=G06F1/16', 'ct=EP1000000' (patents citing it), 'in=smith'
    (inventor). Returns number, title, applicants, dates, IPC classes, a short abstract and how
    many scientific works each cites. Use patent_get for the cited literature."""
    import patents
    if not query.strip():
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), 100))
    start = (max(1, int(page)) - 1) * limit + 1
    q = patents.cql(query)
    try:
        root = await patents.get(_http(), "published-data/search/biblio",
                                 {"q": q, "Range": f"{start}-{start + limit - 1}"})
    except Exception as e:
        return _patent_error(e)
    docs, seen = [], set()
    for d in patents.documents(root):
        if d["number"] not in seen:
            seen.add(d["number"])
            docs.append(_patent_brief(d))
    return {"status": "ok", "query": q, "total": patents.total(root), "page": page, "patents": docs}


@_patent_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def patent_get(number: str, resolve_literature: int = 20, citing: bool = True) -> dict:
    """One patent by publication number (EP1000000, US2019123456, WO2020123456): bibliographic
    data, the patents it cites and the scientific literature it cites (from the search report).
    A literature citation carries 'doi' only when the DOI is printed in the citation. For the
    first resolve_literature citations without one (0 disables; each costs a Crossref call),
    a Crossref lookup with find_doi's acceptance rules may add 'doi_candidate' with its title
    score: a candidate to confirm with verify_doi, not a verified DOI.
    citing=True adds the patents that cite it (up to 25, with the total)."""
    import patents
    num = patents.number(number)
    try:
        root = await patents.get(_http(), f"published-data/publication/epodoc/{num}/biblio")
    except Exception as e:
        return _patent_error(e)
    docs = patents.documents(root)
    if not docs:
        return {"status": "not_found", "number": num}
    # kinds of one publication (A1, A2, B1) carry different parts: merge them
    p = next((d for d in docs if d["title"]), docs[0])
    for d in docs:
        p["cites_patents"] = list(dict.fromkeys(p["cites_patents"] + d["cites_patents"]))
        have = {x["text"] for x in p["cites_literature"]}
        p["cites_literature"] += [x for x in d["cites_literature"] if x["text"] not in have]
        p["abstract"] = p["abstract"] or d["abstract"]
    lit, lookups = [], 0
    for x in p["cites_literature"]:
        f = patents.parse_npl(x["text"])
        row = {"text": x["text"], "phase": x["phase"], **f, "doi_candidate": None}
        if not f["doi"] and f["title"] and lookups < max(0, int(resolve_literature)):
            lookups += 1
            try:
                cands = [c for c in await _crossref(_http(), f["title"], f["first_author"])
                         if c and not _is_derivative(c, f["title"])]
            except httpx.HTTPError as e:
                row["lookup_error"] = _err("crossref", e)
                cands = []
            best = max(((_assess(c, f["title"], f["first_author"], f["year"]), c) for c in cands),
                       key=lambda ac: (_eligible(ac[0]), ac[0]["score"]), default=None)
            if best and _eligible(best[0]):
                row["doi_candidate"] = {"doi": best[1]["doi"], "title": best[1]["title"],
                                        "year": best[1]["year"], "title_score": best[0]["score"],
                                        "author_ok": best[0]["author_ok"], "year_ok": best[0]["year_ok"]}
        lit.append(row)
    out = {"status": "ok", **{k: p[k] for k in ("number", "kind", "family_id", "title", "applicants", "inventors",
                                                "published", "priority", "ipc", "abstract", "cites_patents")},
           "url": f"https://worldwide.espacenet.com/patent/search?q=pn%3D{p['number']}",
           "cites_literature": lit, "literature_with_doi": sum(1 for r in lit if r["doi"]),
           "literature_with_candidate": sum(1 for r in lit if r["doi_candidate"]),
           "note": "doi comes from the citation text; doi_candidate is a Crossref match under find_doi's "
                   "rules and needs verify_doi before being cited"}
    if citing:
        try:
            croot = await patents.get(_http(), "published-data/search/biblio", {"q": f"ct={num}", "Range": "1-25"})
            out["cited_by_total"] = patents.total(croot)
            out["cited_by"] = [_patent_brief(d) for d in patents.documents(croot)]
        except Exception as e:
            out["cited_by_error"] = _patent_error(e)["detail"]
    return out


@_patent_tool()
async def map_add_patents(map: str, numbers: list[str], as_seed: bool = False) -> dict:
    """Add patents to a literature map as 'P:<number>' nodes. Each cites the scientific literature
    its search report lists with a printed DOI, resolved to OpenAlex works that enter the map as
    candidates (found_by 'patent:P:<number>'). Citations without a DOI come back in 'unresolved'
    with their raw text: resolve them with find_doi and verify_doi and add the works with
    map_add_seeds; they are never guessed from a title. Patent nodes appear in map_get
    'patent_edges' but take no part in co-citation, coupling or duplicate merging: an examiner's
    citation establishes prior art, it is not a scholarly citation. 1 to 20 numbers per call."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    if not numbers or len(numbers) > 20:
        raise ValueError("numbers: give 1 to 20 patent publication numbers")
    out = {"status": "ok", "map": m["name"], "patents": {}, "literature_added": 0, "unresolved": [], "source_errors": []}
    for number in numbers:
        try:
            p = await patent_get(number, resolve_literature=0, citing=True)
        except ValueError as e:
            out["patents"][number] = {"status": "invalid_input", "detail": str(e)}
            continue
        if p.get("status") != "ok":
            out["patents"][number] = p
            continue
        dois = [r["doi"] for r in p["cites_literature"] if r.get("doi")]
        found, errs = await _oa_batch("doi", dois) if dois else ([], [])
        out["source_errors"] += errs
        pid = f"P:{p['number']}"
        year = next((int(d[:4]) for d in (p.get("priority"), p.get("published")) if d and d[:4].isdigit()), None)
        node = {"id": pid, "doi": None, "title": p["title"], "first_author": (p.get("applicants") or [None])[0],
                "year": year, "cited_by": p.get("cited_by_total") or 0, "field": None, "subfield": None,
                "topic": "patent", "momentum": None, "is_retracted": None,
                "refs": [w["id"] for w in found], "refs_source": "epo"}
        with db:
            for w in found:
                maps.upsert(db, m["id"], w, "candidate", f"patent:{pid}")
            result = maps.upsert(db, m["id"], node, "seed" if as_seed else "candidate", "patent")
            maps.touch(db, m["id"])
        unresolved = [r["text"] for r in p["cites_literature"] if not r.get("doi")]
        out["unresolved"] += [{"patent": pid, "text": t} for t in unresolved]
        out["literature_added"] += len(found)
        out["patents"][pid] = {"result": result, "title": p["title"], "literature_with_doi": len(dois),
                               "resolved": len(found), "unresolved": len(unresolved), "cited_by_total": p.get("cited_by_total"),
                               "url": p["url"]}
    return out


CONTRAST_RE = re.compile(
    r"\b(however|in contrast|contrary|contradict\w*|fail(?:s|ed)? to|does not|do not|did not|cannot|"
    r"limitation|limited|critic\w*|challeng\w*|question\w*|disput\w*|overlook\w*|neglect\w*|unlike|"
    r"whereas|problematic|shortcoming|insufficient|inadequate|misleading|we argue against|disagree\w*|"
    r"tuttavia|al contrario|contraddi\w*|non riesce|limite|critic\w*|jedoch|im gegensatz|widerspr\w*|kritik\w*)\b", re.I)


def _contrasting(contexts: list[str]) -> bool:
    """Heuristic: the sentences around the citation carry a marker of contrast or critique. A
    candidate to read, not a verdict."""
    return any(CONTRAST_RE.search(c or "") for c in contexts)


@_tool()
async def map_intents(map: str, works: list[str] | None = None, max_works: int = 20) -> dict:
    """Why works in a map cite each other, from Semantic Scholar: for each selected work (the
    seeds by default, or the given ids/DOIs, up to max_works) the citation intents on its
    incoming and outgoing citations ('background', 'methodology', 'result'), whether the citation
    is influential, and the sentences around it. Edges whose sentences carry markers of contrast
    or critique are flagged 'contrasting': candidates for the Objections of a thesis note, to be
    read in the text before use. Stored with the map; map_get returns them as 'edge_intents'
    keyed 'citing->cited'. One request per work and direction (Semantic Scholar allows one per
    second): twenty works take about a minute."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    nodes = maps.nodes(db, m["id"])
    by_doi = {n["doi"]: k for k, n in nodes.items() if n.get("doi")}
    if works:
        chosen = [w for w in _resolve_ids(nodes, works) if w in nodes]
    else:
        chosen = [k for k, n in nodes.items() if n["role"] == "seed"]
    chosen = [k for k in chosen if nodes[k].get("doi")][:max(1, min(int(max_works), 50))]
    if not chosen:
        return {"status": "invalid_input", "detail": "no selected work has a DOI (Semantic Scholar needs one)"}
    rows, errors, fetched = [], [], 0
    fields = "intents,isInfluential,contexts,{side}.externalIds"
    for k in chosen:
        doi = nodes[k]["doi"]
        for direction, side in (("citations", "citingPaper"), ("references", "citedPaper")):
            r = await _s2_api(f"graph/v1/paper/DOI:{_doi_path(doi)}/{direction}",
                              {"fields": fields.format(side=side), "limit": 1000})
            fetched += 1
            if r.get("status") != "ok":
                errors.append(f"{k} {direction}: {r.get('detail') or r.get('http_status')}")
                continue
            for x in (r.get("data") or {}).get("data") or []:
                other_doi = _clean_doi(((x.get(side) or {}).get("externalIds") or {}).get("DOI"))
                other = by_doi.get(other_doi) if other_doi else None
                if not other or other == k:
                    continue
                citing, cited = (other, k) if direction == "citations" else (k, other)
                contexts = [c for c in (x.get("contexts") or []) if c][:5]
                rows.append({"citing": citing, "cited": cited, "intents": x.get("intents") or [],
                             "influential": bool(x.get("isInfluential")), "contexts": contexts,
                             "contrasting": _contrasting(contexts)})
    maps.save_intents(db, m["id"], rows)
    maps.touch(db, m["id"])
    contrasting = [{"citing": r["citing"], "cited": r["cited"], "context": (r["contexts"] or [""])[0][:300]}
                   for r in rows if r["contrasting"]]
    return {"status": "ok", "map": m["name"], "works": chosen, "requests": fetched, "edges_annotated": len(rows),
            "influential": sum(1 for r in rows if r["influential"]), "contrasting": contrasting,
            "source_errors": errors,
            "note": "intents and influence from Semantic Scholar; 'contrasting' is a wording heuristic on the "
                    "citation sentences, to be checked on the text"}


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def internet_archive(title: str | None = None, author: str | None = None, year: int | None = None,
                           isbn: str | None = None, query: str | None = None, limit: int = 10,
                           public_only: bool = False) -> dict:
    """Digitised books (and other texts) on the Internet Archive / Open Library scans: identifier,
    edition data, and access: 'public' (free to read and download), 'borrowable' (controlled
    lending with an archive.org account), 'print_disabled_only'. Give title/author/year/isbn, or a
    raw Lucene `query` (e.g. 'subject:(phenomenology) AND language:ita'). Useful for an old
    or foreign edition, for checking a page, or as a PDF source for public domain books."""
    parts = ["mediatype:texts"]
    for field, value in (("title", title), ("creator", author)):
        if value and (terms := _ia_terms(value)):
            parts.append(f"{field}:{terms}")
    if year:
        parts.append(f"year:{int(year)}")
    if isbn:
        digits = re.sub(r"[^0-9Xx]", "", isbn)
        if digits:
            parts.append(f"isbn:{digits}")
    if query:
        parts.append(f"({query})")
    if public_only:
        parts.append("NOT access-restricted-item:true")
    if len(parts) == 1:
        return {"status": "invalid_input", "detail": "give title, author, year, isbn or query"}
    limit = max(1, min(int(limit), 50))
    headers = {"Authorization": f"LOW {IA_ACCESS}:{IA_SECRET}"} if IA_ACCESS and IA_SECRET else {}
    params = [("q", " AND ".join(parts)), ("rows", str(limit)), ("output", "json")] + [("fl[]", f) for f in IA_FIELDS]
    try:
        r = await _http().get("https://archive.org/advancedsearch.php", params=params, headers=headers)
    except httpx.HTTPError as e:
        return {"status": "error", "detail": _err("Internet Archive", e)}
    if r.status_code != 200:
        return {"status": "error", "http_status": r.status_code}
    data = r.json().get("response", {})
    records = []
    for doc in data.get("docs", []):
        ident = doc.get("identifier")
        records.append({
            "identifier": ident, "title": doc.get("title"), "creator": doc.get("creator"),
            "year": doc.get("year") or (str(doc.get("date") or "")[:4] or None),
            "publisher": doc.get("publisher"), "language": doc.get("language"), "isbn": doc.get("isbn"),
            "pages": doc.get("imagecount"), "access": _ia_access(doc),
            "url": f"https://archive.org/details/{ident}",
            "pdf": f"https://archive.org/download/{ident}/{ident}.pdf" if _ia_access(doc) == "public" else None})
    return {"status": "ok", "query": " AND ".join(parts), "total": data.get("numFound", 0), "records": records}


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


def _path_segment(value: str | None, name: str) -> str:
    if not value or not value.strip() or value.strip() in {".", ".."}:
        raise ValueError(f"{name} is required and must be a non-empty identifier")
    return quote(value.strip(), safe="")


def _s2_ids(ids: list[str] | None, maximum: int) -> list[str]:
    if not ids or len(ids) > maximum or any(not value.strip() for value in ids):
        raise ValueError(f"Provide between 1 and {maximum} non-empty ids")
    return [value.strip() for value in ids]


async def _s2_api(path: str, params: dict[str, str | int] | None = None,
                  body: dict | None = None) -> dict:
    try:
        response = await _s2(_http(), "POST" if body is not None else "GET",
                             f"https://api.semanticscholar.org/{path}",
                             params=params or {}, **({"json": body} if body is not None else {}))
    except httpx.HTTPError as error:
        return {"status": "error", "source": "semantic_scholar",
                "detail": _err("semantic_scholar", error)}
    try:
        data = response.json()
    except ValueError:
        return {"status": "error", "http_status": response.status_code,
                "detail": "Semantic Scholar returned a non-JSON response"}
    return {"status": "ok" if response.is_success else "error",
            "http_status": response.status_code, "data": data}


async def _s2_paper(
    action: Literal["details", "authors", "citations", "references", "batch",
                    "search", "bulk", "match", "autocomplete"],
    paper_id: str | None = None, ids: list[str] | None = None,
    params: dict[str, str | int] | None = None,
) -> dict:
    """Access all nine Academic Graph paper endpoints without normalizing their results.

    details/authors/citations/references require paper_id (S2 hash, CorpusId:123,
    DOI:10.x/y, ARXIV:2401.01234, PMID:123 or other API-supported identifier).
    batch requires ids (1-500); missing papers remain null in the returned list.
    search, bulk, match and autocomplete use params.query, not paper_id.
    A title match is an API candidate, NOT a verified bibliographic match: use find_doi
    and verify_doi for verification.

    params uses native API query names: fields (comma-separated, including nested
    fields or embedding.specter_v2), query, year, publicationDateOrYear,
    publicationTypes, venue, fieldsOfStudy, minCitationCount, openAccessPdf (empty
    string). Availability depends on the endpoint; API validation errors are preserved.
    search: offset/limit (max 100, up to 1000 results), plain text only.
    bulk: Boolean + | - and quoted phrases, sort (e.g. citationCount:desc), token
    for the next page; up to 1000 results/page, no limit/offset parameter.
    authors/citations/references: offset/limit (max 1000). citations/references
    support fields=contexts,intents,isInfluential,title,authors.
    autocomplete: query only. Each call fetches ONE page; data.next/data.token
    are preserved. All API families share the 1 request/s limiter and retries.
    """
    if action == "batch":
        if paper_id is not None:
            raise ValueError("batch uses ids, not paper_id")
        return await _s2_api("graph/v1/paper/batch", params, {"ids": _s2_ids(ids, 500)})
    if ids is not None:
        raise ValueError("ids is only supported for batch")
    if action in {"details", "authors", "citations", "references"}:
        path = f"graph/v1/paper/{_path_segment(paper_id, 'paper_id')}"
        if action != "details":
            path += f"/{action}"
    else:
        if paper_id is not None:
            raise ValueError("Search actions use params.query, not paper_id")
        routes = {"search": "search", "bulk": "search/bulk", "match": "search/match",
                  "autocomplete": "autocomplete"}
        if action not in routes:
            raise ValueError("Unknown paper action")
        if action != "bulk" and not (params or {}).get("query"):
            raise ValueError("params.query is required")
        path = f"graph/v1/paper/{routes[action]}"
    return await _s2_api(path, params)


async def _s2_author(
    action: Literal["details", "papers", "search", "batch"],
    author_id: str | None = None, ids: list[str] | None = None,
    params: dict[str, str | int] | None = None,
) -> dict:
    """Access all four Academic Graph author endpoints, preserving native JSON.

    details/papers require author_id, a Semantic Scholar author ID (not a name).
    search requires params.query (author name); batch requires ids (1-1000).
    params.fields selects fields such as name,affiliations,normalizedAffiliations,
    externalIds,paperCount,citationCount,hIndex,papers.title (endpoint dependent).
    search/papers accept offset and limit (max 1000); papers also accepts
    publicationDateOrYear. Each call fetches one page; data.next is preserved.
    All calls share the Semantic Scholar 1 request/s limit, authentication and retries.
    """
    if action == "batch":
        if author_id is not None:
            raise ValueError("batch uses ids, not author_id")
        return await _s2_api("graph/v1/author/batch", params, {"ids": _s2_ids(ids, 1000)})
    if ids is not None:
        raise ValueError("ids is only supported for batch")
    if action == "search":
        if author_id is not None or not (params or {}).get("query"):
            raise ValueError("search requires params.query, not author_id")
        path = "graph/v1/author/search"
    elif action in {"details", "papers"}:
        path = f"graph/v1/author/{_path_segment(author_id, 'author_id')}"
        if action == "papers":
            path += "/papers"
    else:
        raise ValueError("Unknown author action")
    return await _s2_api(path, params)


async def _s2_snippets(query: str,
                       params: dict[str, str | int] | None = None) -> dict:
    """Search text excerpts via GET /graph/v1/snippet/search; preserve native JSON.

    query is plain text. params supports fields (e.g. snippet.text,snippet.section),
    paperIds (comma-separated, approximately 100), authors (comma-separated, max 10,
    AND semantics with fuzzy matching), year, publicationDateOrYear, venue,
    fieldsOfStudy, minCitationCount, insertedBefore and limit (max 1000).
    No offset/token pagination. Results retain scores, section, offsets, annotations,
    paper metadata and license disclaimers. An excerpt is not the full paper.
    Shares the Semantic Scholar 1 request/s limit, authentication and retries.
    """
    if not query.strip() or "query" in (params or {}):
        raise ValueError("Provide a non-empty query using the query argument only")
    return await _s2_api("graph/v1/snippet/search", {**(params or {}), "query": query})


async def _s2_recommend(action: str, paper_id, positive_paper_ids, negative_paper_ids, params):
    if action == "recommend_single":
        if positive_paper_ids is not None or negative_paper_ids is not None:
            raise ValueError("recommend_single takes paper_id, not positive/negative_paper_ids")
        return await _s2_api(
            f"recommendations/v1/papers/forpaper/{_path_segment(paper_id, 'paper_id')}", params)
    if paper_id is not None or not positive_paper_ids:
        raise ValueError("recommend_multi requires positive_paper_ids (and no paper_id)")
    if any(not value.strip() for value in [*positive_paper_ids, *(negative_paper_ids or [])]):
        raise ValueError("Paper identifiers must not be empty")
    if "from" in (params or {}):
        raise ValueError("params.from ('recent' or 'all-cs') is only accepted by recommend_single")
    body = {"positivePaperIds": [value.strip() for value in positive_paper_ids],
            "negativePaperIds": [value.strip() for value in (negative_paper_ids or [])]}
    return await _s2_api("recommendations/v1/papers/", params, body)


async def _s2_datasets(
    action: Literal["releases", "release", "dataset", "diffs"],
    release_id: str = "latest", dataset_name: str | None = None,
    start_release_id: str | None = None,
) -> dict:
    """Access all four Datasets API endpoints; return metadata and download links only.

    releases: GET /datasets/v1/release/ lists available release IDs.
    release: GET /datasets/v1/release/{release_id} lists datasets and license metadata.
    dataset: GET /datasets/v1/release/{release_id}/dataset/{dataset_name} returns
    metadata and temporary pre-signed download URLs for the dataset's partitions.
    diffs: GET /datasets/v1/diffs/{start_release_id}/to/{release_id}/{dataset_name}
    returns incremental update/delete file URLs between releases.
    release_id defaults to 'latest'; start_release_id is required for diffs.
    Discover dataset names with release, rather than guessing them. Download access
    depends on the API key's permissions. Files can be very large: no file is downloaded
    or applied locally. Preserve README/license information and treat signed URLs as
    temporary access credentials. All calls share the 1 request/s limiter and retries.
    """
    if action != "diffs" and start_release_id is not None:
        raise ValueError("start_release_id is only supported for diffs")
    if action in {"releases", "release"} and dataset_name is not None:
        raise ValueError("dataset_name is only supported for dataset or diffs")
    if action == "releases":
        path = "datasets/v1/release/"
    elif action == "release":
        path = f"datasets/v1/release/{_path_segment(release_id, 'release_id')}"
    elif action == "dataset":
        path = (f"datasets/v1/release/{_path_segment(release_id, 'release_id')}"
                f"/dataset/{_path_segment(dataset_name, 'dataset_name')}")
    elif action == "diffs":
        path = (f"datasets/v1/diffs/{_path_segment(start_release_id, 'start_release_id')}"
                f"/to/{_path_segment(release_id, 'release_id')}"
                f"/{_path_segment(dataset_name, 'dataset_name')}")
    else:
        raise ValueError("Unknown datasets action")
    return await _s2_api(path)


_S2_ACTIONS = {
    "paper_details": ("paper", "details"), "paper_authors": ("paper", "authors"),
    "paper_citations": ("paper", "citations"), "paper_references": ("paper", "references"),
    "paper_batch": ("paper", "batch"), "paper_search": ("paper", "search"),
    "paper_bulk": ("paper", "bulk"), "paper_match": ("paper", "match"),
    "paper_autocomplete": ("paper", "autocomplete"),
    "author_details": ("author", "details"), "author_papers": ("author", "papers"),
    "author_search": ("author", "search"), "author_batch": ("author", "batch"),
    "snippets": ("snippets", ""),
    "recommend_single": ("recommend", "single"), "recommend_multi": ("recommend", "multi"),
}
_S2_ARGS = {"paper": {"paper_id", "ids"}, "author": {"author_id", "ids"},
            "snippets": {"query"},
            "recommend": {"paper_id", "positive_paper_ids", "negative_paper_ids"}}
# Actions that return lists: a default limit keeps the native response readable
_S2_LIST_ACTIONS = {"paper_citations", "paper_references", "paper_search", "paper_bulk",
                    "author_papers", "author_search", "snippets",
                    "recommend_single", "recommend_multi"}
S2_DEFAULT_LIMIT = 20
S2Action = Literal[
    "paper_details", "paper_authors", "paper_citations", "paper_references", "paper_batch",
    "paper_search", "paper_bulk", "paper_match", "paper_autocomplete", "author_details",
    "author_papers", "author_search", "author_batch", "snippets",
    "recommend_single", "recommend_multi"]


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=True))
async def semantic_scholar(
    action: S2Action, paper_id: str | None = None, author_id: str | None = None,
    ids: list[str] | None = None, query: str | None = None,
    positive_paper_ids: list[str] | None = None, negative_paper_ids: list[str] | None = None,
    params: dict[str, str | int] | None = None,
) -> dict:
    """Semantic Scholar Academic Graph and Recommendations; native JSON, one page per call.

    paper_*: paper_id (S2 hash, CorpusId:123, DOI:10.x/y, ARXIV:2401.01234, PMID:123),
    except paper_batch (ids, 1-500) and paper_search/paper_bulk/paper_match/
    paper_autocomplete (params.query). paper_match returns the closest title: a candidate,
    not a verified match (use find_doi and verify_doi).
    author_*: author_id (S2 author ID, not a name), author_batch (ids, 1-1000),
    author_search (params.query). snippets: query (text excerpts, not full papers).
    recommend_single: paper_id, params.from 'recent' or 'all-cs'; recommend_multi:
    positive_paper_ids and optional negative_paper_ids. Not verified citations.
    params uses the API's names (fields, year, venue, fieldsOfStudy, minCitationCount,
    openAccessPdf, offset, limit, token). Lists default to limit 20; next page: data.next
    as offset, or data.token as token (paper_bulk). Arguments an action does not use are
    rejected. Datasets have their own tool, semantic_scholar_datasets.
    """
    if action not in _S2_ACTIONS:
        raise ValueError(f"Unknown action '{action}'; use one of {', '.join(_S2_ACTIONS)}")
    family, op = _S2_ACTIONS[action]
    given = {name for name, value in {
        "paper_id": paper_id, "author_id": author_id, "ids": ids, "query": query,
        "positive_paper_ids": positive_paper_ids,
        "negative_paper_ids": negative_paper_ids}.items() if value is not None}
    if given - _S2_ARGS[family]:
        raise ValueError(f"{action} does not accept: {', '.join(sorted(given - _S2_ARGS[family]))}"
                         f"; it uses {', '.join(sorted(_S2_ARGS[family]))}")
    if action in _S2_LIST_ACTIONS:
        params = {"limit": S2_DEFAULT_LIMIT, **(params or {})}
    if family == "paper":
        return await _s2_paper(op, paper_id=paper_id, ids=ids, params=params)
    if family == "author":
        return await _s2_author(op, author_id=author_id, ids=ids, params=params)
    if family == "snippets":
        return await _s2_snippets(query or "", params)
    return await _s2_recommend(action, paper_id, positive_paper_ids, negative_paper_ids, params)


@_tool(gateway=True, annotations=ToolAnnotations(readOnlyHint=True))
async def semantic_scholar_datasets(
    action: Literal["releases", "release", "files", "diffs"], release_id: str = "latest",
    dataset_name: str | None = None, start_release_id: str | None = None,
) -> dict:
    """Semantic Scholar Datasets API: release list, one release (its dataset names),
    download links of a dataset in a release (files: dataset_name), or incremental diffs
    (diffs: start_release_id, release_id, dataset_name). Only metadata and temporary
    signed links come back; nothing is downloaded. Rarely needed: the bulk snapshots are
    for offline processing, not for answering questions (use semantic_scholar).
    """
    if action in {"files", "diffs"} and not dataset_name:
        raise ValueError(f"action='{action}' requires dataset_name (see action='release')")
    if action == "diffs" and not start_release_id:
        raise ValueError("action='diffs' requires start_release_id")
    op = "dataset" if action == "files" else action
    return await _s2_datasets(op, release_id or "latest", dataset_name, start_release_id)


async def _fill_abstracts_s2(c: httpx.AsyncClient, records: list[dict]) -> str | None:
    """Fill missing abstracts and TL;DRs from Semantic Scholar in one batch request."""
    todo = [r for r in records if r["doi"] or r.get("arxiv_id")]
    if not todo:
        return None
    resp = await _s2(c, "POST", "https://api.semanticscholar.org/graph/v1/paper/batch",
                     params={"fields": "abstract,tldr"},
                     json={"ids": [f"DOI:{r['doi']}" if r["doi"] else f"ARXIV:{r['arxiv_id']}"
                                   for r in todo]})
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


def _expand_terms(query: str, fmt) -> str:
    """Apply fmt to every search term, keeping Boolean operators and parentheses."""
    return " ".join(t if t in ("AND", "OR", "NOT", "(", ")") else fmt(t)
                    for t in re.findall(r'"[^"]+"|[()]|[^\s()]+', query))


def _plain(query: str) -> str:
    """Strip Boolean syntax for engines that only take free text."""
    return " ".join(t.strip('"') for t in re.findall(r'"[^"]+"|[^\s()]+', query)
                    if t not in ("AND", "OR", "NOT"))


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
    q = _expand_terms(query, fmt)
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
    q = _expand_terms(query, fmt)
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
    q = _expand_terms(query, fmt)
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


def _cql(query: str, field: str) -> str:
    """CQL for SRU: every term indexed on field, Boolean operators kept (NOT is binary in
    CQL), and an explicit AND between adjacent terms, since CQL has no implicit conjunction."""
    out: list[str] = []
    for tok in re.findall(r'"[^"]+"|[()]|[^\s()]+', query):
        if tok in ("AND", "OR", "NOT", "(", ")"):
            if tok == "(" and out and out[-1] not in ("AND", "OR", "NOT", "("):
                out.append("AND")
            out.append(tok)
            continue
        if out and out[-1] not in ("AND", "OR", "NOT", "("):
            out.append("AND")
        out.append(f"{field}={tok}")
    return " ".join(out)


async def _search_dnb(query, year_from, year_to, limit, page, scope) -> dict:
    # Deutsche Nationalbibliothek SRU: German-language monographs, dissertations and
    # translations, incl. philosophy, which the other indexes cover thinly. No abstracts.
    field = "tit" if scope == "title_abstract" else "WOE"
    q = _cql(query, field)
    params = {"version": "1.1", "operation": "searchRetrieve", "query": q,
              "recordSchema": "oai_dc", "maximumRecords": min(limit, 100),
              "startRecord": (page - 1) * limit + 1}
    r = await _http().get("https://services.dnb.de/sru/dnb", params=params)
    if r.status_code != 200:
        return {"status": "error", "http_status": r.status_code, "detail": r.text[:300]}
    root = ET.fromstring(r.content)
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    total = int(root.findtext(".//{http://www.loc.gov/zing/srw/}numberOfRecords") or 0)
    recs, dropped = [], 0
    for dc in root.iter("{http://www.openarchives.org/OAI/2.0/oai_dc/}dc"):
        txt = lambda tag: [e.text or "" for e in dc.findall(f"dc:{tag}", ns)]
        year = next((_year(d) for d in txt("date") if _year(d)), None)
        if (year_from and year and year < year_from) or (year_to and year and year > year_to):
            dropped += 1
            continue
        ids = txt("identifier")
        isbn = next((m.group(0) for i in ids if (m := re.search(r"\d[\d-]{8,}[\dXx]", i))), None)
        url = next((i for i in ids if i.startswith("http")), None)
        recs.append(_rec("dnb", doi=next((_clean_doi(i) for i in ids if _clean_doi(i)), None),
                         title=(txt("title") or [None])[0], authors=txt("creator"), year=year,
                         venue=next((p.split(" : ")[-1].strip() for p in txt("publisher")), None),
                         venue_type="publisher", type=(txt("type") or [None])[0],
                         language=(txt("language") or [None])[0], keywords=txt("subject"),
                         oa_url=url, note=f"ISBN {isbn}" if isbn else None))
    return _ok("Deutsche Nationalbibliothek", query, scope, q, page, limit, total, recs,
               year_filter="applied client-side on this page" if year_from or year_to else None,
               dropped_by_year=dropped,
               note="library catalogue: no abstracts; Boolean syntax supported; years filtered "
                    "after retrieval, so a page can return fewer records than requested")


async def _search_zenodo(query, year_from, year_to, limit, page, scope) -> dict:
    # Repository for preprints, working papers, conference papers; mixed quality, no peer review
    fmt = ((lambda t: f"(title:{t} OR description:{t})") if scope == "title_abstract"
           else (lambda t: t))
    q = _expand_terms(query, fmt)
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


async def _search_philarchive(query, year_from, year_to, limit, page, scope) -> dict:
    # Local copy of PhilArchive (philosophy preprints), harvested over OAI-PMH by the weekly job:
    # no network here. Empty until the first harvest; the log says when it was last refreshed.
    import philarchive
    db = philarchive.connect()
    try:
        info = philarchive.count(db)
        if not info["records"]:
            return {"status": "error", "detail": "PhilArchive copy is empty: run philarchive.py harvest (the weekly job does it)"}
        rows, total = await asyncio.to_thread(philarchive.search, db, query, year_from, year_to, limit, page, scope)
    finally:
        db.close()
    recs = [_rec("philarchive", doi=_clean_doi(r.get("doi")), title=r.get("title"),
                 authors=[a.strip() for a in (r.get("creators") or "").split(";") if a.strip()], year=r.get("year"),
                 venue="PhilArchive", venue_type="repository", type="preprint", language=r.get("language"),
                 abstract=r.get("description"), abstract_source="philarchive" if r.get("description") else None,
                 keywords=[k.strip() for k in (r.get("subjects") or "").split(";") if k.strip()], oa_url=r.get("url"),
                 note="self-archived; the published version, if any, may differ") for r in rows]
    return _ok("PhilArchive (local harvest)", query, scope, None, page, limit, total, recs,
               harvested=info["last_harvest"], records_in_copy=info["records"],
               note="local OAI-PMH harvest of PhilArchive, not PhilPapers: preprints and self-archived papers only")


DATABASES = ["openalex", "europepmc", "semanticscholar", "hal", "doaj", "arxiv", "osf", "zenodo", "dnb", "philarchive"]

SEARCH_LOG = os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/doi-mcp").split(":")[0], "searches.jsonl")


def _log_event(kind: str, **fields) -> None:
    """Append a line to the search register (STATE_DIRECTORY/searches.jsonl); never fails a tool."""
    try:
        os.makedirs(os.path.dirname(SEARCH_LOG), exist_ok=True)
        with open(SEARCH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **fields},
                               ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _log_search(out: dict, query: str, names: list[str], year_from, year_to, scope: str) -> dict:
    log = out.get("log") or {}
    hits = ({d["database"]: d.get("total_hits") for d in log.get("databases", []) if isinstance(d, dict)}
            if "databases" in log else {log.get("database", names[0]): log.get("total_hits")})
    _log_event("search_literature", query=query, databases=names, year_from=year_from, year_to=year_to, scope=scope,
               status=out.get("status"), hits=hits, returned=len(out.get("results") or []),
               errors=len(out.get("source_errors") or []) + (1 if out.get("status") != "ok" else 0))
    return out


def _read_search_log(days: int | None = None) -> list[dict]:
    try:
        lines = open(SEARCH_LOG, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds") if days else None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if cutoff and e.get("at", "") < cutoff:
            continue
        out.append(e)
    return out


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def search_log(query: str | None = None, days: int = 365, limit: int = 20) -> dict:
    """The register of past searches and maps: every search_literature call (query, databases,
    years, hits per database, date) and every map_create, kept on the server. With query it
    returns the entries whose query is the same or close (shared words), newest first, so a
    search is not repeated and "already searched on …, N hits" can be said; without query the
    latest entries. Nothing here judges relevance: it records what was asked and what came back."""
    entries = _read_search_log(days)
    if query:
        want = set(_norm(query).split()) - {"and", "or", "not"}

        def close(e):
            have = set(_norm(e.get("query") or e.get("map") or "").split()) - {"and", "or", "not"}
            if not want or not have:
                return 0.0
            return len(want & have) / len(want | have)
        scored = [(close(e), e) for e in entries]
        same = [e for sc, e in scored if _norm(e.get("query") or "") == _norm(query)]
        near = [e for sc, e in scored if sc >= 0.5 and _norm(e.get("query") or "") != _norm(query)]
        out = {"status": "ok", "query": query, "same": sorted(same, key=lambda e: e["at"], reverse=True)[:limit],
               "similar": sorted(near, key=lambda e: e["at"], reverse=True)[:limit]}
        out["summary"] = (f"searched {len(same)} time(s) before, last on {out['same'][0]['at'][:10]}" if same
                          else "not searched before") + (f"; {len(near)} similar search(es)" if near else "")
        return out
    latest = sorted(entries, key=lambda e: e["at"], reverse=True)[:limit]
    return {"status": "ok", "entries": latest, "total_in_period": len(entries), "days": days}


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
             "arxiv": _search_arxiv, "dnb": _search_dnb, "philarchive": _search_philarchive}


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
    c = _http()
    r = await _oa_request(c, "works", params)
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


@_tool()
async def search_literature(query: str, year_from: int | None = None, year_to: int | None = None,
                            doc_types: list[str] | None = None, limit: int = 25, page: int = 1,
                            scope: Literal["title_abstract", "fulltext"] = "title_abstract",
                            open_access_only: bool = False,
                            exclude_retracted: bool = True, fill_missing_abstracts: bool = True,
                            database: str = "openalex") -> dict:
    """Search the literature across open indexes; records with abstracts, keywords and a log.

    database: one name, a comma-separated list, or 'all'. openalex (default; best abstracts
    and keywords), europepmc (life sciences, psychology, health), semanticscholar (broad,
    TL;DRs, free text only), hal (French archive; humanities, HCI), doaj (open-access
    journals), arxiv (physics, maths, CS incl. HCI and AI), osf (PsyArXiv, SocArXiv; title
    only), zenodo (self-deposited, not peer-reviewed), dnb (Deutsche Nationalbibliothek:
    German-language books, dissertations and translations, incl. philosophy; no abstracts),
    philarchive (local harvest of the philosophy preprint archive, refreshed weekly; abstracts).
    Philosophy journals are thinly covered by all; PhilPapers itself has no search API.
    query takes Boolean syntax (AND, OR, NOT, "phrases", parentheses) where the database
    allows it. scope 'fulltext' searches indexed full text on OpenAlex, all fields elsewhere.
    doc_types (OpenAlex types: 'article', 'review', 'book', 'book-chapter', 'preprint',
    'dissertation'), open_access_only, exclude_retracted and fill_missing_abstracts apply to
    OpenAlex only; other databases list them under 'ignored_filters'.
    With several databases, records are merged by DOI or arXiv id, or by normalized title
    when first-author surnames overlap and years are close ('dedup_basis' says which rule;
    a preprint and its published version collapse into one record). 'found_in' lists the
    databases; 'independent_sources' counts OpenAlex and Semantic Scholar as one, since both
    derive largely from Crossref. A failing database goes to source_errors without aborting
    the rest. 'log' records query, filters, date and per-database hit counts, which cannot
    be summed as unique papers; deduplication covers the current page only.
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
            rec["independent_sources"] = _independent(names)
        return _log_search(r, query, names, year_from, year_to, scope)

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
    for rec in merged:
        rec["independent_sources"] = _independent(rec["found_in"])
    out = {
        "status": "ok",
        "log": {"query": query, "scope": scope, "page": page, "per_page": limit,
                "databases": logs, "records_before_dedup": before,
                "records_after_dedup": len(merged), "dedup_scope": "this page only",
                "dedup_summary": stats,
                "with_abstract": sum(1 for x in merged if x["abstract"]),
                "executed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "results": merged, "source_errors": errors,
    }
    return _log_search(out, query, names, year_from, year_to, scope)


CG_SELECT = ("id,doi,title,publication_year,cited_by_count,authorships,primary_topic,"
             "referenced_works,is_retracted,counts_by_year")


def _oa_id(url: str | None) -> str | None:
    return url.rsplit("/", 1)[-1].upper() if url else None


def _momentum(counts: list | None) -> int | None:
    """Citations received in the last three complete years: how much a work is cited now."""
    if counts is None:
        return None
    this_year = datetime.now(timezone.utc).year
    return sum(c.get("cited_by_count", 0) for c in counts if this_year - 3 <= c.get("year", 0) < this_year)


def _cg_node(it: dict) -> dict:
    auth = it.get("authorships") or []
    return {"id": _oa_id(it.get("id")), "doi": _clean_doi(it.get("doi")), "title": it.get("title"),
            "first_author": (auth[0].get("author") or {}).get("display_name") if auth else None,
            "year": it.get("publication_year"), "cited_by": it.get("cited_by_count") or 0,
            "field": ((it.get("primary_topic") or {}).get("field") or {}).get("display_name"),
            "subfield": ((it.get("primary_topic") or {}).get("subfield") or {}).get("display_name"),
            "topic": (it.get("primary_topic") or {}).get("display_name"),
            "momentum": _momentum(it.get("counts_by_year")),
            "is_retracted": it.get("is_retracted"),
            "refs": [_oa_id(r) for r in it.get("referenced_works") or []]}


async def _oa_get(path: str, **params) -> dict:
    r = await _oa_request(_http(), path, params)
    r.raise_for_status()
    return r.json()


async def _cg_seed(s: str) -> dict:
    s = s.strip()
    if re.fullmatch(r"(?:https://openalex\.org/)?W\d+", s, re.I):
        return _cg_node(await _oa_get(f"works/{_oa_id(s)}", select=CG_SELECT))
    d = _clean_doi(s)
    if not d:
        raise ValueError("not a DOI or OpenAlex id")
    return _cg_node(await _oa_get(f"works/doi:{_doi_path(d)}", select=CG_SELECT))


def _graph_links(nodes: dict[str, dict], seed_ids: list[str], min_cocitation: int,
                 min_coupling: int) -> tuple[list, list, list]:
    """Citation edges, co-citation and coupling pairs; also sets each node's 'seeds_linked'."""
    edges, cocit, citers = [], {}, {}
    for n in nodes.values():
        if _is_patent(n["id"]):
            continue  # examiner citations: shown as patent_edges, never as co-citation or coupling
        inside = sorted({r for r in n["refs"] if r in nodes and r != n["id"]})
        edges += [[n["id"], r] for r in inside]
        for i, a in enumerate(inside):
            for b in inside[i + 1:]:
                cocit[(a, b)] = cocit.get((a, b), 0) + 1
        for r in set(n["refs"]):
            citers.setdefault(r, []).append(n["id"])
    shared: dict[tuple[str, str], int] = {}
    for ids in citers.values():
        ids.sort()
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                shared[(a, b)] = shared.get((a, b), 0) + 1
    size = {k: len(set(n["refs"])) for k, n in nodes.items()}
    coupling = sorted(([a, b, w, round(w / (size[a] * size[b]) ** 0.5, 3)]
                       for (a, b), w in shared.items() if w >= min_coupling),
                      key=lambda p: -p[3])[:500]
    cocitation = sorted(([a, b, w] for (a, b), w in cocit.items() if w >= min_cocitation),
                        key=lambda p: -p[2])[:500]
    seeds, linked = set(seed_ids), {k: set() for k in nodes}
    for a, b in edges:
        if a in seeds:
            linked[b].add(a)
        if b in seeds:
            linked[a].add(b)
    for k, n in nodes.items():
        n["seeds_linked"] = len(linked[k] - {k})
        pairs = [tuple(sorted((k, s))) for s in seeds if s != k]
        n["seed_cocited"] = sum(cocit.get(p, 0) for p in pairs)
        n["seed_coupling"] = max((round(shared.get(p, 0) / ((size[p[0]] * size[p[1]]) ** 0.5 or 1), 3)
                                  for p in pairs), default=0.0)
    return edges, cocitation, coupling


def _coverage_verdict(openalex: int, other: int, other_name: str = "semantic_scholar") -> str:
    """Compare two counts of the same thing; 0.8 is a heuristic, definitions differ per source."""
    if not openalex and not other:
        return "both_empty"
    if not openalex:
        return "openalex_missing"
    if not other:
        return f"{other_name}_missing"
    if min(openalex, other) / max(openalex, other) >= 0.8:
        return "concordant"
    return "openalex_lower" if openalex < other else f"{other_name}_lower"


# OpenCitations Index: citation data released CC0, built from Crossref deposits, DataCite, PubMed
# and more, independent of OpenAlex's own processing: the one count that is not derived from
# the same pipeline. A free token (opencitations.net/accesstoken) is polite, not required.
OC_TOKEN = os.environ.get("OPENCITATIONS_TOKEN", "")


async def _oc_count(doi: str, kind: str) -> int | None:
    """Citation or reference count of a DOI in the OpenCitations Index; None when unknown."""
    path = "citation-count" if kind == "citations" else "reference-count"
    headers = {"authorization": OC_TOKEN} if OC_TOKEN else {}
    r = await _http().get(f"https://api.opencitations.net/index/v2/{path}/doi:{_doi_path(doi)}", headers=headers)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
        return int(data[0]["count"]) if data else None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


async def _oc_coverage(nodes: dict, seed_ids: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Seed counts in the OpenCitations Index, two requests per seed, at most 25 seeds."""
    with_doi = [sid for sid in seed_ids if nodes[sid].get("doi")][:25]
    errors: list[str] = []

    async def one(sid):
        try:
            cites = await _oc_count(nodes[sid]["doi"], "citations")
            refs = await _oc_count(nodes[sid]["doi"], "references")
        except httpx.HTTPError as e:
            errors.append(_err("opencitations", e))
            return sid, None
        return sid, {"opencitations_cited_by": cites, "opencitations_references": refs,
                     "opencitations_citations_verdict": _coverage_verdict(nodes[sid]["cited_by"] or 0, cites or 0, "opencitations"),
                     "opencitations_references_verdict": _coverage_verdict(len(nodes[sid]["refs"]), refs or 0, "opencitations")}
    pairs = await asyncio.gather(*(one(s) for s in with_doi))
    return {sid: row for sid, row in pairs if row}, errors


async def _s2_coverage(nodes: dict, seed_ids: list[str]) -> tuple[list[dict], str | None]:
    """Seed reference and citation counts in OpenAlex vs Semantic Scholar (one batch request)."""
    with_doi = [sid for sid in seed_ids if nodes[sid].get("doi")]
    if not with_doi:
        return [], None
    resp = await _s2(_http(), "POST", "https://api.semanticscholar.org/graph/v1/paper/batch",
                     params={"fields": "referenceCount,citationCount"},
                     json={"ids": [f"DOI:{nodes[sid]['doi']}" for sid in with_doi]})
    if resp.status_code != 200:
        return [], f"semantic_scholar: HTTP {resp.status_code}"
    rows = []
    for sid, hit in zip(with_doi, resp.json()):
        row = {"id": sid, "doi": nodes[sid]["doi"], "openalex_references": len(nodes[sid]["refs"]),
               "openalex_cited_by": nodes[sid]["cited_by"]}
        if not hit:
            rows.append({**row, "references_verdict": "not_in_semantic_scholar",
                         "citations_verdict": "not_in_semantic_scholar"})
            continue
        refs, cites = hit.get("referenceCount") or 0, hit.get("citationCount") or 0
        rows.append({**row, "semantic_scholar_references": refs, "semantic_scholar_cited_by": cites,
                     "references_verdict": _coverage_verdict(row["openalex_references"], refs),
                     "citations_verdict": _coverage_verdict(row["openalex_cited_by"], cites)})
    return rows, None


@_tool()
async def citation_graph(seeds: list[str], include_references: bool = True,
                         include_citing: bool = True, citing_per_seed: int = 20,
                         max_nodes: int = 150, min_cocitation: int = 2,
                         min_coupling: int = 3, check_coverage: bool = True) -> dict:
    """One-off citation graph around seed works (DOIs or OpenAlex ids, max 10), not stored.

    Same nodes, scores (seeds_linked, seed_cocited, seed_coupling), citation_edges,
    cocitation and coupling as map_get, computed on this sample (lower bounds). For work
    that continues across chats prefer map_create + map_expand + map_get, which persist the
    graph and the reading state. Use paper_table with node ids for abstracts.
    OpenAlex reference lists are incomplete for humanities and older works:
    'log.seeds_without_references' flags them; 'coverage_check' (check_coverage=True, one
    Semantic Scholar batch plus the OpenCitations Index, the one count independent of
    OpenAlex,
    Semantic Scholar batch, DOI seeds only) says per seed whether OpenAlex counts are
    'concordant', 'openalex_lower', 'openalex_missing' or 'both_empty'. The page in the
    server instructions shows maps, not this output; for a one-off visualization build a
    D3 artifact (timeline year x citations, and a force network on cocitation/coupling).
    """
    seeds = seeds[:10]
    citing_per_seed = max(0, min(citing_per_seed, 100))
    max_nodes = max(len(seeds), min(max_nodes, 300))
    errors: list[str] = []
    nodes: dict[str, dict] = {}
    for s, r in zip(seeds, await asyncio.gather(*map(_cg_seed, seeds), return_exceptions=True)):
        if isinstance(r, Exception):
            errors.append(_err(s, r))
        else:
            nodes[r["id"]] = {**r, "role": "seed"}
    if not nodes:
        return {"status": "not_found", "source_errors": errors}
    seed_ids = list(nodes)

    others: dict[str, dict] = {}
    linked: dict[str, set[str]] = {}
    if include_citing and citing_per_seed:
        res = await asyncio.gather(*(
            _oa_get("works", filter=f"cites:{sid}", sort="cited_by_count:desc",
                    select=CG_SELECT, **{"per-page": citing_per_seed}) for sid in seed_ids),
            return_exceptions=True)
        for sid, r in zip(seed_ids, res):
            if isinstance(r, Exception):
                errors.append(_err(f"citing {sid}", r))
                continue
            for n in map(_cg_node, r.get("results", [])):
                if n["id"] not in nodes:
                    others.setdefault(n["id"], {**n, "role": "citing"})
                    linked.setdefault(n["id"], set()).add(sid)
    if include_references:
        for sid in seed_ids:
            for rid in nodes[sid]["refs"]:
                if rid and rid not in nodes:
                    linked.setdefault(rid, set()).add(sid)
        todo = [rid for rid in linked if rid not in others]
        res = await asyncio.gather(*(
            _oa_get("works", filter="openalex:" + "|".join(todo[i:i + 50]), select=CG_SELECT,
                    **{"per-page": 50}) for i in range(0, len(todo), 50)),
            return_exceptions=True)
        for r in res:
            if isinstance(r, Exception):
                errors.append(_err("references", r))
                continue
            for n in map(_cg_node, r.get("results", [])):
                others.setdefault(n["id"], {**n, "role": "reference"})

    ranked = sorted(others.values(), key=lambda n: (len(linked.get(n["id"], ())), n["cited_by"]),
                    reverse=True)
    room = max_nodes - len(nodes)
    nodes.update((n["id"], n) for n in ranked[:room])

    edges, pairs, coupling = _graph_links(nodes, seed_ids, min_cocitation, min_coupling)
    coverage: list[dict] = []
    if check_coverage:
        try:
            coverage, err = await asyncio.wait_for(_s2_coverage(nodes, seed_ids), timeout=20)
            if err:
                errors.append(err)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as e:
            errors.append(_err("semantic_scholar coverage", e))
        try:
            oc, oc_errors = await asyncio.wait_for(_oc_coverage(nodes, seed_ids), timeout=30)
            errors += oc_errors
            by_id = {row["id"]: row for row in coverage}
            for sid, extra in oc.items():
                if sid in by_id:
                    by_id[sid].update(extra)
                else:
                    coverage.append({"id": sid, "doi": nodes[sid]["doi"], "openalex_references": len(nodes[sid]["refs"]),
                                     "openalex_cited_by": nodes[sid]["cited_by"], **extra})
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            errors.append(_err("opencitations coverage", e))
    return {
        "status": "ok",
        "nodes": [{k: v for k, v in n.items() if k != "refs"} for n in nodes.values()],
        "citation_edges": edges, "cocitation": pairs, "coupling": coupling,
        "coverage_check": coverage,
        "log": {"source": "OpenAlex", "seeds": seed_ids, "nodes": len(nodes),
                "citation_edges": len(edges), "cocitation_pairs": len(pairs),
                "coupling_pairs": len(coupling), "min_cocitation": min_cocitation,
                "min_coupling": min_coupling, "candidates_dropped": max(0, len(ranked) - room),
                "seeds_without_references": [s for s in seed_ids if not nodes[s]["refs"]],
                "seeds_without_doi": [s for s in seed_ids if not nodes[s].get("doi")],
                "executed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "source_errors": errors,
    }


@_tool()
async def paper_table(ids: list[str], fill_from_semantic_scholar: bool = True) -> dict:
    """Detail rows for a list of works: abstract, keywords, topics, TL;DR, venue, OA, retraction.

    ids: DOIs, OpenAlex ids or arXiv ids ('2401.01234', 'arXiv:2401.01234v2'), max 300,
    e.g. node ids from citation_graph or DOIs from search_literature. Fetched from OpenAlex
    in batches of 50 (arXiv ids via their 10.48550 DOI, falling back to the arXiv API);
    missing abstracts and TL;DRs come from one Semantic Scholar batch request. Rows keep the
    input order; unresolved ids are listed in 'not_found'. Abstracts are long: for many
    works show a compact table (title, year, citations, TL;DR) and give abstracts and
    keywords on request.
    """
    ids = list(dict.fromkeys(i.strip() for i in ids if i and i.strip()))[:300]
    is_oa = lambda i: re.fullmatch(r"(?:https://openalex\.org/)?W\d+", i, re.I)
    arxiv = lambda i: (m := ARXIV_ID_RE.match(i)) and re.sub(r"v\d+$", "", m.group(1))
    doi_of = lambda i: f"10.48550/arxiv.{arxiv(i)}".lower() if arxiv(i) else _clean_doi(i)
    want_oa = [_oa_id(i) for i in ids if is_oa(i)]
    want_doi = [d for i in ids if not is_oa(i) and (d := doi_of(i))]
    # '|' and ',' are filter separators in OpenAlex, so such DOIs are fetched one by one
    batchable = [d for d in want_doi if not re.search(r"[|,]", d)]
    singles = [d for d in want_doi if re.search(r"[|,]", d)]
    queries = (["openalex:" + "|".join(want_oa[k:k + 50]) for k in range(0, len(want_oa), 50)]
               + ["doi:" + "|".join(batchable[k:k + 50]) for k in range(0, len(batchable), 50)])
    res = await asyncio.gather(
        *(_oa_get("works", filter=q, select=OA_SELECT, **{"per-page": 50}) for q in queries),
        *(_oa_get(f"works/doi:{_doi_path(d)}", select=OA_SELECT) for d in singles),
        return_exceptions=True)
    by_id, by_doi, errors = {}, {}, []
    for r in res:
        if isinstance(r, httpx.HTTPStatusError) and r.response.status_code == 404:
            continue
        if isinstance(r, Exception):
            errors.append(_err("openalex", r))
            continue
        for it in r.get("results", [r]):
            rec = _oa_record(it)
            rec["id"] = _oa_id(rec.pop("openalex_id"))
            if len(rec["authors"]) > 10:
                rec["authors"] = rec["authors"][:10] + ["et al."]
            rec.setdefault("tldr", None)
            by_id[rec["id"]] = rec
            if rec["doi"]:
                by_doi[rec["doi"]] = rec
    # arXiv preprints merged into a published version are not found under their arXiv DOI
    missing = [a for i in ids if (a := arxiv(i)) and doi_of(i) not in by_doi]
    if missing:
        try:
            r = await _http().get("https://export.arxiv.org/api/query", params={
                "id_list": ",".join(missing), "max_results": len(missing)})
            r.raise_for_status()
            for rec in _arxiv_records(r.content)[0]:
                if rec["title"] and rec["title"] != "Error":
                    rec.update(id=f"arXiv:{rec['arxiv_id']}", tldr=None)
                    by_doi[f"10.48550/arxiv.{rec['arxiv_id']}".lower()] = rec
        except httpx.HTTPError as e:
            errors.append(_err("arxiv", e))
    rows, not_found = [], []
    for i in ids:
        rec = by_id.get(_oa_id(i)) if is_oa(i) else by_doi.get(doi_of(i) or "")
        if rec is None:
            not_found.append(i)
        elif rec not in rows:
            rows.append(rec)
    if fill_from_semantic_scholar and rows:
        if err := await _fill_abstracts_s2(_http(), rows):
            errors.append(err)
    return {"status": "ok" if rows else "not_found", "rows": rows, "not_found": not_found,
            "log": {"requested": len(ids), "returned": len(rows),
                    "with_abstract": sum(1 for r in rows if r["abstract"]),
                    "executed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
            "source_errors": errors}


# ---------------------------------------------------------------------------------------------
# Literature maps: persistent seeds, candidates and reading state (maps.py), built on the
# citation_graph fetchers and _graph_links. Links are recomputed from stored reference lists.

MAP_MAX_NODES = 2000
EXPAND_MODES = ("references", "citing", "similar", "authors")
ExpandMode = Literal["references", "citing", "similar", "authors"]
MapState = Literal["new", "to_read", "read", "dismissed"]


def _map_db():
    import maps
    return maps, maps.connect()


async def _oa_batch(filter_key: str, values: list[str]) -> tuple[list[dict], list[str]]:
    """OpenAlex works for many ids or DOIs, 50 per request, as citation_graph nodes."""
    values = [v for v in dict.fromkeys(values) if v and not re.search(r"[|,]", v)
              and (filter_key != "openalex" or re.fullmatch(r"W\d+", v))]  # not 'Z:'/'B:' book nodes
    res = await asyncio.gather(*(
        _oa_get("works", filter=f"{filter_key}:" + "|".join(values[i:i + 50]), select=CG_SELECT,
                **{"per-page": 50}) for i in range(0, len(values), 50)), return_exceptions=True)
    found, errors = [], []
    for r in res:
        if isinstance(r, Exception):
            errors.append(_err(f"openalex {filter_key}", r))
        else:
            found += [_cg_node(it) for it in r.get("results", [])]
    return found, errors


def _is_oa(work_id: str) -> bool:
    return bool(re.fullmatch(r"W\d+", work_id or ""))


def _is_patent(work_id: str) -> bool:
    return str(work_id or "").startswith("P:")


def _patent_edges(nodes: dict[str, dict]) -> list[list[str]]:
    """[patent node, cited work] for every patent in the map and every cited work in the map."""
    return [[k, r] for k, n in nodes.items() if _is_patent(k) for r in sorted(set(n["refs"])) if r in nodes]


async def _expand(mode: str, seeds: list[dict], per_seed: int) -> tuple[list[tuple[dict, str]], list[str]]:
    """(node, found_by) pairs for one expansion mode."""
    out, errors = [], []
    if mode == "references":
        owner = {}
        for sd in seeds:
            for rid in sd["refs"]:
                owner.setdefault(rid, sd["id"])
        found, errors = await _oa_batch("openalex", list(owner))
        out = [(n, f"references:{owner[n['id']]}") for n in found if n["id"] in owner]
    elif mode == "citing":
        seeds = [sd for sd in seeds if _is_oa(sd["id"])]
        res = await asyncio.gather(*(
            _oa_get("works", filter=f"cites:{sd['id']}", sort="cited_by_count:desc", select=CG_SELECT,
                    **{"per-page": per_seed}) for sd in seeds), return_exceptions=True)
        for sd, r in zip(seeds, res):
            if isinstance(r, Exception):
                errors.append(_err(f"citing {sd['id']}", r))
            else:
                out += [(_cg_node(it), f"citing:{sd['id']}") for it in r.get("results", [])]
    elif mode == "similar":
        with_doi = [sd for sd in seeds if sd.get("doi")][:100]
        if not with_doi:
            return [], ["similar: no seed has a DOI"]
        ids = [f"DOI:{sd['doi']}" for sd in with_doi]
        # Semantic Scholar refuses the whole request (400) if one id is unknown: keep the known ones
        known = await _s2_api("graph/v1/paper/batch", {"fields": "paperId"}, {"ids": ids})
        if known["status"] == "ok":
            ids = [i for i, hit in zip(ids, known["data"]) if hit]
            if not ids:
                return [], ["similar: no seed is in Semantic Scholar"]
        r = await _s2_api("recommendations/v1/papers/", {"fields": "externalIds", "limit": min(per_seed * len(ids), 500)},
                          {"positivePaperIds": ids})
        if r["status"] != "ok":
            return [], [f"similar: semantic_scholar {r.get('http_status') or r.get('detail')}"]
        dois = [_clean_doi((p.get("externalIds") or {}).get("DOI")) for p in r["data"].get("recommendedPapers", [])]
        found, errors = await _oa_batch("doi", [d for d in dois if d])
        out = [(n, "similar:semantic_scholar") for n in found]
    elif mode == "authors":
        seeds = [sd for sd in seeds if _is_oa(sd["id"])]
        res = await asyncio.gather(*(_oa_get(f"works/{sd['id']}", select="id,authorships") for sd in seeds),
                                   return_exceptions=True)
        authors = {}
        for sd, r in zip(seeds, res):
            if isinstance(r, Exception):
                errors.append(_err(f"authors {sd['id']}", r))
                continue
            for a in (r.get("authorships") or [])[:3]:
                if aid := _oa_id((a.get("author") or {}).get("id")):
                    authors.setdefault(aid, sd["id"])
        res = await asyncio.gather(*(
            _oa_get("works", filter=f"authorships.author.id:{aid}", sort="cited_by_count:desc",
                    select=CG_SELECT, **{"per-page": per_seed}) for aid in authors), return_exceptions=True)
        for aid, r in zip(authors, res):
            if isinstance(r, Exception):
                errors.append(_err(f"works of {aid}", r))
            else:
                out += [(_cg_node(it), f"authors:{authors[aid]}") for it in r.get("results", [])]
    return out, errors


def _map_view(nodes: dict[str, dict], min_cocitation: int = 2, min_coupling: int = 3) -> tuple[list, list, list]:
    seeds = [k for k, n in nodes.items() if n["role"] == "seed"]
    edges, cocit, coupling = _graph_links(nodes, seeds, min_cocitation, min_coupling)
    # co-cited with anything in the map: separates candidates when one seed links them all
    total = {k: 0 for k in nodes}
    for n in nodes.values():
        if _is_patent(n["id"]):
            continue
        inside = {r for r in n["refs"] if r in nodes and r != n["id"]}
        for r in inside:
            total[r] += len(inside) - 1
    for k, n in nodes.items():
        n["cocited_total"] = total[k]
    return edges, cocit, coupling


async def _s2_reference_ids(doi: str) -> list[str]:
    """OpenAlex ids of a work's references taken from Semantic Scholar, for works whose OpenAlex
    record has none (common for books and the humanities)."""
    r = await _s2_api(f"graph/v1/paper/DOI:{doi}/references", {"fields": "externalIds", "limit": 1000})
    if r["status"] != "ok":
        return []
    dois = [_clean_doi(((x.get("citedPaper") or {}).get("externalIds") or {}).get("DOI"))
            for x in r["data"].get("data") or []]
    found, _ = await _oa_batch("doi", [d for d in dois if d])
    return [n["id"] for n in found]


# OpenAlex title searches cost $0.001 each against a $1/day budget: capped per call, the rest is
# retried on later calls (results are cached in maps.sqlite)
OA_SEARCHES_PER_CALL = 200


async def _resolve_work(surname: str, title: str, budget: dict | None = None) -> dict | None:
    """The OpenAlex work with this title and first-author surname. First Crossref, then Semantic
    Scholar's title match (both free): a matching DOI (or MAG id) is then fetched from OpenAlex
    by id (free). Else an OpenAlex title search
    (paid, counted in budget['searches']): among the matches, the most cited one that is not a
    review, so every citation of a classic lands on the same record. Raises BudgetExhausted when
    the budget for this call is used up."""
    words, last = _title_words(title), _ascii(surname).split()
    if not words or not last:
        return None
    try:
        for c in [x for x in await _crossref(_http(), title, last[-1]) if x]:
            authors = " ".join(_ascii(a) for a in c.get("authors") or [])
            full = c["title"] + (" " + c["subtitle"] if c.get("subtitle") else "")
            if last[-1] in authors.split() and (_title_match(words, c["title"]) or _title_match(words, full)):
                try:
                    return _cg_node(await _oa_get(f"works/doi:{_doi_path(c['doi'])}", select=CG_SELECT))
                except httpx.HTTPStatusError as e:
                    if e.response.status_code != 404:
                        raise
                break
    except httpx.HTTPError:
        pass  # Crossref unavailable: fall through
    # Semantic Scholar title match (free): its DOI or MAG id leads to the OpenAlex record (free)
    m = await _s2_api("graph/v1/paper/search/match", {"query": " ".join(words[:12]), "fields": "title,authors,externalIds"})
    if m["status"] == "ok":
        for p in (m["data"].get("data") or [])[:1]:
            names = " ".join(_ascii(a.get("name")) for a in p.get("authors") or [])
            ext = p.get("externalIds") or {}
            if last[-1] in names.split() and _title_match(words, p.get("title")):
                for path in ([f"works/doi:{_doi_path(_clean_doi(ext['DOI']))}"] if ext.get("DOI") else []) + \
                            ([f"works/mag:{ext['MAG']}"] if ext.get("MAG") else []):
                    try:
                        return _cg_node(await _oa_get(path, select=CG_SELECT))
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code != 404:
                            raise
    if budget is not None:
        if budget.get("searches", 0) >= budget.get("max", OA_SEARCHES_PER_CALL):
            raise BudgetExhausted()
        budget["searches"] = budget.get("searches", 0) + 1
    r = await _oa_get("works", filter=f"title.search:{' '.join(words[:8])},raw_author_name.search:{last[-1]}",
                      sort="cited_by_count:desc", select=CG_SELECT + ",type", **{"per-page": 8})
    for it in r.get("results", []):
        if it.get("type") in ("review", "book-review", "erratum", "paratext", "retraction"):
            continue
        if _title_match(words, it.get("title")):
            return _cg_node(it)
    return None


class BudgetExhausted(Exception):
    pass


def _stub_id(surname: str, year: int | None, title: str) -> str:
    return "B:" + "-".join([_ascii(surname).split()[-1] if _ascii(surname).split() else "anon", str(year or "nd")]
                          + _title_words(title)[:4])


async def _pdf_reference_ids(maps, db, entries: list[str], budget: dict) -> tuple[list[str], dict]:
    """Bibliography entries read from a PDF → work ids: OpenAlex ids when found (cached in
    maps.sqlite), else 'B:' stub ids for works OpenAlex does not have."""
    parsed = [(e, *_parse_ref(e)) for e in entries]
    todo = {}
    for e, sur, year, title in parsed:
        k = _ref_key(sur, title)
        if k and maps.cached_ref(db, k) is None:
            todo[k] = (sur, title)
    sem = asyncio.Semaphore(2)

    async def one(k, sur, title):
        async with sem:
            try:
                return k, await _resolve_work(sur, title, budget)
            except Exception:
                return k, False  # source error or budget used up: not cached, retried next time
    for k, node in await asyncio.gather(*(one(k, *v) for k, v in todo.items())):
        if node is not False:
            maps.cache_ref(db, k, node["id"] if node else "")
    ids, stats = [], {"entries": len(entries), "openalex": 0, "book_nodes": 0, "unparsed": 0, "pending": 0}
    for e, sur, year, title in parsed:
        k = _ref_key(sur, title)
        wid = maps.cached_ref(db, k) if k else None
        if k and wid is None:
            stats["pending"] += 1  # not looked up yet (budget or source error): next call
            continue
        if wid:
            ids.append(wid)
            stats["openalex"] += 1
        elif k and len(_title_words(title)) >= 2:
            stub = {"id": _stub_id(sur, year, title), "title": title[:300], "first_author": sur, "year": year}
            maps.save_stub(db, stub)
            ids.append(stub["id"])
            stats["book_nodes"] += 1
        else:
            stats["unparsed"] += 1
    return list(dict.fromkeys(ids)), stats


def _knowledge():
    import knowledge
    return knowledge


def _public_node(n: dict) -> dict:
    out = {k: v for k, v in n.items() if k != "refs"}
    out["kind"] = "patent" if _is_patent(n["id"]) else "book" if str(n["id"]).startswith(("B:", "Z:")) else "work"
    return out


@_tool()
async def map_create(name: str, seeds: list[str] | None = None, description: str | None = None) -> dict:
    """Create a persistent literature map, optionally with seed works (DOIs or OpenAlex ids).
    Maps are stored by the server (SQLite) and keep seeds, candidates and reading state across chats;
    the state never goes into the notes. Then: map_expand, map_suggest, map_mark, map_get.
    """
    maps, db = _map_db()
    try:
        maps.create(db, name, description)
    except ValueError as e:
        return {"status": "invalid_input", "detail": str(e)}
    except Exception as e:
        if "UNIQUE" in str(e):
            return {"status": "exists", "detail": f"a map named {name!r} already exists"}
        raise
    out = {"status": "ok", "map": name.strip()}
    if seeds:
        out.update(await map_add_seeds(name, seeds))
        out["status"] = "ok"
    _log_event("map_create", map=name.strip(), description=description, seeds=len(seeds or []))
    return out


@_tool()
async def map_add_seeds(map: str, seeds: list[str] | None = None, zotero_collection: str | None = None,
                        zotero_tag: str | None = None, zotero_items: list[str] | None = None) -> dict:
    """Add seed works to a map: DOIs or OpenAlex ids, and/or every item with a DOI in a Zotero
    collection (name or path, subcollections included) or with a Zotero tag (e.g.
    'cited/designing-resilience'), read from the server's Zotero library snapshot. Max 50 seeds per
    call. A candidate already in the map is promoted to seed. When OpenAlex has no reference
    list for a seed (books, humanities), the references come from Semantic Scholar if it has
    them; 'warnings' names seeds that still have none, which make sparse maps: add more seeds
    from the same field.

    Books and other items without a DOI (from zotero_collection, zotero_tag or zotero_items =
    item keys) are looked up in OpenAlex by title and author; if absent they enter as 'Z:KEY'
    nodes. Their references come from the bibliography in their PDF when the Zotero MCP's
    extract_references has been run on them (also added to OpenAlex's own list); references
    OpenAlex does not have become 'B:' book nodes. 'pdf_references' reports how many were
    found per seed; 'warnings' names items whose PDF bibliography is not extracted yet."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    wanted, warnings, books = list(seeds or []), [], []
    k = _knowledge()
    if zotero_collection or zotero_tag or zotero_items:
        if not k.library():
            return {"status": "no_snapshot", "detail": "the Zotero library snapshot is missing (KNOWLEDGE_DIR/library.json)"}
        items = k.items_where(zotero_collection, zotero_tag) if (zotero_collection or zotero_tag) else []
        items += [it for it in (k.item_by_key(x.strip().upper()) for x in zotero_items or []) if it]
        if not items:
            return {"status": "not_found", "detail": "no library items match that collection/tag/keys"}
        wanted += [it["doi"] for it in items if it.get("doi")]
        books = [it for it in items if not it.get("doi")]
    wanted = [x for x in dict.fromkeys(w.strip() for w in wanted) if x]
    books = list({it["key"]: it for it in books}.values())
    if len(wanted) + len(books) > 50:
        warnings.append(f"{len(wanted) + len(books) - 50} seeds over the limit of 50 were skipped: call again")
        wanted = wanted[:50]
        books = books[:50 - len(wanted)]
    if not wanted and not books:
        return {"status": "invalid_input", "detail": "give seeds, zotero_collection, zotero_tag or zotero_items"}
    res = await asyncio.gather(*(_cg_seed(x) for x in wanted), return_exceptions=True)
    found, errors = [], []
    for x, r in zip(wanted, res):
        if isinstance(r, Exception):
            errors.append(_err(x, r))
        else:
            found.append(r)
    lib_by_doi = k.by_doi()
    for r in found:
        it = lib_by_doi.get(_clean_doi(r.get("doi")) or "")
        r["zotero_key"] = it["key"] if it else None
    budget = {"searches": 0}
    for it in books:  # no DOI: look the work up in OpenAlex, else a Zotero-only node
        node, rk = None, _ref_key((it.get("creators") or [""])[0], it.get("title") or "")
        cached = maps.cached_ref(db, rk) if rk else None
        try:
            if cached:
                node = (await _oa_batch("openalex", [cached]))[0][0]
            elif cached is None:
                node = await _resolve_work((it.get("creators") or [""])[0], it.get("title") or "", budget)
                if rk:
                    maps.cache_ref(db, rk, node["id"] if node else "")
        except Exception as e:
            errors.append(_err(it["key"], e))
        node = node or {"id": f"Z:{it['key']}", "doi": None, "title": it.get("title"),
                        "first_author": (it.get("creators") or [None])[0], "year": it.get("year"),
                        "cited_by": None, "refs": []}
        node["zotero_key"] = it["key"]
        found.append(node)
    pdf_counts = {}
    for r in found:
        r["refs_source"] = "openalex" if r["refs"] else None
        if not r["refs"] and r.get("doi"):
            r["refs"] = await _s2_reference_ids(r["doi"])
            r["refs_source"] = "semantic_scholar" if r["refs"] else None
        entries = k.references(r["zotero_key"]) if r.get("zotero_key") else None
        if entries:
            ids, stats = await _pdf_reference_ids(maps, db, entries, budget)
            if stats["pending"]:
                warnings.append(f"{r['id']}: {stats['pending']} references not looked up yet (OpenAlex daily budget): "
                                "call map_add_seeds again with the same items later; the rest is cached")
            r["refs"] = list(dict.fromkeys(r["refs"] + ids))
            r["refs_source"] = "zotero_pdf" if not r["refs_source"] else r["refs_source"] + "+zotero_pdf"
            pdf_counts[r["id"]] = stats
        elif entries is None and r.get("zotero_key") and not r.get("doi"):
            warnings.append(f"{r['zotero_key']} ({(r.get('title') or '')[:50]}): bibliography not extracted yet, "
                            "run the Zotero MCP's extract_references, then map_add_seeds again")
        if not r["refs"]:
            warnings.append(f"no reference list for {r['id']} ({(r.get('title') or '')[:60]}): expect a sparse map")
    added = {}
    with db:
        for r in found:
            added[r["id"]] = maps.upsert(db, m["id"], r, "seed", "seed")
        maps.touch(db, m["id"])
    merged = _merge_duplicates(maps, db, m["id"])
    return {"status": "ok" if added else "not_found", "map": m["name"], "seeds": added, "duplicates_merged": merged,
            "references_from_semantic_scholar": [r["id"] for r in found if r.get("refs_source") == "semantic_scholar"],
            "pdf_references": pdf_counts,
            "warnings": warnings, "source_errors": errors}


@_tool()
async def map_expand(map: str, modes: list[ExpandMode] | None = None, per_seed: int = 20,
                     seeds: list[str] | None = None, max_new: int = 150) -> dict:
    """Grow a map from its seeds (or from the given seed ids): 'references' (works the seeds
    cite), 'citing' (most-cited works citing each seed), 'similar' (Semantic Scholar
    recommendations for all seeds together), 'authors' (most-cited works of each seed's first
    three authors). Default: references and citing. New works enter as candidates with
    state 'new'; existing ones keep their state and gain the new 'found_by'. per_seed caps
    citing/similar/authors per seed (max 100). seeds may name any works in the map (not only
    seeds) to expand from them. At most max_new new works enter per call (default 150; a map
    holds at most 2000): those linked to most starting works first, then the most cited.
    Duplicate records of one work (preprint and published version) are merged afterwards.
    """
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    modes = modes or ["references", "citing"]
    if bad := [x for x in modes if x not in EXPAND_MODES]:
        return {"status": "invalid_input", "detail": f"unknown modes {bad}; use {', '.join(EXPAND_MODES)}"}
    per_seed = max(1, min(int(per_seed), 100))
    current = maps.nodes(db, m["id"])
    if seeds:
        wanted = set(_resolve_ids(current, seeds))
        chosen = [n for n in current.values() if n["id"] in wanted]
    else:
        chosen = [n for n in current.values() if n["role"] == "seed"]
    if not chosen:
        return {"status": "no_seeds", "detail": "add seeds with map_add_seeds first, or give ids of works in the map"}
    found, errors = [], []
    for mode, (pairs, errs) in zip(modes, await asyncio.gather(*(_expand(x, chosen, per_seed) for x in modes))):
        found += pairs
        errors += errs
    if "references" in modes:
        # works OpenAlex lacks ('B:' ids, from PDF bibliographies) enter only when at least two
        # works in the map cite them: one citation alone is noise on a map
        citers: dict[str, list[str]] = {}
        for n in current.values():
            for r in set(n["refs"]):
                if r.startswith("B:"):
                    citers.setdefault(r, []).append(n["id"])
        chosen_ids = {n["id"] for n in chosen}
        wanted_b = [b for b, who in citers.items() if len(who) >= 2 and chosen_ids & set(who)]
        for b, st in maps.stubs(db, wanted_b).items():
            owner = next(w for w in citers[b] if w in chosen_ids)
            found.append(({"id": b, "doi": None, "title": st["title"], "first_author": st["first_author"],
                           "year": st["year"], "cited_by": None, "refs": [], "refs_source": None},
                          f"references:{owner}"))
    # When the map would overflow, the works linked to most seeds enter first, then the most cited:
    # never an arbitrary cut by arrival order.
    links: dict[str, set] = {}
    for node, how in found:
        links.setdefault(node["id"], set()).add(how.split(":", 1)[-1])
    found.sort(key=lambda p: (p[0]["id"] in current, len(links[p[0]["id"]]), p[0].get("cited_by") or 0), reverse=True)
    counts, skipped = {"added": 0, "updated": 0, "promoted": 0}, 0
    max_new = max(1, min(int(max_new), MAP_MAX_NODES))
    new_ids: set[str] = set()
    with db:
        size = len(current)
        for node, how in found:
            if node["id"] not in current and node["id"] not in new_ids and (size >= MAP_MAX_NODES or len(new_ids) >= max_new):
                skipped += 1
                continue
            result = maps.upsert(db, m["id"], node, "candidate", how)
            if result == "added":
                current[node["id"]] = node
                new_ids.add(node["id"])
                size += 1
            counts[result] += 1
        maps.touch(db, m["id"])
    merged = _merge_duplicates(maps, db, m["id"])
    return {"status": "ok", "map": m["name"], "modes": modes, "started_from": [n["id"] for n in chosen],
            **counts, "skipped": skipped, "duplicates_merged": merged, "works_in_map": size - len(merged),
            "source_errors": errors}


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def map_suggest(map: str, limit: int = 20, include_states: list[MapState] | None = None) -> dict:
    """Candidates most likely missing from the reading, ranked like citation_graph: by how many
    seeds they cite or are cited by (seeds_linked), then how often works in the map cite them
    together with a seed (seed_cocited), then the strongest coupling with a seed, then citation
    count. Dismissed and read works are left out unless include_states asks for them. Each
    suggestion says why (found_by, the three scores). The map is a sample: scores are lower
    bounds, and humanities and older works often lack reference lists in OpenAlex."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    nodes = maps.nodes(db, m["id"])
    _map_view(nodes)
    states = set(include_states or ["new", "to_read"])
    ranked = sorted((n for n in nodes.values() if n["role"] == "candidate" and n["state"] in states),
                    key=lambda n: (n["seeds_linked"], n["seed_cocited"], n["seed_coupling"], n["cocited_total"],
                                   n["cited_by"] or 0), reverse=True)
    top = ranked[:max(1, min(limit, 200))]
    _knowledge().annotate(top)
    keep = ("id", "doi", "title", "first_author", "year", "cited_by", "state", "found_by", "seeds_linked",
            "seed_cocited", "seed_coupling", "cocited_total", "is_retracted", "in_library", "zotero_key", "notes", "zotero_tags")
    seeds = [n for n in nodes.values() if n["role"] == "seed"]
    warnings = []
    if seeds and not any(n["refs"] for n in seeds):
        warnings.append("no seed has a reference list: the ranking rests on citing works and citation counts; "
                        "add seeds from the same field for co-citation")
    return {"status": "ok", "map": m["name"], "candidates": len(ranked), "warnings": warnings,
            "suggestions": [{k: n.get(k) for k in keep} for n in top]}


@_tool()
async def map_mark(map: str, ids: list[str], state: Literal["new", "to_read", "read", "dismissed"],
                   note: str | None = None) -> dict:
    """Set the reading state of works in a map (OpenAlex ids, or DOIs of works in the map):
    to_read, read, dismissed (hidden from suggestions), or back to new. The optional note
    (why it was dismissed, what to look for) is kept with the work. State stays in the map."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    nodes = maps.nodes(db, m["id"])
    by_doi = {n["doi"]: k for k, n in nodes.items() if n.get("doi")}
    wanted = [(_oa_id(x) if re.fullmatch(r"(?:https://openalex\.org/)?W\d+", x.strip(), re.I)
               else by_doi.get(_clean_doi(x) or "", x)) for x in ids]
    missing = maps.mark(db, m["id"], wanted, state, note)
    return {"status": "ok", "map": m["name"], "state": state, "marked": len(wanted) - len(missing), "not_in_map": missing}


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def map_get(map: str, states: list[MapState] | None = None, min_cocitation: int = 2,
                  min_coupling: int = 3) -> dict:
    """Everything in a map for reading or visualizing: works (role, state, found_by, scores)
    and links computed on the whole map: citation_edges, cocitation, coupling (same format and
    caveats as citation_graph). states filters the works returned (links are kept only
    between returned works). To SHOW a map, give the user the 'Literature maps' page (link in
    the server instructions and map_list), not a new artifact, unless the user asks for one."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    nodes = maps.nodes(db, m["id"])
    edges, cocit, coupling = _map_view(nodes, min_cocitation, min_coupling)
    keep = {k for k, n in nodes.items() if not states or n["state"] in states or n["role"] == "seed"}
    inside = lambda pair: pair[0] in keep and pair[1] in keep
    works = [_public_node(n) for k, n in nodes.items() if k in keep]
    _knowledge().annotate(works)
    return {"status": "ok", "map": dict(m), "works": works, "snapshots": _knowledge().status(),
            "citation_edges": [e for e in edges if inside(e)], "cocitation": [p for p in cocit if inside(p)],
            "coupling": [p for p in coupling if inside(p)],
            "patent_edges": [e for e in _patent_edges(nodes) if inside(e)],
            "edge_intents": {k: v for k, v in maps.intents(db, m["id"]).items() if inside(k.split("->"))}}


def _resolve_ids(nodes: dict, ids: list[str]) -> list[str]:
    by_doi = {n["doi"]: k for k, n in nodes.items() if n.get("doi")}
    return [(_oa_id(x) if re.fullmatch(r"(?:https://openalex\.org/)?W\d+", x.strip(), re.I)
             else by_doi.get(_clean_doi(x) or "", x)) for x in ids]


def _dup_key(n: dict) -> str | None:
    title = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", n.get("title") or "").encode("ascii", "ignore").decode().lower()).strip()
    author = re.sub(r"[^a-z]", "", (unicodedata.normalize("NFKD", (n.get("first_author") or "").split(" ")[-1]).encode("ascii", "ignore").decode().lower()))
    return f"{title[:120]}|{author}" if len(title) >= 12 else None


def _merge_duplicates(maps, db, map_id: int) -> list[dict]:
    """Merge records of the same work (same normalised title and first author's surname, years
    at most 2 apart): the most cited record stays, takes the union of found_by and references,
    the seed role and the most advanced reading state."""
    nodes = maps.nodes(db, map_id)
    groups: dict[str, list[dict]] = {}
    for n in nodes.values():
        if not _is_patent(n["id"]) and (k := _dup_key(n)):
            groups.setdefault(k, []).append(n)
    order = {"new": 0, "dismissed": 1, "to_read": 2, "read": 3}
    merged = []
    with db:
        for group in groups.values():
            if len(group) < 2:
                continue
            years = [g["year"] for g in group if g.get("year")]
            # editions of a book can be decades apart; a short title + same surname could be two works
            long_title = len((_dup_key(group[0]) or "").split("|")[0]) >= 25
            if years and max(years) - min(years) > 2 and not long_title:
                continue
            keep = max(group, key=lambda g: (g["role"] == "seed", g.get("cited_by") or 0))
            drop = [g for g in group if g is not keep]
            refs = list(dict.fromkeys(keep["refs"] + [r for g in drop for r in g["refs"]]))
            found = list(dict.fromkeys(keep["found_by"] + [f for g in drop for f in g["found_by"]]))
            state = max([keep] + drop, key=lambda g: order.get(g["state"], 0))["state"]
            role = "seed" if any(g["role"] == "seed" for g in group) else "candidate"
            note = keep.get("note") or next((g["note"] for g in drop if g.get("note")), None)
            db.execute("UPDATE nodes SET refs = ?, found_by = ?, state = ?, role = ?, note = ? WHERE map_id = ? AND work_id = ?",
                       (json.dumps(refs), json.dumps(found), state, role, note, map_id, keep["id"]))
            for g in drop:
                db.execute("DELETE FROM nodes WHERE map_id = ? AND work_id = ?", (map_id, g["id"]))
            # references to a removed record now point to the kept one, so citations are not lost
            gone = {g["id"] for g in drop}
            for other in nodes.values():
                if other["id"] not in gone and other["id"] != keep["id"] and gone & set(other["refs"]):
                    other["refs"] = list(dict.fromkeys(keep["id"] if r in gone else r for r in other["refs"]))
                    db.execute("UPDATE nodes SET refs = ? WHERE map_id = ? AND work_id = ?",
                               (json.dumps(other["refs"]), map_id, other["id"]))
            merged.append({"kept": keep["id"], "removed": [g["id"] for g in drop], "title": (keep.get("title") or "")[:80]})
    return merged


async def _map_watch(maps, db, m, per_seed: int) -> dict:
    """New works citing the seeds since the last check, added as candidates."""
    since = (m["watched"] or m["created"])[:10]
    current = maps.nodes(db, m["id"])
    seeds = [k for k, n in current.items() if n["role"] == "seed" and _is_oa(k)]
    if not seeds:
        return {"status": "no_seeds"}
    res = await asyncio.gather(*(
        _oa_get("works", filter=f"cites:{sid},from_created_date:{since}", sort="publication_date:desc",
                select=CG_SELECT, **{"per-page": max(1, min(per_seed, 100))}) for sid in seeds),
        return_exceptions=True)
    today, new, errors = maps.now()[:10], [], []
    with db:
        for sid, r in zip(seeds, res):
            if isinstance(r, Exception):
                errors.append(_err(f"citing {sid}", r))
                continue
            for it in r.get("results", []):
                n = _cg_node(it)
                if n["id"] not in current and maps.upsert(db, m["id"], n, "candidate", f"watch:{today}") == "added":
                    current[n["id"]] = n
                    new.append({k: n.get(k) for k in ("id", "doi", "title", "first_author", "year")})
        maps.touch(db, m["id"])
    maps.set_watched(db, m["id"], maps.now())
    return {"status": "ok", "map": m["name"], "since": since, "new": new, "source_errors": errors}


@_tool()
async def map_refresh(map: str, new_citing: bool = False, per_seed: int = 25) -> dict:
    """Fetch fresh metadata from OpenAlex for every work in a map (citation counts, momentum,
    topic, retraction flag, reference lists), 50 works per request. States, roles and notes are
    kept. Also merges duplicate records, and for each seed stores the OpenCitations citation
    count (cited_by_oc); 'coverage_gaps' lists seeds OpenAlex counts much lower than
    OpenCitations does, where citing works may be missing from the map. With new_citing=True it first adds, as candidates with
    state 'new' and found_by 'watch:<date>', the works citing the seeds since the last such
    check (or since the map was created), up to per_seed per seed: 'new' and 'since' report
    them. Run it when you come back to a map; it is a check on demand, not an alert."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    watched = await _map_watch(maps, db, m, per_seed) if new_citing else {}
    if watched.get("status") == "no_seeds":
        return watched
    nodes = maps.nodes(db, m["id"])
    found, errors = await _oa_batch("openalex", list(nodes))
    with db:
        for n in found:
            if n["id"] in nodes:
                old = nodes[n["id"]]
                if "zotero_pdf" in (old.get("refs_source") or ""):  # keep the references read from the PDF
                    n["refs"] = list(dict.fromkeys(n["refs"] + old["refs"]))
                    n["refs_source"] = old["refs_source"]
                maps.upsert(db, m["id"], n, old["role"], old["found_by"][0] if old["found_by"] else "refresh")
        maps.touch(db, m["id"])
    merged = _merge_duplicates(maps, db, m["id"])
    # the OpenCitations count for every seed with a DOI: a gap shows where OpenAlex misses citations
    gaps, seeds = [], [k for k, n in nodes.items() if n["role"] == "seed" and n.get("doi")][:25]
    for sid in seeds:
        try:
            oc = await _oc_count(nodes[sid]["doi"], "citations")
        except httpx.HTTPError as e:
            errors.append(_err("opencitations", e))
            continue
        if oc is None:
            continue
        with db:
            db.execute("UPDATE nodes SET cited_by_oc = ? WHERE map_id = ? AND work_id = ?", (oc, m["id"], sid))
        oa = next((n["cited_by"] for n in found if n["id"] == sid), nodes[sid].get("cited_by")) or 0
        if oc >= 10 and oc >= 1.25 * oa:
            gaps.append({"id": sid, "title": nodes[sid].get("title"), "openalex_cited_by": oa, "opencitations_cited_by": oc})
    return {"status": "ok", "map": m["name"], "refreshed": len(found), "works": len(nodes),
            "duplicates_merged": len(merged), "coverage_gaps": gaps,
            "source_errors": errors + watched.get("source_errors", []),
            **{k: watched[k] for k in ("since", "new") if k in watched}}


@_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
async def map_prune(map: str, keep: int = 300) -> dict:
    """Shrink a map: keep the seeds, every work marked to_read, read or dismissed (so a dismissed
    work stays dismissed), and the `keep` candidates most linked to the seeds; remove the other
    new candidates. Also merges duplicate records. Ask the user before calling."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    merged = _merge_duplicates(maps, db, m["id"])
    nodes = maps.nodes(db, m["id"])
    _map_view(nodes)
    loose = sorted((n for n in nodes.values() if n["role"] == "candidate" and n["state"] == "new"),
                   key=lambda n: (n["seeds_linked"], n["seed_cocited"], n["seed_coupling"], n["cocited_total"],
                                  n["cited_by"] or 0), reverse=True)
    drop = [n["id"] for n in loose[max(0, int(keep)):]]
    removed = maps.remove(db, m["id"], drop) if drop else 0
    return {"status": "ok", "map": m["name"], "removed": removed, "duplicates_merged": len(merged),
            "works_in_map": len(nodes) - removed}


@_tool()
async def map_rename(map: str, new_name: str | None = None, description: str | None = None) -> dict:
    """Rename a map and/or change its description ('' clears it)."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
        maps.rename(db, m["id"], new_name, description)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    except ValueError as e:
        return {"status": "invalid_input", "detail": str(e)}
    except Exception as e:
        if "UNIQUE" in str(e):
            return {"status": "exists", "detail": f"a map named {new_name!r} already exists"}
        raise
    return {"status": "ok", "map": (new_name or m["name"]).strip()}


@_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
async def map_remove(map: str, ids: list[str]) -> dict:
    """Remove works from a map (OpenAlex ids or DOIs). Unlike map_mark 'dismissed', the work
    and its state are gone; a later map_expand may bring it back as new."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    wanted = _resolve_ids(maps.nodes(db, m["id"]), ids)
    return {"status": "ok", "map": m["name"], "removed": maps.remove(db, m["id"], wanted)}


@_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
async def map_delete(map: str, confirm_name: str) -> dict:
    """Delete a whole map with its works and reading states. Cannot be undone. confirm_name
    must repeat the map's name exactly; ask the user to confirm before calling."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    if confirm_name.strip() != m["name"]:
        return {"status": "not_confirmed", "detail": f"confirm_name must be exactly {m['name']!r}"}
    return {"status": "ok", "deleted": m["name"], "works": maps.delete(db, m["id"])}


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def map_export(map: str, states: list[MapState] | None = None) -> dict:
    """A map as a Markdown reading list (seeds, then works by state, with DOI links and
    whether each is in Zotero or has a note), to save in Obsidian with the Obsidian MCP's
    write_note outside the schema scope, e.g. 'Maps/<map name>.md'. states defaults to
    to_read and read."""
    maps, db = _map_db()
    try:
        m = maps.map_row(db, map)
    except KeyError as e:
        return {"status": "not_found", "detail": str(e)}
    nodes = maps.nodes(db, m["id"])
    _map_view(nodes)
    works = list(nodes.values())
    _knowledge().annotate(works)
    states = states or ["to_read", "read"]

    def line(n):
        ref = f"{n.get('first_author') or 'Unknown'} ({n.get('year') or 'n.d.'}). {n.get('title') or n['id']}."
        bits = [f"[DOI](https://doi.org/{n['doi']})" if n.get("doi") else None,
                f"[Espacenet](https://worldwide.espacenet.com/patent/search?q=pn%3D{n['id'][2:]})" if _is_patent(n["id"]) else None,
                f"[Zotero](zotero://select/library/items/{n['zotero_key']})" if n.get("zotero_key") else None,
                f"verified {n['verified']['date']}" if (n.get("verified") or {}).get("status") == "match" else None,
                ", ".join(f"[[{x['title']}]]" for x in n.get("notes", [])) or None,
                "**retracted**" if n.get("is_retracted") else None,
                n.get("note")]
        return "- " + ref + "".join(f" · {b}" for b in bits if b)

    title = {"to_read": "To read", "read": "Read", "new": "New", "dismissed": "Dismissed"}
    parts = [f"# {m['name']}", "", m["description"] or "", "",
             f"Literature map exported {maps.now()[:10]}; live view: {MAPS_PAGE}", "", "## Seeds", ""]
    parts += [line(n) for n in works if n["role"] == "seed"]
    for st in states:
        rows = sorted((n for n in works if n["role"] == "candidate" and n["state"] == st),
                      key=lambda n: (-n["seeds_linked"], -n["seed_cocited"], -(n["cited_by"] or 0)))
        if rows:
            parts += ["", f"## {title.get(st, st)}", ""] + [line(n) for n in rows]
    return {"status": "ok", "map": m["name"], "suggested_path": f"Maps/{m['name']}.md", "markdown": "\n".join(parts) + "\n"}


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def map_list() -> dict:
    """All literature maps with counts: seeds, works, to_read, read, dismissed.
    The user sees and manages maps on the 'Literature maps' page, which reads them live
    (timeline, network, suggestions, reading-state buttons): https://claude.ai/artifact/34ALXDYU74t27u6qmYnfUT . Give this link whenever
    a map is created, expanded or asked for; it is the only map page."""
    maps, db = _map_db()
    return {"status": "ok", "maps": maps.summary(db)}


# --- the three archives together: search, drafts, coverage, weekly report ---------------------

CorpusScope = Literal["all", "notes", "library", "annotations"]


def _corpus_db():
    import corpus
    db = corpus.connect()
    corpus.sync(db)
    return corpus, db


SEMANTIC_EMBEDDER = None  # tests inject an embedder here; otherwise semantic.load_embedder()


def _embedder():
    import semantic
    return SEMANTIC_EMBEDDER or semantic.load_embedder()


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def search_everything(query: str, scope: CorpusScope = "all", limit: int = 20,
                            zotero_key: str | None = None, semantic: bool = False) -> dict:
    """Search the user's own corpus, not the web: Obsidian notes (by section), the page texts of
    the PDFs and EPUBs in Zotero, and their highlights and comments. Results are passages with
    where they come from: a note path and heading, or a Zotero key with the page and a
    zotero://open-pdf link. Lexical search (words, "phrases", AND, OR, NOT, trailing * for
    prefixes; diacritics ignored): a query finds passages in the language it is written in, so
    search again in German or Italian for sources in those languages. zotero_key restricts to
    one item. The index follows the snapshot files written by the other two servers
    (notes_text.json, fulltext/KEY.json): an item whose PDF was never indexed with the Zotero
    MCP's index_fulltext is not searched; the log says how many sources are indexed.
    semantic=True searches by meaning instead (local multilingual embeddings: a query in one
    language finds passages in another; 'score' is the cosine similarity); it needs the model
    on the server (status 'semantic_unavailable' says what is missing) and only passages
    already embedded are reachable ('index.pending_embeddings')."""
    limit = max(1, min(int(limit), 100))
    corpus, db = _corpus_db()
    try:
        if semantic:
            import semantic as sem
            emb = _embedder()
            if emb is None:
                return {"status": "semantic_unavailable", "detail": sem.available()[1],
                        "hint": "lexical search still works: call again with semantic=false"}
            progress = await asyncio.to_thread(corpus.embed_pending, db, emb, 5.0)
            hits = await asyncio.to_thread(corpus.semantic_search, db, emb, query, scope, limit, zotero_key)
        else:
            progress = None
            hits = await asyncio.to_thread(corpus.search, db, query, scope, limit, zotero_key)
        st = corpus.stats(db)
    finally:
        db.close()
    out = {"status": "ok", "query": query, "scope": scope, "semantic": semantic, "results": hits, "returned": len(hits),
           "index": st, "note": ("passages by meaning (local embeddings); passages not yet embedded are not reachable"
                                 if semantic else "lexical index over notes, PDF pages and annotations; nothing from the web")}
    if progress:
        out["embedding_progress"] = progress
    return out


_CITE_GROUP = re.compile(r"\[([^\[\]]*@[^\[\]]+)\]")
_CITE_BARE = re.compile(r"(?<![\w@\[])-?@([A-Za-z][\w:.#$%&+?<>~/-]*[\w])")
_CITE_IN_GROUP = re.compile(r"-?@([A-Za-z][\w:.#$%&+?<>~/-]*[\w])\s*(?:,\s*)?([^;]*)")
_LOCATOR = re.compile(r"\b(?:pp?\.|p\b|pages?|§|chap\.|ch\.|sec\.|n\.)\s*\d|\b\d{1,4}\s*[-–]\s*\d{1,4}\b")
_QUOTE = re.compile(r"[\"“«„]([^\"”»“]{25,})[\"”»“]")


def _draft_citations(text: str) -> list[dict]:
    """Pandoc-style citations in a draft: [@key, p. 3; @other], @key [p. 3], -@key."""
    found = []
    spans = []
    for m in _CITE_GROUP.finditer(text):
        spans.append((m.start(), m.end()))
        for c in _CITE_IN_GROUP.finditer(m.group(1)):
            found.append({"key": c.group(1).rstrip(".,;:"), "locator": c.group(2).strip() or None, "pos": m.start(),
                          "has_locator": bool(_LOCATOR.search(c.group(2) or ""))})
    for m in _CITE_BARE.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        after = text[m.end():m.end() + 40]
        loc = re.match(r"\s*\[([^\]]*)\]", after)
        found.append({"key": m.group(1).rstrip(".,;:"), "locator": loc.group(1).strip() if loc else None, "pos": m.start(),
                      "has_locator": bool(loc and _LOCATOR.search(loc.group(1)))})
    return found


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def check_draft(text: str, retractions: bool = False) -> dict:
    """Check a manuscript's citations against the Zotero library before it leaves the desk. The
    draft uses Pandoc citekeys ([@cross1982designerly, p. 221], @key [p. 3], -@key): each is
    resolved to an item by Better BibTeX citation key ('Citation Key:' in Extra, carried by the
    library snapshot), by Zotero item key, or by surname+year when that names exactly one item.
    Reports: unresolved and ambiguous keys; resolved items without a 'Verified: … match' line
    (verify them with verify_doi / verify_book and record it with the Zotero MCP's
    set_verification); quotations of 25+ characters cited without a page; with
    retractions=True, the resolved DOIs are checked on Crossref. 'bibliography_keys' are the
    Zotero keys to pass to export_items for the reference list. The check is offline except
    for retractions; it does not judge whether a source supports a claim (check_claims does)."""
    k = _knowledge()
    if not k.library():
        return {"status": "no_snapshot", "detail": "library.json is missing: refresh_library_snapshot in the Zotero MCP"}
    cites = _draft_citations(text)
    if not cites:
        return {"status": "ok", "citations": 0, "detail": "no [@citekey] citations found in the text", "unresolved": [],
                "ambiguous": [], "unverified": [], "quotes_without_page": [], "bibliography_keys": []}
    resolved: dict[str, dict] = {}
    unresolved, ambiguous = [], []
    for c in cites:
        if c["key"] in resolved or c["key"] in unresolved or c["key"] in ambiguous:
            continue
        it, how = k.resolve_citekey(c["key"])
        if it:
            resolved[c["key"]] = {"key": it["key"], "title": it.get("title"), "year": it.get("year"), "doi": it.get("doi"),
                                  "resolved_by": how, "verified": (it.get("verified") or {}).get("status")}
        elif how == "ambiguous":
            ambiguous.append(c["key"])
        else:
            unresolved.append(c["key"])
    quotes = []
    for q in _QUOTE.finditer(text):
        # the citation that follows the quotation, within a sentence's reach
        after = [c for c in cites if q.end() <= c["pos"] <= q.end() + 120]
        if after:
            first = min(after, key=lambda c: c["pos"])
            if not first["has_locator"]:
                quotes.append({"quote": q.group(1)[:80] + ("…" if len(q.group(1)) > 80 else ""), "citation": first["key"]})
    out = {"status": "ok", "citations": len(cites), "distinct_keys": len({c["key"] for c in cites}),
           "resolved": resolved, "unresolved": unresolved, "ambiguous": ambiguous,
           "unverified": [{"citekey": ck, **{x: v[x] for x in ("key", "title")}} for ck, v in resolved.items() if v["verified"] != "match"],
           "quotes_without_page": quotes,
           "bibliography_keys": sorted({v["key"] for v in resolved.values()})}
    if retractions:
        dois = sorted({v["doi"] for v in resolved.values() if v.get("doi")})
        out["retractions"] = await check_retractions(dois=dois) if dois else {"status": "ok", "detail": "no DOIs to check"}
    out["summary"] = (f"{len(cites)} citations, {len(resolved)} keys resolved, {len(unresolved)} unresolved, "
                      f"{len(ambiguous)} ambiguous, {len(out['unverified'])} unverified, {len(quotes)} quotes without page")
    return out


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def coverage_report(project: str | None = None, collection: str | None = None) -> dict:
    """What a project still lacks across the three archives, from the snapshots: the Zotero items
    tagged cited/<project> (or in a Zotero collection; neither: the whole library) without a
    reading note, without a PDF, or without a 'Verified: … match' line; the reading notes with
    no Zotero item in their reference; notes pointing to an item that no longer exists. The
    weekly job writes the same per project to the vault inbox."""
    return await asyncio.to_thread(_knowledge().coverage, project, collection)


@_tool(annotations=ToolAnnotations(readOnlyHint=True))
async def weekly_report(run_now: bool = False) -> dict:
    """The latest weekly report (new works citing the seeds of each map, retractions in the
    library, coverage per project, maintenance) as Markdown, with the date it was generated.
    run_now=True runs the job immediately (minutes: it calls OpenAlex and Crossref for every map
    and DOI; no backup, no harvest) instead of waiting for the timer."""
    import weekly
    if run_now:
        results = await weekly.run(sys.modules[__name__], _knowledge(), do_backup=False, do_harvest=False)
        path = results["report"]
    else:
        try:
            path = json.load(open(weekly.LAST, encoding="utf-8")).get("report")
        except (OSError, ValueError):
            return {"status": "not_found", "detail": "no weekly report yet: the timer has not run; run_now=True runs it"}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return {"status": "not_found", "detail": f"report file missing: {path}"}
    return {"status": "ok", "report": path, "markdown": text,
            "note": "the Obsidian MCP copies new reports into the vault inbox once an hour"}


PROMPT_RULES = ("Regole: allega un DOI solo se find_doi restituisce `match` e verify_doi "
                "`registered_match`; non fornire mai un DOI a memoria. Se `is_retracted` è vero, "
                "segnalalo. Riporta solo correzioni, campi non verificati, DOI mancanti e "
                "ritrattazioni, non i controlli andati a buon fine.")


FROM_CONTEXT = ("Se un dato manca, ricavalo dalla conversazione; chiedimelo solo se non puoi "
                "procedere. Se ho dato titoli invece di DOI, risolvili prima con find_doi e usa "
                "solo quelli con esito `match`, dicendomi quali non hai trovato.")


def _opt(text: str, empty: str) -> str:
    return text.strip() or empty


@mcp.prompt(name="verifica_riferimenti", title="Verifica riferimenti")
def prompt_verify_references(
    references: Annotated[str, Field(description="Un riferimento come lo ricordi, un DOI o una "
                                     "bibliografia incollata; lascia vuoto per tutti quelli "
                                     "citati in chat")] = "",
) -> str:
    target = (f"Riferimenti da verificare (anche incompleti o imprecisi):\n\n{references}"
              if references.strip() else
              "Verifica tutti i riferimenti citati in questa conversazione.")
    return (f"{target}\n\nPer ogni voce:\n"
            "- articolo senza DOI: ricava titolo, primo autore e anno e cerca il DOI con "
            "find_doi; se ci sono più candidati plausibili, mostrameli invece di sceglierne uno;\n"
            "- articolo con DOI: controllalo con verify_doi passando titolo, autore, anno, "
            "volume, numero e pagine attesi;\n"
            "- libro: find_doi con is_book=true e l'anno dell'edizione citata, poi verify_book "
            "per editore e anno.\n"
            "Con una sola voce dammi il riferimento APA (format_apa se c'è un DOI). Con più voci "
            "restituisci una tabella con voce, esito (ok / corretta / DOI aggiunto / DOI "
            "mancante / non verificata / ritrattata) e cosa è cambiato, poi la lista APA "
            "aggiornata.\n\n" + PROMPT_RULES)


@mcp.prompt(name="ricerca_letteratura", title="Ricerca letteratura")
def prompt_search(
    topic: Annotated[str, Field(description="Cosa cerchi, con parole tue")],
    years: Annotated[str, Field(description="Facoltativo: intervallo di anni, es. 2015-2025")] = "",
) -> str:
    return ("Cerco letteratura su questo tema. Costruisci tu la query booleana e scegli i "
            "database adatti al campo (per esempio openalex e arxiv per HCI e AI, europepmc "
            "per psicologia, openalex e hal per scienze umane). Usa search_literature e "
            "riassumi i risultati più pertinenti in una tabella (autori, anno, titolo, sede, "
            "DOI, una riga sul contenuto). Riporta query, database, filtri e numero di "
            "risultati dal log, così posso rifare la ricerca.\n\n"
            f"Tema: {topic}\nAnni: {_opt(years, 'nessun limite')}")


@mcp.prompt(name="mappa_citazioni", title="Mappa delle citazioni")
def prompt_citation_map(
    papers: Annotated[str, Field(description="DOI o titoli, uno per riga; lascia vuoto per "
                                 "i lavori di cui stiamo parlando")] = "",
) -> str:
    return ("Chiama citation_graph con questi lavori come seed, poi costruisci un artifact "
            "D3 con:\n"
            "1. due viste commutabili: timeline (x = anno, y = citazioni in scala log, frecce per "
            "citation_edges, seed evidenziati, colore per field) e rete di somiglianza "
            "(force layout con selettore tra cocitation e coupling, spessore dei link = peso, "
            "dimensione dei nodi = seeds_linked, nodi isolati nascosti, nodi trascinabili);\n"
            "2. zoom su entrambe con d3.zoom: rotella o pinch per zoomare, trascinamento per "
            "spostarsi, doppio clic e un pulsante per tornare alla vista iniziale; sulla "
            "timeline riscala gli assi invece di ingrandire i punti, e mostra i titoli solo "
            "per i seed o oltre un certo livello di zoom;\n"
            "3. tooltip con titolo, primo autore, anno, citazioni e link al DOI; clic su un nodo "
            "per evidenziare i suoi vicini;\n"
            "4. una nota breve: le distanze nella rete non hanno valore metrico e le citazioni "
            "favoriscono i lavori più vecchi.\n"
            "Dopo la mappa elenca le possibili letture mancanti, ordinate per seeds_linked, "
            "seed_cocited e seed_coupling, e chiedimi se voglio la tabella dei dettagli.\n\n"
            f"{FROM_CONTEXT}\n\nLavori:\n{_opt(papers, 'quelli di cui stiamo parlando')}")


@mcp.prompt(name="tabella_paper", title="Tabella dei paper")
def prompt_paper_table(
    papers: Annotated[str, Field(description="DOI o titoli, uno per riga; lascia vuoto per "
                                 "i lavori della mappa o della ricerca precedente")] = "",
) -> str:
    return ("Chiama paper_table con questi lavori (usa gli id dei nodi se vengono da una "
            "mappa). Mostra una tabella compatta: autori, anno, titolo, sede, citazioni, "
            "TL;DR o prima frase dell'abstract, keyword principali, accesso aperto. Segnala i "
            "lavori ritrattati e quelli non trovati. Abstract completi solo se li chiedo.\n\n"
            f"{FROM_CONTEXT}\n\nLavori:\n"
            f"{_opt(papers, 'quelli della mappa o della ricerca precedente')}")


def _status() -> dict:
    """Operator view for GET /status (loopback only): maps, snapshots, corpus, PhilArchive copy,
    last weekly run. No credentials, no secrets."""
    out: dict = {"service": "paper-doi-mcp", "version": VERSION, "gateway": GATEWAY_ENABLED, "patents": EPO_CONFIGURED}
    try:
        maps, db = _map_db()
        out["maps"] = len(maps.summary(db))
    except Exception as e:  # noqa: BLE001
        out["maps"] = f"error: {e}"
    out["snapshots"] = _knowledge().status()
    try:
        import corpus
        cdb = corpus.connect()
        out["corpus"] = corpus.stats(cdb)
        cdb.close()
        import semantic
        ok, why = semantic.available()
        out["semantic"] = {"available": ok, "detail": why, "model_dir": semantic.MODEL_DIR}
    except Exception as e:  # noqa: BLE001
        out["corpus"] = f"error: {e}"
    try:
        import philarchive
        pdb = philarchive.connect()
        out["philarchive"] = philarchive.count(pdb)
        pdb.close()
    except Exception as e:  # noqa: BLE001
        out["philarchive"] = f"error: {e}"
    try:
        import weekly
        last = json.load(open(weekly.LAST, encoding="utf-8"))
        out["weekly"] = {"date": last.get("date"), "report": last.get("report"),
                         "errors": [k for k, v in last.items() if isinstance(v, dict) and "error" in v]}
    except (OSError, ValueError):
        out["weekly"] = None
    return out


class _TokenAuth:
    """Authorization: Bearer <token> on every request. A token in the URL is refused: URLs end up
    in proxy logs and browser histories (Claude web sends the header from the connector's
    "Request headers"). GET /status answers loopback clients without credentials."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from starlette.responses import JSONResponse
            if scope.get("path") == "/status" and scope.get("method") == "GET":
                client = (scope.get("client") or ("", 0))[0]
                if client in ("127.0.0.1", "::1"):
                    await JSONResponse(_status())(scope, receive, send)
                else:
                    await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
                return
            header = dict(scope["headers"]).get(b"authorization", b"")
            bearer = header[7:] if header[:7].lower() == b"bearer " else b""
            if not hmac.compare_digest(bearer, self.token):
                await JSONResponse({"error": "unauthorized"}, status_code=401,
                                   headers={"WWW-Authenticate": 'Bearer realm="paper-doi-mcp"'})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main() -> None:
    if os.environ.get("MCP_TRANSPORT", "stdio") != "http":
        mcp.run()
        return
    import uvicorn
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("MCP_AUTH_TOKEN of at least 32 characters is required in http mode")
    uvicorn.run(_TokenAuth(mcp.streamable_http_app(), token), host="127.0.0.1",
                port=int(os.environ.get("PORT", "3002")), access_log=False)


if __name__ == "__main__":
    main()

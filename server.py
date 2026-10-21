"""MCP server: verify citations (DOIs, book editions), search the literature, map citations.

Sources: Crossref, OpenAlex, Semantic Scholar, Open Library, DNB, doi.org and open
repositories (Europe PMC, HAL, DOAJ, arXiv, OSF, Zenodo). See README.md.
Env (optional):
  CONTACT_EMAIL            used for Crossref/OpenAlex polite pool
    OPENALEX_API_KEY         authenticates OpenAlex requests via api_key
    OPENALEX_ENABLE_WRITES   enables confirmed OpenAlex account writes; default false
    SEMANTIC_SCHOLAR_API_KEY authenticates requests; 1 request/s across all endpoints
  MCP_TRANSPORT            'stdio' (default) or 'http' (streamable HTTP on 127.0.0.1)
  PORT                     HTTP port, default 3002
  MCP_AUTH_TOKEN           required in http mode, as Bearer header or ?token= query
  MCP_ALLOWED_HOSTS        comma-separated public host names allowed behind a proxy
"""

import asyncio
import hmac
import html
import json
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Annotated, Any, Literal, get_args
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

VERSION = "0.2.0"
_PUBLIC_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
mcp = FastMCP("paper-doi", stateless_http=True, transport_security=TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *_PUBLIC_HOSTS],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                     *(f"https://{h}" for h in _PUBLIC_HOSTS)]))

EMAIL = os.environ.get("CONTACT_EMAIL", "")
OA_KEY = os.environ.get("OPENALEX_API_KEY", "")
OA_WRITES_ENABLED = os.environ.get("OPENALEX_ENABLE_WRITES", "").strip().lower() == "true"
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
    CONCURRENCY = {"api.semanticscholar.org": 1, "export.arxiv.org": 1,
                   "api.openalex.org": 1, "user.openalex.org": 1}
    MIN_INTERVAL = {"api.semanticscholar.org": 1.0, "export.arxiv.org": 3.0,
                    "api.openalex.org": 1.0, "user.openalex.org": 1.0}

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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def openalex_entity(entity: OpenAlexEntity, entity_id: str | None = None,
                          params: dict[str, str | int | float | bool] | None = None) -> dict:
    """Read OpenAlex entity lists or a single entity, with native query parameters.

    Covers works, authors, sources, institutions, publishers, funders, awards,
    locations, raw-affiliation-strings, topics, subfields, fields, domains, keywords,
    sdgs, study-designs, concepts (deprecated), and vocabulary endpoints.
    Omit entity_id for a list; provide an OpenAlex ID or supported external ID for
    details (DOI/PMID for works, ORCID for authors, ROR for institutions, ISSN for
    sources). Identifiers are URL-encoded, never interpreted as a destination URL.
    raw-affiliation-strings has no single-entity identifier; use its list filters.

    params forwards native filter, search, sort, group_by, select, page, per_page,
    cursor, sample, seed, corpus and other API-supported options without rewriting.
    Endpoint-specific validity is checked by OpenAlex. For deep pagination start
    with cursor='*', then pass data.meta.next_cursor. Each call fetches one page.
    Response data preserves metadata, groups, nested fields and request cost; API
    credentials are redacted. Uses OPENALEX_API_KEY and CONTACT_EMAIL automatically.
    Requests can consume your OpenAlex budget; no bulk auto-pagination or downloads.
    """
    if entity not in _OA_ENTITIES:
        raise ValueError("Unknown OpenAlex entity")
    path = entity
    if entity_id is not None:
        if entity in _OA_LIST_ONLY:
            raise ValueError(f"{entity} only supports list queries")
        path += "/" + _oa_segment(entity_id)
    return await _oa_api(path, params)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def openalex_autocomplete(
    query: str,
    entity: Literal["works", "authors", "sources", "institutions", "topics", "keywords",
                    "concepts", "publishers", "funders"] | None = None,
    params: dict[str, str | int | bool] | None = None,
) -> dict:
    """GET /autocomplete or /autocomplete/{entity}; query becomes q.

    Returns native typeahead matches, including hints and identifier lookups.
    params can add filter/search; do not pass q there. concepts is deprecated.
    Uses the server's OpenAlex credentials and shared conservative 1 request/s limit.
    """
    allowed = {"works", "authors", "sources", "institutions", "topics", "keywords",
               "concepts", "publishers", "funders"}
    if not query.strip() or "q" in (params or {}) or (entity is not None and entity not in allowed):
        raise ValueError("Provide query, an optional supported entity, and no params.q")
    return await _oa_api("autocomplete" + (f"/{entity}" if entity else ""),
                         {**(params or {}), "q": query})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def openalex_query(
    action: Literal["execute", "translate", "validate", "context", "properties", "spec"],
    query: str | dict | None = None, query_format: Literal["oql", "oqo", "oxurl"] = "oql",
    method: Literal["GET", "POST"] = "POST", entity: OpenAlexEntity | None = None,
    artifact: str | None = None, params: dict[str, str | int | bool] | None = None,
) -> dict:
    """Execute/translate OQL and OQO and inspect the OpenAlex query language.

    execute: GET or POST / with oql string or oqo object. oxurl is translation-only;
    use openalex_entity for REST queries. params holds sibling view options (sort,
    select, page, per_page, cursor, rerank, format). Fetches one page; retains
    meta.next_cursor, validation diagnostics, costs and meta.x_query.
    translate: GET /query/{query_format}/{query} or POST /query; accepts oql,
    oqo or oxurl (classic URL). Translates and validates WITHOUT executing a search.
    validate: GET /validate?q=<oql>; context: GET /parse-context?q=<oql>&pos=<n>.
    properties: GET /properties or /properties/{entity} (queryable column registry).
    spec: GET /query/spec or /query/spec/{artifact}, e.g. artifact='oqo'.
    validate/context require an OQL string; context position goes in params.pos.
    Execution consumes the account's query budget; translation is free. JSON and
    CSV/text responses are supported; binary archive exports are not decoded.
    """
    if method not in {"GET", "POST"} or query_format not in {"oql", "oqo", "oxurl"}:
        raise ValueError("Invalid query method or format")
    if entity is not None and (entity not in _OA_ENTITIES or action != "properties"):
        raise ValueError("entity is only supported for properties")
    if artifact is not None and action != "spec":
        raise ValueError("artifact is only supported for spec")
    params = dict(params or {})
    if {"oql", "oqo", "oxurl", "q"} & params.keys():
        raise ValueError("Pass query using the query argument, not params")
    if action in {"properties", "spec"}:
        if query is not None:
            raise ValueError("This action does not accept query")
        path = "properties" if action == "properties" else "query/spec"
        suffix = entity if action == "properties" else artifact
        return await _oa_api(path + ("/" + _oa_segment(suffix) if suffix else ""), params)
    if query is None or (isinstance(query, str) and not query.strip()):
        raise ValueError("query is required")
    if action in {"validate", "context"}:
        if query_format != "oql" or not isinstance(query, str):
            raise ValueError("validate/context require OQL text")
        path = "validate" if action == "validate" else "parse-context"
        if action == "context" and "pos" not in params:
            raise ValueError("context requires params.pos")
        return await _oa_api(path, {**params, "q": query})
    if query_format == "oqo" and not isinstance(query, dict):
        raise ValueError("oqo requires a JSON object")
    if query_format != "oqo" and not isinstance(query, str):
        raise ValueError("oql/oxurl require text")
    if action not in {"execute", "translate"} or (action == "execute" and query_format == "oxurl"):
        raise ValueError("Unsupported query action or execution format")
    path = "" if action == "execute" else "query"
    if method == "POST":
        return await _oa_api(path, params, "POST", {query_format: query})
    encoded_query = json.dumps(query) if isinstance(query, dict) else query
    if action == "translate":
        path += f"/{query_format}/{_oa_segment(encoded_query)}"
        return await _oa_api(path, params)
    return await _oa_api(path, {**params, query_format: encoded_query})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                     idempotentHint=False))
async def openalex_account_write(
    action: Literal["create_collection", "update_collection", "delete_collection",
                    "add_members", "remove_members", "remove_member", "import_collection",
                    "create_saved_search", "update_saved_search", "delete_saved_search", "curate"],
    body: dict | list[dict] | None = None, collection_id: str | None = None,
    member_id: str | None = None, saved_search_id: str | None = None,
    params: dict[str, str | int | bool] | None = None, confirm: bool = False,
) -> dict:
    """Modify OpenAlex account data. Defaults to preview ONLY; never silently confirm.

    Show the exact operation/body and obtain explicit user approval BEFORE calling
    with confirm=true. Requires the server's PERSONAL OPENALEX_API_KEY and appropriate
    ownership/curator permissions. Successful writes are not automatically retried.

    create_collection: POST /collections; body display_name, entity_type, optional
    description, access, member_ids (max 10000); or copy_of to copy a collection.
    update_collection: PATCH /collections/{collection_id}; display_name/description/access.
    delete_collection: DELETE that path; permanently removes collection and members.
    add_members: POST /collections/{collection_id}/members with body.member_ids.
    remove_members: DELETE same path, params.member_ids comma-string (max 100), NO body.
    remove_member: DELETE /collections/{collection_id}/members/{member_id}, NO body.
    import_collection: POST /collections/{collection_id}/imports with exactly one of
    body.query (OpenAlex search URL), body.oql, body.copy_of; optional exclude_ids.
    Import executes server-side, can CONSUME QUERY CREDITS and add up to 1000000 members.
    Inspect progress with openalex_account_read; no polling or import is automatic.

    create_saved_search: POST /saved-searches with body.name, body.url, optional
    description and alert={frequency:daily|weekly|monthly}; sends future account emails.
    update_saved_search: PATCH /saved-searches/{saved_search_id}; same fields optional,
    alert=null disables emails. delete_saved_search: DELETE path; also deletes its alert.

    curate: POST https://user.openalex.org/curations using Bearer authentication;
    body is a correction object or list of objects, each with entity, entity_id,
    property, action, value. Correct authorships, display_name, full_name or orcid.
    Only submit corrections the user has reviewed; changes can affect public records.
    The API validates permissions and correction syntax. No admin/legacy routes.
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
            "author": [{"family": c.get("familyName") or c.get("name", "")}
                       for c in a.get("creators") or []],
            "issued": {"date-parts": [[a.get("publicationYear")]]},
            "container-title": [cont["title"]] if cont.get("title") else [],
            "type": (a.get("types") or {}).get("resourceTypeGeneral"),
            "volume": cont.get("volume"), "issue": cont.get("issue"), "page": pages,
            "publisher": pub.get("name") if isinstance(pub, dict) else pub, "updated-by": []}


@mcp.tool()
async def verify_doi(doi: str, expected_title: str | None = None,
                     expected_first_author: str | None = None, expected_year: int | None = None,
                     expected_volume: str | None = None, expected_issue: str | None = None,
                     expected_pages: str | None = None) -> dict:
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
    'is_retracted' and 'updates' report retractions, corrections and expressions of concern
    registered in Crossref (including Retraction Watch data): a retracted work may still
    be cited, but the reference must say so. DataCite has no retraction source: for those
    DOIs 'is_retracted' is null (unknown), never false, unless the title says RETRACTED.
    """
    d = _clean_doi(doi)
    if not d:
        return {"status": "invalid_format", "doi": doi}
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
                                  "so title, authors and year are unverified"}
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


@mcp.tool()
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


def _s2_segment(value: str | None, name: str) -> str:
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
        path = f"graph/v1/paper/{_s2_segment(paper_id, 'paper_id')}"
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
        path = f"graph/v1/author/{_s2_segment(author_id, 'author_id')}"
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def semantic_scholar_recommendations(
    action: Literal["single", "multi"], paper_id: str | None = None,
    positive_paper_ids: list[str] | None = None,
    negative_paper_ids: list[str] | None = None,
    params: dict[str, str | int] | None = None,
) -> dict:
    """Access both Recommendations API endpoints, preserving recommendedPapers.

    single: GET /recommendations/v1/papers/forpaper/{paper_id}; requires paper_id.
    params supports from ('recent' or 'all-cs'), limit (max 500), fields.
    multi: POST /recommendations/v1/papers/; requires positive_paper_ids and accepts
    optional negative_paper_ids to steer recommendations away from unwanted papers.
    params supports limit (max 500), fields; 'from' is only supported by single.
    IDs use the API-supported paper identifier formats, e.g. DOI:10.x/y.
    One call, no automatic pagination. Recommendations are not verified citations.
    Shares the Semantic Scholar 1 request/s limit, authentication and retries.
    """
    if action == "single":
        if positive_paper_ids is not None or negative_paper_ids is not None:
            raise ValueError("single uses paper_id, not positive/negative paper lists")
        return await _s2_api(
            f"recommendations/v1/papers/forpaper/{_s2_segment(paper_id, 'paper_id')}", params)
    if action != "multi":
        raise ValueError("Unknown recommendations action")
    if paper_id is not None or not positive_paper_ids:
        raise ValueError("multi requires positive_paper_ids, not paper_id")
    if any(not value.strip() for value in [*positive_paper_ids, *(negative_paper_ids or [])]):
        raise ValueError("Paper identifiers must not be empty")
    if "from" in (params or {}):
        raise ValueError("params.from is only supported by single")
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
        path = f"datasets/v1/release/{_s2_segment(release_id, 'release_id')}"
    elif action == "dataset":
        path = (f"datasets/v1/release/{_s2_segment(release_id, 'release_id')}"
                f"/dataset/{_s2_segment(dataset_name, 'dataset_name')}")
    elif action == "diffs":
        path = (f"datasets/v1/diffs/{_s2_segment(start_release_id, 'start_release_id')}"
                f"/to/{_s2_segment(release_id, 'release_id')}"
                f"/{_s2_segment(dataset_name, 'dataset_name')}")
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
    "dataset_releases": ("dataset", "releases"), "dataset_release": ("dataset", "release"),
    "dataset_files": ("dataset", "dataset"), "dataset_diffs": ("dataset", "diffs"),
}
_S2_ARGS = {"paper": {"paper_id", "ids"}, "author": {"author_id", "ids"},
            "snippets": {"query"}, "dataset": {"release_id", "dataset_name", "start_release_id"}}
S2Action = Literal[
    "paper_details", "paper_authors", "paper_citations", "paper_references", "paper_batch",
    "paper_search", "paper_bulk", "paper_match", "paper_autocomplete", "author_details",
    "author_papers", "author_search", "author_batch", "snippets", "dataset_releases",
    "dataset_release", "dataset_files", "dataset_diffs"]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def semantic_scholar(
    action: S2Action, paper_id: str | None = None, author_id: str | None = None,
    ids: list[str] | None = None, query: str | None = None, release_id: str | None = None,
    dataset_name: str | None = None, start_release_id: str | None = None,
    params: dict[str, str | int] | None = None,
) -> dict:
    """Semantic Scholar Academic Graph and Datasets; native JSON, one page per call.

    Returns status, http_status and data exactly as the API sent them. Recommendations
    has its own tool. Actions and the arguments each one needs:
    - paper_details, paper_authors, paper_citations, paper_references: paper_id (S2 hash,
      CorpusId:123, DOI:10.x/y, ARXIV:2401.01234, PMID:123...).
    - paper_batch: ids (1-500); papers not found stay null in the list.
    - paper_search (plain text, limit up to 100), paper_bulk (Boolean + | - and "phrases",
      sort such as citationCount:desc, token pagination, up to 1000 per page),
      paper_match (closest title: a candidate, NOT a verified match; use find_doi and
      verify_doi), paper_autocomplete: all take params.query.
    - author_details, author_papers: author_id (a Semantic Scholar author ID, not a name).
      author_search: params.query. author_batch: ids (1-1000).
    - snippets: query (plain text); params: paperIds, authors (max 10, AND), year,
      fields, limit up to 1000. An excerpt is not the full paper.
    - dataset_releases; dataset_release (release_id, default latest); dataset_files
      (+ dataset_name); dataset_diffs (start_release_id, release_id, dataset_name).
      Only metadata and temporary signed links come back: no file is downloaded.
      Find dataset names with dataset_release.
    params uses the API's own names: fields (nested allowed, e.g. citations.title), year,
    publicationDateOrYear, venue, fieldsOfStudy, minCitationCount, openAccessPdf (empty
    string), offset and limit (up to 1000 on lists). For the next page pass data.next as
    offset, or data.token as token for paper_bulk. Arguments an action does not use are
    rejected. Shares the 1 request/s limit, the API key and the 429 retries.
    """
    if action not in _S2_ACTIONS:
        raise ValueError("Unknown Semantic Scholar action")
    family, op = _S2_ACTIONS[action]
    given = {name for name, value in {
        "paper_id": paper_id, "author_id": author_id, "ids": ids, "query": query,
        "release_id": release_id, "dataset_name": dataset_name,
        "start_release_id": start_release_id}.items() if value is not None}
    if given - _S2_ARGS[family]:
        raise ValueError(f"{action} does not accept: {', '.join(sorted(given - _S2_ARGS[family]))}")
    if family == "paper":
        return await _s2_paper(op, paper_id=paper_id, ids=ids, params=params)
    if family == "author":
        return await _s2_author(op, author_id=author_id, ids=ids, params=params)
    if family == "snippets":
        return await _s2_snippets(query or "", params)
    if params:
        raise ValueError("dataset actions take no params")
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
    (broad, TL;DRs; free-text only; 1 request/s across endpoints, also with API key), hal (French open
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


CG_SELECT = ("id,doi,title,publication_year,cited_by_count,authorships,primary_topic,"
             "referenced_works")


def _oa_id(url: str | None) -> str | None:
    return url.rsplit("/", 1)[-1].upper() if url else None


def _cg_node(it: dict) -> dict:
    auth = it.get("authorships") or []
    return {"id": _oa_id(it.get("id")), "doi": _clean_doi(it.get("doi")), "title": it.get("title"),
            "first_author": (auth[0].get("author") or {}).get("display_name") if auth else None,
            "year": it.get("publication_year"), "cited_by": it.get("cited_by_count") or 0,
            "field": ((it.get("primary_topic") or {}).get("field") or {}).get("display_name"),
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


def _coverage_verdict(openalex: int, other: int) -> str:
    """Compare two counts of the same thing; 0.8 is a heuristic, definitions differ per source."""
    if not openalex and not other:
        return "both_empty"
    if not openalex:
        return "openalex_missing"
    if not other:
        return "semantic_scholar_missing"
    if min(openalex, other) / max(openalex, other) >= 0.8:
        return "concordant"
    return "openalex_lower" if openalex < other else "semantic_scholar_lower"


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


@mcp.tool()
async def citation_graph(seeds: list[str], include_references: bool = True,
                         include_citing: bool = True, citing_per_seed: int = 20,
                         max_nodes: int = 150, min_cocitation: int = 2,
                         min_coupling: int = 3, check_coverage: bool = True) -> dict:
    """Build a citation graph around seed works (DOIs or OpenAlex ids, max 10) for visualization.

    Nodes are the seeds, the works they cite (role 'reference') and the most-cited works
    citing them (role 'citing'), ranked by how many seeds they connect to, then by citation
    count, up to max_nodes. Each node has id, doi, title, first_author, year, cited_by
    (OpenAlex citation count), field (OpenAlex primary field), role, seeds_linked (how
    many seeds it cites or is cited by), seed_cocited (how often works in the graph cite it
    together with a seed) and seed_coupling (highest coupling strength with a seed). Rank
    likely missing readings by these three, in this order; with one seed seeds_linked is 1
    for every node, so the other two decide.
    'citation_edges' are [citing_id, cited_id] pairs inside the graph. 'cocitation' lists
    [id_a, id_b, n] where n works in this graph cite both; computed on this sample, not the
    whole literature, so n is a lower bound. 'coupling' lists [id_a, id_b, shared, strength]:
    the two works share 'shared' references in their full reference lists, and strength =
    shared / sqrt(refs_a * refs_b) in 0-1. Coupling links recent works that nobody has
    co-cited yet. Use paper_table with node ids for abstracts and keywords.

    To visualize, build a D3 artifact with two views: (1) timeline, x = year, y = cited_by
    on a log scale, arrows for citation_edges, seeds highlighted, colour by field;
    (2) similarity network, force layout with a toggle between cocitation (weight n) and
    coupling (weight strength), node size by seeds_linked, isolated nodes hidden.
    Enable d3.zoom on both (wheel/pinch to zoom, drag to pan,
    double-click or a button to reset); on the timeline rescale the axes instead of
    scaling the marks, and show titles only for seeds or once zoomed in.
    Tell the reader that distances in a force layout have no metric
    meaning and that citation counts favour older works. OpenAlex reference lists are
    incomplete for many humanities and older works: 'log.seeds_without_references' flags them.
    'coverage_check' (check_coverage=True, one Semantic Scholar batch request, seeds with a
    DOI only, never blocks the graph) compares each seed's reference and citation counts in
    OpenAlex and Semantic Scholar: 'both_empty' means two sources agree there is nothing to
    find; 'openalex_lower' or 'openalex_missing' means the graph under-counts that seed and
    its absences are coverage gaps, not facts. Counts are definitions that differ per
    source and both derive partly from Crossref, so 'concordant' is not independent proof.
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


@mcp.tool()
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


class _TokenAuth:
    """Bearer header or ?token= query: Claude web connectors cannot send custom headers."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            header = dict(scope["headers"]).get(b"authorization", b"")
            bearer = header[7:] if header[:7].lower() == b"bearer " else b""
            query = parse_qs(scope.get("query_string", b"").decode()).get("token", [""])[0].encode()
            if not (hmac.compare_digest(bearer, self.token) or hmac.compare_digest(query, self.token)):
                from starlette.responses import JSONResponse
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
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
    # Access log off: the token travels in the query string
    uvicorn.run(_TokenAuth(mcp.streamable_http_app(), token), host="127.0.0.1",
                port=int(os.environ.get("PORT", "3002")), access_log=False)


if __name__ == "__main__":
    main()

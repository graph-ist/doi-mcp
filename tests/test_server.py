import asyncio
from types import SimpleNamespace

import httpx
import pytest

import server as s


@pytest.mark.parametrize("api_key", ["", "test-key"])
@pytest.mark.parametrize("retry", [False, True])
def test_semantic_scholar_rate_limit_shared_across_endpoints(monkeypatch, api_key, retry):
    clock = [100.0]
    sent = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        clock[0] += delay
        await real_sleep(0)

    async def fake_request(transport, request):
        sent.append((clock[0], request.url.path, request.headers.get("x-api-key")))
        status_code = 429 if retry and len(sent) == 1 else 200
        return httpx.Response(status_code, headers={"Retry-After": "0"}, json={})

    monkeypatch.setattr(s, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(s.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(s, "S2_KEY", api_key)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_request)

    async def run():
        async with httpx.AsyncClient(transport=s._PoliteTransport()) as client:
            return await asyncio.gather(
                s._s2(client, "GET", "https://api.semanticscholar.org/graph/v1/paper/search/match"),
                s._s2(client, "GET", "https://api.semanticscholar.org/graph/v1/paper/search"),
                s._s2(client, "POST", "https://api.semanticscholar.org/graph/v1/paper/batch",
                      json={"ids": ["DOI:10.1000/test"]}),
                s._s2(client, "GET", "https://api.semanticscholar.org/recommendations/v1/papers/forpaper/123"),
                s._s2(client, "GET", "https://api.semanticscholar.org/datasets/v1/release/"),
            )

    responses = asyncio.run(run())
    assert all(response.status_code == 200 for response in responses)
    assert len(sent) == 5 + int(retry)
    assert len({path for _, path, _ in sent}) == 5
    assert all(later[0] - earlier[0] >= 1.0 for earlier, later in zip(sent, sent[1:]))
    assert all(key == (api_key or None) for _, _, key in sent)


@pytest.mark.parametrize("action,suffix", [
    ("details", "/DOI%3A10.1000%2Ftest"),
    ("authors", "/DOI%3A10.1000%2Ftest/authors"),
    ("citations", "/DOI%3A10.1000%2Ftest/citations"),
    ("references", "/DOI%3A10.1000%2Ftest/references"),
    ("batch", "/batch"), ("search", "/search"), ("bulk", "/search/bulk"),
    ("match", "/search/match"), ("autocomplete", "/autocomplete"),
])
def test_semantic_scholar_paper_routes(monkeypatch, action, suffix):
    async def handle(request):
        assert request.url.raw_path.split(b"?")[0].decode() == "/graph/v1/paper" + suffix
        assert request.method == ("POST" if action == "batch" else "GET")
        assert request.headers["x-api-key"] == "test-key"
        assert request.url.params["fields"] == "title,authors"
        if action == "batch":
            import json
            assert json.loads(request.content) == {"ids": ["DOI:10.1000/test"]}
        return httpx.Response(200, json={"data": [{"paperId": "test"}], "next": 100,
                                         "token": "next-page"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s, "S2_KEY", "test-key")
            kwargs = {"params": {"query": "design", "fields": "title,authors"}}
            if action in {"details", "authors", "citations", "references"}:
                kwargs["paper_id"] = "DOI:10.1000/test"
            elif action == "batch":
                kwargs["ids"] = ["DOI:10.1000/test"]
            return await s.semantic_scholar("paper_" + action, **kwargs)

    result = asyncio.run(run())
    assert result["status"] == "ok"
    assert result["data"]["next"] == 100
    assert result["data"]["token"] == "next-page"


@pytest.mark.parametrize("tool,kwargs,method,path,body", [
    (s.semantic_scholar, {"action": "author_details", "author_id": "123"},
     "GET", "/graph/v1/author/123", None),
    (s.semantic_scholar, {"action": "author_papers", "author_id": "123",
                          "params": {"offset": 100, "limit": 50}},
     "GET", "/graph/v1/author/123/papers", None),
    (s.semantic_scholar, {"action": "author_search", "params": {"query": "Smith"}},
     "GET", "/graph/v1/author/search", None),
    (s.semantic_scholar, {"action": "author_batch", "ids": ["123", "456"]},
     "POST", "/graph/v1/author/batch", {"ids": ["123", "456"]}),
    (s.semantic_scholar, {"action": "snippets", "query": "interaction design",
                          "params": {"limit": 3}},
     "GET", "/graph/v1/snippet/search", None),
    (s.semantic_scholar_recommendations, {"action": "single", "paper_id": "123",
                                         "params": {"from": "all-cs", "limit": 5}},
     "GET", "/recommendations/v1/papers/forpaper/123", None),
    (s.semantic_scholar_recommendations, {"action": "multi", "positive_paper_ids": ["123"],
                                         "negative_paper_ids": ["456"]},
     "POST", "/recommendations/v1/papers/",
     {"positivePaperIds": ["123"], "negativePaperIds": ["456"]}),
    (s.semantic_scholar, {"action": "dataset_releases"},
     "GET", "/datasets/v1/release/", None),
    (s.semantic_scholar, {"action": "dataset_release"},
     "GET", "/datasets/v1/release/latest", None),
    (s.semantic_scholar, {"action": "dataset_files", "dataset_name": "papers",
                          "release_id": "2026-10-01"},
     "GET", "/datasets/v1/release/2026-10-01/dataset/papers", None),
    (s.semantic_scholar, {"action": "dataset_diffs", "dataset_name": "papers",
                          "start_release_id": "2026-10-01"},
     "GET", "/datasets/v1/diffs/2026-10-01/to/latest/papers", None),
])
def test_semantic_scholar_other_routes(monkeypatch, tool, kwargs, method, path, body):
    import json

    async def handle(request):
        assert request.method == method
        assert request.url.path == path
        assert request.headers["x-api-key"] == "test-key"
        assert dict(request.url.params) == {
            key: str(value) for key, value in kwargs.get("params", {}).items()
        } | ({"query": kwargs["query"]} if "query" in kwargs else {})
        assert (json.loads(request.content) if request.content else None) == body
        return httpx.Response(200, json={"original": [None, {"license": "CC-BY"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s, "S2_KEY", "test-key")
            return await tool(**kwargs)

    result = asyncio.run(run())
    assert result == {"status": "ok", "http_status": 200,
                      "data": {"original": [None, {"license": "CC-BY"}]}}


@pytest.mark.parametrize("tool,kwargs", [
    (s.semantic_scholar, {"action": "paper_details"}),
    (s.semantic_scholar, {"action": "paper_details", "paper_id": ".."}),
    (s.semantic_scholar, {"action": "paper_search"}),
    (s.semantic_scholar, {"action": "paper_batch", "ids": []}),
    (s.semantic_scholar, {"action": "paper_batch", "ids": ["123"] * 501}),
    (s.semantic_scholar, {"action": "paper_batch", "ids": [" "]}),
    (s.semantic_scholar, {"action": "paper_search", "ids": ["123"]}),
    (s.semantic_scholar, {"action": "author_details"}),
    (s.semantic_scholar, {"action": "author_search"}),
    (s.semantic_scholar, {"action": "author_batch", "ids": ["123"] * 1001}),
    (s.semantic_scholar, {"action": "snippets"}),
    (s.semantic_scholar, {"action": "snippets", "query": " "}),
    (s.semantic_scholar, {"action": "snippets", "query": "design", "params": {"query": "other"}}),
    (s.semantic_scholar, {"action": "dataset_files"}),
    (s.semantic_scholar, {"action": "dataset_diffs", "dataset_name": "papers"}),
    (s.semantic_scholar, {"action": "dataset_release", "dataset_name": "papers"}),
    (s.semantic_scholar, {"action": "dataset_releases", "start_release_id": "2026-10-01"}),
    (s.semantic_scholar, {"action": "dataset_releases", "params": {"fields": "x"}}),
    (s.semantic_scholar, {"action": "paper_details", "paper_id": "1", "author_id": "2"}),
    (s.semantic_scholar, {"action": "author_details", "author_id": "1", "paper_id": "2"}),
    (s.semantic_scholar, {"action": "paper_search", "params": {"query": "x"}, "query": "y"}),
    (s.semantic_scholar, {"action": "paper_details", "paper_id": "1", "release_id": "latest"}),
    (s.semantic_scholar, {"action": "unknown"}),
    (s.semantic_scholar_recommendations, {"action": "single"}),
    (s.semantic_scholar_recommendations, {"action": "single", "paper_id": "123",
                                         "positive_paper_ids": ["456"]}),
    (s.semantic_scholar_recommendations, {"action": "multi"}),
    (s.semantic_scholar_recommendations, {"action": "multi", "positive_paper_ids": [" "]}),
    (s.semantic_scholar_recommendations, {"action": "multi", "positive_paper_ids": ["123"],
                                         "params": {"from": "recent"}}),
])
def test_semantic_scholar_rejects_invalid_inputs_before_network(monkeypatch, tool, kwargs):
    def unexpected_http():
        pytest.fail("Invalid input must not send a request")

    monkeypatch.setattr(s, "_http", unexpected_http)
    with pytest.raises(ValueError):
        asyncio.run(tool(**kwargs))


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 429, 500])
def test_semantic_scholar_preserves_api_errors(monkeypatch, status_code):
    attempts = []

    async def no_sleep(delay):
        pass

    async def handle(request):
        attempts.append(request)
        return httpx.Response(status_code, json={"error": "API error"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s.asyncio, "sleep", no_sleep)
            return await s.semantic_scholar("dataset_releases")

    assert asyncio.run(run()) == {"status": "error", "http_status": status_code,
                                 "data": {"error": "API error"}}
    assert len(attempts) == (4 if status_code == 429 else 1)


@pytest.mark.parametrize("failure", ["timeout", "non_json"])
def test_semantic_scholar_transport_errors(monkeypatch, failure):
    async def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        return httpx.Response(502, text="bad gateway")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            return await s.semantic_scholar("dataset_releases")

    result = asyncio.run(run())
    assert result["status"] == "error"
    assert "detail" in result


def test_semantic_scholar_mcp_tools_registered():
    tools = {tool.name: tool for tool in asyncio.run(s.mcp.list_tools())}
    for name in ("semantic_scholar", "semantic_scholar_recommendations"):
        assert tools[name].inputSchema["type"] == "object"
        assert tools[name].annotations.readOnlyHint is True
    for retired in ("paper", "author", "snippets", "datasets"):
        assert f"semantic_scholar_{retired}" not in tools
    enum = set(tools["semantic_scholar"].inputSchema["properties"]["action"]["enum"])
    assert len(enum) == 18 and enum == set(s._S2_ACTIONS)


@pytest.mark.parametrize("api_key", ["", "test-openalex-key"])
@pytest.mark.parametrize("route", ["doi", "search", "details"])
def test_openalex_api_key_on_all_request_paths(monkeypatch, api_key, route):
    async def handle(request):
        assert request.url.host == "api.openalex.org"
        assert request.url.params.get("api_key") == (api_key or None)
        assert request.url.params["mailto"] == "contact@example.org"
        assert "api_key" not in request.headers
        return httpx.Response(200, json={"results": [], "meta": {"count": 0}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s, "OA_KEY", api_key)
            monkeypatch.setattr(s, "EMAIL", "contact@example.org")
            if route == "doi":
                return await s._openalex(client, "Design ethics")
            if route == "search":
                return await s._search_openalex("design", None, None, None, 5, 1,
                                                "title_abstract", False, True, False)
            return await s._oa_get("works/W123", select="title")

    result = asyncio.run(run())
    assert "test-openalex-key" not in str(result)
    assert "api_key" not in str(result)


def test_openalex_key_does_not_mutate_params_or_leak_in_http_errors(monkeypatch):
    params = {"search": "design"}

    async def handle(request):
        assert request.url.params["api_key"] == "test-openalex-key"
        return httpx.Response(403, json={"error": "forbidden"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "OA_KEY", "test-openalex-key")
            response = await s._oa_request(client, "works", params)
            with pytest.raises(httpx.HTTPStatusError) as caught:
                response.raise_for_status()
            assert "test-openalex-key" not in str(caught.value)
            assert "api_key" not in str(caught.value)

    asyncio.run(run())
    assert params == {"search": "design"}


def test_openalex_key_redacted_from_transport_errors(monkeypatch):
    monkeypatch.setattr(s, "OA_KEY", "test key+/secret")
    error = httpx.ConnectError(
        "https://api.openalex.org/works?" + str(httpx.QueryParams({"api_key": s.OA_KEY})))
    assert "test" not in s._err("openalex", error)
    assert "[redacted]" in s._err("openalex", error)


@pytest.mark.parametrize("entity", sorted(s._OA_ENTITIES))
def test_openalex_entity_lists(monkeypatch, entity):
    params = {"filter": "works_count:>10", "group_by": "country_code", "cursor": "*",
              "select": "id,display_name", "per_page": 10}
    payload = {"meta": {"next_cursor": "next", "cost_usd": 0.001}, "results": [],
               "group_by": [{"key": "IT", "count": 5}]}

    async def handle(request):
        assert request.url.path == "/" + entity
        assert dict(request.url.params) == {**{key: str(value) for key, value in params.items()},
                                           "api_key": "test-key"}
        return httpx.Response(200, json=payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s, "OA_KEY", "test-key")
            monkeypatch.setattr(s, "EMAIL", "")
            return await s.openalex_entity(entity, params=params)

    assert asyncio.run(run()) == {"status": "ok", "http_status": 200, "data": payload}
    assert "api_key" not in params


@pytest.mark.parametrize("identifier", ["W123", "doi:10.1000/a?b#c", "https://doi.org/10.1000/a"])
def test_openalex_entity_single_identifier(monkeypatch, identifier):
    from urllib.parse import quote

    async def handle(request):
        assert request.url.raw_path.decode() == "/works/" + quote(identifier, safe="")
        return httpx.Response(200, json={"id": "W123"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s, "OA_KEY", "")
            monkeypatch.setattr(s, "EMAIL", "")
            return await s.openalex_entity("works", identifier)

    assert asyncio.run(run())["data"] == {"id": "W123"}


@pytest.mark.parametrize("tool,kwargs,method,path,body", [
    (s.openalex_autocomplete, {"query": "design"}, "GET", "/autocomplete", None),
    (s.openalex_autocomplete, {"query": "Smith", "entity": "authors"},
     "GET", "/autocomplete/authors", None),
    (s.openalex_text, {"title": "Human computer interaction and ethics"},
     "POST", "/text", {"title": "Human computer interaction and ethics", "abstract": ""}),
    (s.openalex_text, {"title": "Human computer interaction and ethics", "target": "topics",
                       "method": "GET"}, "GET", "/text/topics", None),
    (s.openalex_text, {"title": "Human computer interaction and ethics", "target": "keywords"},
     "POST", "/text/keywords", {"title": "Human computer interaction and ethics", "abstract": ""}),
    (s.openalex_query, {"action": "execute", "query": "works where year is (2020)"},
     "POST", "/", {"oql": "works where year is (2020)"}),
    (s.openalex_query, {"action": "execute", "query": {"get_rows": "works"}, "query_format": "oqo"},
     "POST", "/", {"oqo": {"get_rows": "works"}}),
    (s.openalex_query, {"action": "execute", "query": "works", "method": "GET"},
     "GET", "/", None),
    (s.openalex_query, {"action": "translate", "query": "/works", "query_format": "oxurl"},
     "POST", "/query", {"oxurl": "/works"}),
    (s.openalex_query, {"action": "translate", "query": "works", "method": "GET"},
     "GET", "/query/oql/works", None),
    (s.openalex_query, {"action": "validate", "query": "works"}, "GET", "/validate", None),
    (s.openalex_query, {"action": "context", "query": "works", "params": {"pos": 5}},
     "GET", "/parse-context", None),
    (s.openalex_query, {"action": "properties"}, "GET", "/properties", None),
    (s.openalex_query, {"action": "properties", "entity": "works"},
     "GET", "/properties/works", None),
    (s.openalex_query, {"action": "spec"}, "GET", "/query/spec", None),
    (s.openalex_query, {"action": "spec", "artifact": "oqo"}, "GET", "/query/spec/oqo", None),
    (s.openalex_status, {}, "GET", "/rate-limit", None),
    (s.openalex_status, {"action": "changefiles"}, "GET", "/changefiles", None),
    (s.openalex_status, {"action": "changefiles", "date": "2026-10-01"},
     "GET", "/changefiles/2026-10-01", None),
])
def test_openalex_special_routes(monkeypatch, tool, kwargs, method, path, body):
    import json

    async def handle(request):
        assert request.url.host == "api.openalex.org"
        assert request.method == method
        assert request.url.path == path
        assert request.url.params["api_key"] == "test-key"
        assert (json.loads(request.content) if request.content else None) == body
        if tool is s.openalex_autocomplete:
            assert request.url.params["q"] == kwargs["query"]
        if tool is s.openalex_query and kwargs["action"] in {"validate", "context"}:
            assert request.url.params["q"] == kwargs["query"]
        return httpx.Response(200, json={"api_key": "test-key", "results": [],
                                        "message": "test-key", "meta": {"next_cursor": "next"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s, "OA_KEY", "test-key")
            return await tool(**kwargs)

    result = asyncio.run(run())
    assert result["status"] == "ok"
    assert "test-key" not in str(result)
    assert result["data"]["meta"]["next_cursor"] == "next"


@pytest.mark.parametrize("action", sorted(s._OA_ACCOUNT_READ_ROUTES))
def test_openalex_account_read_routes(monkeypatch, action):
    template = s._OA_ACCOUNT_READ_ROUTES[action]
    identifiers = {name: value for name, value in {"collection_id": "col_123",
                   "import_id": "imp_123", "saved_search_id": "saved123"}.items()
                   if "{" + name + "}" in template}

    async def handle(request):
        assert request.method == "GET"
        assert request.url.path == "/" + template.format_map(identifiers)
        return httpx.Response(200, json={"results": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            return await s.openalex_account_read(action, **identifiers)

    assert asyncio.run(run())["status"] == "ok"


@pytest.mark.parametrize("action", sorted(s._OA_ACCOUNT_WRITE_ROUTES))
def test_openalex_account_writes_preview_and_confirmation(monkeypatch, action):
    import json

    method, template = s._OA_ACCOUNT_WRITE_ROUTES[action]
    identifiers = {name: value for name, value in {"collection_id": "col_123",
                   "member_id": "W123", "saved_search_id": "saved123"}.items()
                   if "{" + name + "}" in template}
    body = None if method == "DELETE" else {"display_name": "test"}
    params = {"member_ids": "W123,W456"} if action == "remove_members" else {}
    attempts = []

    async def handle(request):
        attempts.append(request)
        assert request.method == method
        assert request.url.path == "/" + template.format_map(identifiers)
        assert (json.loads(request.content) if request.content else None) == body
        if action == "curate":
            assert request.url.host == "user.openalex.org"
            assert request.headers["Authorization"] == "Bearer test-key"
            assert "api_key" not in request.url.params
        else:
            assert request.url.host == "api.openalex.org"
            assert request.url.params["api_key"] == "test-key"
        return httpx.Response(204) if method == "DELETE" else httpx.Response(201, json={"id": "test"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s, "OA_KEY", "test-key")
            monkeypatch.setattr(s, "OA_WRITES_ENABLED", True)
            preview = await s.openalex_account_write(action, body=body, params=params, **identifiers)
            assert preview["status"] == "confirmation_required"
            assert not attempts
            assert "test-key" not in str(preview)
            return await s.openalex_account_write(action, body=body, params=params,
                                                  confirm=True, **identifiers)

    assert asyncio.run(run())["status"] == "ok"
    assert len(attempts) == 1


@pytest.mark.parametrize("entity", sorted(s._OA_ENTITIES - s._OA_LIST_ONLY))
def test_openalex_single_routes(monkeypatch, entity):
    async def fake_api(path, params):
        assert path == f"{entity}/test-id"
        return {"status": "ok"}

    monkeypatch.setattr(s, "_oa_api", fake_api)
    assert asyncio.run(s.openalex_entity(entity, "test-id"))["status"] == "ok"


@pytest.mark.parametrize("tool,kwargs", [
    (s.openalex_entity, {"entity": "unknown"}),
    (s.openalex_entity, {"entity": "works", "entity_id": ".."}),
    (s.openalex_entity, {"entity": "works", "entity_id": " "}),
    (s.openalex_entity, {"entity": "raw-affiliation-strings", "entity_id": "123"}),
    (s.openalex_entity, {"entity": "works", "params": {"api_key": "do-not-send"}}),
    (s.openalex_autocomplete, {"query": " "}),
    (s.openalex_autocomplete, {"query": "test", "entity": "unknown"}),
    (s.openalex_text, {"title": "short"}),
    (s.openalex_text, {"title": "x" * 2001}),
    (s.openalex_query, {"action": "execute"}),
    (s.openalex_query, {"action": "execute", "query": "works", "query_format": "oxurl"}),
    (s.openalex_query, {"action": "execute", "query": "works", "query_format": "oqo"}),
    (s.openalex_query, {"action": "context", "query": "works"}),
    (s.openalex_query, {"action": "execute", "query": "works", "params": {"oql": "authors"}}),
    (s.openalex_query, {"action": "spec", "artifact": ".."}),
    (s.openalex_account_read, {"action": "collection"}),
    (s.openalex_account_read, {"action": "collections", "collection_id": "col_123"}),
    (s.openalex_account_write, {"action": "create_collection"}),
    (s.openalex_account_write, {"action": "remove_members", "collection_id": "col_123"}),
    (s.openalex_account_write, {"action": "delete_collection", "collection_id": "col_123", "body": {"x": 1}}),
    (s.openalex_account_write, {"action": "create_collection", "body": [{"x": 1}]}),
])
def test_openalex_invalid_inputs_do_not_send_requests(monkeypatch, tool, kwargs):
    def unexpected_http():
        pytest.fail("Invalid input must not send HTTP requests")

    monkeypatch.setattr(s, "_http", unexpected_http)
    with pytest.raises(ValueError):
        asyncio.run(tool(**kwargs))


@pytest.mark.parametrize("method,path,retry_count", [
    ("GET", "works", 4), ("POST", "", 4), ("POST", "text", 4),
    ("POST", "collections", 1), ("PATCH", "collections/col_123", 1),
    ("DELETE", "collections/col_123", 1), ("POST", "curations", 1),
])
def test_openalex_429_retry_policy(monkeypatch, method, path, retry_count):
    attempts = []
    delays = []

    async def handle(request):
        attempts.append(request.method)
        return httpx.Response(429, headers={"Retry-After": "2"}, json={"code": "rate_limited"})

    async def fake_sleep(delay):
        delays.append(delay)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s.asyncio, "sleep", fake_sleep)
            return await s._oa_api(path, method=method, body={"test": True} if method != "GET" else None)

    result = asyncio.run(run())
    assert result["status"] == "error"
    assert result["http_status"] == 429
    assert len(attempts) == retry_count
    assert delays == [2.0] * (retry_count - 1)


@pytest.mark.parametrize("failure", ["timeout", "non_json", "forbidden", "csv"])
def test_openalex_response_formats_and_errors(monkeypatch, failure):
    async def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("test-key", request=request)
        if failure == "non_json":
            return httpx.Response(502, text="bad gateway")
        if failure == "forbidden":
            return httpx.Response(403, json={"code": "permission_denied", "message": "test-key"})
        return httpx.Response(200, headers={"Content-Type": "text/csv", "X-Next-Cursor": "next"},
                              text="id,added_at\nW123,2026-10-21\n")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s, "OA_KEY", "test-key")
            return await s._oa_api("rate-limit")

    result = asyncio.run(run())
    assert "test-key" not in str(result)
    if failure == "csv":
        assert result["status"] == "ok"
        assert result["content_type"] == "text/csv"
        assert result["next_cursor"] == "next"
    else:
        assert result["status"] == "error"


def test_openalex_write_requires_configured_key(monkeypatch):
    monkeypatch.setattr(s, "OA_KEY", "")
    monkeypatch.setattr(s, "OA_WRITES_ENABLED", True)
    with pytest.raises(ValueError, match="OPENALEX_API_KEY"):
        asyncio.run(s.openalex_account_write("create_collection", body={"display_name": "test"},
                                              confirm=True))


def test_openalex_write_disabled_by_default(monkeypatch):
    monkeypatch.setattr(s, "OA_WRITES_ENABLED", False)

    def unexpected_http():
        pytest.fail("Disabled OpenAlex writes must not send HTTP requests")

    monkeypatch.setattr(s, "_http", unexpected_http)
    with pytest.raises(ValueError, match="OPENALEX_ENABLE_WRITES=true"):
        asyncio.run(s.openalex_account_write("create_collection", body={"display_name": "test"},
                                              confirm=True))


def test_openalex_mcp_tool_annotations():
    tools = {tool.name: tool for tool in asyncio.run(s.mcp.list_tools())}
    for name in ("entity", "autocomplete", "text", "query", "status", "account_read"):
        assert tools[f"openalex_{name}"].annotations.readOnlyHint is True
    assert tools["openalex_account_write"].annotations.readOnlyHint is False
    assert tools["openalex_account_write"].annotations.destructiveHint is True


def _run_verify(monkeypatch, handler, doi, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            s._WORK_CACHE.clear()
            return await s.verify_doi(doi, **kwargs)

    return asyncio.run(run())


def _datacite_handler(ra="DataCite", datacite_status=200):
    attributes = {"titles": [{"title": "Interaction design dataset"}],
                  "creators": [{"familyName": "Rossi", "name": "Rossi, Anna"}],
                  "publicationYear": 2021, "publisher": "Zenodo",
                  "types": {"resourceTypeGeneral": "Dataset"}}

    async def handle(request):
        if request.url.host == "api.crossref.org":
            return httpx.Response(404, text="Resource not found.")
        if request.url.host == "doi.org":
            assert request.url.path.startswith("/doiRA/")
            return httpx.Response(200, json=[{"DOI": "10.5281/zenodo.1"}] if ra is None
                                  else [{"DOI": "10.5281/zenodo.1", "RA": ra}])
        assert request.url.host == "api.datacite.org"
        if datacite_status != 200:
            return httpx.Response(datacite_status, json={"errors": []})
        return httpx.Response(200, json={"data": {"attributes": attributes}})

    return handle


def test_verify_doi_reads_datacite_metadata(monkeypatch):
    result = _run_verify(monkeypatch, _datacite_handler(), "10.5281/zenodo.1",
                         expected_title="Interaction design dataset",
                         expected_first_author="Rossi", expected_year=2021)
    assert result["status"] == "registered_match"
    assert result["registration_agency"] == "DataCite"
    assert result["authors"] == ["Rossi"] and result["year"] == 2021
    assert result["bibliographic_checks"] == {"year": "match", "first_author": "match"}
    assert result["is_retracted"] is None


def test_verify_doi_detects_datacite_title_mismatch(monkeypatch):
    result = _run_verify(monkeypatch, _datacite_handler(), "10.5281/zenodo.1",
                         expected_title="A study of something unrelated entirely")
    assert result["status"] == "registered_title_mismatch"


def test_verify_doi_flags_unsupported_agency(monkeypatch):
    result = _run_verify(monkeypatch, _datacite_handler(ra="mEDRA"), "10.5281/zenodo.1")
    assert result["status"] == "registered_unsupported_agency"
    assert result["registration_agency"] == "mEDRA"
    assert "title" not in result


def test_verify_doi_unregistered_is_not_found(monkeypatch):
    assert _run_verify(monkeypatch, _datacite_handler(ra=None), "10.5281/zenodo.1")["status"] == "not_found"


def test_verify_doi_datacite_failure_is_error_not_not_found(monkeypatch):
    result = _run_verify(monkeypatch, _datacite_handler(datacite_status=500), "10.5281/zenodo.1")
    assert result["status"] == "error" and result["registration_agency"] == "DataCite"


def test_verify_doi_crossref_path_unchanged(monkeypatch):
    async def handle(request):
        assert request.url.host == "api.crossref.org"
        return httpx.Response(200, json={"message": {
            "title": ["Interaction design"], "author": [{"family": "Rossi"}],
            "issued": {"date-parts": [[2020]]}, "updated-by": []}})

    result = _run_verify(monkeypatch, handle, "10.1000/test", expected_title="Interaction design")
    assert result["status"] == "registered_match"
    assert result["registration_agency"] == "Crossref" and result["is_retracted"] is False


@pytest.mark.parametrize("openalex,other,verdict", [
    (0, 0, "both_empty"), (0, 40, "openalex_missing"), (40, 0, "semantic_scholar_missing"),
    (40, 45, "concordant"), (10, 40, "openalex_lower"), (40, 10, "semantic_scholar_lower"),
])
def test_coverage_verdict(openalex, other, verdict):
    assert s._coverage_verdict(openalex, other) == verdict


def _graph_run(monkeypatch, s2_status=200, s2_body=None, **kwargs):
    requests = []

    async def fake_oa_get(path, **params):
        return {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/a", "title": "T",
                "publication_year": 2020, "cited_by_count": 10, "authorships": [],
                "referenced_works": ["https://openalex.org/W2"]}

    async def handle(request):
        requests.append(request)
        return httpx.Response(s2_status, json=s2_body if s2_body is not None else
                              [{"referenceCount": 60, "citationCount": 12}])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            monkeypatch.setattr(s, "_oa_get", fake_oa_get)
            return await s.citation_graph(["10.1000/a"], include_references=False,
                                          include_citing=False, **kwargs)

    return asyncio.run(run()), requests


def test_citation_graph_reports_coverage_gap(monkeypatch):
    result, requests = _graph_run(monkeypatch)
    assert len(requests) == 1 and requests[0].url.path.endswith("/paper/batch")
    row = result["coverage_check"][0]
    assert row["openalex_references"] == 1 and row["semantic_scholar_references"] == 60
    assert row["references_verdict"] == "openalex_lower"
    assert row["citations_verdict"] == "concordant"


def test_citation_graph_survives_semantic_scholar_failure(monkeypatch):
    result, _ = _graph_run(monkeypatch, s2_status=500, s2_body={})
    assert result["status"] == "ok" and result["coverage_check"] == []
    assert any("semantic_scholar" in e for e in result["source_errors"])


def test_citation_graph_coverage_check_can_be_disabled(monkeypatch):
    result, requests = _graph_run(monkeypatch, check_coverage=False)
    assert not requests and result["coverage_check"] == []


def test_citation_graph_marks_unknown_seed_in_semantic_scholar(monkeypatch):
    result, _ = _graph_run(monkeypatch, s2_body=[None])
    assert result["coverage_check"][0]["references_verdict"] == "not_in_semantic_scholar"


def cand(title, authors=("Smith",), year=2010, **kw):
    return {"title": title, "authors": list(authors), "year": year, **kw}


def eligible(c, title, author=None, year=None):
    return s._eligible(s._assess(c, title, author, year))


def test_non_latin_titles_do_not_collapse():
    assert s._norm("Бытие и время") == "бытие и время"
    assert s._title_sim("Бытие и время", "Война и мир")[0] < s.TITLE_THRESHOLD


def test_accents_are_folded():
    assert s._norm("Phénoménologie de la perception") == "phenomenologie de la perception"


def test_generic_title_wrong_author_is_rejected():
    assert not eligible(cand("Introduction"), "Introduction", "Norman", 2010)


def test_short_title_needs_author():
    assert not eligible(cand("Introduction"), "Introduction")
    assert eligible(cand("Introduction", ["Donald A. Norman"]), "Introduction", "Norman")


def test_author_surname_is_token_not_substring():
    assert s._author_ok("Li", ["Williams"]) is False
    assert s._author_ok("Fox, M. C.", ["Mark C. Fox"]) is True
    assert s._author_ok("van der Berg", ["J. van der Berg"]) is True
    assert s._author_ok("Norman", []) is None


def test_year_outside_tolerance_is_rejected():
    t = "The ecological approach to visual perception"
    assert not eligible(cand(t, ["Gibson"], 1950), t, "Gibson", 1979)


def test_derivative_records_are_detected():
    t = "The ecological approach to visual perception in interaction design"
    assert s._is_derivative(cand("Correction to: " + t), t)
    assert s._is_derivative(cand("Erratum to \u201c" + t + "\u201d"), t)
    assert s._is_derivative(cand(t, type="peer-review"), t)
    assert not s._is_derivative(cand("Response inhibition in older adults"), "x")
    assert not s._is_derivative(cand("Reply to: " + t), "Reply to: " + t)


def test_subtitle_variants():
    full = "Distributed cognition: Toward a new foundation for human-computer interaction research"
    assert s._title_sim("Distributed cognition", full)[0] == 1.0
    assert s._title_sim(full, "Distributed cognition",
                        "Toward a new foundation for human-computer interaction research")[0] > 0.95
    # Head-only matches are short, so they still need the author
    assert not eligible(cand(full, ["Hollan"]), "Distributed cognition")
    assert eligible(cand(full, ["Hollan"]), "Distributed cognition", "Hollan")


def test_html_and_retracted_prefix_are_ignored():
    assert s._title_sim("Drosophila wing development", "<i>Drosophila</i> wing development")[0] == 1.0
    assert s._title_sim("Ileal lymphoid nodular hyperplasia",
                        "RETRACTED: Ileal-lymphoid-nodular hyperplasia")[0] == 1.0


def test_clean_doi():
    assert s._clean_doi("https://doi.org/10.1000/abc?x=1#y") == "10.1000/abc"
    assert s._clean_doi("https://doi.org/10.1000/abc%2Fdef") == "10.1000/abc/def"
    assert s._clean_doi("(see doi:10.1000/xyz).") == "10.1000/xyz"
    assert s._clean_doi("10.1016/S0140-6736(97)11096-0") == "10.1016/s0140-6736(97)11096-0"
    assert s._clean_doi("10.1000/foo_(bar)") == "10.1000/foo_(bar)"
    assert s._clean_doi("not a doi") is None


def test_doi_path_escapes_query_characters():
    assert s._doi_path("10.1000/a?b#c") == "10.1000/a%3Fb%23c"


def test_page_ranges():
    assert s._biblio_norm("123\u201345") == s._biblio_norm("123-145")
    assert s._biblio_norm("e12345") == "e12345"


def test_error_message_never_empty():
    import httpx
    assert s._err("crossref", httpx.ReadTimeout("")) == "crossref: ReadTimeout"


def test_graph_links():
    nodes = {
        "S": {"id": "S", "refs": ["A", "B", "X"]},
        "C": {"id": "C", "refs": ["A", "B", "S", "X"]},
        "A": {"id": "A", "refs": []},
        "B": {"id": "B", "refs": ["Y"]},
    }
    edges, cocit, coupling = s._graph_links(nodes, ["S"], min_cocitation=2, min_coupling=2)
    assert ["C", "S"] in edges and ["S", "A"] in edges
    assert ["A", "B", 2] in cocit
    # S and C share A, B and X in their full reference lists
    assert coupling[0][:3] == ["C", "S", 3] and 0 < coupling[0][3] <= 1
    assert nodes["C"]["seeds_linked"] == 1 and nodes["S"]["seeds_linked"] == 0
    # C cites both A and the seed S
    assert nodes["A"]["seed_cocited"] == 1 and nodes["C"]["seed_coupling"] > 0


@pytest.mark.parametrize("value,expected", [
    ("0000-0002-1825-0097", "0000-0002-1825-0097"),
    ("https://orcid.org/0000-0002-1694-233x", "0000-0002-1694-233X"),
    ("http://www.orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),
    ("0000-0002-1825-0098", None), ("0000000218250097", None), ("", None), (None, None),
])
def test_clean_orcid(value, expected):
    assert s._clean_orcid(value) == expected


def _run_with(monkeypatch, handler, call):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(s, "_http", lambda: client)
            return await call()

    return asyncio.run(run())


def test_orcid_search_and_records(monkeypatch):
    seen = []

    async def handle(request):
        seen.append((request.url.path, dict(request.url.params)))
        assert request.url.host == "pub.orcid.org"
        assert request.headers["accept"] == "application/json"
        return httpx.Response(200, json={"expanded-result": []})

    search = _run_with(monkeypatch, handle, lambda: s.orcid("search", query="family-name:Rossi"))
    person = _run_with(monkeypatch, handle, lambda: s.orcid(
        "person", orcid_id="https://orcid.org/0000-0002-1825-0097"))
    works = _run_with(monkeypatch, handle, lambda: s.orcid("works", orcid_id="0000-0002-1825-0097"))
    assert [r["status"] for r in (search, person, works)] == ["ok"] * 3
    assert seen == [("/v3.0/expanded-search/", {"rows": "10", "q": "family-name:Rossi"}),
                    ("/v3.0/0000-0002-1825-0097/person", {}),
                    ("/v3.0/0000-0002-1825-0097/works", {})]


@pytest.mark.parametrize("kwargs", [
    {"action": "search"}, {"action": "search", "query": "x", "orcid_id": "0000-0002-1825-0097"},
    {"action": "search", "query": "x", "params": {"q": "y"}},
    {"action": "person"}, {"action": "person", "orcid_id": "0000-0002-1825-0098"},
    {"action": "works", "orcid_id": "0000-0002-1825-0097", "query": "x"},
])
def test_orcid_rejects_invalid_inputs_before_network(monkeypatch, kwargs):
    def handle(request):
        raise AssertionError("no request expected")

    with pytest.raises(ValueError):
        _run_with(monkeypatch, handle, lambda: s.orcid(**kwargs))


def test_orcid_preserves_api_errors(monkeypatch):
    async def handle(request):
        return httpx.Response(404, json={"error-code": 9016})

    result = _run_with(monkeypatch, handle, lambda: s.orcid("person", orcid_id="0000-0002-1825-0097"))
    assert result == {"status": "error", "http_status": 404, "data": {"error-code": 9016}}


def test_google_books_routes_and_key_header(monkeypatch):
    seen = []

    async def handle(request):
        seen.append((request.url.path, dict(request.url.params)))
        assert request.headers["x-goog-api-key"] == "gb-key"
        assert "key" not in request.url.params
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(s, "GB_KEY", "gb-key")
    search = _run_with(monkeypatch, handle, lambda: s.google_books(
        "search", query="intitle:design", params={"maxResults": 5}))
    volume = _run_with(monkeypatch, handle, lambda: s.google_books("volume", volume_id="zyTCAlFPjgYC"))
    assert search["status"] == volume["status"] == "ok"
    assert seen == [("/books/v1/volumes", {"maxResults": "5", "q": "intitle:design"}),
                    ("/books/v1/volumes/zyTCAlFPjgYC", {})]


@pytest.mark.parametrize("kwargs", [
    {"action": "search"}, {"action": "search", "query": "x", "volume_id": "v"},
    {"action": "search", "query": "x", "params": {"key": "secret"}},
    {"action": "volume"}, {"action": "volume", "volume_id": "v", "query": "x"},
])
def test_google_books_rejects_invalid_inputs_before_network(monkeypatch, kwargs):
    def handle(request):
        raise AssertionError("no request expected")

    with pytest.raises(ValueError):
        _run_with(monkeypatch, handle, lambda: s.google_books(**kwargs))


def test_book_google_books_adapter(monkeypatch):
    async def handle(request):
        assert request.url.params["q"] == 'intitle:"the google story" inauthor:vise'
        return httpx.Response(200, json={"items": [{"volumeInfo": {
            "title": "The Google Story", "authors": ["David A. Vise"],
            "publisher": "Random House", "publishedDate": "2005-11-15",
            "industryIdentifiers": [{"type": "ISBN_10", "identifier": "055380457X"},
                                    {"type": "OTHER", "identifier": "ABC"}]}}, {}]})

    recs = _run_with(monkeypatch, handle, lambda: s._book_google_books(
        s._http(), "The Google Story", "Vise, D."))
    assert recs[0] == {"source": "google_books", "title": "The Google Story",
                       "authors": ["David A. Vise"], "publishers": ["Random House"],
                       "years": [2005], "isbns": ["055380457X"], "doi": None}
    assert recs[1]["title"] == "" and recs[1]["years"] == []


def test_verify_book_counts_google_books_as_a_source(monkeypatch):
    async def handle(request):
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": [{"volumeInfo": {
                "title": "The Google Story", "authors": ["David Vise"],
                "publisher": "Delacorte", "publishedDate": "2005"}}]})
        if request.url.host == "openlibrary.org":
            return httpx.Response(200, json={"docs": [{"title": "The Google Story",
                "author_name": ["David Vise"], "editions": {"docs": [{
                    "title": "The Google Story", "publisher": ["Delacorte"],
                    "publish_year": [2005]}]}}]})
        return httpx.Response(500)

    result = _run_with(monkeypatch, handle, lambda: s.verify_book(
        "The Google Story", "Vise", 2005))
    assert result["status"] == "verified"
    assert result["best"]["confirmed_by"] == ["google_books", "open_library"]
    assert len(result["source_errors"]) == 2


@pytest.mark.parametrize("expected,check", [
    ("https://orcid.org/0000-0002-1825-0097", "match"),
    ("0000-0001-5109-3700", "not_in_source"),
])
def test_verify_doi_checks_orcid(monkeypatch, expected, check):
    async def handle(request):
        return httpx.Response(200, json={"message": {
            "title": ["Interaction design"], "updated-by": [],
            "author": [{"family": "Carberry", "ORCID": "http://orcid.org/0000-0002-1825-0097"},
                       {"family": "Rossi"}],
            "issued": {"date-parts": [[2020]]}}})

    result = _run_verify(monkeypatch, handle, "10.1000/test", expected_orcid=expected)
    assert result["author_orcids"] == ["0000-0002-1825-0097"]
    assert result["bibliographic_checks"] == {"orcid": check}


def test_verify_doi_reads_datacite_orcid(monkeypatch):
    attributes = {"titles": [{"title": "Dataset"}], "publicationYear": 2021,
                  "creators": [{"familyName": "Carberry", "nameIdentifiers": [
                      {"nameIdentifier": "https://orcid.org/0000-0002-1825-0097",
                       "nameIdentifierScheme": "ORCID"}]}]}

    async def handle(request):
        if request.url.host == "api.crossref.org":
            return httpx.Response(404)
        if request.url.host == "doi.org":
            return httpx.Response(200, json=[{"RA": "DataCite"}])
        return httpx.Response(200, json={"data": {"attributes": attributes}})

    result = _run_verify(monkeypatch, handle, "10.5281/zenodo.1", expected_orcid="0000-0002-1825-0097")
    assert result["bibliographic_checks"] == {"orcid": "match"}


def test_verify_doi_rejects_malformed_orcid_before_network(monkeypatch):
    def handle(request):
        raise AssertionError("no request expected")

    result = _run_verify(monkeypatch, handle, "10.1000/test", expected_orcid="0000-0002-1825-0098")
    assert result == {"status": "invalid_orcid", "orcid": "0000-0002-1825-0098"}


@pytest.fixture(autouse=True)
def _no_orcid_credentials(monkeypatch):
    monkeypatch.setattr(s, "ORCID_CLIENT_ID", "")
    monkeypatch.setattr(s, "ORCID_CLIENT_SECRET", "")
    monkeypatch.setattr(s, "_orcid_token", None)
    monkeypatch.setattr(s, "_orcid_lock", asyncio.Lock())


def test_orcid_uses_cached_read_public_token(monkeypatch):
    monkeypatch.setattr(s, "ORCID_CLIENT_ID", "APP-TEST")
    monkeypatch.setattr(s, "ORCID_CLIENT_SECRET", "shh")
    calls = []

    async def handle(request):
        calls.append((request.url.host, request.method, request.headers.get("authorization")))
        if request.url.host == "orcid.org":
            assert request.url.path == "/oauth/token"
            assert dict(httpx.QueryParams(request.content.decode())) == {
                "client_id": "APP-TEST", "client_secret": "shh",
                "grant_type": "client_credentials", "scope": "/read-public"}
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 631138518})
        return httpx.Response(200, json={})

    async def twice():
        await s.orcid("person", orcid_id="0000-0002-1825-0097")
        return await s.orcid("works", orcid_id="0000-0002-1825-0097")

    assert _run_with(monkeypatch, handle, twice)["status"] == "ok"
    assert calls == [("orcid.org", "POST", None),
                     ("pub.orcid.org", "GET", "Bearer tok"),
                     ("pub.orcid.org", "GET", "Bearer tok")]


def test_orcid_token_failure_is_reported_without_secrets(monkeypatch):
    monkeypatch.setattr(s, "ORCID_CLIENT_ID", "APP-TEST")
    monkeypatch.setattr(s, "ORCID_CLIENT_SECRET", "shh")

    async def handle(request):
        return httpx.Response(401, json={"error": "invalid_client"})

    result = _run_with(monkeypatch, handle, lambda: s.orcid("person", orcid_id="0000-0002-1825-0097"))
    assert result["status"] == "error" and "shh" not in str(result)


def test_independent_sources_collapse_crossref_derived_indexes():
    assert s._independent(["crossref", "openalex", "semantic_scholar"]) == 1
    assert s._independent(["openalex", "semanticscholar", "hal"]) == 2
    assert s._independent([]) == 0


def _book(source, years, publishers):
    return s._book_rec(source, "T", ["A"], publishers, years, [])


def test_book_conflicts_flag_disjoint_years_and_publishers():
    matched = [_book("open_library", [2005, 2010], ["Delacorte Press"]),
               _book("dnb", [2010], ["Delacorte"]),
               _book("google_books", [2007], ["Penguin"])]
    conflicts = {c["field"]: c["values"] for c in s._book_conflicts(matched)}
    assert set(conflicts["year"]) == {"open_library", "dnb", "google_books"}
    assert set(conflicts["publisher"]) == {"google_books", "dnb", "open_library"}


def test_book_conflicts_ignore_sources_without_values_and_overlaps():
    matched = [_book("open_library", [2005, 2010], ["Delacorte Press"]),
               _book("dnb", [2010], []), _book("google_books", [2010], ["Delacorte"])]
    assert s._book_conflicts(matched) == []


def _book_handler(gb_year):
    async def handle(request):
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": [{"volumeInfo": {
                "title": "The Google Story", "authors": ["David Vise"],
                "publisher": "Delacorte", "publishedDate": str(gb_year)}}]})
        if request.url.host == "openlibrary.org":
            return httpx.Response(200, json={"docs": [{"title": "The Google Story",
                "author_name": ["David Vise"], "editions": {"docs": [{
                    "title": "The Google Story", "publisher": ["Delacorte"],
                    "publish_year": [2005]}]}}]})
        return httpx.Response(500)

    return handle


def test_verify_book_agreement_without_conflict(monkeypatch):
    result = _run_with(monkeypatch, _book_handler(2005),
                       lambda: s.verify_book("The Google Story", "Vise"))
    assert result["agreement"] == {"level": "low", "independent_sources": 1, "conflicts": []}
    assert "note" not in result


def test_verify_book_flags_conflict_without_excluding(monkeypatch):
    result = _run_with(monkeypatch, _book_handler(2007),
                       lambda: s.verify_book("The Google Story", "Vise"))
    assert result["status"] == "verified"
    assert result["best"]["confirmed_by"] == ["google_books", "open_library"]
    assert [c["field"] for c in result["agreement"]["conflicts"]] == ["year"]
    assert "year" in result["note"]


def test_book_feeds_count_as_one_group():
    assert s._independent(["google_books", "open_library"]) == 1
    assert s._independent(["google_books", "open_library", "dnb"]) == 2


def test_verify_book_high_agreement_needs_independent_groups(monkeypatch):
    base = _book_handler(2005)

    async def handle(request):
        if request.url.host == "services.dnb.de":
            return httpx.Response(200, content=(
                b'<r xmlns:dc="http://purl.org/dc/elements/1.1/" '
                b'xmlns:o="http://www.openarchives.org/OAI/2.0/oai_dc/"><o:dc>'
                b'<dc:title>The Google Story</dc:title><dc:creator>Vise, David</dc:creator>'
                b'<dc:publisher>New York : Delacorte</dc:publisher><dc:date>2005</dc:date>'
                b'</o:dc></r>'))
        return await base(request)

    result = _run_with(monkeypatch, handle, lambda: s.verify_book("The Google Story", "Vise"))
    assert result["agreement"]["level"] == "high"
    assert result["agreement"]["independent_sources"] == 2


def test_orcid_malformed_token_is_an_error_result(monkeypatch):
    monkeypatch.setattr(s, "ORCID_CLIENT_ID", "APP-TEST")
    monkeypatch.setattr(s, "ORCID_CLIENT_SECRET", "shh")

    async def handle(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    result = _run_with(monkeypatch, handle, lambda: s.orcid("person", orcid_id="0000-0002-1825-0097"))
    assert result["status"] == "error" and "malformed" in result["detail"]


def test_orcid_refreshes_token_once_after_401(monkeypatch):
    monkeypatch.setattr(s, "ORCID_CLIENT_ID", "APP-TEST")
    monkeypatch.setattr(s, "ORCID_CLIENT_SECRET", "shh")
    monkeypatch.setattr(s, "_orcid_token", (1e18, "revoked"))
    calls = []

    async def handle(request):
        calls.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "orcid.org":
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        auth = request.headers["authorization"]
        return httpx.Response(401 if auth == "Bearer revoked" else 200, json={})

    result = _run_with(monkeypatch, handle, lambda: s.orcid("works", orcid_id="0000-0002-1825-0097"))
    assert result["status"] == "ok"
    assert calls == [("pub.orcid.org", "Bearer revoked"), ("orcid.org", None),
                     ("pub.orcid.org", "Bearer fresh")]


def test_verify_doi_orcid_mismatch_when_all_authors_have_ids(monkeypatch):
    async def handle(request):
        return httpx.Response(200, json={"message": {
            "title": ["T"], "updated-by": [], "issued": {"date-parts": [[2020]]},
            "author": [{"family": "Carberry", "ORCID": "http://orcid.org/0000-0002-1825-0097"}]}})

    result = _run_verify(monkeypatch, handle, "10.1000/test", expected_orcid="0000-0001-5109-3700")
    assert result["bibliographic_checks"] == {"orcid": "mismatch"}


def test_verify_doi_unsupported_agency_reports_orcid_unchecked(monkeypatch):
    result = _run_verify(monkeypatch, _datacite_handler(ra="mEDRA"), "10.5281/zenodo.1",
                         expected_orcid="0000-0002-1825-0097")
    assert result["status"] == "registered_unsupported_agency"
    assert result["unchecked"] == ["orcid"]


def test_find_doi_filters_google_books_book_info(monkeypatch):
    async def handle(request):
        host = request.url.host
        if host == "www.googleapis.com":
            return httpx.Response(200, json={"items": [
                {"volumeInfo": {"title": "The Google Story", "authors": ["David Vise"]}},
                {"volumeInfo": {"title": "Cooking with Garlic", "authors": ["Someone"]}}]})
        if host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": []}})
        if host == "openlibrary.org":
            return httpx.Response(200, json={"docs": []})
        return httpx.Response(404, json={})

    result = _run_with(monkeypatch, handle, lambda: s.find_doi(
        "The Google Story", "Vise", 2005, is_book=True))
    assert [r["title"] for r in result["book_info_google_books"]] == ["The Google Story"]


def test_check_retractions_flags_notices(monkeypatch):
    records = {
        "10.1234/ret": (200, {"title": ["RETRACTED: Bad study"], "author": [{"family": "Doe"}],
                           "updated-by": [{"type": "retraction", "DOI": "10.1234/notice", "source": "retraction-watch",
                                           "updated": {"date-time": "2020-05-01T00:00:00Z"}}]}),
        "10.1234/eoc": (200, {"title": ["Doubtful"], "updated-by": [{"type": "expression_of_concern", "DOI": "10.1234/e"}]}),
        "10.1234/ok": (200, {"title": ["Fine"]}),
        "10.5281/zenodo.1": (404, None),
    }

    async def fake_work(d):
        return records[d]

    monkeypatch.setattr(s, "_crossref_work", fake_work)
    out = asyncio.run(s.check_retractions(["10.1234/ok", "https://doi.org/10.1234/RET", "10.1234/eoc", "10.5281/zenodo.1", "nope"]))
    assert out["retracted"] == 1 and out["checked"] == 3
    assert [f["doi"] for f in out["with_notices"]] == ["10.1234/ret", "10.1234/eoc"]
    assert out["with_notices"][0]["title"] == "Bad study"
    assert out["not_checked"] == ["10.5281/zenodo.1"] and out["invalid"] == ["nope"]


@pytest.mark.parametrize("fmt,accept", [
    ("bibtex", "application/x-bibtex"),
    ("chicago-author-date", "text/x-bibliography; style=chicago-author-date; locale=en-US"),
])
def test_format_reference_negotiates(monkeypatch, fmt, accept):
    seen = []

    async def fake_request(transport, request):
        seen.append(request.headers["accept"])
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="@article{x}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_request)
    monkeypatch.setattr(s, "_client", None, raising=False)
    out = asyncio.run(s.format_reference("10.1234/x", fmt))
    assert out["status"] == "ok" and seen == [accept]


def test_format_reference_rejects_bad_style():
    assert asyncio.run(s.format_reference("10.1234/x", "Bad Style!"))["status"] == "invalid_format"

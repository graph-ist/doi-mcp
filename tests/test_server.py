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
    with pytest.raises(ValueError, match="OPENALEX_API_KEY"):
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

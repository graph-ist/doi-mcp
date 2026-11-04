"""Conventions every registered tool must respect, so the audit does not have to be repeated."""
import asyncio
import json
import re
import tomllib

import pytest

import server as s

TOOLS = {t.name: t for t in asyncio.run(s.mcp.list_tools())}
MAX_DESCRIPTION_CHARS = 1800


def test_tool_names_are_snake_case_and_prefixed_by_family():
    for name in TOOLS:
        assert re.fullmatch(r"[a-z][a-z0-9_]+", name), name
    for name in TOOLS:
        if name.startswith("map_") or name.startswith("openalex_") or name.startswith("semantic_scholar"):
            continue
        assert "_" in name or name in {"orcid"}, name


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_tool_has_a_bounded_description(name):
    tool = TOOLS[name]
    assert tool.description and tool.description.strip()
    assert len(tool.description) <= MAX_DESCRIPTION_CHARS, (name, len(tool.description))


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_finite_valued_parameters_are_enums(name):
    """A parameter whose docstring lists quoted alternatives must be an enum in the schema."""
    schema = TOOLS[name].inputSchema
    props = schema.get("properties", {})
    for pname, prop in props.items():
        if pname in {"action", "state", "states", "modes", "include_states", "scope", "entity",
                     "method", "query_format", "target", "format_type"}:
            text = json.dumps(prop)
            assert "enum" in text or "const" in text, (name, pname)


def test_gateway_tools_are_listed_and_registered_only_when_enabled():
    expected = {"openalex_entity", "openalex_text", "openalex_query", "openalex_query_inspect",
                "openalex_status", "openalex_account_read", "openalex_account_write",
                "semantic_scholar", "semantic_scholar_datasets", "google_books", "orcid"}
    assert set(s.GATEWAY_TOOLS) == expected
    assert s.GATEWAY_ENABLED and expected <= set(TOOLS)
    core = set(TOOLS) - expected
    assert {"find_doi", "verify_doi", "verify_book", "search_literature", "map_create"} <= core
    assert len(core) <= 24


def test_validation_errors_reach_the_client_as_status_dicts():
    async def run():
        return await s.mcp.call_tool("check_retractions", {})

    result = asyncio.run(run())
    contents = result[0] if isinstance(result, tuple) else result
    text = " ".join(getattr(c, "text", "") for c in contents)
    assert "invalid_input" in text and "dois" in text


def test_direct_calls_still_raise_for_tests():
    with pytest.raises(ValueError):
        asyncio.run(s.check_retractions())


def test_wheel_includes_every_module_server_imports():
    with open("pyproject.toml", "rb") as f:
        included = set(tomllib.load(f)["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"])
    src = open("server.py", encoding="utf-8").read()
    local = {m + ".py" for m in re.findall(r"^\s*import (maps|knowledge)\b", src, re.M)}
    assert local and local <= included


def test_no_top_level_name_is_defined_twice():
    """A later def silently shadows an earlier one (this broke the Boolean query expansion
    of four search adapters once); the server module must define each name once."""
    import ast
    tree = ast.parse(open("server.py", encoding="utf-8").read())
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, dupes

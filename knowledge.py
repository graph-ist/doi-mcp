"""Read-only view of the Zotero library and the Obsidian vault, from snapshot files.

Two JSON files in KNOWLEDGE_DIR (default /var/lib/knowledge), written by their own services:
- library.json, by the Zotero MCP (snapshot.py): items with key, DOI, title, tags, collections;
- notes.json, by the Obsidian MCP (deploy/snapshot.mjs): notes with the Zotero keys their
  `reference` links to.
This server never calls Zotero or reads the vault itself: if a file is missing, the bridge
features say so and everything else keeps working.
"""
import json
import os
import re

KNOWLEDGE_DIR = os.environ.get("KNOWLEDGE_DIR", "/var/lib/knowledge")
# Highest schema_version of each snapshot this reader understands. The writers (Zotero MCP
# snapshot.py, Obsidian MCP snapshot.ts) bump theirs when a field changes meaning or is removed;
# a newer file is refused rather than misread. A file without the field is a pre-1 snapshot.
SUPPORTED_SCHEMA = {"library.json": 1, "notes.json": 1}
_cache: dict[str, tuple[float, dict]] = {}
_problems: dict[str, str] = {}


def _load(name: str) -> dict | None:
    path = os.path.join(KNOWLEDGE_DIR, name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _problems[name] = "missing"
        return None
    hit = _cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        _problems[name] = "unreadable"
        return None
    version = data.get("schema_version")
    if version is not None and int(version) > SUPPORTED_SCHEMA[name]:
        _problems[name] = f"schema_version {version} is newer than the {SUPPORTED_SCHEMA[name]} this server reads: update the DOI MCP"
        return None
    _problems.pop(name, None)
    _cache[name] = (mtime, data)
    return data


def norm_doi(doi: str | None) -> str:
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", (doi or "").strip(), flags=re.I).rstrip(".").lower()


def library() -> dict | None:
    return _load("library.json")


def notes() -> dict | None:
    return _load("notes.json")


def status() -> dict:
    """When each snapshot was generated (None when absent or unusable), and why when unusable."""
    lib, nts = library(), notes()
    out = {"library_snapshot": lib.get("generated") if lib else None,
           "notes_snapshot": nts.get("generated") if nts else None}
    problems = {k: v for k, v in _problems.items() if v != "missing"}
    if problems:
        out["problems"] = problems
    return out


def by_doi() -> dict[str, dict]:
    lib = library()
    if not lib:
        return {}
    out = {}
    for it in lib.get("items", []):
        for d in [it.get("doi")] + it.get("extra_dois", []):
            if d:
                out.setdefault(norm_doi(d), it)
    return out


def notes_by_key() -> dict[str, list[dict]]:
    nts = notes()
    if not nts:
        return {}
    out: dict[str, list[dict]] = {}
    for n in nts.get("notes", []):
        for k in n.get("zotero_keys", []):
            out.setdefault(k, []).append({"title": n.get("title"), "path": n.get("path")})
    return out


def items_where(collection: str | None = None, tag: str | None = None) -> list[dict]:
    """Library items in a collection (name or full path, subcollections included) or with a tag."""
    lib = library()
    if not lib:
        return []
    want_c = collection.strip().lower() if collection else None
    want_t = tag.strip().lower() if tag else None
    out = []
    for it in lib.get("items", []):
        paths = [p.lower() for p in it.get("collections", [])]
        if want_c and not any(p == want_c or p.endswith(" › " + want_c) or p.startswith(want_c + " › ")
                              or (" › " + want_c + " › ") in p for p in paths):
            continue
        if want_t and want_t not in [t.lower() for t in it.get("tags", [])]:
            continue
        out.append(it)
    return out


def annotate(nodes: list[dict]) -> None:
    """Add in_library, zotero_key and notes to map nodes (in place)."""
    lib, by_key = by_doi(), notes_by_key()
    keys = {it["key"]: it for it in (library() or {}).get("items", [])}
    for n in nodes:
        it = lib.get(norm_doi(n.get("doi"))) if n.get("doi") else None
        # books without DOI: the node's zotero_key (set when the seed came from Zotero) or a "Z:KEY" id
        zk = n.get("zotero_key") or (n["id"][2:] if str(n.get("id", "")).startswith("Z:") else None)
        it = it or (keys.get(zk) if zk else None)
        n["in_library"] = bool(it)
        n["zotero_key"] = it["key"] if it else None
        n["notes"] = by_key.get(it["key"], []) if it else []
        # only the tags that mean something on a map: core/seminal and cited/*
        n["zotero_tags"] = [t for t in it.get("tags", []) if t == "core/seminal" or t.startswith("cited/")] if it else []


def item_by_key(key: str) -> dict | None:
    lib = library()
    return next((it for it in (lib or {}).get("items", []) if it["key"] == key), None)


def references(item_key: str) -> list[str] | None:
    """Raw bibliography entries extracted from the item's PDF by the Zotero MCP
    (extract_references); None when not extracted yet."""
    try:
        data = json.load(open(os.path.join(KNOWLEDGE_DIR, "references", f"{item_key}.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("entries") or []

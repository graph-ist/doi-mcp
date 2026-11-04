"""One full-text index over the three archives: Obsidian notes (notes_text.json, written by the
Obsidian MCP), the PDF/EPUB texts of Zotero items page by page and their highlights and comments
(fulltext/KEY.json, written by the Zotero MCP's index_fulltext). All of it sits in KNOWLEDGE_DIR;
this module keeps an SQLite FTS5 index of it in STATE_DIRECTORY and answers search_everything.

The index follows the files: every call compares their mtimes with what was indexed and
re-indexes only what changed, so no service has to notify another. Passages are the unit:
a note section (level-4 heading) split into chunks of at most ~1200 characters, a PDF page,
one annotation. Retrieval is lexical (FTS5, unicode61 with diacritics removed): a query in
Italian does not find a German passage; the model is told so in the result.
"""
import json
import os
import re
import sqlite3
import time

import knowledge

DB_PATH = os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/doi-mcp").split(":")[0], "corpus.sqlite")
CHUNK = 1200
SCOPES = ("notes", "library", "annotations")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, mtime REAL NOT NULL, indexed TEXT NOT NULL, passages INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
  scope UNINDEXED, source UNINDEXED, ref UNINDEXED, locator UNINDEXED, title, text,
  tokenize = 'unicode61 remove_diacritics 2');
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    db = sqlite3.connect(p, check_same_thread=False)  # used from asyncio.to_thread
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


# --- splitting -----------------------------------------------------------------------------------

def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Paragraph-aligned chunks of at most about `size` characters."""
    out, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 2 > size and buf:
            out.append(buf)
            buf = ""
        while len(para) > size:
            out.append(para[:size])
            para = para[size:]
        buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


def note_passages(path: str, title: str, text: str) -> list[tuple]:
    """(scope, source, ref, locator, title, text) per section chunk of a note."""
    rows = []
    section = ""
    pieces = re.split(r"^(#{1,6}\s+.+?)\s*$", text, flags=re.M)
    # pieces alternates: text, heading, text, heading, ...
    for i, piece in enumerate(pieces):
        if i % 2 == 1:
            section = re.sub(r"^#+\s+", "", piece)
            continue
        for ch in chunks(piece):
            rows.append(("notes", "notes_text.json", path, section, title, ch))
    return rows


def item_passages(name: str, doc: dict) -> list[tuple]:
    key = doc.get("item") or name.removesuffix(".json")
    title = doc.get("title") or key
    rows = []
    for p in doc.get("pages") or []:
        for ch in chunks(p.get("text") or ""):
            rows.append(("library", f"fulltext/{name}", key, f"p. {p.get('page')}", title, ch))
    for a in doc.get("annotations") or []:
        text = " ".join(x for x in (a.get("text"), a.get("comment")) if x)
        if text.strip():
            rows.append(("annotations", f"fulltext/{name}", key, f"p. {a.get('page') or '?'}", title, text))
    return rows


# --- sync ----------------------------------------------------------------------------------------

def _mtimes(knowledge_dir: str) -> dict[str, float]:
    out = {}
    p = os.path.join(knowledge_dir, "notes_text.json")
    if os.path.exists(p):
        out["notes_text.json"] = os.path.getmtime(p)
    folder = os.path.join(knowledge_dir, "fulltext")
    if os.path.isdir(folder):
        for f in os.listdir(folder):
            if f.endswith(".json"):
                out[f"fulltext/{f}"] = os.path.getmtime(os.path.join(folder, f))
    return out


def sync(db: sqlite3.Connection, knowledge_dir: str | None = None) -> dict:
    """Bring the index in line with the files; returns what changed."""
    kdir = knowledge_dir or knowledge.KNOWLEDGE_DIR
    current = _mtimes(kdir)
    known = {r["name"]: r["mtime"] for r in db.execute("SELECT name, mtime FROM sources")}
    added, removed = 0, 0
    with db:
        for name in set(known) - set(current):
            db.execute("DELETE FROM passages WHERE source = ?", (name,))
            db.execute("DELETE FROM sources WHERE name = ?", (name,))
            removed += 1
        for name, mtime in current.items():
            if known.get(name) == mtime:
                continue
            try:
                data = json.load(open(os.path.join(kdir, name), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if name == "notes_text.json":
                if int(data.get("schema_version", 1)) > knowledge.SUPPORTED_SCHEMA["notes.json"]:
                    continue
                rows = [r for n in data.get("notes", []) for r in note_passages(n["path"], n.get("title") or n["path"], n.get("text") or "")]
            else:
                rows = item_passages(name.split("/", 1)[1], data)
            db.execute("DELETE FROM passages WHERE source = ?", (name,))
            db.executemany("INSERT INTO passages (scope, source, ref, locator, title, text) VALUES (?, ?, ?, ?, ?, ?)", rows)
            db.execute("INSERT OR REPLACE INTO sources (name, mtime, indexed, passages) VALUES (?, ?, ?, ?)",
                       (name, mtime, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), len(rows)))
            added += 1
    return {"reindexed": added, "removed": removed, "sources": len(current)}


def stats(db: sqlite3.Connection) -> dict:
    by_scope = {r["scope"]: r["n"] for r in db.execute("SELECT scope, count(*) AS n FROM passages GROUP BY scope")}
    srcs = db.execute("SELECT count(*) AS n, max(indexed) AS last FROM sources").fetchone()
    return {"passages": by_scope, "sources": srcs["n"], "last_indexed": srcs["last"]}


# --- query ---------------------------------------------------------------------------------------

def fts_query(query: str) -> str:
    """User query → FTS5 syntax. Phrases in quotes stay phrases; AND, OR, NOT are operators;
    every other word is quoted so punctuation cannot break the parser; implicit AND."""
    tokens = re.findall(r'"[^"]+"|\S+', query.strip())
    out = []
    for t in tokens:
        if t.startswith('"') and t.endswith('"') and len(t) > 2:
            out.append('"' + t.strip('"').replace('"', '') + '"')
        elif t.upper() in ("AND", "OR", "NOT"):
            out.append(t.upper())
        elif t == "(" or t == ")":
            out.append(t)
        else:
            w = re.sub(r'[\"*]', "", t)
            if w:
                out.append(f'"{w}"' + ("*" if t.endswith("*") else ""))
    return " ".join(out)


def search(db: sqlite3.Connection, query: str, scope: str = "all", limit: int = 20, ref: str | None = None) -> list[dict]:
    q = fts_query(query)
    if not q:
        return []
    where, params = ["passages MATCH ?"], [q]
    if scope != "all":
        where.append("scope = ?")
        params.append(scope)
    if ref:
        where.append("ref = ?")
        params.append(ref)
    sql = ("SELECT scope, ref, locator, title, snippet(passages, 5, '[', ']', ' … ', 24) AS snippet, bm25(passages) AS rank "
           f"FROM passages WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?")
    params.append(int(limit))
    try:
        rows = db.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        raise ValueError(f"query not understood by the index ({e}); use words, \"phrases\", AND, OR, NOT") from None
    out = []
    for r in rows:
        d = {"scope": r["scope"], "ref": r["ref"], "locator": r["locator"] or None, "title": r["title"], "snippet": r["snippet"],
             "rank": round(-r["rank"], 3)}
        if r["scope"] == "notes":
            d["note"] = r["ref"]
        else:
            d["zotero_key"] = r["ref"]
            page = re.sub(r"\D", "", r["locator"] or "")
            d["open"] = f"zotero://open-pdf/library/items/{r['ref']}" + (f"?page={page}" if page else "")
        out.append(d)
    return out

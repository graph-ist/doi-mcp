"""Persistent literature maps (Litmaps / ResearchRabbit style) in SQLite.

A map holds works (OpenAlex ids) with their metadata and reference lists, a role ('seed' or
'candidate') and a reading state that lives here, never in the notes. Links (citations,
co-citation, coupling) are not stored: they are recomputed from the reference lists, so they
stay consistent whatever is added or removed.
"""
import json
import os
import re
import sqlite3
import time

DB_PATH = os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/doi-mcp").split(":")[0], "maps.sqlite")
STATES = ("new", "to_read", "read", "dismissed")
NODE_FIELDS = ("doi", "title", "first_author", "year", "cited_by", "field", "is_retracted")

SCHEMA = """
CREATE TABLE IF NOT EXISTS maps (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, description TEXT,
  created TEXT NOT NULL, updated TEXT NOT NULL, watched TEXT);
CREATE TABLE IF NOT EXISTS nodes (
  map_id INTEGER NOT NULL REFERENCES maps(id) ON DELETE CASCADE,
  work_id TEXT NOT NULL, doi TEXT, title TEXT, first_author TEXT, year INTEGER,
  cited_by INTEGER, field TEXT, is_retracted INTEGER, refs TEXT NOT NULL DEFAULT '[]',
  refs_source TEXT,
  role TEXT NOT NULL CHECK (role IN ('seed', 'candidate')),
  state TEXT NOT NULL DEFAULT 'new' CHECK (state IN ('new', 'to_read', 'read', 'dismissed')),
  found_by TEXT NOT NULL DEFAULT '[]', added TEXT NOT NULL, note TEXT,
  PRIMARY KEY (map_id, work_id));
"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    # columns added after the first release
    for table, column, kind in (("nodes", "is_retracted", "INTEGER"), ("nodes", "refs_source", "TEXT"),
                                ("maps", "watched", "TEXT")):
        if column not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    return db


def map_row(db: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM maps WHERE name = ?", (name.strip(),)).fetchone()
    if row is None:
        raise KeyError(f"no map named {name!r}")
    return row


def create(db: sqlite3.Connection, name: str, description: str | None = None) -> int:
    name = name.strip()
    if not re.fullmatch(r"[\w .,'()&+-]{1,80}", name, re.UNICODE):
        raise ValueError("map name: 1-80 letters, digits, spaces or .,'()&+-")
    with db:
        cur = db.execute("INSERT INTO maps (name, description, created, updated) VALUES (?, ?, ?, ?)",
                         (name, description, now(), now()))
    return cur.lastrowid


def upsert(db: sqlite3.Connection, map_id: int, node: dict, role: str, found_by: str) -> str:
    """Add a work, or refresh its metadata. A seed never goes back to candidate; 'found_by'
    accumulates how the work entered the map (seed, references:W1, citing:W2, ...)."""
    old = db.execute("SELECT role, found_by FROM nodes WHERE map_id = ? AND work_id = ?",
                     (map_id, node["id"])).fetchone()
    values = [node.get(k) for k in NODE_FIELDS]
    refs = json.dumps([r for r in node.get("refs") or [] if r])
    if old is None:
        db.execute("INSERT INTO nodes (map_id, work_id, doi, title, first_author, year, cited_by, field,"
                   " is_retracted, refs, refs_source, role, found_by, added) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (map_id, node["id"], *values, refs, node.get("refs_source"), role, json.dumps([found_by]), now()))
        return "added"
    sources = json.loads(old["found_by"])
    if found_by not in sources:
        sources.append(found_by)
    new_role = "seed" if "seed" in (old["role"], role) else "candidate"
    db.execute("UPDATE nodes SET doi = ?, title = ?, first_author = ?, year = ?, cited_by = ?, field = ?,"
               " is_retracted = ?, refs = CASE WHEN ? = '[]' THEN refs ELSE ? END,"
               " refs_source = CASE WHEN ? = '[]' THEN refs_source ELSE ? END, role = ?, found_by = ?"
               " WHERE map_id = ? AND work_id = ?",
               (*values, refs, refs, refs, node.get("refs_source"), new_role, json.dumps(sources), map_id, node["id"]))
    return "promoted" if new_role != old["role"] else "updated"


def touch(db: sqlite3.Connection, map_id: int) -> None:
    db.execute("UPDATE maps SET updated = ? WHERE id = ?", (now(), map_id))


def nodes(db: sqlite3.Connection, map_id: int) -> dict[str, dict]:
    out = {}
    for r in db.execute("SELECT * FROM nodes WHERE map_id = ? ORDER BY added", (map_id,)):
        n = dict(r)
        n["id"] = n.pop("work_id")
        n["refs"] = json.loads(n["refs"])
        n["found_by"] = json.loads(n["found_by"])
        n["is_retracted"] = None if n["is_retracted"] is None else bool(n["is_retracted"])
        del n["map_id"]
        out[n["id"]] = n
    return out


def mark(db: sqlite3.Connection, map_id: int, work_ids: list[str], state: str, note: str | None) -> list[str]:
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    missing = []
    with db:
        for wid in work_ids:
            cur = db.execute("UPDATE nodes SET state = ?, note = COALESCE(?, note) WHERE map_id = ? AND work_id = ?",
                             (state, note, map_id, wid))
            if not cur.rowcount:
                missing.append(wid)
        touch(db, map_id)
    return missing


def remove(db: sqlite3.Connection, map_id: int, work_ids: list[str]) -> int:
    with db:
        n = sum(db.execute("DELETE FROM nodes WHERE map_id = ? AND work_id = ?", (map_id, w)).rowcount
                for w in work_ids)
        touch(db, map_id)
    return n


def summary(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT m.name, m.description, m.created, m.updated,"
        " SUM(n.role = 'seed') AS seeds, COUNT(n.work_id) AS works,"
        " SUM(n.state = 'to_read') AS to_read, SUM(n.state = 'read') AS read,"
        " SUM(n.state = 'dismissed') AS dismissed"
        " FROM maps m LEFT JOIN nodes n ON n.map_id = m.id GROUP BY m.id ORDER BY m.updated DESC").fetchall()
    return [{k: (r[k] or 0) if k in ("seeds", "works", "to_read", "read", "dismissed") else r[k]
             for k in r.keys()} for r in rows]


def rename(db: sqlite3.Connection, map_id: int, name: str | None, description: str | None) -> None:
    with db:
        if name is not None:
            name = name.strip()
            if not re.fullmatch(r"[\w .,'()&+-]{1,80}", name, re.UNICODE):
                raise ValueError("map name: 1-80 letters, digits, spaces or .,'()&+-")
            db.execute("UPDATE maps SET name = ? WHERE id = ?", (name, map_id))
        if description is not None:
            db.execute("UPDATE maps SET description = ? WHERE id = ?", (description or None, map_id))
        touch(db, map_id)


def delete(db: sqlite3.Connection, map_id: int) -> int:
    with db:
        works = db.execute("SELECT COUNT(*) FROM nodes WHERE map_id = ?", (map_id,)).fetchone()[0]
        db.execute("DELETE FROM nodes WHERE map_id = ?", (map_id,))
        db.execute("DELETE FROM maps WHERE id = ?", (map_id,))
    return works


def set_watched(db: sqlite3.Connection, map_id: int, when: str) -> None:
    with db:
        db.execute("UPDATE maps SET watched = ? WHERE id = ?", (when, map_id))

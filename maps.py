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
import threading
import time

DB_PATH = os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/doi-mcp").split(":")[0], "maps.sqlite")
STATES = ("new", "to_read", "read", "dismissed")
NODE_FIELDS = ("doi", "title", "first_author", "year", "cited_by", "field", "is_retracted", "subfield", "topic", "momentum", "zotero_key", "cited_by_oc")

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
-- references read from PDFs (books, humanities) resolved to OpenAlex works; work_id '' = not found
CREATE TABLE IF NOT EXISTS ref_cache (
  key TEXT PRIMARY KEY, work_id TEXT NOT NULL, checked TEXT NOT NULL);
-- why one work cites another, from Semantic Scholar (map_intents): intents, influence, the
-- sentences around the citation, and whether those sentences read as contrasting (heuristic)
CREATE TABLE IF NOT EXISTS edge_intents (
  map_id INTEGER NOT NULL, citing TEXT NOT NULL, cited TEXT NOT NULL, intents TEXT NOT NULL,
  influential INTEGER NOT NULL DEFAULT 0, contexts TEXT NOT NULL, contrasting INTEGER NOT NULL DEFAULT 0,
  fetched TEXT NOT NULL, PRIMARY KEY (map_id, citing, cited));
-- background jobs of the long map tools (map_job_status); result is the tool's own JSON payload
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, tool TEXT, map_id INTEGER, status TEXT, progress_done INTEGER, progress_total INTEGER,
  step TEXT, result TEXT, error TEXT, created TEXT, updated TEXT, finished TEXT);
-- works cited in PDFs that OpenAlex does not have: id 'B:...', shown as book nodes
CREATE TABLE IF NOT EXISTS ref_stubs (
  id TEXT PRIMARY KEY, title TEXT, first_author TEXT, year INTEGER);
"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_READY: set[str] = set()  # database files already migrated by this process
_READY_LOCK = threading.Lock()
BUSY_TIMEOUT_MS = 15000


def _migrate(db: sqlite3.Connection) -> None:
    """Journal mode, schema and columns added after the first release: idempotent, once per process
    and database file (see connect)."""
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    for table, column, kind in (("nodes", "is_retracted", "INTEGER"), ("nodes", "refs_source", "TEXT"),
                                ("maps", "watched", "TEXT"), ("nodes", "subfield", "TEXT"), ("nodes", "topic", "TEXT"),
                                ("nodes", "momentum", "INTEGER"), ("nodes", "zotero_key", "TEXT"),
                                ("nodes", "cited_by_oc", "INTEGER")):
        if column not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
            try:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            except sqlite3.OperationalError as e:  # another process added it meanwhile
                if "duplicate column" not in str(e):
                    raise
    with db:
        # intents of deleted maps (maps.id is reused after deleting the newest map)
        db.execute("DELETE FROM edge_intents WHERE map_id NOT IN (SELECT id FROM maps)")
        # a job cannot be running in a process that has just started: its process died
        db.execute("UPDATE jobs SET status = 'interrupted', finished = ?, updated = ?,"
                   " error = COALESCE(error, 'server restarted while the job was running') WHERE status = 'running'",
                   (now(), now()))


def connect(path: str | None = None) -> sqlite3.Connection:
    """A new connection (one per request; the caller closes it). Waits up to 15 s for a lock.
    WAL and the migrations are applied the first time a process opens a database file."""
    path = os.path.abspath(path or DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    db.row_factory = sqlite3.Row
    try:
        db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA foreign_keys = ON")
        if path not in _READY or not os.path.exists(path):
            with _READY_LOCK:
                _migrate(db)
                _READY.add(path)
    except BaseException:
        db.close()
        raise
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
    cols = ", ".join(NODE_FIELDS)
    if old is None:
        db.execute(f"INSERT INTO nodes (map_id, work_id, {cols}, refs, refs_source, role, found_by, added)"
                   f" VALUES (?, ?, {', '.join('?' * len(NODE_FIELDS))}, ?, ?, ?, ?, ?)",
                   (map_id, node["id"], *values, refs, node.get("refs_source"), role, json.dumps([found_by]), now()))
        return "added"
    sources = json.loads(old["found_by"])
    if found_by not in sources:
        sources.append(found_by)
    new_role = "seed" if "seed" in (old["role"], role) else "candidate"
    # a field missing from the new record (None) keeps the stored value
    sets = ", ".join(f"{c} = COALESCE(?, {c})" for c in NODE_FIELDS)
    db.execute(f"UPDATE nodes SET {sets}, refs = CASE WHEN ? = '[]' THEN refs ELSE ? END,"
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
        # the citation intents of a removed work go with it
        db.executemany("DELETE FROM edge_intents WHERE map_id = ? AND (citing = ? OR cited = ?)",
                       [(map_id, w, w) for w in work_ids])
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
        db.execute("DELETE FROM edge_intents WHERE map_id = ?", (map_id,))
        db.execute("DELETE FROM jobs WHERE map_id = ?", (map_id,))
        db.execute("DELETE FROM maps WHERE id = ?", (map_id,))
    return works


def set_watched(db: sqlite3.Connection, map_id: int, when: str) -> None:
    with db:
        db.execute("UPDATE maps SET watched = ? WHERE id = ?", (when, map_id))


def cached_ref(db: sqlite3.Connection, key: str) -> str | None:
    """OpenAlex id for a reference key, '' when known to be missing, None when never looked up."""
    row = db.execute("SELECT work_id FROM ref_cache WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def cache_ref(db: sqlite3.Connection, key: str, work_id: str) -> None:
    with db:
        db.execute("INSERT OR REPLACE INTO ref_cache (key, work_id, checked) VALUES (?, ?, ?)", (key, work_id, now()))


def save_stub(db: sqlite3.Connection, stub: dict) -> None:
    with db:
        db.execute("INSERT OR IGNORE INTO ref_stubs (id, title, first_author, year) VALUES (?, ?, ?, ?)",
                   (stub["id"], stub.get("title"), stub.get("first_author"), stub.get("year")))


def stubs(db: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in db.execute(f"SELECT * FROM ref_stubs WHERE id IN ({','.join('?' * len(part))})", part):
            out[r["id"]] = dict(r)
    return out


def save_intents(db: sqlite3.Connection, map_id: int, rows: list[dict]) -> None:
    with db:
        db.executemany("INSERT OR REPLACE INTO edge_intents (map_id, citing, cited, intents, influential, contexts, contrasting, fetched)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       [(map_id, r["citing"], r["cited"], json.dumps(r.get("intents") or []), int(bool(r.get("influential"))),
                         json.dumps(r.get("contexts") or []), int(bool(r.get("contrasting"))), now()) for r in rows])


def intents(db: sqlite3.Connection, map_id: int) -> dict[str, dict]:
    """'citing->cited' -> {intents, influential, contexts, contrasting, fetched}."""
    out = {}
    for r in db.execute("SELECT * FROM edge_intents WHERE map_id = ?", (map_id,)):
        out[f"{r['citing']}->{r['cited']}"] = {"intents": json.loads(r["intents"]), "influential": bool(r["influential"]),
                                               "contexts": json.loads(r["contexts"]), "contrasting": bool(r["contrasting"]),
                                               "fetched": r["fetched"]}
    return out


def job(db: sqlite3.Connection, job_id: str) -> dict | None:
    """A background job as map_job_status reports it, or None."""
    r = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if r is None:
        return None
    return {"status": r["status"], "tool": r["tool"],
            "progress": {"done": r["progress_done"] or 0, "total": r["progress_total"] or 0, "step": r["step"] or ""},
            "result": json.loads(r["result"]) if r["result"] else None, "error": r["error"],
            "created": r["created"], "updated": r["updated"], "finished": r["finished"]}

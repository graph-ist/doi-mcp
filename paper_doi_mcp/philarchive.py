"""PhilArchive, harvested: the open archive of philosophy preprints exposes OAI-PMH (harvest by
date and set), not search. This module harvests its Dublin Core records into an SQLite FTS5
database in STATE_DIRECTORY, incrementally from the last harvest, and search_literature
queries that local copy as database 'philarchive'. The weekly job runs the harvest; by hand:
    philarchive.py harvest [--full]
Env: PHILARCHIVE_OAI_URL (default https://philarchive.org/oai.pl), STATE_DIRECTORY.
PhilPapers itself (the index) has no API; this covers the preprints, not the whole index.
"""
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from .paths import state_dir

OAI_URL = os.environ.get("PHILARCHIVE_OAI_URL", "https://philarchive.org/oai.pl")
DB_PATH = os.path.join(state_dir(), "philarchive.sqlite")
NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "dc": "http://purl.org/dc/elements/1.1/",
      "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, title TEXT, creators TEXT, year INTEGER, date TEXT, description TEXT,
  subjects TEXT, url TEXT, doi TEXT, language TEXT, datestamp TEXT, deleted INTEGER NOT NULL DEFAULT 0);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
  id UNINDEXED, title, creators, description, subjects, tokenize = 'unicode61 remove_diacritics 2');
"""
DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    db = sqlite3.connect(p, check_same_thread=False)  # used from asyncio.to_thread
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def parse_records(xml: bytes | str) -> tuple[list[dict], str | None]:
    """Records of one ListRecords page and the resumption token, if any."""
    root = ET.fromstring(xml)
    out = []
    for rec in root.iter(f"{{{NS['oai']}}}record"):
        header = rec.find("oai:header", NS)
        ident = header.findtext("oai:identifier", default="", namespaces=NS)
        stamp = header.findtext("oai:datestamp", default="", namespaces=NS)
        if header.get("status") == "deleted":
            out.append({"id": ident, "deleted": 1, "datestamp": stamp})
            continue
        dc = rec.find("oai:metadata/oai_dc:dc", NS)
        if dc is None:
            continue
        txt = lambda tag: [" ".join((e.text or "").split()) for e in dc.findall(f"dc:{tag}", NS) if (e.text or "").strip()]
        ids = txt("identifier")
        dates = txt("date")
        year = next((int(m.group(0)) for d in dates if (m := re.match(r"(1[5-9]|20)\d{2}", d))), None)
        out.append({"id": ident, "deleted": 0, "datestamp": stamp, "title": (txt("title") or [None])[0],
                    "creators": "; ".join(txt("creator")), "year": year, "date": (dates or [None])[0],
                    "description": " ".join(txt("description")) or None, "subjects": "; ".join(txt("subject")) or None,
                    "url": next((i for i in ids if i.startswith("http")), None),
                    "doi": next((m.group(0).rstrip(".") for i in ids if (m := DOI_RE.search(i))), None),
                    "language": (txt("language") or [None])[0]})
    token = root.findtext(".//oai:resumptionToken", default=None, namespaces=NS)
    return out, (token.strip() if token and token.strip() else None)


def store(db: sqlite3.Connection, records: list[dict]) -> int:
    n = 0
    with db:
        for r in records:
            db.execute("DELETE FROM records_fts WHERE id = ?", (r["id"],))
            if r.get("deleted"):
                db.execute("INSERT OR REPLACE INTO records (id, datestamp, deleted) VALUES (?, ?, 1)", (r["id"], r["datestamp"]))
                continue
            db.execute("INSERT OR REPLACE INTO records (id, title, creators, year, date, description, subjects, url, doi, language, datestamp, deleted)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                       (r["id"], r["title"], r["creators"], r["year"], r["date"], r["description"], r["subjects"], r["url"], r["doi"], r["language"], r["datestamp"]))
            db.execute("INSERT INTO records_fts (id, title, creators, description, subjects) VALUES (?, ?, ?, ?, ?)",
                       (r["id"], r["title"] or "", r["creators"] or "", r["description"] or "", r["subjects"] or ""))
            n += 1
    return n


def harvest(db: sqlite3.Connection, fetch=None, full: bool = False, max_pages: int = 2000) -> dict:
    """Harvest new and changed records since the last run (or everything with full). `fetch`
    takes query params and returns the XML body; the default uses httpx with a 60 s timeout."""
    if fetch is None:
        import httpx

        def fetch(params):
            r = httpx.get(OAI_URL, params=params, timeout=60, headers={"User-Agent": "paper-doi-mcp philarchive harvest"})
            r.raise_for_status()
            return r.content
    since = None if full else (db.execute("SELECT value FROM meta WHERE key = 'last_harvest'").fetchone() or [None])[0]
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    params = {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
    if since:
        params["from"] = since[:10]
    pages, stored, token = 0, 0, None
    while pages < max_pages:
        body = fetch({"verb": "ListRecords", "resumptionToken": token} if token else params)
        if b"noRecordsMatch" in (body if isinstance(body, bytes) else body.encode()):
            break
        records, token = parse_records(body)
        stored += store(db, records)
        pages += 1
        if not token:
            break
    with db:
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_harvest', ?)", (started,))
    return {"status": "ok", "pages": pages, "records": stored, "since": since, "full": full or since is None}


def count(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT count(*) AS n, max(datestamp) AS newest FROM records WHERE deleted = 0").fetchone()
    last = (db.execute("SELECT value FROM meta WHERE key = 'last_harvest'").fetchone() or [None])[0]
    return {"records": row["n"], "newest_record": row["newest"], "last_harvest": last}


def _fts(query: str) -> str:
    tokens = re.findall(r'"[^"]+"|\S+', query.strip())
    out = []
    for t in tokens:
        if t.startswith('"') and t.endswith('"') and len(t) > 2:
            out.append('"' + t.strip('"').replace('"', "") + '"')
        elif t.upper() in ("AND", "OR", "NOT") or t in "()":
            out.append(t.upper())
        else:
            w = re.sub(r'["*]', "", t)
            if w:
                out.append(f'"{w}"')
    return " ".join(out)


def search(db: sqlite3.Connection, query: str, year_from: int | None, year_to: int | None, limit: int, page: int,
           scope: str = "title_abstract") -> tuple[list[dict], int]:
    """Records matching the query (title and abstract, or every indexed field), newest first
    among equal ranks; returns (page of records, total hits)."""
    q = _fts(query)
    if not q:
        return [], 0
    cols = "{title creators description subjects}" if scope == "fulltext" else "{title description}"
    where = [f"records_fts MATCH ?"]
    params: list = [f"{cols} : ({q})"]
    if year_from:
        where.append("r.year >= ?")
        params.append(int(year_from))
    if year_to:
        where.append("r.year <= ?")
        params.append(int(year_to))
    base = (f"FROM records_fts f JOIN records r ON r.id = f.id WHERE r.deleted = 0 AND {' AND '.join(where)}")
    try:
        total = db.execute(f"SELECT count(*) {base}", params).fetchone()[0]
        rows = db.execute(f"SELECT r.*, bm25(records_fts) AS rank {base} ORDER BY rank, r.year DESC LIMIT ? OFFSET ?",
                          [*params, int(limit), (int(page) - 1) * int(limit)]).fetchall()
    except sqlite3.OperationalError as e:
        raise ValueError(f"query not understood ({e}); use words, \"phrases\", AND, OR, NOT") from None
    return [dict(r) for r in rows], total


def main() -> None:
    db = connect()
    if "harvest" in sys.argv:
        print(harvest(db, full="--full" in sys.argv))
    else:
        print(count(db))


if __name__ == "__main__":
    main()

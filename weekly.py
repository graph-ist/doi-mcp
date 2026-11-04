#!/usr/bin/env python3
"""The weekly job: what changed in the literature, in the library and in the reading since last
week, written as a Markdown report for the vault inbox (the Obsidian MCP copies it from
KNOWLEDGE_DIR/reports), plus the maintenance nobody remembers to ask for.

Steps, each failing on its own without stopping the others:
1. map_refresh(new_citing=True) on every literature map: new works citing the seeds;
2. check_retractions on every DOI of the Zotero library snapshot;
3. coverage per project (every cited/<project> tag in the library);
4. PhilArchive harvest (incremental);
5. backups: maps.sqlite, philarchive.sqlite and the knowledge directory, keeping the last
   BACKUP_KEEP (8) in BACKUP_DIR (default STATE_DIRECTORY/backups).
Run by deploy/doi-weekly.timer; by hand: weekly.py [--no-backup] [--no-harvest].
Env: STATE_DIRECTORY, KNOWLEDGE_DIR, BACKUP_DIR, BACKUP_KEEP, plus the server's own.
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import time

STATE = os.environ.get("STATE_DIRECTORY", "/var/lib/doi-mcp").split(":")[0]
BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(STATE, "backups"))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "8"))
LAST = os.path.join(STATE, "weekly-last.json")


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


async def refresh_maps(server) -> dict:
    out = {}
    for m in (await server.map_list()).get("maps", []):
        r = await server.map_refresh(m["name"], new_citing=True)
        out[m["name"]] = {k: r.get(k) for k in ("status", "new_citing", "added", "updated", "retracted") if k in r}
    return out


async def retractions(server, knowledge) -> dict:
    dois = sorted(knowledge.by_doi())
    found, not_checked = [], 0
    for i in range(0, len(dois), 1000):
        r = await server.check_retractions(dois=dois[i:i + 1000])
        found += r.get("notices") or r.get("retracted") or r.get("results") or []
        not_checked += len(r.get("not_checked") or [])
    return {"checked": len(dois), "notices": found, "not_checked": not_checked}


def coverage(knowledge) -> dict:
    lib = knowledge.library() or {}
    projects = sorted({t.split("/", 1)[1] for it in lib.get("items", []) for t in it.get("tags", []) if t.startswith("cited/") and "/" in t})
    return {p: knowledge.coverage(project=p) for p in projects}


def harvest() -> dict:
    import philarchive
    db = philarchive.connect()
    try:
        return philarchive.harvest(db)
    finally:
        db.close()


def backup(knowledge_dir: str) -> dict:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    made = []
    for name in ("maps.sqlite", "philarchive.sqlite", "corpus.sqlite"):
        src = os.path.join(STATE, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(BACKUP_DIR, f"{stamp}-{name}")
        with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
            a.backup(b)  # consistent copy even while the server writes
        made.append(dst)
    if os.path.isdir(knowledge_dir):
        dst = os.path.join(BACKUP_DIR, f"{stamp}-knowledge.tar.gz")
        with tarfile.open(dst, "w:gz") as tar:
            tar.add(knowledge_dir, arcname="knowledge")
        made.append(dst)
    # rotation: keep the newest BACKUP_KEEP of each kind
    by_kind: dict[str, list[str]] = {}
    for f in sorted(os.listdir(BACKUP_DIR)):
        kind = f.split("-", 2)[-1] if f.count("-") >= 2 else f
        by_kind.setdefault(kind, []).append(f)
    removed = 0
    for files in by_kind.values():
        for f in files[:-BACKUP_KEEP]:
            os.remove(os.path.join(BACKUP_DIR, f))
            removed += 1
    return {"made": made, "removed": removed, "dir": BACKUP_DIR}


def render(results: dict) -> str:
    lines = [f"# Weekly report {today()}", ""]
    maps = results.get("maps") or {}
    lines.append("## Literature maps")
    if isinstance(maps, dict) and maps and "error" not in maps:
        for name, r in maps.items():
            new = r.get("new_citing") or r.get("added") or 0
            lines.append(f"- **{name}**: {new} new citing work(s) since last check" + (f", {r['retracted']} retracted" if r.get("retracted") else ""))
    else:
        lines.append(f"- {maps.get('error') if isinstance(maps, dict) else 'no maps'}")
    lines += ["", "## Retractions in the library"]
    ret = results.get("retractions") or {}
    notices = ret.get("notices") or []
    if ret.get("error"):
        lines.append(f"- {ret['error']}")
    elif notices:
        for n in notices:
            lines.append(f"- {n.get('doi') or n}: {n.get('type') or n.get('status') or 'notice'}")
    else:
        lines.append(f"- none among {ret.get('checked', 0)} DOIs checked ({ret.get('not_checked', 0)} not checkable)")
    lines += ["", "## Coverage by project"]
    cov = results.get("coverage") or {}
    if isinstance(cov, dict) and "error" in cov:
        lines.append(f"- {cov['error']}")
    for project, c in (cov.items() if isinstance(cov, dict) else []):
        if not isinstance(c, dict) or c.get("status") != "ok":
            continue
        lines.append(f"- **{project}**: {c['items']} items, {c['with_note']} with a note, {len(c['without_pdf'])} without PDF, {len(c['unverified'])} unverified")
        for r in c["without_note"][:15]:
            lines.append(f"  - to read or to write up: {r.get('title')} ({r.get('year') or 'n.d.'}) · zotero://select/library/items/{r['key']}")
        for n in c.get("notes_with_missing_item", [])[:10]:
            lines.append(f"  - note whose item is gone: [[{n['note'].rsplit('/', 1)[-1].removesuffix('.md')}]]")
    lines += ["", "## Maintenance"]
    h = results.get("harvest") or {}
    lines.append(f"- PhilArchive: {h.get('records', 0)} record(s) harvested" if "error" not in h else f"- PhilArchive: {h['error']}")
    b = results.get("backup") or {}
    lines.append(f"- backups: {len(b.get('made', []))} file(s) in {b.get('dir')}" if "error" not in b else f"- backups: {b['error']}")
    lines += ["", "_Generated by the DOI MCP weekly job; the Obsidian MCP copies it to the inbox._", ""]
    return "\n".join(lines)


async def run(server, knowledge, do_backup: bool = True, do_harvest: bool = True) -> dict:
    results: dict = {"date": today()}
    steps = [("maps", lambda: refresh_maps(server)), ("retractions", lambda: retractions(server, knowledge)),
             ("coverage", lambda: asyncio.to_thread(coverage, knowledge))]
    if do_harvest:
        steps.append(("harvest", lambda: asyncio.to_thread(harvest)))
    if do_backup:
        steps.append(("backup", lambda: asyncio.to_thread(backup, knowledge.KNOWLEDGE_DIR)))
    for name, step in steps:
        try:
            results[name] = await step()
        except Exception as e:  # noqa: BLE001 - one failed step must not stop the others
            results[name] = {"error": f"{type(e).__name__}: {e}"}
    report = render(results)
    reports = os.path.join(knowledge.KNOWLEDGE_DIR, "reports")
    os.makedirs(reports, exist_ok=True)
    path = os.path.join(reports, f"weekly-{today()}.md")
    old_umask = os.umask(0o027)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
        os.makedirs(STATE, exist_ok=True)
        with open(LAST, "w", encoding="utf-8") as f:
            json.dump({**results, "report": path}, f, ensure_ascii=False, default=str)
    finally:
        os.umask(old_umask)
    results["report"] = path
    return results


def main() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import knowledge
    import server
    out = asyncio.run(run(server, knowledge, do_backup="--no-backup" not in sys.argv, do_harvest="--no-harvest" not in sys.argv))
    print(json.dumps({k: (v if k in ("date", "report") else ("error" if isinstance(v, dict) and "error" in v else "ok")) for k, v in out.items()}))


if __name__ == "__main__":
    main()

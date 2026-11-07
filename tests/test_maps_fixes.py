"""Fixes to the map tools reported in the critique (server part). All sources are mocked."""
import asyncio

import pytest

import maps
import server as s
from test_maps import db, node, run  # noqa: F401  (db is a fixture)


# --- A-1 map_create ---------------------------------------------------------------------------

def test_create_all_seeds_failed_is_partial(db):
    out = run(s.map_create("P", ["W404", "W405"]))
    assert out["status"] == "partial" and out["map"] == "P"
    assert out["detail"] and out["source_errors"]
    assert any("W404" in w for w in out["warnings"]) and out["detail"] in out["warnings"]
    assert run(s.map_list())["maps"][0]["name"] == "P"  # the map exists


def test_create_some_seeds_failed_stays_ok_with_warnings(db):
    out = run(s.map_create("Q", ["W1", "W404"]))
    assert out["status"] == "ok" and set(out["seeds"]) == {"W1"}
    assert any("W404" in w for w in out["warnings"]) and out["source_errors"]
    assert "detail" not in out


def test_create_without_seeds_unchanged(db):
    assert run(s.map_create("E")) == {"status": "ok", "map": "E"}


# --- A-2 map_refresh watermark ----------------------------------------------------------------

def _watch_env(db, monkeypatch, results=None, fail=False):
    run(s.map_create("W", ["W1"]))

    async def oa_get(path, **params):
        if fail:
            raise RuntimeError("429")
        return {"results": results or []}

    async def batch_none(key, values):
        return [], []

    async def no_oc(doi, kind):
        return None
    monkeypatch.setattr(s, "_oa_get", oa_get)
    monkeypatch.setattr(s, "_oa_batch", batch_none)
    monkeypatch.setattr(s, "_oc_count", no_oc)


def _watched(name="W"):
    return maps.map_row(maps.connect(), name)["watched"]


def test_refresh_advances_watermark_when_clean(db, monkeypatch):
    _watch_env(db, monkeypatch)
    out = run(s.map_refresh("W", new_citing=True))
    assert "truncated" not in out and "watch_not_advanced" not in out and _watched()


def test_refresh_does_not_advance_on_seed_error(db, monkeypatch):
    _watch_env(db, monkeypatch, fail=True)
    out = run(s.map_refresh("W", new_citing=True))
    assert out["truncated"] is True and out["watch_not_advanced"] is True and out["source_errors"]
    assert _watched() is None


def test_refresh_does_not_advance_when_page_is_full(db, monkeypatch):
    items = [{"id": f"https://openalex.org/W{90 + i}", "title": f"N{i}", "authorships": [], "referenced_works": []}
             for i in range(2)]
    _watch_env(db, monkeypatch, results=items)
    out = run(s.map_refresh("W", new_citing=True, per_seed=2))
    assert len(out["new"]) == 2 and out["truncated"] and out["watch_not_advanced"] and _watched() is None


def test_refresh_no_seeds_has_full_shape(db):
    run(s.map_create("Empty"))
    out = run(s.map_refresh("Empty", new_citing=True))
    assert out == {"status": "no_seeds", "coverage_gaps": [], "new": []}


# --- A-3 SQLite: WAL, busy timeout, once-per-process migrations, busy status --------------------

def test_connect_wal_busy_timeout_and_migration_once(tmp_path, monkeypatch):
    path = str(tmp_path / "x" / "maps.sqlite")
    maps._READY.discard(path)
    calls = []
    real = maps._migrate
    monkeypatch.setattr(maps, "_migrate", lambda d: (calls.append(1), real(d)))
    a = maps.connect(path)
    b = maps.connect(path)
    try:
        assert a.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert a.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
        assert a.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert len(calls) == 1  # migrated on the first connection only
        assert {r[0] for r in b.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"maps", "nodes", "jobs"}
    finally:
        a.close(); b.close()


def test_two_concurrent_connections(tmp_path):
    path = str(tmp_path / "maps.sqlite")
    a, b = maps.connect(path), maps.connect(path)
    try:
        mid = maps.create(a, "A")
        # a reader sees committed data while another connection holds an open write transaction (WAL)
        b.execute("BEGIN IMMEDIATE")
        b.execute("INSERT INTO maps (name, created, updated) VALUES ('B', 'x', 'x')")
        assert [r["name"] for r in a.execute("SELECT name FROM maps")] == ["A"]
        b.commit()
        assert {r["name"] for r in a.execute("SELECT name FROM maps")} == {"A", "B"} and mid
        # a second writer waits for the first (busy_timeout) and then succeeds
        import threading, time
        started = threading.Event()

        def holder():
            c = maps.connect(path)
            c.execute("BEGIN IMMEDIATE")
            started.set()
            time.sleep(0.4)
            c.commit()
            c.close()
        th = threading.Thread(target=holder)
        th.start()
        started.wait()
        t0 = time.time()
        maps.create(a, "C")
        assert time.time() - t0 >= 0.25
        th.join()
    finally:
        a.close(); b.close()


def test_locked_database_becomes_busy_status(db, monkeypatch):
    def locked(*a, **k):
        raise __import__("sqlite3").OperationalError("database is locked")
    monkeypatch.setattr(maps, "summary", locked)
    registered = {t.name: t for t in asyncio.run(s.mcp.list_tools())}
    assert "map_list" in registered
    out = asyncio.run(s.mcp.call_tool("map_list", {}))
    text = out[0][0].text if isinstance(out, tuple) else out[0].text
    assert '"busy"' in text


def test_tool_closes_its_connections(db, monkeypatch):
    opened = []
    real = maps.connect

    def spy(*a, **k):
        c = real(*a, **k)
        opened.append(c)
        return c
    monkeypatch.setattr(maps, "connect", spy)
    asyncio.run(s.mcp.call_tool("map_list", {}))
    assert opened
    for c in opened:
        with pytest.raises(__import__("sqlite3").ProgrammingError):
            c.execute("SELECT 1")


# --- B-1 background jobs -----------------------------------------------------------------------

def _slow_expand(monkeypatch, gate_holder, fail=False):
    """Replace _expand with one that waits for a gate (created inside the running loop)."""
    async def expand(mode, seeds, per_seed):
        s._progress(inc=1, step="fake")
        await gate_holder["gate"].wait()
        if fail:
            raise RuntimeError("boom")
        return [], []
    monkeypatch.setattr(s, "_expand", expand)


async def _until_done(job_id, tries=300):
    for _ in range(tries):
        st = await s.map_job_status(job_id)
        if st["status"] != "running":
            return st
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


def test_background_job_finishes_and_is_polled(db, monkeypatch):
    holder = {}
    _slow_expand(monkeypatch, holder)

    async def scenario():
        holder["gate"] = asyncio.Event()
        await s.map_create("J", ["W1"])
        r = await s.map_expand("J", background=True)
        assert r["status"] == "accepted" and r["poll_after_s"] == 2 and len(r["job_id"]) == 32
        await asyncio.sleep(0.05)
        st = await s.map_job_status(r["job_id"])
        assert st["status"] == "running" and st["result"] is None and st["error"] is None
        assert st["progress"]["done"] >= 1 and st["progress"]["step"] == "fake" and st["progress"]["total"] >= 1
        holder["gate"].set()
        st = await _until_done(r["job_id"])
        assert st["status"] == "ok" and st["error"] is None
        assert st["result"]["status"] == "ok" and st["result"]["map"] == "J" and st["result"]["added"] == 0
        assert st["progress"]["done"] == st["progress"]["total"]
    asyncio.run(scenario())


def test_background_result_equals_foreground_payload(db):
    async def scenario():
        await s.map_create("J", ["W1"])
        await s.map_create("K", ["W1"])
        direct = await s.map_expand("J")
        r = await s.map_expand("K", background=True)
        st = await _until_done(r["job_id"])
        assert st["result"] == {**direct, "map": "K"}
    asyncio.run(scenario())


def test_job_that_raises_is_an_error(db, monkeypatch):
    holder = {}
    _slow_expand(monkeypatch, holder, fail=True)

    async def scenario():
        holder["gate"] = asyncio.Event()
        holder["gate"].set()
        await s.map_create("J", ["W1"])
        r = await s.map_expand("J", background=True)
        st = await _until_done(r["job_id"])
        assert st["status"] == "error" and "boom" in st["error"] and st["result"] is None
        # the map is free again
        assert (await s.map_expand("J", background=True))["status"] == "accepted"
    asyncio.run(scenario())


def test_value_error_in_background_is_a_stored_invalid_input(db):
    async def scenario():
        await s.map_create("J")
        r = await s.map_add_patents("J", [], background=True)
        st = await _until_done(r["job_id"])
        assert st["status"] == "ok" and st["result"]["status"] == "invalid_input"
    asyncio.run(scenario())


def test_one_job_per_map(db, monkeypatch):
    holder = {}
    _slow_expand(monkeypatch, holder)

    async def scenario():
        holder["gate"] = asyncio.Event()
        await s.map_create("J", ["W1"])
        await s.map_create("K", ["W1"])
        first = await s.map_expand("J", background=True)
        again = await s.map_expand("j", background=True)  # names are case-insensitive
        assert again == {"status": "busy", "job_id": first["job_id"], "detail": again["detail"]}
        assert (await s.map_refresh("J"))["status"] == "busy"
        other = await s.map_expand("K", background=True)  # another map is free
        assert other["status"] == "accepted"
        holder["gate"].set()
        await _until_done(first["job_id"]); await _until_done(other["job_id"])
        assert (await s.map_expand("J", background=True))["status"] == "accepted"
    asyncio.run(scenario())


def test_foreground_call_goes_pending_after_the_wait(db, monkeypatch):
    holder = {}
    _slow_expand(monkeypatch, holder)
    monkeypatch.setattr(s, "JOB_WAIT_S", 0.1)

    async def scenario():
        holder["gate"] = asyncio.Event()
        await s.map_create("J", ["W1"])
        r = await s.map_expand("J")
        assert r["status"] == "pending" and r["poll_after_s"] == 2
        st = await s.map_job_status(r["job_id"])
        assert st["status"] == "running"
        holder["gate"].set()
        st = await _until_done(r["job_id"])
        assert st["status"] == "ok" and st["result"]["map"] == "J"
    asyncio.run(scenario())


def test_short_foreground_call_leaves_no_job_row(db):
    out = run(s.map_create("J", ["W1"]))
    assert out["status"] == "ok"
    d = maps.connect()
    try:
        assert d.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        d.close()


def test_value_error_still_raises_on_direct_foreground_calls(db):
    run(s.map_create("J"))
    with pytest.raises(ValueError):
        run(s.map_add_patents("J", []))


def test_unknown_job(db):
    out = run(s.map_job_status("0" * 32))
    assert out["status"] == "not_found" and out["result"] is None and out["progress"]["total"] == 0


def test_restart_marks_running_jobs_interrupted(db):
    d = maps.connect()
    d.execute("INSERT INTO jobs (id, tool, status, progress_done, progress_total, step, created, updated)"
              " VALUES ('a1', 'map_expand', 'running', 3, 9, 'citing', 'x', 'x')")
    d.execute("INSERT INTO jobs (id, tool, status, result, created, updated, finished)"
              " VALUES ('b2', 'map_expand', 'ok', '{\"status\": \"ok\"}', 'x', 'x', 'x')")
    d.commit(); d.close()
    maps._READY.clear()  # a new process
    d = maps.connect(); d.close()
    st = run(s.map_job_status("a1"))
    assert st["status"] == "interrupted" and st["progress"] == {"done": 3, "total": 9, "step": "citing"} and st["error"]
    assert run(s.map_job_status("b2"))["status"] == "ok"
    # idempotent: a second migration changes nothing
    maps._READY.clear()
    d = maps.connect(); d.close()
    assert run(s.map_job_status("a1"))["status"] == "interrupted"


def test_old_finished_jobs_are_deleted_when_a_job_is_stored(db):
    d = maps.connect()
    d.execute("INSERT INTO jobs (id, tool, status, created, updated, finished) VALUES ('old', 'x', 'ok', 'x', 'x', '2020-01-01T00:00:00Z')")
    d.execute("INSERT INTO jobs (id, tool, status, created, updated, finished) VALUES ('new', 'x', 'ok', 'x', 'x', ?)", (maps.now(),))
    d.commit(); d.close()

    async def scenario():
        await s.map_create("J")
        r = await s.map_expand("J", background=True)
        await _until_done(r["job_id"])
    asyncio.run(scenario())
    assert run(s.map_job_status("old"))["status"] == "not_found"
    assert run(s.map_job_status("new"))["status"] == "ok"


def test_mcp_surface_has_background_and_job_status():
    tools = {t.name: t for t in asyncio.run(s.mcp.list_tools())}
    for name in ("map_create", "map_add_seeds", "map_expand", "map_intents", "map_refresh"):
        prop = tools[name].inputSchema["properties"]["background"]
        assert prop["default"] is False and prop["type"] == "boolean", name
        assert "background" not in tools[name].inputSchema.get("required", [])
    assert tools["map_job_status"].annotations.readOnlyHint is True
    assert tools["map_job_status"].inputSchema["required"] == ["job_id"]


def test_background_through_the_mcp_layer(db):
    async def scenario():
        await s.map_create("J", ["W1"])
        res = await s.mcp.call_tool("map_expand", {"map": "J", "background": True})
        import json
        text = (res[0] if isinstance(res, tuple) else res)[0].text
        r = json.loads(text)
        assert r["status"] == "accepted"
        st = await _until_done(r["job_id"])
        assert st["status"] == "ok" and st["result"]["map"] == "J"
    asyncio.run(scenario())


def test_progress_outside_a_job_is_a_noop():
    s._progress(done=3, total=5, step="x")


# --- M-6 views in worker threads, M-9 merge inside the transaction -----------------------------

def test_views_run_off_the_event_loop_thread(db, monkeypatch):
    import threading
    seen = {}
    real = s._map_view

    def spy(nodes, *a, **k):
        seen.setdefault("threads", []).append(threading.get_ident())
        return real(nodes, *a, **k)
    monkeypatch.setattr(s, "_map_view", spy)

    async def scenario():
        main = threading.get_ident()
        await s.map_create("T", ["W1", "W2"])
        await s.map_expand("T")
        for call in (s.map_get("T"), s.map_suggest("T"), s.map_export("T"), s.map_prune("T", keep=1)):
            s._VIEW_CACHE.clear()  # the view cache would otherwise answer map_suggest without computing
            await call
        assert len(seen["threads"]) == 4 and main not in seen["threads"]
    asyncio.run(scenario())


def test_event_loop_stays_responsive_during_a_view(db, monkeypatch):
    import time
    real = s._map_view

    def slow(nodes, *a, **k):
        time.sleep(0.5)
        return real(nodes, *a, **k)
    monkeypatch.setattr(s, "_map_view", slow)

    async def scenario():
        await s.map_create("T", ["W1"])
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1
        tk = asyncio.create_task(ticker())
        await s.map_get("T")
        tk.cancel()
        assert ticks >= 10  # a blocked loop would have counted none
    asyncio.run(scenario())


def test_map_suggest_limit_none_uses_the_default(db):
    run(s.map_create("T", ["W1", "W2"]))
    run(s.map_expand("T"))
    assert run(s.map_suggest("T", limit=None))["suggestions"] == run(s.map_suggest("T"))["suggestions"]


def test_merge_takes_the_write_lock_before_reading(db):
    """A map_mark committed before the merge starts is never overwritten by stale rows."""
    d = maps.connect()
    try:
        mid = maps.create(d, "D")
        a = node("W20", ["W3"]); a.update(title="Same long title about design research", first_author="Ann Smith", cited=10)
        b = node("W21", []); b.update(title="Same long title about design research", first_author="Ann Smith", cited=1)
        with d:
            maps.upsert(d, mid, a, "candidate", "x"); maps.upsert(d, mid, b, "candidate", "y")
        # another connection holds the write lock: the merge must wait, then read the fresh state
        other = maps.connect()
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE nodes SET state = 'read' WHERE map_id = ? AND work_id = 'W21'", (mid,))
        import threading
        out = {}

        def merge():
            c = maps.connect()
            out["r"] = s._merge_duplicates(maps, c, mid)
            c.close()
        th = threading.Thread(target=merge)
        th.start()
        th.join(0.3)
        assert th.is_alive()  # waiting for the lock
        other.commit(); other.close()
        th.join(5)
        kept = maps.nodes(d, mid)["W20"]
        assert out["r"][0]["kept"] == "W20" and kept["state"] == "read"  # the mark made on W21 survived
    finally:
        d.close()


# --- M-1, M-2 map_get: how a work is cited inside the map, truncated pair lists ----------------

def _small_graph():
    def n(i, role, refs):
        return {**node(i, refs), "role": role, "state": "new", "found_by": []}
    return {"S1": n("S1", "seed", ["C1", "C2"]), "S2": n("S2", "seed", ["C1", "S1"]),
            "C1": n("C1", "candidate", []), "C2": n("C2", "candidate", ["C1"]),
            "C3": n("C3", "candidate", ["C1", "S2", "X"]),
            "P:EP1": n("P:EP1", "candidate", ["C1"])}  # an examiner's citation never counts


def test_cited_in_map_counts_on_a_hand_computed_graph():
    nodes = _small_graph()
    s._map_view(nodes)
    got = {k: (v["cited_in_map"], v["cited_in_map_nonseed"], v["cited_by_seed_citers"]) for k, v in nodes.items()}
    # C1 is cited by S1, S2, C2, C3 (4); of those, candidates C2 and C3 (2); of those, S2 and C3 cite a
    # seed other than C1 (2). Seeds are counted as citing works, and seeds as cited works.
    assert got["C1"] == (4, 2, 2)
    assert got["C2"] == (1, 0, 0)       # only S1 cites it, and S1 cites no seed
    assert got["S1"] == (1, 0, 0)       # S2 cites S1; "a seed other than S1" does not exist for S2
    assert got["S2"] == (1, 1, 0)       # C3 cites S2
    assert got["C3"] == (0, 0, 0)
    assert got["P:EP1"] == (0, 0, 0)


def test_pair_lists_truncation_flags_and_max_pairs():
    nodes = _small_graph()
    trunc = {}
    _, cocit, _ = s._map_view(nodes, 1, 3, 1, trunc)
    assert len(cocit) == 1 and trunc["cocitation"] is True and trunc["coupling"] is False
    trunc = {}
    _, cocit, _ = s._map_view(_small_graph(), 1, 3, 500, trunc)
    assert len(cocit) == 3 and trunc == {"cocitation": False, "coupling": False}


def test_map_get_exposes_counts_and_flags(db):
    run(s.map_create("G", ["W1", "W2"]))
    run(s.map_expand("G"))
    got = run(s.map_get("G"))
    assert got["cocitation_truncated"] is False and got["coupling_truncated"] is False
    w3 = next(w for w in got["works"] if w["id"] == "W3")
    # W3 is cited by the seeds W1 and W2 (neither cites a seed) and by the candidate W6, which cites both seeds
    assert (w3["cited_in_map"], w3["cited_in_map_nonseed"], w3["cited_by_seed_citers"]) == (3, 1, 1)
    assert run(s.map_get("G", min_cocitation=1, max_pairs=1))["cocitation_truncated"] is True
    assert run(s.map_get("G", max_pairs=10 ** 9))["status"] == "ok"  # clamped to 5000


# --- M-3 map_intents: skipped works, truncation, incremental saves -----------------------------

def _intents_env(db, monkeypatch, n_seeds=7, big=False):
    ids = [f"W{100 + i}" for i in range(n_seeds)]
    for i, w in enumerate(ids):
        db[w] = node(w, [])
        db[w]["doi"] = f"10.1111/{i}" if i != 1 else None  # the second seed has no DOI
    db["W500"] = node("W500", []); db["W500"]["doi"] = "10.1111/other"
    saves, calls = [], []
    real_save = maps.save_intents
    monkeypatch.setattr(maps, "save_intents", lambda d, mid, rows: (saves.append(len(rows)), real_save(d, mid, rows)))

    async def fake_s2(path, params=None, body=None):
        calls.append(path)
        if path.endswith("/citations"):
            data = [{"intents": [], "isInfluential": False, "contexts": [], "citingPaper": {"externalIds": {"DOI": "10.1111/other"}}}]
            return {"status": "ok", "data": {"data": data * (1000 if big else 1)}}
        return {"status": "ok", "data": {"data": []}}
    monkeypatch.setattr(s, "_s2_api", fake_s2)
    run(s.map_create("I", ids))
    run(s.map_add_seeds("I", ["W500"]))
    return ids, saves, calls


def test_map_intents_reports_skipped_works_and_saves_every_five(db, monkeypatch):
    ids, saves, calls = _intents_env(db, monkeypatch)
    out = run(s.map_intents("I", max_works=5))
    # 8 seeds in the map: one without a DOI, and with max_works=5 two more beyond the cut
    assert len(out["works"]) == 5 and out["truncated"] is False
    assert "W101" in out["works_skipped"] and len(out["works_skipped"]) == 3
    assert set(out["works"]).isdisjoint(out["works_skipped"])
    assert saves == [5]  # five works: a single save at the end
    saves.clear()
    out = run(s.map_intents("I", max_works=50))
    assert out["works_skipped"] == ["W101"] and len(out["works"]) == 7
    assert saves == [5, 1]  # five edges after the fifth work, then the sixth (W500 cites nothing of itself)


def test_map_intents_truncated_when_the_s2_limit_is_hit(db, monkeypatch):
    _intents_env(db, monkeypatch, n_seeds=2, big=True)
    out = run(s.map_intents("I"))
    assert out["status"] == "ok" and out["truncated"] is True


# --- M-4 edge_intents cleanup -----------------------------------------------------------------

def _with_intents(db):
    run(s.map_create("M", ["W1"]))
    run(s.map_expand("M"))
    d = maps.connect()
    try:
        mid = maps.map_row(d, "M")["id"]
        maps.save_intents(d, mid, [{"citing": "W6", "cited": "W1", "intents": ["background"]},
                                   {"citing": "W1", "cited": "W3", "intents": ["result"]},
                                   {"citing": "W6", "cited": "W3", "intents": []}])
    finally:
        d.close()
    return mid


def _intent_keys(mid):
    d = maps.connect()
    try:
        return {(r[0], r[1]) for r in d.execute("SELECT citing, cited FROM edge_intents WHERE map_id = ?", (mid,))}
    finally:
        d.close()


def test_remove_deletes_the_intents_of_the_removed_work(db):
    mid = _with_intents(db)
    run(s.map_remove("M", ["W1"]))
    assert _intent_keys(mid) == {("W6", "W3")}


def test_delete_map_deletes_its_intents_and_a_reused_id_inherits_nothing(db):
    mid = _with_intents(db)
    run(s.map_delete("M", "M"))
    assert _intent_keys(mid) == set()
    run(s.map_create("N", ["W1"]))
    run(s.map_expand("N"))
    d = maps.connect()
    try:
        assert maps.map_row(d, "N")["id"] == mid  # sqlite reuses the id
    finally:
        d.close()
    assert run(s.map_get("N"))["edge_intents"] == {}


def test_prune_deletes_intents_of_pruned_works(db):
    mid = _with_intents(db)
    run(s.map_prune("M", keep=0))
    assert run(s.map_prune("M", keep=0))["status"] == "ok"
    nodes = {w["id"] for w in run(s.map_get("M"))["works"]}
    assert all(a in nodes and b in nodes for a, b in _intent_keys(mid))


def test_migration_drops_orphan_intents(db):
    d = maps.connect()
    d.execute("INSERT INTO edge_intents (map_id, citing, cited, intents, contexts, fetched) VALUES (999, 'a', 'b', '[]', '[]', 'x')")
    d.commit(); d.close()
    maps._READY.clear()
    d = maps.connect()
    try:
        assert d.execute("SELECT COUNT(*) FROM edge_intents").fetchone()[0] == 0
    finally:
        d.close()


# --- M-5 works_in_map -------------------------------------------------------------------------

def test_works_in_map_counts_every_removed_record(db):
    # three records of the same work (preprint, published, reprint): one group, two removed
    for i, c in (("W33", 3), ("W34", 30), ("W35", 1)):
        db[i] = {**node(i, []), "title": "A very long and distinctive title about design theory",
                 "first_author": "Bob Jones", "cited_by": c}
    db["W1"]["refs"] = ["W3", "W33", "W34", "W35"]
    run(s.map_create("D", ["W1"]))
    out = run(s.map_expand("D", ["references"]))
    d = maps.connect()
    try:
        real = d.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        d.close()
    assert [len(g["removed"]) for g in out["duplicates_merged"]] == [2]
    assert out["works_in_map"] == real == 3  # W1, W3 and the merged W34


# --- M-8 OpenCitations: concurrency and retry --------------------------------------------------

class _Resp:
    def __init__(self, code, data=None):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


def test_oc_count_retries_429_then_succeeds(monkeypatch):
    seq = [_Resp(429), _Resp(429), _Resp(200, [{"count": "42"}])]
    sleeps = []

    class Client:
        async def get(self, url, headers=None):
            return seq.pop(0)

    async def fake_sleep(d):
        sleeps.append(d)
    monkeypatch.setattr(s, "_http", lambda: Client())
    monkeypatch.setattr(s.asyncio, "sleep", fake_sleep)
    assert run(s._oc_count("10.1234/x", "citations")) == 42 and sleeps == [1.0, 2.0]


def test_oc_count_gives_up_after_the_retries(monkeypatch):
    calls = []

    class Client:
        async def get(self, url, headers=None):
            calls.append(1)
            return _Resp(429)

    async def no_sleep(d):
        pass
    monkeypatch.setattr(s, "_http", lambda: Client())
    monkeypatch.setattr(s.asyncio, "sleep", no_sleep)
    assert run(s._oc_count("10.1234/x", "citations")) is None and len(calls) == 3


def test_refresh_runs_opencitations_three_at_a_time_and_counts_unknowns(db, monkeypatch):
    for i in range(6):
        db[f"W7{i}"] = {**node(f"W7{i}", []), "doi": f"10.1234/s{i}"}
    async def no_refs(doi):
        return []
    monkeypatch.setattr(s, "_s2_reference_ids", no_refs)
    run(s.map_create("O", [f"W7{i}" for i in range(6)]))

    async def batch(key, ids):
        return [], []
    running = {"now": 0, "max": 0}

    async def oc(doi, kind):
        running["now"] += 1
        running["max"] = max(running["max"], running["now"])
        await asyncio.sleep(0.02)
        running["now"] -= 1
        return None if doi.endswith(("s0", "s1")) else 7
    monkeypatch.setattr(s, "_oa_batch", batch)
    monkeypatch.setattr(s, "_oc_count", oc)
    out = run(s.map_refresh("O"))
    assert running["max"] == 3
    assert any("no citation count for 2 of 6 seeds" in e for e in out["source_errors"])
    got = {w["id"]: w["cited_by_oc"] for w in run(s.map_get("O"))["works"]}
    assert got["W72"] == 7 and got["W70"] is None


# --- M-7 OpenAlex rate with a key ---------------------------------------------------------------

def test_openalex_interval_is_shorter_only_with_a_key(monkeypatch):
    tr = s._PoliteTransport()
    monkeypatch.setattr(s, "OA_KEY", "")
    assert tr.interval("api.openalex.org") == 1.0
    monkeypatch.setattr(s, "OA_KEY", "k")
    assert tr.interval("api.openalex.org") == 0.2
    assert tr.interval("api.semanticscholar.org") == 1.0 and tr.interval("user.openalex.org") == 1.0


# --- low: patents ---------------------------------------------------------------------------------

def test_add_patents_all_failed_is_an_error_with_detail(db, monkeypatch):
    async def failing(number, resolve_literature=0, citing=True):
        return {"status": "not_found", "number": number}
    monkeypatch.setattr(s, "patent_get", failing)
    run(s.map_create("P"))
    out = run(s.map_add_patents("P", ["EP1", "EP2"]))
    assert out["status"] == "error" and "EP1" in out["detail"] and out["literature_added"] == 0


def test_add_patents_retries_a_403_once_after_a_pause(db, monkeypatch):
    calls, sleeps = [], []

    async def throttled_then_ok(number, resolve_literature=0, citing=True):
        calls.append(number)
        if len(calls) == 1:
            return {"status": "error", "http_status": 403, "detail": "fair use"}
        return {"status": "ok", "number": "EP3500000", "title": "T", "applicants": [], "priority": "2017-01-01",
                "published": "2018-01-01", "cited_by_total": 0, "url": "u", "cites_literature": []}

    async def fake_sleep(d):
        sleeps.append(d)
    monkeypatch.setattr(s, "patent_get", throttled_then_ok)
    monkeypatch.setattr(s.asyncio, "sleep", fake_sleep)
    run(s.map_create("P"))
    out = run(s.map_add_patents("P", ["EP3500000"]))
    assert out["status"] == "ok" and calls == ["EP3500000"] * 2 and sleeps == [s.PATENT_RETRY_PAUSE_S]
    assert out["patents"]["P:EP3500000"]["result"] == "added"


# --- map_get: compact / fields --------------------------------------------------------------------

def _two_works(db):
    run(s.map_create("F", ["W1", "W2"]))
    run(s.map_expand("F"))
    run(s.map_mark("F", ["W3"], "to_read", "look"))


def test_map_get_default_has_all_fields_and_no_warnings(db):
    _two_works(db)
    out = run(s.map_get("F"))
    w = next(x for x in out["works"] if x["id"] == "W3")
    assert {"notes", "verified", "zotero_tags", "found_by", "note", "added", "refs_source"} <= set(w)
    assert "warnings" not in out
    assert run(s.map_get("F", compact=False, fields=None)) == out


def test_map_get_compact_drops_heavy_fields(db):
    _two_works(db)
    full, out = run(s.map_get("F")), run(s.map_get("F", compact=True))
    heavy = {"notes", "verified", "zotero_tags", "found_by", "note", "added", "refs_source"}
    for w in out["works"]:
        assert not heavy & set(w) and {"id", "title", "state", "cited_in_map"} <= set(w)
    assert [w["id"] for w in out["works"]] == [w["id"] for w in full["works"]]
    assert {k: v for k, v in out.items() if k != "works"} == {k: v for k, v in full.items() if k != "works"}


def test_map_get_fields_keeps_listed_plus_id_and_wins_over_compact(db):
    _two_works(db)
    out = run(s.map_get("F", fields=["title", "note"], compact=True))
    assert all(set(w) == {"id", "title", "note"} for w in out["works"])  # note kept although compact drops it
    assert "warnings" not in out


def test_map_get_fields_unknown_are_ignored_with_warning(db):
    _two_works(db)
    out = run(s.map_get("F", fields=["state", "nope", "nope", "titel"]))
    assert all(set(w) == {"id", "state"} for w in out["works"])
    assert out["warnings"] == ["unknown fields ignored: nope, titel"]


# --- duplicates: the intents of removed records move to the kept id -------------------------------

def _intent(citing, cited, contexts=(), infl=False, contr=False, intents=("background",)):
    return {"citing": citing, "cited": cited, "intents": list(intents), "influential": infl,
            "contexts": list(contexts), "contrasting": contr}


def test_merge_remaps_intents_of_removed_duplicates(db):
    d = maps.connect()
    try:
        mid = maps.create(d, "R")
        title = dict(title="Same long title about design research", first_author="Ann Smith")
        recs = []
        for i, c in (("W30", 10), ("W31", 5), ("W32", 1)):
            n = node(i, []); n.update(title, cited=c); recs.append(n)
        for i in ("W40", "W41"):
            n = node(i, []); n.update(title=f"Other {i}", first_author="Bo Lee"); recs.append(n)
        with d:
            for n in recs:
                maps.upsert(d, mid, n, "candidate", "x")
        maps.save_intents(d, mid, [
            _intent("W40", "W30", ["one"]),                       # exists on the kept id, fewer contexts
            _intent("W40", "W31", ["a", "b"], infl=True),         # fuller: wins, flags OR-ed
            _intent("W40", "W32", ["c"], contr=True),
            _intent("W31", "W41", ["out"]),                       # citing side moves
            _intent("W32", "W30", ["self"]),                      # would cite itself: dropped
            _intent("W41", "W40", ["untouched"]),
        ])
        out = s._merge_duplicates(maps, d, mid)
        assert out[0]["kept"] == "W30" and set(out[0]["removed"]) == {"W31", "W32"}
        got = maps.intents(d, mid)
        assert set(got) == {"W40->W30", "W30->W41", "W41->W40"}
        e = got["W40->W30"]
        assert e["contexts"] == ["a", "b"] and e["influential"] and e["contrasting"]
        assert got["W30->W41"]["contexts"] == ["out"] and got["W41->W40"]["contexts"] == ["untouched"]
        # idempotent
        assert s._merge_duplicates(maps, d, mid) == [] and maps.intents(d, mid) == got
    finally:
        d.close()


# --- cache of the map view ------------------------------------------------------------------------

@pytest.fixture
def view_counter(monkeypatch):
    s._VIEW_CACHE.clear()
    calls = {"n": 0}
    real = s._map_view

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(s, "_map_view", counting)
    yield calls
    s._VIEW_CACHE.clear()


def _states(name="V"):
    return {w["id"]: w["state"] for w in run(s.map_get(name))["works"]}


def test_view_cache_two_equal_gets_compute_once(db, view_counter):
    _two_works(db)  # map "F"
    view_counter["n"] = 0
    a = run(s.map_get("F"))
    b = run(s.map_get("F"))
    assert a == b and view_counter["n"] == 1
    run(s.map_get("F", min_cocitation=3))           # other parameters: another entry
    assert view_counter["n"] == 2
    run(s.map_suggest("F")); run(s.map_suggest("F"))
    assert view_counter["n"] == 2                    # suggest shares the entry of map_get's default parameters
    run(s.map_get("F", compact=True, fields=["title"]))  # only the output differs: same view
    assert view_counter["n"] == 2


def test_view_cache_never_serves_old_data_even_within_one_second(db, view_counter, monkeypatch):
    monkeypatch.setattr(maps, "now", lambda: "2026-01-01T00:00:00Z")  # `updated` can no longer tell writes apart
    run(s.map_create("V", ["W1", "W2"]))
    run(s.map_expand("V"))
    before = _states()
    assert before["W3"] == "new" and _states() == before
    run(s.map_mark("V", ["W3"], "read"))
    assert _states()["W3"] == "read"
    # the cached answer is not changed by what a caller did to it
    out = run(s.map_get("V")); out["works"][0]["state"] = "tampered"; out["works"].clear()
    assert _states()["W3"] == "read"
    run(s.map_remove("V", ["W3"]))
    assert "W3" not in _states()
    # map_expand again (simulated source): the removed work comes back as new
    run(s.map_expand("V"))
    assert _states().get("W3") == "new"
    run(s.map_rename("V", description="x"))
    assert run(s.map_get("V"))["map"]["description"] == "x"
    run(s.map_add_seeds("V", ["W4"]))
    assert _states()["W4"] == "new" and {w["id"]: w["role"] for w in run(s.map_get("V"))["works"]}["W4"] == "seed"


def test_view_cache_intents_prune_merge_delete_and_suggest(db, view_counter, monkeypatch):
    monkeypatch.setattr(maps, "now", lambda: "2026-01-01T00:00:00Z")
    run(s.map_create("V", ["W1", "W2"]))
    run(s.map_expand("V"))
    assert run(s.map_get("V"))["edge_intents"] == {}
    assert "W3" in {x["id"] for x in run(s.map_suggest("V"))["suggestions"]}
    d = maps.connect()
    maps.save_intents(d, maps.map_row(d, "V")["id"], [_intent("W1", "W3", ["c"])])
    assert list(run(s.map_get("V"))["edge_intents"]) == ["W1->W3"]
    maps.mark(d, maps.map_row(d, "V")["id"], ["W3"], "dismissed", None)  # a write made outside any tool
    d.close()
    assert "W3" not in {x["id"] for x in run(s.map_suggest("V"))["suggestions"]}
    run(s.map_prune("V", keep=0))
    assert set(_states()) == {"W1", "W2", "W3"}      # seeds and the dismissed work stay, the rest is pruned
    assert list(run(s.map_get("V"))["edge_intents"]) == ["W1->W3"]
    run(s.map_remove("V", ["W3"]))
    assert run(s.map_get("V"))["edge_intents"] == {}
    run(s.map_delete("V", "V"))
    assert run(s.map_get("V"))["status"] == "not_found"
    run(s.map_create("V", ["W1"]))                   # same name, same id: nothing of the old map is served
    assert set(_states()) == {"W1"}


def test_view_cache_merge_is_visible_at_once(db, view_counter, monkeypatch):
    monkeypatch.setattr(maps, "now", lambda: "2026-01-01T00:00:00Z")
    d = maps.connect()
    mid = maps.create(d, "M")
    title = dict(title="Same long title about design research", first_author="Ann Smith")
    for i, c in (("W50", 9), ("W51", 1)):
        n = node(i, []); n.update(title, cited=c)
        with d:
            maps.upsert(d, mid, n, "candidate", "x")
    assert {w["id"] for w in run(s.map_get("M"))["works"]} == {"W50", "W51"}
    s._merge_duplicates(maps, d, mid)
    d.close()
    assert {w["id"] for w in run(s.map_get("M"))["works"]} == {"W50"}


def test_view_cache_holds_at_most_eight_entries(db, view_counter):
    run(s.map_create("V", ["W1"]))
    for i in range(12):
        run(s.map_get("V", max_pairs=100 + i))
    assert len(s._VIEW_CACHE) == s.VIEW_CACHE_SIZE == 8


def test_job_progress_writes_do_not_flush_the_cache(db):
    before = maps.WRITES
    j = s._Job("map_expand", "x", False)
    j.persist(); j.progress(); j.finish(result={"status": "ok"})
    assert maps.WRITES == before


# --- request priority: quick calls pass ahead of queued jobs ---------------------------------------

def _priority_env(monkeypatch):
    import httpx
    from types import SimpleNamespace
    clock, sent, real_sleep = [100.0], [], asyncio.sleep

    async def fake_sleep(delay):
        clock[0] += delay
        await real_sleep(0)

    async def fake_request(transport, request):
        sent.append((clock[0], request.url.params["q"]))
        for _ in range(8):
            await real_sleep(0)  # the request takes a while: others queue meanwhile
        return httpx.Response(200, json={})

    monkeypatch.setattr(s, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(s.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(s, "OA_KEY", "")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_request)
    return clock, sent, real_sleep


def test_quick_calls_pass_ahead_of_queued_job_requests(monkeypatch):
    import httpx
    clock, sent, real_sleep = _priority_env(monkeypatch)

    async def call(client, name, priority):
        s._priority.set(priority)  # each task has its own copy of the context
        await client.get("https://api.openalex.org/works", params={"q": name})

    async def scenario():
        async with httpx.AsyncClient(transport=s._PoliteTransport()) as client:
            jobs = [asyncio.create_task(call(client, f"job{i}", s.PRIORITY_LOW)) for i in range(4)]
            await real_sleep(0); await real_sleep(0)      # job0 holds the slot, job1..3 wait
            quick = [asyncio.create_task(call(client, f"quick{i}", s.PRIORITY_HIGH)) for i in range(2)]
            await asyncio.gather(*jobs, *quick)
    asyncio.run(scenario())
    order = [q for _, q in sent]
    assert order == ["job0", "quick0", "quick1", "job1", "job2", "job3"]
    # the interval and the concurrency of 1 are unchanged: one request per second
    assert all(b[0] - a[0] >= 1.0 for a, b in zip(sent, sent[1:]))


def test_equal_priority_keeps_arrival_order_and_cancelled_waiters_are_skipped(monkeypatch):
    import httpx
    clock, sent, real_sleep = _priority_env(monkeypatch)

    async def call(client, name):
        await client.get("https://api.openalex.org/works", params={"q": name})

    async def scenario():
        async with httpx.AsyncClient(transport=s._PoliteTransport()) as client:
            tasks = [asyncio.create_task(call(client, f"r{i}")) for i in range(4)]
            await real_sleep(0); await real_sleep(0)
            tasks[1].cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await call(client, "after")                     # the slot was not lost
    asyncio.run(scenario())
    assert [q for _, q in sent] == ["r0", "r2", "r3", "after"]


def test_jobs_run_at_low_priority_and_other_tools_at_high(db):
    seen = {}

    async def in_job():
        async def f():
            seen["job"] = s._priority.get()
        seen["before"] = s._priority.get()
        await asyncio.create_task(s._run_job(s._Job("x", None, False), f, (), {}))
        seen["after"] = s._priority.get()
    asyncio.run(in_job())
    assert seen["before"] == s.PRIORITY_HIGH and seen["job"] == s.PRIORITY_LOW and seen["after"] == s.PRIORITY_HIGH

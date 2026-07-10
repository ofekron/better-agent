"""Security + behavior tests for the native-transcript read-only SQL sandbox.

`native_transcript_index.run_readonly_sql` lets agents run arbitrary
SELECT queries against the FTS corpus. These lock the guarantees that make that
safe and useful:

  * a plain SELECT / bm25 / GROUP BY query works and returns rows.
  * writes and DDL are rejected (INSERT/DELETE/DROP), including a CTE that
    smuggles a DELETE past the leading-keyword guard — the authorizer must deny.
  * ATTACH (the arbitrary-file-read vector) is rejected.
  * results and cells are returned complete; SQL authors add LIMIT when needed.
  * multi-statement input is rejected.
  * a missing index reports index_not_built rather than raising.

Run:
    cd backend && .venv/bin/python scripts/test_native_sql_search.py
"""
from __future__ import annotations

import io
import itertools
import logging
import os
import shutil
import statistics
import sys
import time
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-sql-")

import native_transcript_index as idx  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _seed() -> None:
    """Build a tiny FTS index directly through the writer connection."""
    conn = idx._writer_connection()
    conn.execute("DELETE FROM native_element_fts")
    conn.execute("DELETE FROM native_element_path")
    conn.execute("DELETE FROM native_element_meta")
    conn.execute("DELETE FROM native_element_text")
    rows = [
        ("offline backlog keeps dropping actions", "/p/a.jsonl", "sA", "/proj", "claude",
         "user_prompt", "", "2024-01-01T00:00:00.000000Z", "user"),
        ("acknowledged the offline backlog", "/p/a.jsonl", "sA", "/proj", "claude",
         "assistant_text", "", "2024-01-01T00:00:01.000000Z", "assistant"),
        ("offline sync note", "/p/b.jsonl", "sB", "/proj", "codex",
         "user_prompt", "", "2024-01-02T00:00:00.000000Z", "user"),
        ("x" * 5000 + " offline", "/p/c.jsonl", "sC", "/proj", "gemini",
         "assistant_text", "", "2024-01-03T00:00:00.000000Z", "assistant"),
    ]
    rows.extend(
        (f"large path row {i}", "/p/large.jsonl", "sLarge", "/proj", "codex",
         "assistant_text", "", f"2024-01-04T00:{i:02d}:00.000000Z", "assistant")
        for i in range(2000)
    )
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text, path, sid, cwd, tag, element_kind, tool_name, ts_utc, role) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    indexed = conn.execute(
        "SELECT rowid, path, sid, cwd, tag, element_kind, tool_name, ts_utc, role, element_id, element_index "
        "FROM native_element_fts"
    ).fetchall()
    conn.executemany(
        "INSERT INTO native_element_path(rowid, path) VALUES (?, ?)",
        [(row[0], row[1]) for row in indexed],
    )
    conn.executemany(
        "INSERT INTO native_element_meta"
        "(rowid, path, sid, cwd, tag, element_kind, tool_name, ts_utc, role, element_id, element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        indexed,
    )
    conn.execute(
        "INSERT INTO native_element_text(rowid, text) "
        "SELECT rowid, text FROM native_element_fts"
    )
    conn.commit()


def _count_rows() -> int:
    conn = idx._writer_connection()
    return conn.execute("SELECT count(*) FROM native_element_fts").fetchone()[0]


def test_select_group_by_and_bm25() -> bool:
    out = idx.run_readonly_sql(
        "SELECT sid, count(*) c FROM native_element_fts "
        "WHERE native_element_fts MATCH 'offline' GROUP BY sid ORDER BY c DESC, sid"
    )
    ok = out.get("error") is None and out["columns"] == ["sid", "c"] and [r[0] for r in out["rows"]][0] == "sA"
    # bm25 ranking must also parse + run without error.
    bm = idx.run_readonly_sql(
        "SELECT sid, bm25(native_element_fts) r FROM native_element_fts "
        "WHERE native_element_fts MATCH 'offline' ORDER BY r LIMIT 3"
    )
    ok = ok and bm.get("error") is None and len(bm["rows"]) >= 1
    print(f"{OK if ok else FAIL} SELECT + GROUP BY + bm25 run (group={out.get('rows')}, bm_err={bm.get('error')})")
    return ok


def test_write_is_denied() -> bool:
    before = _count_rows()
    ins = idx.run_readonly_sql("INSERT INTO native_element_fts(text) VALUES ('x')")
    drop = idx.run_readonly_sql("DROP TABLE native_element_fts")
    # CTE prefix passes the leading-keyword guard; the AUTHORIZER must still deny.
    cte_del = idx.run_readonly_sql("WITH t AS (SELECT 1) DELETE FROM native_element_fts")
    after = _count_rows()
    ok = (
        ins.get("error") and drop.get("error") and cte_del.get("error")
        and before == after and after > 0
    )
    print(f"{OK if ok else FAIL} writes/DDL denied, table intact "
          f"(ins={bool(ins.get('error'))}, drop={bool(drop.get('error'))}, "
          f"cte_delete={bool(cte_del.get('error'))}, rows {before}->{after})")
    return ok


def test_attach_is_denied() -> bool:
    out = idx.run_readonly_sql("ATTACH DATABASE '/etc/passwd' AS leak")
    # Even dressed as a CTE it must not open another file.
    out2 = idx.run_readonly_sql("WITH t AS (SELECT 1) SELECT * FROM t; ATTACH DATABASE 'x' AS y")
    ok = bool(out.get("error")) and bool(out2.get("error"))
    print(f"{OK if ok else FAIL} ATTACH denied (a={out.get('error')!r})")
    return ok


def test_no_row_or_cell_truncation() -> bool:
    all_rows = idx.run_readonly_sql(
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH 'offline' ORDER BY sid"
    )
    row_ok = len(all_rows["rows"]) == 4 and "truncated" not in all_rows
    long_cell = idx.run_readonly_sql(
        "SELECT e.text FROM native_element_meta m "
        "JOIN native_element_fts e ON e.rowid = m.rowid "
        "WHERE m.sid = 'sC'"
    )
    cell = long_cell["rows"][0][0] if long_cell.get("rows") else ""
    cell_ok = cell == ("x" * 5000 + " offline")
    ok = row_ok and cell_ok
    print(f"{OK if ok else FAIL} complete rows ({len(all_rows.get('rows', []))}) "
          f"+ complete cell (len={len(cell)})")
    return ok


def test_multi_statement_and_nonselect() -> bool:
    multi = idx.run_readonly_sql("SELECT 1; SELECT 2")
    pragma = idx.run_readonly_sql("PRAGMA table_info(native_element_fts)")
    ok = bool(multi.get("error")) and bool(pragma.get("error"))
    print(f"{OK if ok else FAIL} multi-statement + PRAGMA rejected "
          f"(multi={bool(multi.get('error'))}, pragma={bool(pragma.get('error'))})")
    return ok


def test_missing_index_reports_cleanly() -> bool:
    idx.reset_for_test()
    out = idx.run_readonly_sql("SELECT 1")
    ok = out.get("error") == "index_not_built" and out.get("covered") is False
    print(f"{OK if ok else FAIL} missing index reports index_not_built (got {out.get('error')!r})")
    return ok


def test_readonly_sql_refreshes_before_opening_db() -> bool:
    original_ensure = idx.ensure_fresh_for_read
    original_connect = idx._connect
    calls: list[str] = []

    def fake_ensure(timeout=idx._FRESH_WAIT_TIMEOUT):
        calls.append("ensure")
        return {"schema_ok": True, "covered": True, "usable": True}

    def checked_connect(*args, **kwargs):
        calls.append("connect")
        if calls[0] != "ensure":
            raise AssertionError("SQL opened DB before freshness check")
        return original_connect(*args, **kwargs)

    try:
        idx.ensure_fresh_for_read = fake_ensure  # type: ignore[assignment]
        idx._connect = checked_connect  # type: ignore[assignment]
        out = idx.run_readonly_sql("SELECT 1")
    finally:
        idx.ensure_fresh_for_read = original_ensure  # type: ignore[assignment]
        idx._connect = original_connect  # type: ignore[assignment]
    ok = out.get("error") is None and calls[:2] == ["ensure", "connect"]
    print(f"{OK if ok else FAIL} SQL freshness check precedes DB open (calls={calls})")
    return ok


def test_ensure_fresh_for_read_refreshes_stale_covered_index() -> bool:
    conn = idx._writer_connection()
    conn.execute(
        "INSERT INTO native_corpus_state(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(idx._SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT INTO native_corpus_state(key, value) VALUES ('covered', '1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    )
    conn.execute(
        "INSERT INTO native_corpus_state(key, value) VALUES ('last_walk_at', '1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    )
    conn.commit()
    original_ensure_started = idx.ensure_started
    original_request_refresh = idx.request_refresh
    original_wait_fresh = idx.wait_fresh
    calls: list[str] = []

    def fake_ensure_started() -> None:
        calls.append("ensure_started")

    def fake_request_refresh() -> None:
        calls.append("request_refresh")

    def fake_wait_fresh(timeout=idx._FRESH_WAIT_TIMEOUT) -> bool:
        calls.append("wait_fresh")
        conn.execute(
            "INSERT INTO native_corpus_state(key, value) VALUES ('last_walk_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(time.time()),),
        )
        conn.commit()
        return True

    try:
        idx.ensure_started = fake_ensure_started  # type: ignore[assignment]
        idx.request_refresh = fake_request_refresh  # type: ignore[assignment]
        idx.wait_fresh = fake_wait_fresh  # type: ignore[assignment]
        state = idx.ensure_fresh_for_read()
    finally:
        idx.ensure_started = original_ensure_started  # type: ignore[assignment]
        idx.request_refresh = original_request_refresh  # type: ignore[assignment]
        idx.wait_fresh = original_wait_fresh  # type: ignore[assignment]
    ok = calls == ["ensure_started", "request_refresh", "wait_fresh"] and state.get("usable") is True
    print(f"{OK if ok else FAIL} stale covered index refresh protocol (calls={calls}, state={state})")
    return ok


def test_slow_query_shape_is_measured_without_sql_leak() -> bool:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    old_level = idx.logger.level
    old_monotonic = idx.time.monotonic
    tick_values = [100.0, 100.0, 100.75]
    def fake_monotonic() -> float:
        if len(tick_values) > 1:
            return tick_values.pop(0)
        return tick_values[0]
    idx.logger.addHandler(handler)
    idx.logger.setLevel(logging.WARNING)
    idx.time.monotonic = fake_monotonic
    try:
        out = idx.run_readonly_sql(
            "SELECT text FROM native_element_fts "
            "WHERE native_element_fts MATCH ? "
            "ORDER BY bm25(native_element_fts) LIMIT 2",
            ("offline",),
        )
    finally:
        idx.time.monotonic = old_monotonic
        idx.logger.setLevel(old_level)
        idx.logger.removeHandler(handler)
    log = stream.getvalue()
    ok = (
        out.get("error") is None
        and out.get("elapsed_ms") == 750.0
        and "slow native transcript SQL" in log
        and '"has_match":true' in log
        and '"has_limit":true' in log
        and '"has_bm25":true' in log
        and '"filters":[]' in log
        and "offline" not in log
    )
    print(f"{OK if ok else FAIL} slow SQL shape measured without raw SQL leak "
          f"(elapsed={out.get('elapsed_ms')}, log={log.strip()!r})")
    return ok


def test_sql_shape_detects_filters_and_ts_ordering() -> bool:
    compact = idx._sql_shape(
        "SELECT text FROM native_element_fts "
        "WHERE sid='sA' AND e.cwd=? AND ts_utc BETWEEN ? AND ? ORDER BY ts_utc DESC LIMIT 5"
    )
    nested = idx._sql_shape(
        "SELECT * FROM (SELECT text FROM native_element_fts ORDER BY rank) t "
        "WHERE ts_utc > '2026-01-01T00:00:00Z'"
    )
    utc_order = idx._sql_shape(
        "SELECT text FROM native_element_fts "
        "WHERE native_element_fts MATCH 'needle' ORDER BY ts_utc DESC"
    )
    literal_noise = idx._sql_shape(
        "SELECT text FROM native_element_fts "
        "WHERE native_element_fts MATCH 'order by ts'"
    )
    literal_a = idx._sql_shape("SELECT text FROM native_element_fts WHERE sid='sA'")
    literal_b = idx._sql_shape("SELECT text FROM native_element_fts WHERE sid='sB'")
    ok = (
        compact["filters"] == ["sid", "cwd", "ts_utc"]
        and compact["orders_by_ts_utc"] is True
        and nested["orders_by_ts_utc"] is False
        and utc_order["orders_by_ts_utc"] is True
        and literal_noise["orders_by_ts_utc"] is False
        and literal_a["fingerprint"] == literal_b["fingerprint"]
    )
    print(f"{OK if ok else FAIL} SQL shape detects compact filters + ts ordering "
          f"(compact={compact}, nested={nested})")
    return ok


def test_metadata_recency_queries_use_meta_index() -> bool:
    conn = idx._writer_connection()
    plan_rows = conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT m.rowid, m.path, m.role, m.ts_utc "
        "FROM native_element_meta m "
        "WHERE m.path = '/p/a.jsonl' AND m.role = 'assistant' "
        "ORDER BY m.ts_utc DESC LIMIT 20"
    ).fetchall()
    joined = idx.run_readonly_sql(
        "SELECT e.text FROM native_element_meta m "
        "JOIN native_element_fts e ON e.rowid = m.rowid "
        "WHERE m.path = '/p/a.jsonl' "
        "ORDER BY m.ts_utc DESC LIMIT 1"
    )
    details = " ".join(str(row[-1]) for row in plan_rows)
    ok = (
        "native_element_meta_path_role_ts_idx" in details
        and "SCAN native_element_fts" not in details
        and joined.get("error") is None
        and joined.get("rows")
        and joined["rows"][0][0] == "acknowledged the offline backlog"
    )
    print(f"{OK if ok else FAIL} metadata recency query uses meta index "
          f"(plan={details!r}, joined={joined.get('rows')})")
    return ok


def test_path_rowid_query_is_rewritten_through_meta_index() -> bool:
    conn = idx._writer_connection()
    explicit = idx.run_readonly_sql(
        "SELECT e.text, e.path FROM native_element_meta m "
        "JOIN native_element_fts e ON e.rowid = m.rowid "
        "WHERE m.path = ? ORDER BY m.rowid DESC LIMIT ?",
        ("/p/large.jsonl", 3),
    )
    cases = [
        (
            "SELECT text, path FROM native_element_fts "
            "WHERE path = ? ORDER BY rowid DESC LIMIT ?",
            ("/p/large.jsonl", 3),
        ),
        (
            "SELECT text, path FROM native_element_fts "
            "WHERE path = '/p/large.jsonl' ORDER BY rowid DESC LIMIT 3",
            (),
        ),
    ]
    details = []
    outputs = []
    vm_callbacks = []
    for query, params in cases:
        rewritten = idx._rewrite_fast_metadata_sql(query)
        plan = conn.execute("EXPLAIN QUERY PLAN " + rewritten, params).fetchall()
        plan_details = " ".join(str(row[-1]) for row in plan)
        callbacks = 0
        def progress() -> int:
            nonlocal callbacks
            callbacks += 1
            return 0
        conn.set_progress_handler(progress, 10)
        try:
            output = conn.execute(rewritten, params).fetchall()
        finally:
            conn.set_progress_handler(None, 0)
        details.append(plan_details)
        outputs.append(output)
        vm_callbacks.append(callbacks)
    ok = (
        all("native_element_meta_path_rowid_idx" in detail for detail in details)
        and all(not any(token in detail for token in (
            "CO-ROUTINE", "MATERIALIZE", "USE TEMP B-TREE"
        )) for detail in details)
        and outputs[0] == outputs[1] == [tuple(row) for row in explicit.get("rows", [])]
        and [row[0] for row in outputs[0]] == [
            "large path row 1999",
            "large path row 1998",
            "large path row 1997",
        ]
        and max(vm_callbacks) <= 25
    )
    print(f"{OK if ok else FAIL} path rowid query rewrites through meta index "
          f"(plans={details!r}, callbacks={vm_callbacks}, rows={outputs[0]})")
    return ok


def test_path_role_rowid_query_is_rewritten_through_meta_index() -> bool:
    conn = idx._writer_connection()
    cases = [
        (
            "SELECT text, role FROM native_element_fts "
            "WHERE path = ? AND role = ? ORDER BY rowid DESC LIMIT ?",
            ("/p/a.jsonl", "assistant", 2),
        ),
        (
            "SELECT text, role FROM native_element_fts "
            "WHERE role = ? AND path = ? ORDER BY rowid DESC LIMIT ?",
            ("assistant", "/p/a.jsonl", 2),
        ),
        (
            "SELECT text, role FROM native_element_fts "
            "WHERE path = '/p/a.jsonl' AND role = 'assistant' "
            "ORDER BY rowid DESC LIMIT 2",
            (),
        ),
        (
            "SELECT text, role FROM native_element_fts "
            "WHERE role = 'assistant' AND path = '/p/a.jsonl' "
            "ORDER BY rowid DESC LIMIT 2",
            (),
        ),
    ]
    details = []
    outputs = []
    for query, params in cases:
        rewritten = idx._rewrite_fast_metadata_sql(query)
        details.append(" ".join(
            str(row[-1])
            for row in conn.execute("EXPLAIN QUERY PLAN " + rewritten, params).fetchall()
        ))
        outputs.append(conn.execute(rewritten, params).fetchall())
    ok = (
        all("native_element_meta_path_role_rowid_idx" in detail for detail in details)
        and all(not any(token in detail for token in (
            "CO-ROUTINE", "MATERIALIZE", "USE TEMP B-TREE"
        )) for detail in details)
        and all(output == outputs[0] for output in outputs[1:])
        and outputs[0] == [("acknowledged the offline backlog", "assistant")]
    )
    print(f"{OK if ok else FAIL} path+role rowid query rewrites through meta index "
          f"(plans={details!r}, rows={outputs[0]})")
    return ok


def test_match_recency_query_rewrites_with_plan_and_param_parity() -> bool:
    conn = idx._writer_connection()
    extra = [
        ("newest nonmatch", "/p/z.jsonl", "sZ", "/proj", "claude",
         "assistant_text", "", "2025-01-03T00:00:00.000000Z", "assistant"),
        ("offline tie first", "/p/z.jsonl", "sZ", "/proj", "claude",
         "assistant_text", "", "2025-01-02T00:00:00.000000Z", "assistant"),
        ("offline tie second", "/p/z.jsonl", "sZ", "/proj", "claude",
         "assistant_text", "", "2025-01-02T00:00:00.000000Z", "assistant"),
    ]
    start_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM native_element_fts").fetchone()[0]
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text, path, sid, cwd, tag, element_kind, tool_name, ts_utc, role) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        extra,
    )
    indexed = conn.execute(
        "SELECT rowid, path, sid, cwd, tag, element_kind, tool_name, ts_utc, role, element_id, element_index "
        "FROM native_element_fts WHERE rowid > ?", (start_rowid,),
    ).fetchall()
    conn.executemany(
        "INSERT INTO native_element_meta"
        "(rowid, path, sid, cwd, tag, element_kind, tool_name, ts_utc, role, element_id, element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        indexed,
    )
    conn.commit()

    predicates = {
        "match": ("native_element_fts MATCH ?", "offline"),
        "cwd": ("cwd = ?", "/proj"),
        "role": ("role = ?", "assistant"),
    }
    parity = True
    plan_details = ""
    for order in itertools.permutations(predicates):
        clauses = [predicates[name][0] for name in order]
        params = tuple(predicates[name][1] for name in order) + (4,)
        sql = (
            "SELECT text, path, ts_utc FROM native_element_fts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts_utc DESC LIMIT ?"
        )
        rewritten = idx._rewrite_fast_metadata_sql(sql)
        if rewritten is None:
            parity = False
            continue
        expected = [list(row) for row in conn.execute(sql, params).fetchall()]
        actual = idx.run_readonly_sql(sql, params).get("rows")
        parity = parity and actual == expected
        plan_details = " ".join(
            str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + rewritten, params).fetchall()
        )
        parity = parity and "native_element_meta_cwd_role_ts_idx" in plan_details
        parity = parity and "USE TEMP B-TREE" not in plan_details

    literal_sql = (
        "SELECT text FROM native_element_fts WHERE role='assistant' "
        "AND native_element_fts MATCH 'offline' AND cwd='/proj' "
        "ORDER BY ts_utc DESC LIMIT 2"
    )
    literal_expected = [list(row) for row in conn.execute(literal_sql).fetchall()]
    literal_actual = idx.run_readonly_sql(literal_sql).get("rows")
    ok = parity and literal_actual == literal_expected
    conn.execute("DELETE FROM native_element_meta WHERE rowid > ?", (start_rowid,))
    conn.execute("DELETE FROM native_element_fts WHERE rowid > ?", (start_rowid,))
    conn.commit()
    print(f"{OK if ok else FAIL} MATCH recency rewrite preserves params/results and indexed plan "
          f"(plan={plan_details!r}, literal={literal_actual})")
    return ok


def test_match_recency_rewrite_rejects_near_misses() -> bool:
    near_misses = [
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH ? AND cwd=? OR role=? ORDER BY ts_utc DESC LIMIT 2",
        "SELECT bm25(native_element_fts) FROM native_element_fts WHERE native_element_fts MATCH ? AND cwd=? AND role=? ORDER BY ts_utc DESC LIMIT 2",
        "SELECT * FROM native_element_fts WHERE native_element_fts MATCH ? AND cwd=? AND role=? ORDER BY ts_utc DESC LIMIT 2",
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH ? AND cwd=? AND role=? ORDER BY ts_utc DESC LIMIT 2 OFFSET 1",
        "SELECT text FROM native_element_fts e WHERE native_element_fts MATCH ? AND cwd=? AND role=? ORDER BY ts_utc DESC LIMIT 2",
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH ? AND cwd=? AND role=? AND path=? ORDER BY ts_utc DESC LIMIT 2",
    ]
    ok = all(idx._rewrite_fast_metadata_sql(sql) is None for sql in near_misses)
    print(f"{OK if ok else FAIL} MATCH recency rewrite rejects complex/near-miss SQL")
    return ok


def test_unbounded_match_rewrite_parity_plans_and_edge_cases() -> bool:
    conn = idx._writer_connection()
    start_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM native_element_fts").fetchone()[0]
    rows = [
        ("quoted and offline", "/p/ties.jsonl", "ties", "/ties", "claude",
         "user_prompt", "", "2025-01-02T00:00:00.000000Z", "user"),
        ("offline tie second", "/p/ties.jsonl", "ties", "/ties", "claude",
         "user_prompt", "", "2025-01-02T00:00:00.000000Z", "user"),
        ("offline assistant", "/p/ties.jsonl", "ties", "/ties", "claude",
         "assistant_text", "", "2025-01-03T00:00:00.000000Z", "assistant"),
        ("offline old", "/p/ties.jsonl", "ties", "/ties", "claude",
         "user_prompt", "", "2025-01-01T00:00:00.000000Z", "user"),
    ]
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    indexed = conn.execute(
        "SELECT rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index "
        "FROM native_element_fts WHERE rowid > ?", (start_rowid,),
    ).fetchall()
    conn.executemany(
        "INSERT INTO native_element_meta"
        "(rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        indexed,
    )
    conn.commit()

    predicate_sets = [
        [("native_element_fts MATCH ?", "offline"), ("cwd = ?", "/ties")],
        [("native_element_fts MATCH ?", "offline"), ("cwd = ?", "/ties"), ("role = ?", "user")],
        [("native_element_fts MATCH ?", "offline"), ("cwd = ?", "/ties"),
         ("ts_utc >= ?", "2025-01-02T00:00:00.000000Z")],
        [("native_element_fts MATCH ?", "offline"), ("cwd = ?", "/ties"),
         ("role = ?", "user"), ("ts_utc > ?", "2025-01-01T00:00:00.000000Z"),
         ("ts_utc <= ?", "2025-01-02T00:00:00.000000Z")],
    ]
    parity = True
    checked = 0
    for predicates in predicate_sets:
        permutations = list(itertools.permutations(predicates))
        for direction in ("ASC", "DESC"):
            for ordered in permutations:
                for limit in (None, 0, 1, 99):
                    clauses = [item[0] for item in ordered]
                    params = tuple(item[1] for item in ordered)
                    suffix = "" if limit is None else " LIMIT ?"
                    if limit is not None:
                        params += (limit,)
                    sql = (
                        "SELECT rowid, text, ts_utc FROM native_element_fts WHERE "
                        + " AND ".join(clauses)
                        + f" ORDER BY ts_utc {direction}{suffix}"
                    )
                    rewritten = idx._rewrite_match_recency_sql(sql)
                    if rewritten is None:
                        parity = False
                        continue
                    expected = conn.execute(sql, params).fetchall()
                    actual = conn.execute(rewritten, params).fetchall()
                    details = " ".join(
                        str(row[-1])
                        for row in conn.execute("EXPLAIN QUERY PLAN " + rewritten, params).fetchall()
                    )
                    wanted_index = "cwd_role_ts" if any("role" in part for part in clauses) else "cwd_ts"
                    if direction == "ASC":
                        wanted_index += "_asc"
                    expected_groups: dict[str, set[tuple[int, str]]] = {}
                    actual_groups: dict[str, set[tuple[int, str]]] = {}
                    for rowid, text, timestamp in expected:
                        expected_groups.setdefault(timestamp, set()).add((rowid, text))
                    for rowid, text, timestamp in actual:
                        actual_groups.setdefault(timestamp, set()).add((rowid, text))
                    parity = parity and [row[2] for row in actual] == [row[2] for row in expected]
                    parity = parity and actual_groups == expected_groups
                    parity = parity and f"native_element_meta_{wanted_index}_idx" in details
                    parity = parity and "USE TEMP B-TREE" not in details
                    checked += 1

    quoted_sql = (
        "SELECT text FROM native_element_fts WHERE "
        "native_element_fts MATCH '\"quoted and offline\"' AND cwd='/ties' ORDER BY ts_utc"
    )
    quoted = idx._rewrite_match_recency_sql(quoted_sql)
    quoted_parity = quoted is not None and conn.execute(quoted_sql).fetchall() == conn.execute(quoted).fetchall()
    empty_sql = (
        "SELECT text FROM native_element_fts WHERE "
        "native_element_fts MATCH '' AND cwd='/ties' ORDER BY ts_utc"
    )
    empty = idx._rewrite_match_recency_sql(empty_sql)
    empty_errors = []
    for query in (empty_sql, empty):
        try:
            conn.execute(query or "").fetchall()
        except Exception as exc:
            empty_errors.append(type(exc))
    parity = parity and quoted_parity and len(empty_errors) == 2 and empty_errors[0] is empty_errors[1]

    conn.execute("DELETE FROM native_element_meta WHERE rowid > ?", (start_rowid,))
    conn.execute("DELETE FROM native_element_fts WHERE rowid > ?", (start_rowid,))
    conn.commit()
    print(f"{OK if parity else FAIL} unbounded MATCH parity + indexed plans "
          f"(cases={checked}, quoted={quoted_parity}, empty_errors={empty_errors})")
    return parity


def test_match_recency_rewrite_reduces_vm_work() -> bool:
    conn = idx._writer_connection()
    start_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM native_element_fts").fetchone()[0]
    rows = []
    for i in range(4000):
        rows.append((f"offline distractor {i}", f"/other/{i}.jsonl", f"o{i}", "/other", "claude",
                     "assistant_text", "", f"2025-02-01T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}.000000Z", "assistant"))
        if i < 120:
            rows.append((f"target nonmatch {i}", "/p/perf.jsonl", "perf", "/perf", "claude",
                         "assistant_text", "", f"2025-03-01T00:{i // 60:02d}:{i % 60:02d}.000000Z", "assistant"))
        if i < 80:
            rows.append((f"offline target {i}", "/p/perf.jsonl", "perf", "/perf", "claude",
                         "assistant_text", "", f"2025-01-01T00:{i // 60:02d}:{i % 60:02d}.000000Z", "assistant"))
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text, path, sid, cwd, tag, element_kind, tool_name, ts_utc, role) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    indexed = conn.execute(
        "SELECT rowid, path, sid, cwd, tag, element_kind, tool_name, ts_utc, role, element_id, element_index "
        "FROM native_element_fts WHERE rowid > ?", (start_rowid,),
    ).fetchall()
    conn.executemany(
        "INSERT INTO native_element_meta"
        "(rowid, path, sid, cwd, tag, element_kind, tool_name, ts_utc, role, element_id, element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        indexed,
    )
    conn.commit()
    sql = (
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH ? "
        "AND cwd = ? AND role = ? ORDER BY ts_utc DESC LIMIT ?"
    )
    params = ("offline", "/perf", "assistant", 60)
    rewritten = idx._rewrite_fast_metadata_sql(sql)

    def measured(query: str) -> tuple[list[tuple], int]:
        calls = 0
        def progress() -> int:
            nonlocal calls
            calls += 1
            return 0
        conn.set_progress_handler(progress, 100)
        try:
            return conn.execute(query, params).fetchall(), calls
        finally:
            conn.set_progress_handler(None, 0)

    original_rows, original_ops = measured(sql)
    rewritten_rows, rewritten_ops = measured(rewritten or sql)
    ok = (
        rewritten is not None
        and rewritten_rows == original_rows
        and len(rewritten_rows) == 60
        and rewritten_ops * 5 < original_ops
    )
    conn.execute("DELETE FROM native_element_meta WHERE rowid > ?", (start_rowid,))
    conn.execute("DELETE FROM native_element_fts WHERE rowid > ?", (start_rowid,))
    conn.commit()
    print(f"{OK if ok else FAIL} MATCH recency rewrite reduces VM work "
          f"(callbacks={original_ops}->{rewritten_ops}, rows={len(rewritten_rows)})")
    return ok


def test_unbounded_match_rewrite_reduces_median_vm_work() -> bool:
    conn = idx._writer_connection()
    start_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM native_element_fts").fetchone()[0]
    rows = [
        (f"floodneedle distractor {i}", f"/other/{i}.jsonl", f"other-{i}", "/other",
         "claude", "assistant_text", "", f"2025-02-01T00:{i // 60:02d}:{i % 60:02d}Z", "assistant")
        for i in range(4000)
    ]
    rows.extend(
        (f"floodneedle target {i}", "/target/a.jsonl", "target", "/target", "claude",
         "user_prompt", "", f"2025-01-01T00:{i // 60:02d}:{i % 60:02d}Z", "user")
        for i in range(100)
    )
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    indexed = conn.execute(
        "SELECT rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index "
        "FROM native_element_fts WHERE rowid > ?", (start_rowid,),
    ).fetchall()
    conn.executemany(
        "INSERT INTO native_element_meta"
        "(rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        indexed,
    )
    conn.commit()

    ratios = []
    parity = True
    for role_clause, params in (("", ("floodneedle", "/target")), (" AND role = ?", ("floodneedle", "/target", "user"))):
        sql = (
            "SELECT rowid, text FROM native_element_fts WHERE native_element_fts MATCH ? "
            f"AND cwd = ?{role_clause} ORDER BY ts_utc ASC"
        )
        rewritten = idx._rewrite_match_recency_sql(sql)
        measurements: list[tuple[int, int]] = []
        for _ in range(5):
            counts = []
            outputs = []
            for query in (sql, rewritten or sql):
                callbacks = 0
                def progress() -> int:
                    nonlocal callbacks
                    callbacks += 1
                    return 0
                conn.set_progress_handler(progress, 100)
                try:
                    outputs.append(conn.execute(query, params).fetchall())
                finally:
                    conn.set_progress_handler(None, 0)
                counts.append(callbacks)
            parity = parity and outputs[0] == outputs[1]
            measurements.append((counts[0], counts[1]))
        original_median = statistics.median(item[0] for item in measurements)
        rewritten_median = statistics.median(item[1] for item in measurements)
        ratios.append(original_median / max(1, rewritten_median))

    ok = parity and min(ratios) > 5
    conn.execute("DELETE FROM native_element_meta WHERE rowid > ?", (start_rowid,))
    conn.execute("DELETE FROM native_element_fts WHERE rowid > ?", (start_rowid,))
    conn.commit()
    print(f"{OK if ok else FAIL} unbounded MATCH rewrite cuts median VM work >5x "
          f"(ratios={ratios}, parity={parity})")
    return ok


def test_match_recency_plan_selects_lower_cardinality_path() -> bool:
    conn = idx._writer_connection()
    start_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM native_element_fts").fetchone()[0]
    rows = [
        (f"common local nonmatch {i}", f"/large/{i}.jsonl", f"local-{i}", "/large",
         "claude", "assistant_text", "", f"2025-01-01T00:{i % 60:02d}:00Z", "assistant")
        for i in range(12_000)
    ]
    rows.extend(
        (f"rareplan global {i}", f"/other/{i}.jsonl", f"other-{i}", "/other",
         "claude", "assistant_text", "", f"2025-02-01T00:{i % 60:02d}:00Z", "assistant")
        for i in range(200)
    )
    rows.extend(
        (f"rareplan local {i}", f"/large/hit-{i}.jsonl", f"hit-{i}", "/large",
         "claude", "assistant_text", "", f"2025-03-01T00:0{i}:00Z", "assistant")
        for i in range(5)
    )
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    indexed = conn.execute(
        "SELECT rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index "
        "FROM native_element_fts WHERE rowid > ?", (start_rowid,),
    ).fetchall()
    conn.executemany(
        "INSERT INTO native_element_meta"
        "(rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        indexed,
    )
    conn.commit()

    selective_sql = (
        "SELECT rowid, text, ts_utc FROM native_element_fts "
        "WHERE cwd = ? AND native_element_fts MATCH ? ORDER BY ts_utc DESC"
    )
    selective_params = ("/large", "rareplan")
    selective = idx._choose_match_recency_sql(conn, selective_sql, selective_params)
    metadata_sql = idx._rewrite_match_recency_sql(selective_sql)

    broad_sql = (
        "SELECT rowid, text FROM native_element_fts "
        "WHERE native_element_fts MATCH ? AND cwd = ? ORDER BY ts_utc ASC"
    )
    broad = idx._choose_match_recency_sql(conn, broad_sql, ("common", "/large"))

    callbacks: list[int] = []
    outputs = []
    for query in (metadata_sql, selective[0] if selective else None):
        count = 0
        def progress() -> int:
            nonlocal count
            count += 1
            return 0
        conn.set_progress_handler(progress, 100)
        try:
            outputs.append(conn.execute(query or "", selective_params).fetchall())
        finally:
            conn.set_progress_handler(None, 0)
        callbacks.append(count)

    ok = (
        selective is not None
        and selective[1] == "match_fts"
        and selective[2] == {"match_rows": 205, "metadata_rows": idx._SQL_PLAN_PROBE_LIMIT + 1}
        and broad is not None
        and broad[1] == "match_fts"
        and outputs[0] == outputs[1]
        and len(outputs[1]) == 5
        and callbacks[1] * 10 < callbacks[0]
    )
    conn.execute("DELETE FROM native_element_meta WHERE rowid > ?", (start_rowid,))
    conn.execute("DELETE FROM native_element_fts WHERE rowid > ?", (start_rowid,))
    conn.commit()
    print(f"{OK if ok else FAIL} MATCH planner selects bounded lower-cardinality path "
          f"(selective={selective[1:] if selective else None}, broad={broad[1:] if broad else None}, "
          f"callbacks={callbacks})")
    return ok


def test_observed_match_recency_templates_preserve_direct_results() -> bool:
    conn = idx._writer_connection()
    start_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM native_element_fts").fetchone()[0]
    rows = [
        ('0166f772 ask-fork requirements-service', '/obs/a', 'obs-a', '/Users/ofekron/better-claude',
         'claude', 'user_prompt', '', '2026-07-10T16:55:00Z', 'user'),
        ('reliability no results completion dispatch processor timeout empty', '/obs/b', 'obs-b',
         '/Users/ofekron/better-extra', 'codex', 'user_prompt', '', '2026-07-10T16:55:00Z', 'user'),
        ('0166f772 complete dispatch semaphore completion orphan — שלום', '/obs/c', 'obs-c',
         '/Users/ofekron/better-claude', 'gemini', 'user_prompt', '', '2026-07-10T17:00:00Z', None),
        ('rate limit parse classifier error retry nns agents infra ' + ('x' * 20_000), '/obs/d', 'obs-d',
         '/external', 'claude', 'assistant_text', '', '2026-07-10T17:09:00Z', 'user'),
    ]
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    inserted = conn.execute(
        "SELECT rowid,text,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index "
        "FROM native_element_fts WHERE rowid > ?", (start_rowid,),
    ).fetchall()
    conn.executemany(
        "INSERT INTO native_element_text(rowid,text) VALUES (?,?)",
        [(row[0], row[1]) for row in inserted],
    )
    conn.executemany(
        "INSERT INTO native_element_meta"
        "(rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(row[0], *row[2:]) for row in inserted],
    )
    conn.commit()
    queries = [
        "SELECT ts_utc, role, element_kind, substr(text,1,400) AS snippet, path, element_index "
        "FROM native_element_fts WHERE cwd = '/Users/ofekron/better-claude' AND "
        "(native_element_fts MATCH '0166f772 OR \"ask-fork\" OR \"ask fork\" OR \"requirements-service\"') "
        "ORDER BY ts_utc ASC",
        "SELECT ts_utc, role, element_kind, substr(text,1,600) AS snippet, path, element_index "
        "FROM native_element_fts WHERE cwd GLOB '/Users/ofekron/better*' AND role='user' "
        "AND element_kind='user_prompt' AND ts_utc >= '2026-07-08' AND "
        "(native_element_fts MATCH 'reliability OR \"no results\" OR completion OR dispatch OR fork OR processor OR timeout OR \"empty\"') "
        "ORDER BY ts_utc ASC",
        "SELECT ts_utc, role, element_kind, substr(text,1,1800) AS snippet, path, element_index "
        "FROM native_element_fts WHERE cwd GLOB '/Users/ofekron/better*' "
        "AND ts_utc >= '2026-07-10T16:40:00' AND ts_utc <= '2026-07-10T17:30:00' AND "
        "(native_element_fts MATCH '0166f772 OR \"complete\" OR dispatch OR semaphore OR completion OR orphan') "
        "ORDER BY ts_utc ASC",
        "SELECT path, element_index, role, substr(text,1,400) as snippet, ts_utc "
        "FROM native_element_fts WHERE native_element_fts MATCH "
        "'rate AND (limit OR limits) AND (parse OR classifier OR error OR retry OR nns OR agents OR infra)' "
        "AND role='user' ORDER BY ts_utc DESC",
    ]
    parity = True
    routes = []
    probes = []
    for query in queries:
        expected = conn.execute(query).fetchall()
        parsed = idx._parse_match_recency_sql(query)
        chosen = idx._choose_match_recency_sql(conn, query, ())
        if parsed is None or chosen is None:
            parity = False
            continue
        actual = conn.execute(chosen[0]).fetchall()
        parity = parity and actual == expected
        parity = parity and [item[0] for item in conn.execute(query).description] == [item[0] for item in conn.execute(chosen[0]).description]
        routes.append(chosen[1])
        probes.append(chosen[2])
    projection_count = conn.execute("SELECT COUNT(*) FROM native_element_text WHERE rowid > ?", (start_rowid,)).fetchone()[0]
    projection_text = conn.execute("SELECT text FROM native_element_text WHERE rowid = ?", (inserted[-1][0],)).fetchone()[0]
    conn.execute("DELETE FROM native_element_text WHERE rowid > ?", (start_rowid,))
    conn.execute("DELETE FROM native_element_meta WHERE rowid > ?", (start_rowid,))
    conn.execute("DELETE FROM native_element_fts WHERE rowid > ?", (start_rowid,))
    conn.commit()
    ok = (
        parity and len(routes) == 4
        and all(set(probe) == {'match_rows', 'metadata_rows'} for probe in probes)
        and projection_count == len(rows) and projection_text == rows[-1][0]
    )
    print(f"{OK if ok else FAIL} observed MATCH templates preserve direct rows/columns "
          f"(routes={routes}, projected={projection_count}, parity={parity})")
    return ok


def test_match_recency_recognizer_falls_back_on_unsafe_shapes() -> bool:
    queries = [
        "SELECT substr(text,1,2+2) AS snippet FROM native_element_fts "
        "WHERE native_element_fts MATCH 'offline' ORDER BY ts_utc",
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH 'offline' "
        "AND cwd GLOB '/proj/?' ORDER BY ts_utc",
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH 'offline' "
        "AND cwd GLOB '/proj/[ab]*' ORDER BY ts_utc",
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH 'offline' "
        "AND cwd GLOB '*' ORDER BY ts_utc",
        "SELECT text FROM native_element_fts WHERE (native_element_fts MATCH 'offline' OR role='user') "
        "ORDER BY ts_utc",
        "SELECT random() AS text FROM native_element_fts WHERE native_element_fts MATCH 'offline' ORDER BY ts_utc",
    ]
    recognized = [idx._parse_match_recency_sql(query) for query in queries]
    direct = [idx.run_readonly_sql(query) for query in queries]
    ok = all(item is None for item in recognized) and len(direct) == len(queries)
    print(f"{OK if ok else FAIL} unsafe optimizer shapes retain direct SELECT fallback "
          f"(recognized={[item is not None for item in recognized]}, errors={[item.get('error') for item in direct]})")
    return ok


def test_raw_text_projection_replace_and_delete_converges() -> bool:
    conn = idx._writer_connection()
    tables = ('native_element_fts', 'native_element_meta', 'native_element_text')
    baseline = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in tables
    }
    candidate = SimpleNamespace(
        transcript=__import__('pathlib').Path('/projection/replaced.jsonl'),
        format='claude', sid='projection-sid', cwd='/projection',
    )
    original_index_rows = idx._index_candidate_rows
    original_classify = idx.run_source_index.classify_path
    texts = ['first projection text', 'replacement projection text — שלום']

    def row_for(text: str):
        return (
            text, str(candidate.transcript), candidate.sid, candidate.cwd, 'claude',
            'user_prompt', '', '2026-07-10T00:00:00Z', 'user', 'element', 0,
            'text-hash', 'norm-hash', 'p1024', 'p4096', 'p8192', len(text), len(text),
        )

    try:
        idx.run_source_index.classify_path = lambda _path: idx.run_source_index.EXTERNAL
        idx._index_candidate_rows = lambda *_args, **_kwargs: [row_for(texts[0])]
        idx._replace_candidate(conn, candidate, 1.0, 10)
        conn.commit()
        first_rowid = conn.execute(
            "SELECT rowid FROM native_element_meta WHERE path=?", (str(candidate.transcript),)
        ).fetchone()[0]
        first_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        first = conn.execute(
            "SELECT f.text, r.text FROM native_element_fts f "
            "JOIN native_element_text r ON r.rowid=f.rowid WHERE f.path=?",
            (str(candidate.transcript),),
        ).fetchall()
        idx._index_candidate_rows = lambda *_args, **_kwargs: [row_for(texts[1])]
        idx._replace_candidate(conn, candidate, 2.0, 20)
        conn.commit()
        replacement_rowid = conn.execute(
            "SELECT rowid FROM native_element_meta WHERE path=?", (str(candidate.transcript),)
        ).fetchone()[0]
        replacement_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        reused_raw_text = conn.execute(
            "SELECT text FROM native_element_text WHERE rowid=?", (first_rowid,)
        ).fetchone()
        replaced = conn.execute(
            "SELECT f.text, r.text FROM native_element_fts f "
            "JOIN native_element_text r ON r.rowid=f.rowid WHERE f.path=?",
            (str(candidate.transcript),),
        ).fetchall()
        idx._delete_path(conn, str(candidate.transcript))
        conn.commit()
        final_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        replacement_raw = conn.execute(
            "SELECT COUNT(*) FROM native_element_text WHERE rowid=?", (replacement_rowid,)
        ).fetchone()[0]
    finally:
        idx._index_candidate_rows = original_index_rows
        idx.run_source_index.classify_path = original_classify
    ok = (
        first == [(texts[0], texts[0])]
        and replaced == [(texts[1], texts[1])]
        and reused_raw_text == (texts[1],) and replacement_raw == 0
        and all(first_counts[table] == baseline[table] + 1 for table in tables)
        and all(replacement_counts[table] == baseline[table] + 1 for table in tables)
        and final_counts == baseline
    )
    print(f"{OK if ok else FAIL} raw text projection replace/delete convergence "
          f"(rowids={first_rowid}->{replacement_rowid}, replaced_raw={reused_raw_text == (texts[1],)}, "
          f"replacement_raw={replacement_raw}, final={final_counts == baseline})")
    return ok


def test_unbounded_rowid_metadata_scan_is_allowed() -> bool:
    out = idx.run_readonly_sql(
        "SELECT text FROM native_element_fts WHERE path = '/p/large.jsonl' ORDER BY rowid DESC"
    )
    ok = (
        out.get("error") is None
        and len(out.get("rows") or []) == 2000
        and out["rows"][0][0] == "large path row 1999"
    )
    print(f"{OK if ok else FAIL} unbounded rowid metadata scan allowed "
          f"(rows={len(out.get('rows') or [])}, error={out.get('error')!r})")
    return ok


def test_metadata_on_fts_shapes_are_allowed() -> bool:
    queries = [
        (
            "SELECT text FROM native_element_fts "
            "WHERE native_element_fts MATCH 'offline' AND cwd = '/proj' "
            "ORDER BY bm25(native_element_fts) LIMIT 10",
            4,
        ),
        (
            "SELECT text FROM native_element_fts "
            "WHERE native_element_fts MATCH 'offline' AND role = 'assistant' "
            "ORDER BY ts_utc DESC LIMIT 10",
            2,
        ),
        (
            "SELECT text FROM native_element_fts "
            "WHERE native_element_fts MATCH 'offline' AND cwd = '/proj' AND role = 'assistant' "
            "ORDER BY ts_utc DESC LIMIT 10",
            2,
        ),
        (
            "SELECT text FROM native_element_fts "
            "WHERE native_element_fts MATCH 'offline' AND path = '/p/a.jsonl' AND role = 'assistant' "
            "ORDER BY ts_utc DESC LIMIT 10",
            1,
        ),
        (
            "SELECT text FROM native_element_fts WHERE path = '/p/a.jsonl'",
            2,
        ),
    ]
    results = [idx.run_readonly_sql(sql) for sql, _count in queries]
    ok = all(
        result.get("error") is None and len(result.get("rows") or []) == count
        for result, (_sql, count) in zip(results, queries)
    )
    print(f"{OK if ok else FAIL} metadata-on-FTS shapes allowed "
          f"(row_counts={[len(result.get('rows') or []) for result in results]}, "
          f"errors={[result.get('error') for result in results]})")
    return ok


def test_analytics_metadata_fallback_query_uses_path_index() -> bool:
    query = (
        "SELECT path, COALESCE(MAX(sid), '') AS sid, "
        "COUNT(CASE WHEN element_kind IN ('user_prompt', 'assistant_text') THEN 1 END) AS message_count "
        "FROM native_element_meta INDEXED BY native_element_meta_path_ts_idx "
        "WHERE path IN (?, ?) AND ts_utc >= ? AND ts_utc <= ? "
        "AND element_kind IN ('user_prompt', 'assistant_text') "
        "GROUP BY path"
    )
    params = ("/p/a.jsonl", "/p/b.jsonl", "2024-01-01T00:00:00.000000Z", "2024-01-02T23:59:59.000000Z")
    conn = idx._writer_connection()
    plan_rows = conn.execute("EXPLAIN QUERY PLAN " + query, params).fetchall()
    out = idx.run_readonly_sql(query, params)
    details = " ".join(str(row[-1]) for row in plan_rows)
    ok = (
        "native_element_meta_path_ts_idx" in details
        and "SCAN native_element_fts" not in details
        and "USE TEMP B-TREE" not in details
        and out.get("error") is None
        and out["rows"] == [["/p/a.jsonl", "sA", 2], ["/p/b.jsonl", "sB", 1]]
    )
    print(f"{OK if ok else FAIL} analytics metadata fallback query uses path index "
          f"(plan={details!r}, rows={out.get('rows')})")
    return ok


def test_analytics_conversations_turns_query_uses_kind_path_index() -> bool:
    query = (
        "SELECT path, ts_utc FROM native_element_meta "
        "INDEXED BY native_element_meta_kind_path_ts_idx "
        "WHERE element_kind = 'user_prompt' AND ts_utc >= ? AND ts_utc <= ? "
        "ORDER BY path, ts_utc, rowid"
    )
    params = ("2024-01-01T00:00:00.000000Z", "2024-01-02T23:59:59.000000Z")
    conn = idx._writer_connection()
    plan_rows = conn.execute("EXPLAIN QUERY PLAN " + query, params).fetchall()
    out = idx.run_readonly_sql(query, params)
    details = " ".join(str(row[-1]) for row in plan_rows)
    ok = (
        "native_element_meta_kind_path_ts_idx" in details
        and "SCAN native_element_fts" not in details
        and "USE TEMP B-TREE" not in details
        and out.get("error") is None
        and out["rows"] == [
            ["/p/a.jsonl", "2024-01-01T00:00:00.000000Z"],
            ["/p/b.jsonl", "2024-01-02T00:00:00.000000Z"],
        ]
    )
    print(f"{OK if ok else FAIL} analytics conversations turns query uses kind+path index "
          f"(plan={details!r}, rows={out.get('rows')})")
    return ok


def main_run() -> int:
    _seed()
    tests = [
        test_select_group_by_and_bm25,
        test_write_is_denied,
        test_attach_is_denied,
        test_no_row_or_cell_truncation,
        test_multi_statement_and_nonselect,
        test_slow_query_shape_is_measured_without_sql_leak,
        test_sql_shape_detects_filters_and_ts_ordering,
        test_metadata_recency_queries_use_meta_index,
        test_path_rowid_query_is_rewritten_through_meta_index,
        test_path_role_rowid_query_is_rewritten_through_meta_index,
        test_match_recency_query_rewrites_with_plan_and_param_parity,
        test_match_recency_rewrite_rejects_near_misses,
        test_unbounded_match_rewrite_parity_plans_and_edge_cases,
        test_match_recency_rewrite_reduces_vm_work,
        test_unbounded_match_rewrite_reduces_median_vm_work,
        test_match_recency_plan_selects_lower_cardinality_path,
        test_observed_match_recency_templates_preserve_direct_results,
        test_match_recency_recognizer_falls_back_on_unsafe_shapes,
        test_raw_text_projection_replace_and_delete_converges,
        test_analytics_metadata_fallback_query_uses_path_index,
        test_analytics_conversations_turns_query_uses_kind_path_index,
        test_unbounded_rowid_metadata_scan_is_allowed,
        test_metadata_on_fts_shapes_are_allowed,
        test_readonly_sql_refreshes_before_opening_db,
        test_ensure_fresh_for_read_refreshes_stale_covered_index,
        test_missing_index_reports_cleanly,  # last: it wipes the index
    ]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {fn.__name__} raised: {e}")
            results.append(False)
    n_pass = sum(1 for r in results if r)
    print(f"\n{n_pass}/{len(results)} native-sql-sandbox tests passed")
    idx.shutdown()
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main_run())

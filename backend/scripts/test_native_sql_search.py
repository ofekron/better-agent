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
import json
import itertools
import logging
import os
import shutil
import statistics
import sys
import threading
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
    original_request_refresh = idx.request_refresh
    calls: list[str] = []

    def fake_request_refresh() -> None:
        calls.append("request_refresh")
        return None

    try:
        idx.request_refresh = fake_request_refresh  # type: ignore[assignment]
        started = time.perf_counter()
        state = idx.ensure_fresh_for_read()
        elapsed = time.perf_counter() - started
    finally:
        idx.request_refresh = original_request_refresh  # type: ignore[assignment]
    ok = calls == ["request_refresh"] and state.get("usable") is False and elapsed < 0.1
    print(f"{OK if ok else FAIL} stale query without owner exits immediately (calls={calls}, state={state})")
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
    ingest_started = time.perf_counter()
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
    rejected = [item.get("error_code") for item in direct]
    ok = (
        all(item is None for item in recognized)
        and rejected == ["unsupported_native_transcript_query_shape"] * len(queries)
    )
    print(f"{OK if ok else FAIL} dangerous MATCH metadata shapes reject before execution "
          f"(recognized={[item is not None for item in recognized]}, errors={[item.get('error') for item in direct]})")
    return ok


def test_production_path_element_window_rewrites_with_parity_and_bounded_work() -> bool:
    conn = idx._writer_connection()
    start_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM native_element_fts").fetchone()[0]
    rows = [
        (f"window payload {i}", "/production/window.jsonl", "window", "/proj", "codex",
         "assistant_text", "", "2026-07-11T00:00:00.000000Z", "assistant", f"w{i}", i)
        for i in range(20_000)
    ]
    ingest_started = time.perf_counter()
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
    )
    indexed = conn.execute(
        "SELECT rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index "
        "FROM native_element_fts WHERE rowid > ?", (start_rowid,),
    ).fetchall()
    conn.executemany(
        "INSERT INTO native_element_meta"
        "(rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", indexed,
    )
    conn.commit()
    ingest_elapsed = time.perf_counter() - ingest_started
    conn.execute("DROP INDEX native_element_meta_path_element_index_idx")
    build_started = time.perf_counter()
    conn.execute(
        "CREATE INDEX native_element_meta_path_element_index_idx "
        "ON native_element_meta(path, element_index, rowid)"
    )
    conn.commit()
    build_elapsed = time.perf_counter() - build_started
    sql = (
        "SELECT path, element_index, role, element_kind, text FROM native_element_fts "
        "WHERE path = ? AND element_index BETWEEN ? AND ? ORDER BY element_index"
    )
    params = ("/production/window.jsonl", 9_995, 10_005)
    rewritten = idx._rewrite_path_element_window_sql(sql, params)
    expected = [list(row) for row in conn.execute(sql, params).fetchall()]
    started = time.perf_counter()
    result = idx.run_readonly_sql(sql, params, timeout_s=5)
    elapsed = time.perf_counter() - started
    plan = " ".join(str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + rewritten, params))
    conn.execute("DELETE FROM native_element_meta WHERE rowid > ?", (start_rowid,))
    conn.execute("DELETE FROM native_element_fts WHERE rowid > ?", (start_rowid,))
    conn.commit()
    ok = (
        rewritten is not None and result.get("rows") == expected
        and result.get("execution_route") == "path_element_window" and elapsed < 1.0
        and ingest_elapsed < 5.0 and build_elapsed < 5.0
        and "native_element_meta_path_element_index_idx" in plan
        and "USE TEMP B-TREE" not in plan
    )
    print(f"{OK if ok else FAIL} production path+element window is indexed "
          f"(elapsed={elapsed:.3f}s, indexed_ingest={ingest_elapsed:.3f}s, "
          f"index_build={build_elapsed:.3f}s, plan={plan!r})")
    return ok


def test_path_window_endpoint_validation_covers_mixed_bindings_and_int64() -> bool:
    maximum = idx._SQLITE_INT64_MAX
    cases = [
        ("SELECT text FROM native_element_fts WHERE path = '/p/a.jsonl' "
         "AND element_index BETWEEN ? AND 9223372036854775807 ORDER BY element_index", (maximum - 1,), True),
        ("SELECT text FROM native_element_fts WHERE path = ? "
         "AND element_index BETWEEN 9223372036854775806 AND ? ORDER BY element_index", ("/p/a.jsonl", maximum), True),
        ("SELECT text FROM native_element_fts WHERE path = ? "
         "AND element_index BETWEEN ? AND ? ORDER BY element_index", ("/p/a.jsonl", True, 2), False),
        ("SELECT text FROM native_element_fts WHERE path = ? "
         "AND element_index BETWEEN ? AND ? ORDER BY element_index", ("/p/a.jsonl", 0, maximum + 1), False),
        ("SELECT text FROM native_element_fts WHERE path = '/p/a.jsonl' "
         "AND element_index BETWEEN 0 AND 9223372036854775808 ORDER BY element_index", (), False),
    ]
    rewritten = [idx._rewrite_path_element_window_sql(sql, params) for sql, params, _ok in cases]
    ok = all((value is not None) == expected for value, (_sql, _params, expected) in zip(rewritten, cases))
    print(f"{OK if ok else FAIL} path window validates mixed bindings and int64 endpoints")
    return ok


def test_sid_element_window_without_order_is_indexed() -> bool:
    conn = idx._writer_connection()
    start_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM native_element_fts").fetchone()[0]
    rows = [
        (f"sid window payload {i}", f"/sid-window/{i // 100}.jsonl", "shared-sid", "/proj", "codex",
         "assistant_text", "", "2026-07-11T00:00:00Z", "assistant", f"sid-{i}", i)
        for i in range(20_000)
    ]
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
    )
    indexed = conn.execute(
        "SELECT rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index "
        "FROM native_element_fts WHERE rowid > ?", (start_rowid,),
    ).fetchall()
    conn.executemany(
        "INSERT INTO native_element_meta"
        "(rowid,path,sid,cwd,tag,element_kind,tool_name,ts_utc,role,element_id,element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", indexed,
    )
    conn.commit()
    sql = (
        "SELECT sid, path, element_index, text FROM native_element_fts "
        "WHERE sid = ? AND element_index BETWEEN ? AND ? LIMIT ?"
    )
    params = ("shared-sid", 9_995, 10_005, 5)
    rewritten = idx._rewrite_path_element_window_sql(sql, params)
    expected = [list(row) for row in conn.execute(sql, params).fetchall()]
    started = time.perf_counter()
    result = idx.run_readonly_sql(sql, params, timeout_s=5)
    elapsed = time.perf_counter() - started
    plan = " ".join(str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + rewritten, params))
    conn.execute("DELETE FROM native_element_meta WHERE rowid > ?", (start_rowid,))
    conn.execute("DELETE FROM native_element_fts WHERE rowid > ?", (start_rowid,))
    conn.commit()
    ok = (
        rewritten is not None and result.get("rows") == expected
        and result.get("execution_route") == "path_element_window" and elapsed < 1.0
        and "native_element_meta_sid_element_index_idx" in plan
        and "USE TEMP B-TREE" not in plan and "ORDER BY" not in rewritten
    )
    print(f"{OK if ok else FAIL} sid+element window without order is indexed "
          f"(elapsed={elapsed:.3f}s, plan={plan!r})")
    return ok


def test_path_element_near_miss_rejects_before_open() -> bool:
    sql = (
        "SELECT path, element_index, text FROM native_element_fts WHERE path = ? "
        "AND element_index >= ? AND element_index <= ? ORDER BY element_index"
    )
    original_connect = idx._connect
    calls: list[str] = []
    idx._connect = lambda *_args, **_kwargs: calls.append("open")  # type: ignore[assignment]
    try:
        result = idx.run_readonly_sql(sql, ("/private/path", 1, 9))
    finally:
        idx._connect = original_connect  # type: ignore[assignment]
    ok = result.get("error_code") == "unsupported_native_transcript_query_shape" and calls == []
    print(f"{OK if ok else FAIL} path window near-miss rejects before open (calls={calls})")
    return ok


def test_interrupt_watchdog_bounds_execute_wall_time() -> bool:
    sql = (
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000000000) "
        "SELECT sum(x) FROM n"
    )
    started = time.perf_counter()
    result = idx.run_readonly_sql(sql, timeout_s=0.1)
    elapsed = time.perf_counter() - started
    ok = "interrupted" in str(result.get("error") or "") and elapsed < 0.5
    print(f"{OK if ok else FAIL} interrupt watchdog bounds execute ({elapsed:.3f}s)")
    return ok


def test_watchdog_repeated_near_deadline_has_no_thread_or_close_race() -> bool:
    sql = (
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000000000) "
        "SELECT sum(x) FROM n"
    )
    results = [idx.run_readonly_sql(sql, timeout_s=0.1) for _ in range(8)]
    lingering = [thread.name for thread in threading.enumerate()
                 if thread.name == "native-transcript-sql-deadline"]
    ok = all("interrupted" in str(result.get("error") or "") for result in results) and not lingering
    print(f"{OK if ok else FAIL} watchdog completion leaves no threads (lingering={lingering})")
    return ok


def test_nonfinite_timeout_rejects_before_open_or_timer() -> bool:
    original_connect = idx._connect
    calls: list[str] = []
    idx._connect = lambda *_args, **_kwargs: calls.append("open")  # type: ignore[assignment]
    try:
        results = [idx.run_readonly_sql("SELECT 1", timeout_s=value) for value in (float("nan"), float("inf"), -1)]
    finally:
        idx._connect = original_connect  # type: ignore[assignment]
    lingering = [thread.name for thread in threading.enumerate()
                 if thread.name == "native-transcript-sql-deadline"]
    ok = all(result.get("error_code") == "invalid_timeout" for result in results) and calls == [] and not lingering
    print(f"{OK if ok else FAIL} nonfinite timeout rejects before open/timer")
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
    texts = ['first projection text', 'replacement projection text — שלום']

    def row_for(text: str):
        return (
            text, str(candidate.transcript), candidate.sid, candidate.cwd, 'claude',
            'user_prompt', '', '2026-07-10T00:00:00Z', 'user', 'element', 0,
            'text-hash', 'norm-hash', 'p1024', 'p4096', 'p8192', len(text), len(text),
        )

    try:
        idx._insert_index_rows(conn, [row_for(texts[0])], str(candidate.transcript))
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
        idx._delete_path(conn, str(candidate.transcript), file_state=False)
        idx._insert_index_rows(conn, [row_for(texts[1])], str(candidate.transcript))
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
        idx._delete_path(conn, str(candidate.transcript), file_state=False)
        conn.commit()
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
    ok = (
        all(result.get("error") is None and len(result.get("rows") or []) == count
            for result, (_sql, count) in zip(results[:3], queries[:3]))
        and results[3].get("error_code") == "unsupported_native_transcript_query_shape"
        and results[4].get("error") is None and len(results[4].get("rows") or []) == 2
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


def test_match_recency_rejection_is_structural_and_private() -> bool:
    sql = (
        "SELECT random() AS secret_alias FROM native_element_fts "
        "WHERE native_element_fts MATCH 'private_literal' AND cwd = '/private/path' "
        "ORDER BY ts_utc DESC"
    )
    rejection = idx._match_recency_rejection(sql)
    encoded = json.dumps(rejection, sort_keys=True)
    ok = (
        rejection["stage"] == "projection"
        and rejection["reason"] == "unsupported_projection"
        and rejection["features"]["projection_alias"] is True
        and "private_literal" not in encoded
        and "/private/path" not in encoded
        and "secret_alias" not in encoded
    )
    print(f"{OK if ok else FAIL} MATCH rejection diagnostics are structural/private ({rejection})")
    return ok


def test_dangerous_match_shape_rejects_before_freshness_or_open() -> bool:
    sql = (
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH ? "
        "AND (cwd = ? OR role = ?) ORDER BY ts_utc DESC"
    )
    original_freshness = idx.ensure_fresh_for_read
    original_connect = idx._connect
    calls: list[str] = []
    idx.ensure_fresh_for_read = lambda *_args, **_kwargs: calls.append("freshness")  # type: ignore[assignment]
    idx._connect = lambda *_args, **_kwargs: calls.append("open")  # type: ignore[assignment]
    try:
        result = idx.run_readonly_sql(sql, ("needle", "/proj", "user"))
    finally:
        idx.ensure_fresh_for_read = original_freshness  # type: ignore[assignment]
        idx._connect = original_connect  # type: ignore[assignment]
    encoded = json.dumps(result, sort_keys=True)
    ok = (
        result.get("error_code") == "unsupported_native_transcript_query_shape"
        and result.get("execution_route") is None and calls == []
        and "needle" not in encoded and "/proj" not in encoded
    )
    print(f"{OK if ok else FAIL} dangerous MATCH shape rejects before freshness/open (calls={calls})")
    return ok


def test_match_allowlist_validates_parameter_types_counts_and_bounds() -> bool:
    sql = (
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH ? "
        "AND cwd = ? AND role = ? ORDER BY ts_utc DESC LIMIT ?"
    )
    valid = idx._validated_match_recency_query(sql, ("needle", "/proj", "user", 50))
    invalid = [
        idx._validated_match_recency_query(sql, ("needle", "/proj", "user")),
        idx._validated_match_recency_query(sql, ("needle", "/proj", "user", True)),
        idx._validated_match_recency_query(sql, ("needle", "/proj", "user", idx._SQL_SAFE_LIMIT + 1)),
        idx._validated_match_recency_query(sql, ("x" * (idx._SQL_SAFE_LITERAL_CHARS + 1), "/proj", "user", 1)),
    ]
    ok = valid is not None and all(value is None for value in invalid)
    print(f"{OK if ok else FAIL} MATCH allowlist validates params and bounds")
    return ok


def test_match_recency_alias_and_rowid_order_preserve_exact_parity() -> bool:
    sql = (
        "SELECT native_element_fts.text AS body, native_element_fts.ts_utc AS occurred "
        "FROM native_element_fts WHERE native_element_fts MATCH ? AND cwd = ? "
        "ORDER BY native_element_fts.ts_utc DESC, native_element_fts.rowid ASC"
    )
    params = ("offline", "/proj")
    rewritten = idx._rewrite_match_recency_sql(sql)
    conn = idx._writer_connection()
    direct = conn.execute(sql, params).fetchall()
    optimized = conn.execute(rewritten or "", params).fetchall()
    ok = rewritten is not None and direct == optimized
    print(f"{OK if ok else FAIL} alias + deterministic rowid MATCH rewrite preserves parity")
    return ok


def test_match_recency_text_like_is_rejected_before_execution() -> bool:
    template = (
        "SELECT path, substr(text,1,500) AS snippet FROM native_element_fts "
        "WHERE native_element_fts MATCH 'private_match_literal' AND {predicate} "
        "ORDER BY ts_utc DESC"
    )
    predicates = [
        "text LIKE ?",
        "text LIKE '%private_leading%'",
        "text LIKE 'private_prefix%'",
        "text LIKE 'private_under_score_'",
        "text LIKE NULL",
        "text LIKE '%private\\_%' ESCAPE '\\'",
        "lower(text) LIKE '%private_lower%'",
        "'%private_reverse%' LIKE text",
        '"text" LIKE "%private_quoted%"',
        '`native_element_fts`.`text` LIKE "%private_backtick%"',
        '[text] LIKE "%private_bracket%"',
        "trim(/* private_comment */ text) COLLATE nocase LIKE '%private_trim%'",
        "coalesce(text, '') LIKE '%private_coalesce%'",
        "native_element_fts.text /* private_gap */ LIKE '%private_qualified%'",
        "'%private_reverse_fn%' LIKE lower(text) COLLATE binary",
    ]
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    idx.logger.addHandler(handler)
    started = time.perf_counter()
    try:
        results = [idx.run_readonly_sql(template.format(predicate=predicate), ("%private_param%",))
                   for predicate in predicates]
    finally:
        idx.logger.removeHandler(handler)
    elapsed = time.perf_counter() - started
    encoded = json.dumps(results, sort_keys=True)
    metadata_like = [
        idx.run_readonly_sql(
            "SELECT text FROM native_element_fts WHERE path LIKE '/p/%' ORDER BY rowid LIMIT 2"
        ),
        idx.run_readonly_sql(
            "SELECT text FROM native_element_fts WHERE cwd LIKE '/pr_j' "
            "AND role = 'user' ORDER BY rowid LIMIT 2"
        ),
        idx.run_readonly_sql(
            "SELECT text FROM native_element_fts WHERE path LIKE '%text%' "
            "AND role = 'user' ORDER BY rowid LIMIT 2"
        ),
        idx.run_readonly_sql(
            "SELECT text FROM native_element_fts WHERE path /* text LIKE */ LIKE '/p/%' "
            "ORDER BY rowid LIMIT 2"
        ),
        idx.run_readonly_sql(
            "SELECT path FROM native_element_fts WHERE path LIKE ? LIMIT 2", ("/p/%",)
        ),
        idx.run_readonly_sql(
            "SELECT path FROM native_element_fts WHERE cwd LIKE NULL LIMIT 2"
        ),
        idx.run_readonly_sql(
            "SELECT path FROM native_element_fts WHERE lower(cwd) COLLATE nocase LIKE '/proj' LIMIT 2"
        ),
        idx.run_readonly_sql(
            "SELECT path FROM native_element_fts WHERE '/p/%' LIKE path LIMIT 2"
        ),
    ]
    mixed = idx.run_readonly_sql(
        "SELECT path FROM native_element_fts WHERE path LIKE '/p/%' "
        "AND text LIKE '%private_mixed%' ORDER BY rowid LIMIT 2"
    )
    nested = idx.run_readonly_sql(
        "SELECT path FROM native_element_fts WHERE rowid IN "
        "(SELECT rowid FROM native_element_fts WHERE text LIKE '%private_nested%')"
    )
    ok = (
        all(result.get("error_code") == "unsupported_expensive_predicate" for result in results)
        and all(result.get("remediation") == {
            "use_indexed_predicate": True, "use_match": True,
        } for result in results)
        and all(result.get("rows") == [] and result.get("columns") == [] for result in results)
        and elapsed < 0.1 and stream.getvalue() == ""
        and "private_" not in encoded and "private_match_literal" not in encoded
        and all(result.get("error_code") == "unsupported_expensive_predicate"
                for result in metadata_like)
        and all(result.get("remediation") == {"use_indexed_predicate": True}
                for result in metadata_like)
        and mixed.get("error_code") == "unsupported_expensive_predicate"
        and nested.get("error_code") == "unsupported_expensive_predicate"
    )
    print(f"{OK if ok else FAIL} text LIKE rejects privately before DB execution ({elapsed:.4f}s)")
    return ok


def test_result_byte_budget_fails_atomically_at_boundary() -> bool:
    exact = idx.run_readonly_sql("SELECT 'abcd'", max_result_bytes=4)
    over = idx.run_readonly_sql("SELECT 'abcde'", max_result_bytes=4)
    ok = (
        exact.get("rows") == [["abcd"]] and exact.get("error") is None
        and over.get("error_code") == "result_too_large"
        and over.get("rows") == [] and over.get("columns") == []
        and over.get("max_result_bytes") == 4
    )
    print(f"{OK if ok else FAIL} result byte budget is exact and atomic")
    return ok


def test_metadata_count_rewrite_preserves_results_and_rejects_near_misses() -> bool:
    conn = idx._writer_connection()
    cases = [
        ("SELECT count(*) AS n FROM native_element_fts WHERE cwd='/proj'", ()),
        ("SELECT COUNT(*) FROM native_element_fts WHERE role = ? AND cwd = ?", ("user", "/proj")),
        (
            "SELECT count(*) AS matching FROM native_element_fts "
            "WHERE (element_kind='assistant_text') AND path='/p/large.jsonl'",
            (),
        ),
        ("SELECT count(*) AS n FROM native_element_fts WHERE cwd='/PROJ'", ()),
    ]
    parity = True
    routes = []
    for sql, params in cases:
        rewritten = idx._rewrite_metadata_count_sql(sql, params)
        expected = [list(row) for row in conn.execute(sql, params).fetchall()]
        result = idx.run_readonly_sql(sql, params)
        parity = parity and rewritten is not None and result.get("rows") == expected
        routes.append(result.get("execution_route"))
    near_misses = [
        "SELECT count(text) FROM native_element_fts WHERE cwd='/proj'",
        "SELECT count(*) FROM native_element_fts WHERE cwd='/proj' OR role='user'",
        "SELECT count(*) FROM native_element_fts WHERE cwd GLOB '/proj*'",
        "SELECT count(*) FROM native_element_fts WHERE sid='sA'",
        "SELECT count(*) FROM native_element_fts",
    ]
    non_string_cases = [
        ("SELECT count(*) AS n FROM native_element_fts WHERE cwd=?", (1,)),
        ("SELECT count(*) AS n FROM native_element_fts WHERE cwd=?", (1.5,)),
        ("SELECT count(*) AS n FROM native_element_fts WHERE cwd=?", (True,)),
        ("SELECT count(*) AS n FROM native_element_fts WHERE cwd=?", (None,)),
        (
            "SELECT count(*) AS n FROM native_element_fts WHERE cwd=? AND role=?",
            ("/proj", 1),
        ),
    ]
    non_string_parity = True
    for sql, params in non_string_cases:
        expected = [list(row) for row in conn.execute(sql, params).fetchall()]
        result = idx.run_readonly_sql(sql, params)
        non_string_parity = (
            non_string_parity
            and idx._rewrite_metadata_count_sql(sql, params) is None
            and result.get("execution_route") == "direct"
            and result.get("rows") == expected
        )
    ok = (
        parity and all(route == "metadata_count" for route in routes)
        and all(idx._rewrite_metadata_count_sql(sql) is None for sql in near_misses)
        and non_string_parity
    )
    print(f"{OK if ok else FAIL} metadata COUNT rewrite preserves exact results "
          f"and rejects near misses (routes={routes})")
    return ok


def test_metadata_count_rewrite_uses_index_and_bounds_vm_steps() -> bool:
    conn = idx._writer_connection()
    sql = "SELECT count(*) AS n FROM native_element_fts WHERE cwd='/proj'"
    rewritten = idx._rewrite_metadata_count_sql(sql)
    if rewritten is None:
        print(f"{FAIL} metadata COUNT rewrite was not recognized")
        return False
    plan = " ".join(
        str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + rewritten).fetchall()
    )

    def vm_callbacks(statement: str) -> tuple[int, list[tuple]]:
        callbacks = [0]
        def progress() -> int:
            callbacks[0] += 1
            return 0
        conn.set_progress_handler(progress, 10)
        try:
            rows = conn.execute(statement).fetchall()
        finally:
            conn.set_progress_handler(None, 0)
        return callbacks[0], rows

    direct_steps, direct_rows = vm_callbacks(sql)
    indexed_steps, indexed_rows = vm_callbacks(rewritten)
    ok = (
        direct_rows == indexed_rows
        and "native_element_meta_cwd" in plan
        and indexed_steps < direct_steps / 5
        and indexed_steps < 1_000
    )
    print(f"{OK if ok else FAIL} metadata COUNT uses cwd index with bounded VM work "
          f"(direct={direct_steps}, indexed={indexed_steps}, plan={plan!r})")
    return ok


def test_sql_timings_split_sqlite_steps_transform_and_reconcile_overlap() -> bool:
    out = idx.run_readonly_sql(
        "SELECT text, path FROM native_element_fts WHERE path='/p/large.jsonl' "
        "ORDER BY rowid DESC LIMIT 2000",
    )
    timings = out.get("timings", {})
    split = {key: timings.get(key) for key in (
        "cursor_execute_ms", "first_row_ms", "fetch_ms", "post_execute_fetch_ms",
        "sqlite_work_ms",
        "transform_ms", "materialize_ms", "query_concurrency",
        "reconcile_active_start", "reconcile_active_end", "wal_bytes_start", "wal_bytes_end",
    )}
    ok = (
        out.get("error") is None and len(out.get("rows") or []) == 2000
        and all(isinstance(split[key], (int, float)) for key in split)
        and abs(split["post_execute_fetch_ms"] - split["first_row_ms"] - split["fetch_ms"]) <= 0.002
        and abs(
            split["sqlite_work_ms"] - split["cursor_execute_ms"]
            - split["post_execute_fetch_ms"]
        ) <= 0.002
        and split["materialize_ms"] + 0.01 >= split["post_execute_fetch_ms"] + split["transform_ms"]
        and split["query_concurrency"] >= 1
        and split["reconcile_active_start"] in {-1, 0, 1}
        and split["reconcile_active_end"] in {-1, 0, 1}
    )
    print(f"{OK if ok else FAIL} SQL timings separate SQLite stepping, transform, and reconcile overlap "
          f"({split})")
    return ok


def test_expensive_aggregate_is_attributed_to_sqlite_work() -> bool:
    out = idx.run_readonly_sql(
        "SELECT count(*) AS n FROM native_element_fts WHERE sid='sLarge'",
    )
    timings = out.get("timings", {})
    ok = (
        out.get("error") is None and out.get("rows") == [[2000]]
        and out.get("execution_route") == "direct"
        and timings.get("sqlite_work_ms", 0) > timings.get("transform_ms", 0)
        and abs(
            timings.get("sqlite_work_ms", 0)
            - timings.get("cursor_execute_ms", 0)
            - timings.get("post_execute_fetch_ms", 0)
        ) <= 0.002
    )
    print(f"{OK if ok else FAIL} aggregate latency is attributed to total SQLite work "
          f"(timings={timings})")
    return ok


def test_query_activity_counter_survives_normalization_and_open_failures() -> bool:
    with idx._sql_activity_lock:
        baseline = idx._sql_active_queries
    invalid_timeout = idx.run_readonly_sql("SELECT 1", timeout_s="invalid")
    raised = [invalid_timeout.get("error_code") == "invalid_timeout"]
    for kwargs in ({"max_result_bytes": "invalid"},):
        try:
            idx.run_readonly_sql("SELECT 1", **kwargs)
        except ValueError:
            raised.append(True)
        else:
            raised.append(False)
        with idx._sql_activity_lock:
            raised.append(idx._sql_active_queries == baseline)

    old_connect = idx._connect
    def fail_open(*_args, **_kwargs):
        raise idx.sqlite3.OperationalError("injected open failure")
    idx._connect = fail_open
    try:
        open_result = idx.run_readonly_sql("SELECT 1")
    finally:
        idx._connect = old_connect
    with idx._sql_activity_lock:
        final_count = idx._sql_active_queries
    ok = (
        all(raised) and final_count == baseline
        and "injected open failure" in str(open_result.get("error") or "")
    )
    print(f"{OK if ok else FAIL} query activity counter survives normalization/open failures "
          f"(baseline={baseline}, final={final_count}, raised={raised})")
    return ok


def test_match_recency_9303_row_materialization_is_measured() -> bool:
    conn = idx._writer_connection()
    start_rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM native_element_fts").fetchone()[0]
    rows = [
        (f"perf9303 payload {i}", f"/perf/{i}.jsonl", f"perf-{i}", "/perf9303",
         "codex", "assistant_text", "", f"2026-01-01T00:{i % 60:02d}:{i % 60:02d}Z", "assistant")
        for i in range(9303)
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
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", indexed,
    )
    conn.commit()
    sql = (
        "SELECT text AS body, ts_utc FROM native_element_fts "
        "WHERE cwd = ? AND native_element_fts MATCH ? ORDER BY ts_utc DESC, rowid ASC"
    )
    parsed = idx._parse_match_recency_sql(sql)
    out = idx.run_readonly_sql(sql, ("/perf9303", "perf9303"), timeout_s=20.0)
    conn.execute("DELETE FROM native_element_meta WHERE rowid > ?", (start_rowid,))
    conn.execute("DELETE FROM native_element_fts WHERE rowid > ?", (start_rowid,))
    conn.commit()
    ok = (
        parsed is not None and out.get("error") is None and len(out.get("rows") or []) == 9303
        and out.get("elapsed_ms", 99999) < 5000
        and out.get("execution_route") in {"match_metadata", "match_fts"}
        and out.get("timings", {}).get("result_bytes", 0) > 0
    )
    print(f"{OK if ok else FAIL} 9303-row canonical MATCH query stays bounded "
          f"(rows={len(out.get('rows') or [])}, elapsed_ms={out.get('elapsed_ms')})")
    return ok


def test_total_timer_attributes_injected_freshness_delay() -> bool:
    clock = [100.0]
    old_monotonic = idx.time.monotonic
    old_freshness = idx.ensure_fresh_for_read
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    old_level = idx.logger.level
    idx.time.monotonic = lambda: clock[0]
    def delayed_freshness(timeout=idx._FRESH_WAIT_TIMEOUT) -> None:
        clock[0] += 9.303
    idx.ensure_fresh_for_read = delayed_freshness
    idx.logger.addHandler(handler)
    idx.logger.setLevel(logging.WARNING)
    try:
        out = idx.run_readonly_sql("SELECT text FROM native_element_fts LIMIT 1", timeout_s=20)
    finally:
        idx.time.monotonic = old_monotonic
        idx.ensure_fresh_for_read = old_freshness
        idx.logger.setLevel(old_level)
        idx.logger.removeHandler(handler)
    timings = out.get("timings", {})
    log = stream.getvalue()
    ok = (
        out.get("error") is None and timings.get("freshness_ms") == 9303.0
        and timings.get("total_ms") == 9303.0 and '"freshness_ms":9303.0' in log
        and "SELECT text" not in log
    )
    print(f"{OK if ok else FAIL} injected freshness delay is attributed to total ({timings})")
    return ok


def test_freshness_wait_is_bounded_by_remaining_query_budget() -> bool:
    clock = [300.0]
    received: list[float] = []
    old_monotonic = idx.time.monotonic
    old_freshness = idx.ensure_fresh_for_read
    old_connect = idx._connect
    connected = [False]
    idx.time.monotonic = lambda: clock[0]
    def over_budget_freshness(timeout=idx._FRESH_WAIT_TIMEOUT):
        received.append(timeout)
        clock[0] += timeout + 1.0
        return {"covered": True, "usable": False}
    def tracked_connect(*args, **kwargs):
        connected[0] = True
        return old_connect(*args, **kwargs)
    idx.ensure_fresh_for_read = over_budget_freshness
    idx._connect = tracked_connect
    wall_started = time.perf_counter()
    try:
        out = idx.run_readonly_sql("SELECT text FROM native_element_fts LIMIT 1", timeout_s=0.1)
    finally:
        wall_elapsed = time.perf_counter() - wall_started
        idx.time.monotonic = old_monotonic
        idx.ensure_fresh_for_read = old_freshness
        idx._connect = old_connect
    ok = (
        len(received) == 1 and 0 < received[0] <= 0.1
        and out.get("error", "").startswith("TimeoutError:")
        and connected[0] is False and wall_elapsed < 0.1
    )
    print(f"{OK if ok else FAIL} freshness wait respects remaining query budget "
          f"(received={received}, wall={wall_elapsed:.4f}, error={out.get('error')})")
    return ok


def test_materialization_enforces_absolute_deadline_without_partial_content() -> bool:
    calls = [0]
    old_monotonic = idx.time.monotonic
    old_freshness = idx.ensure_fresh_for_read
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    old_level = idx.logger.level
    def clock() -> float:
        calls[0] += 1
        return 401.0 if calls[0] >= 26 else 400.0
    idx.time.monotonic = clock
    idx.ensure_fresh_for_read = lambda timeout=idx._FRESH_WAIT_TIMEOUT: {
        "schema_ok": True, "covered": True, "usable": True,
    }
    idx.logger.addHandler(handler)
    idx.logger.setLevel(logging.WARNING)
    try:
        out = idx.run_readonly_sql(
            "SELECT text, path, sid, cwd FROM native_element_fts", timeout_s=0.1,
        )
    finally:
        idx.time.monotonic = old_monotonic
        idx.ensure_fresh_for_read = old_freshness
        idx.logger.setLevel(old_level)
        idx.logger.removeHandler(handler)
    log = stream.getvalue()
    ok = (
        out.get("error", "").startswith("TimeoutError:") and out.get("rows") == []
        and '"materialize_ms":' in log and '"total_ms":1000.0' in log
        and "SELECT text" not in log
    )
    print(f"{OK if ok else FAIL} materialization deadline fails closed "
          f"(calls={calls[0]}, error={out.get('error')})")
    return ok


def test_huge_text_is_chunk_deadline_bounded_and_blob_uses_length() -> bool:
    old_monotonic = idx.time.monotonic
    old_freshness = idx.ensure_fresh_for_read
    idx.ensure_fresh_for_read = lambda timeout=idx._FRESH_WAIT_TIMEOUT: {
        "schema_ok": True, "covered": True, "usable": True,
    }
    text_calls = [0]
    def text_clock() -> float:
        text_calls[0] += 1
        return 501.0 if text_calls[0] >= 30 else 500.0
    idx.time.monotonic = text_clock
    try:
        text_out = idx.run_readonly_sql("SELECT ? AS payload", ("x" * (2 * 1024 * 1024),), timeout_s=0.1)
        blob_calls = [0]
        def blob_clock() -> float:
            blob_calls[0] += 1
            return 601.0 if blob_calls[0] >= 30 else 600.0
        idx.time.monotonic = blob_clock
        blob = b"x" * (2 * 1024 * 1024)
        blob_out = idx.run_readonly_sql("SELECT ? AS payload", (blob,), timeout_s=0.1)
    finally:
        idx.time.monotonic = old_monotonic
        idx.ensure_fresh_for_read = old_freshness
    ok = (
        text_out.get("error", "").startswith("TimeoutError:") and text_out.get("rows") == []
        and blob_out.get("error") is None and blob_out.get("rows") == [[blob]]
        and blob_out.get("timings", {}).get("result_bytes") == len(blob)
        and blob_calls[0] < 30
    )
    print(f"{OK if ok else FAIL} huge TEXT is chunk-bounded and BLOB accounting is bounded "
          f"(text_calls={text_calls[0]}, blob_calls={blob_calls[0]}, text_error={text_out.get('error')})")
    return ok


def test_total_timer_attributes_probe_delay_and_enforces_budget() -> bool:
    clock = [200.0]
    old_monotonic = idx.time.monotonic
    old_choose = idx._choose_match_recency_sql
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    old_level = idx.logger.level
    idx.time.monotonic = lambda: clock[0]
    def delayed_probe(conn, sql, params):
        clock[0] += 9.303
        return old_choose(conn, sql, params)
    idx._choose_match_recency_sql = delayed_probe
    idx.logger.addHandler(handler)
    idx.logger.setLevel(logging.WARNING)
    sql = (
        "SELECT text FROM native_element_fts WHERE native_element_fts MATCH 'offline' "
        "AND cwd = '/proj' ORDER BY ts_utc DESC"
    )
    try:
        out = idx.run_readonly_sql(sql, timeout_s=5)
    finally:
        idx.time.monotonic = old_monotonic
        idx._choose_match_recency_sql = old_choose
        idx.logger.setLevel(old_level)
        idx.logger.removeHandler(handler)
    log = stream.getvalue()
    ok = (
        out.get("error", "").startswith(("TimeoutError:", "OperationalError: interrupted"))
        and '"plan_probe_ms":9303.0' in log and '"total_ms":9303.0' in log
        and "offline" not in log and "/proj" not in log
    )
    print(f"{OK if ok else FAIL} injected probe delay exhausts total budget ({out.get('error')})")
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
        test_match_recency_rejection_is_structural_and_private,
        test_dangerous_match_shape_rejects_before_freshness_or_open,
        test_match_allowlist_validates_parameter_types_counts_and_bounds,
        test_match_recency_alias_and_rowid_order_preserve_exact_parity,
        test_match_recency_text_like_is_rejected_before_execution,
        test_result_byte_budget_fails_atomically_at_boundary,
        test_metadata_count_rewrite_preserves_results_and_rejects_near_misses,
        test_metadata_count_rewrite_uses_index_and_bounds_vm_steps,
        test_sql_timings_split_sqlite_steps_transform_and_reconcile_overlap,
        test_expensive_aggregate_is_attributed_to_sqlite_work,
        test_query_activity_counter_survives_normalization_and_open_failures,
        test_match_recency_9303_row_materialization_is_measured,
        test_total_timer_attributes_injected_freshness_delay,
        test_freshness_wait_is_bounded_by_remaining_query_budget,
        test_materialization_enforces_absolute_deadline_without_partial_content,
        test_huge_text_is_chunk_deadline_bounded_and_blob_uses_length,
        test_total_timer_attributes_probe_delay_and_enforces_budget,
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
        test_production_path_element_window_rewrites_with_parity_and_bounded_work,
        test_sid_element_window_without_order_is_indexed,
        test_path_window_endpoint_validation_covers_mixed_bindings_and_int64,
        test_path_element_near_miss_rejects_before_open,
        test_interrupt_watchdog_bounds_execute_wall_time,
        test_watchdog_repeated_near_deadline_has_no_thread_or_close_race,
        test_nonfinite_timeout_rejects_before_open_or_timer,
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

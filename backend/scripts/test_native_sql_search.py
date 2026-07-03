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
import logging
import os
import shutil
import sys

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
    rows = [
        ("offline backlog keeps dropping actions", "/p/a.jsonl", "sA", "/proj", "claude",
         "user_prompt", "", "2024-01-01T00:00:00"),
        ("acknowledged the offline backlog", "/p/a.jsonl", "sA", "/proj", "claude",
         "assistant_text", "", "2024-01-01T00:00:01"),
        ("offline sync note", "/p/b.jsonl", "sB", "/proj", "codex",
         "user_prompt", "", "2024-01-02T00:00:00"),
        ("x" * 5000 + " offline", "/p/c.jsonl", "sC", "/proj", "gemini",
         "assistant_text", "", "2024-01-03T00:00:00"),
    ]
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text, path, sid, cwd, tag, element_kind, tool_name, ts) VALUES (?,?,?,?,?,?,?,?)",
        rows,
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
        "SELECT text FROM native_element_fts WHERE sid = 'sC'"
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
            "WHERE native_element_fts MATCH ? AND cwd=? AND element_kind=? "
            "ORDER BY bm25(native_element_fts) LIMIT 2",
            ("offline", "/proj", "user_prompt"),
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
        and '"filters":["cwd","element_kind"]' in log
        and "offline" not in log
        and "/proj" not in log
        and "user_prompt" not in log
    )
    print(f"{OK if ok else FAIL} slow SQL shape measured without raw SQL leak "
          f"(elapsed={out.get('elapsed_ms')}, log={log.strip()!r})")
    return ok


def test_sql_shape_detects_filters_and_ts_ordering() -> bool:
    compact = idx._sql_shape(
        "SELECT text FROM native_element_fts "
        "WHERE sid='sA' AND e.cwd=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 5"
    )
    nested = idx._sql_shape(
        "SELECT * FROM (SELECT text FROM native_element_fts ORDER BY rank) t "
        "WHERE ts > '2026-01-01T00:00:00Z'"
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
        compact["filters"] == ["sid", "cwd", "ts"]
        and compact["orders_by_ts"] is True
        and nested["orders_by_ts"] is False
        and utc_order["orders_by_ts"] is True
        and literal_noise["orders_by_ts"] is False
        and literal_a["fingerprint"] == literal_b["fingerprint"]
    )
    print(f"{OK if ok else FAIL} SQL shape detects compact filters + ts ordering "
          f"(compact={compact}, nested={nested})")
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

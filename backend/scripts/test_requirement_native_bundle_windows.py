#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-req-native-windows-")

import native_transcript_index as idx  # noqa: E402
import requirement_context  # noqa: E402


def _seed() -> None:
    conn = idx._writer_connection()
    conn.execute("DELETE FROM native_element_fts")
    conn.execute("DELETE FROM native_element_path")
    conn.execute("DELETE FROM native_element_meta")
    rows = [
        ("setup context", "/p/native.jsonl", "s1", "/repo", "claude", "assistant_text", "", "2026-01-01T00:00:00.000000Z", "assistant", "e0", 0),
        ("first requirements hit", "/p/native.jsonl", "s1", "/repo", "claude", "user_prompt", "", "2026-01-01T00:00:01.000000Z", "user", "e1", 1),
        ("shared requirement confirmation", "/p/native.jsonl", "s1", "/repo", "claude", "assistant_text", "", "2026-01-01T00:00:02.000000Z", "assistant", "e2", 2),
        ("second requirements hit", "/p/native.jsonl", "s1", "/repo", "claude", "user_prompt", "", "2026-01-01T00:00:03.000000Z", "user", "e3", 3),
        ("tail context", "/p/native.jsonl", "s1", "/repo", "claude", "assistant_text", "", "2026-01-01T00:00:04.000000Z", "assistant", "e4", 4),
    ]
    conn.executemany(
        "INSERT INTO native_element_fts"
        "(text, path, sid, cwd, tag, element_kind, tool_name, ts_utc, role, element_id, element_index) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
        "INSERT INTO native_corpus_state(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(idx._SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT INTO native_corpus_state(key, value) VALUES ('covered', '1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    )
    conn.execute(
        "INSERT INTO native_corpus_state(key, value) VALUES ('last_walk_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(idx.time.time()),),
    )
    conn.commit()


def main() -> int:
    try:
        _seed()
        rows = requirement_context._native_transcript_sql_window_rows(
            idx,
            tokens=["requirements"],
            cwds=("/repo",),
            limit=2,
        )
        records = requirement_context._native_bundle_records_from_rows(rows)
        if len(records) != 1:
            raise AssertionError(f"expected one merged bundle, got {len(records)}")
        text = records[0]["text"]
        if text.count("shared requirement confirmation") != 1:
            raise AssertionError("overlapping SQL windows duplicated shared transcript row")
        if "first requirements hit" not in text or "second requirements hit" not in text:
            raise AssertionError("merged bundle lost one of the overlapping hits")
        if records[0]["native_hit_index"] not in {1, 3}:
            raise AssertionError(f"unexpected native_hit_index {records[0]['native_hit_index']!r}")
        print("PASS requirement native bundle windows merge")
        return 0
    finally:
        idx.shutdown()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

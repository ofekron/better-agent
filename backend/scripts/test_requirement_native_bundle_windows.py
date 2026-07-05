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


def _row(path: str, element_index: int, text: str) -> dict[str, object]:
    return {
        "hit_index": element_index,
        "text": text,
        "path": path,
        "sid": "s1",
        "cwd": "/repo",
        "tag": "claude",
        "element_kind": "user_prompt",
        "tool_name": "",
        "ts_utc": f"2026-01-01T00:00:{element_index:02d}.000000Z",
        "role": "user",
        "element_id": f"e{element_index}",
        "element_index": element_index,
    }


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

        short_repeat = "continue"
        records = requirement_context._native_bundle_records_from_rows([
            _row("/p/short-first.jsonl", 1, short_repeat),
            _row("/p/short-second.jsonl", 1, short_repeat),
        ])
        if "<repeated_text_ref " in records[1]["text"]:
            raise AssertionError("short repeated user prompt should stay expanded")
        if short_repeat not in records[1]["text"]:
            raise AssertionError("short repeated user prompt text was lost")

        repeated_text = " ".join(["same injected harness text with a durable requirement"] * 20)
        records = requirement_context._native_bundle_records_from_rows([
            _row("/p/first.jsonl", 1, repeated_text),
            _row("/p/second.jsonl", 1, repeated_text),
        ])
        if len(records) != 2:
            raise AssertionError(f"expected two bundles for repeated text, got {len(records)}")
        if repeated_text not in records[0]["text"]:
            raise AssertionError("first repeated text occurrence should stay expanded")
        if "<repeated_text_ref " not in records[1]["text"]:
            raise AssertionError("second exact repeated text occurrence was not collapsed")
        if repeated_text in records[1]["text"]:
            raise AssertionError("second exact repeated text occurrence still repeats full text")

        shared_prefix = " ".join(f"harness{i}" for i in range(900))
        first_text = f"{shared_prefix} first tail"
        second_tail = "\n    second tail has the actual new requirement\n    keep indentation"
        second_text = f"{shared_prefix} {second_tail}"
        records = requirement_context._native_bundle_records_from_rows([
            _row("/p/prefix-first.jsonl", 1, first_text),
            _row("/p/prefix-second.jsonl", 1, second_text),
        ])
        if "<repeated_prefix_ref " not in records[1]["text"]:
            raise AssertionError("second shared-prefix occurrence was not collapsed")
        if second_tail not in records[1]["text"]:
            raise AssertionError("shared-prefix collapse lost the unique tail")
        if shared_prefix in records[1]["text"]:
            raise AssertionError("shared-prefix collapse still repeats the full prefix")
        print("PASS requirement native bundle windows merge")
        return 0
    finally:
        idx.shutdown()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

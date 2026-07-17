#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOME = tempfile.mkdtemp(prefix="ba-requirement-context-fts-schema-")

import paths  # noqa: E402

paths.engage_test_home(HOME)

import requirement_context  # noqa: E402


def _make_stale_db(db_path: Path, table_name: str) -> None:
    """Simulate an on-disk FTS index built before cwds_json existed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(f"""
            CREATE TABLE {table_name}_state(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE {table_name} USING fts5(
                search_text,
                record_json UNINDEXED,
                sort_key UNINDEXED,
                tokenize='unicode61'
            );
            INSERT INTO {table_name}(search_text, record_json, sort_key)
            VALUES ('stale row', '{{}}', '0');
            INSERT INTO {table_name}_state(key, value) VALUES ('record_count', '1');
        """)
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ba-fts-schema-source-"))
    source_path = tmp / "units.jsonl"
    source_path.write_text("{}\n", encoding="utf-8")
    db_path = tmp / "fts.sqlite3"
    table_name = "requirement_units_fts"

    _make_stale_db(db_path, table_name)

    records = [{"id": "u1", "cwd": "/repo"}]
    result = requirement_context._ensure_fts_index(
        db_path=db_path,
        table_name=table_name,
        source_path=source_path,
        records=records,
        search_text_fn=lambda r: r["id"],
        cwds_fn=lambda r: [r["cwd"]] if r.get("cwd") else [],
        sort_key_fn=lambda r: r["id"],
    )
    assert result["ready"] is True, result

    matches, err = requirement_context._run_fts_query(
        db_path=db_path,
        table_name=table_name,
        match_expr='"u1"',
        allowed_cwds=set(),
        id_field="id",
    )
    assert err is None, f"query failed against rebuilt index: {err}"
    assert len(matches) == 1 and matches[0]["id"] == "u1", matches

    print("OK: stale FTS schema self-heals and query succeeds")


def _test_concurrent_rebuild_is_serialized() -> None:
    """Two threads racing _ensure_fts_index against the same stale schema
    must not observe a transient 'no such table' from an unlocked
    DROP/CREATE interleaving (regression for the race the fix's DROP TABLE
    step introduced without _FTS_INDEX_LOCK)."""
    tmp = Path(tempfile.mkdtemp(prefix="ba-fts-schema-race-"))
    source_path = tmp / "units.jsonl"
    source_path.write_text("{}\n", encoding="utf-8")
    db_path = tmp / "fts.sqlite3"
    table_name = "requirement_units_fts"
    _make_stale_db(db_path, table_name)

    records = [{"id": "u1", "cwd": "/repo"}]
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        try:
            result = requirement_context._ensure_fts_index(
                db_path=db_path,
                table_name=table_name,
                source_path=source_path,
                records=records,
                search_text_fn=lambda r: r["id"],
                cwds_fn=lambda r: [r["cwd"]] if r.get("cwd") else [],
                sort_key_fn=lambda r: r["id"],
            )
            assert result["ready"] is True, result
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent _ensure_fts_index raised: {errors}"

    matches, err = requirement_context._run_fts_query(
        db_path=db_path,
        table_name=table_name,
        match_expr='"u1"',
        allowed_cwds=set(),
        id_field="id",
    )
    assert err is None, f"query failed after concurrent rebuild: {err}"
    assert len(matches) == 1 and matches[0]["id"] == "u1", matches

    print("OK: concurrent stale-schema rebuilds are serialized, no races")


if __name__ == "__main__":
    main()
    _test_concurrent_rebuild_is_serialized()

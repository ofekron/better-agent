from __future__ import annotations

import atexit
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("ba-test-nti-dedup-")
atexit.register(lambda: shutil.rmtree(_TMP_HOME, ignore_errors=True))

import native_transcript_index as idx


def _seed_rows(conn, count: int, *, sid: str = "sid1", text: str = "hello world") -> None:
    for index in range(count):
        row = (
            f"{text} {index}", f"/tmp/t-{index}.jsonl", sid, "/tmp", "claude",
            "assistant_text", "", f"2026-07-17T00:00:{index % 60:02d}Z", "assistant",
            f"el-{index}", index, f"h{index}", f"n{index}", "", "", "",
            len(text) + 2, len(text) + 2,
        )
        cursor = conn.execute(
            f"INSERT INTO native_element_fts({', '.join(idx._FTS_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in idx._FTS_COLUMNS)})",
            row,
        )
        rowid = cursor.lastrowid
        conn.execute(
            "INSERT INTO native_element_path(rowid, path) VALUES (?, ?)",
            (rowid, row[1]),
        )
        conn.execute(
            f"INSERT INTO native_element_meta(rowid, {', '.join(idx._META_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in range(len(idx._META_COLUMNS) + 1))})",
            (rowid, *row[1:]),
        )
    conn.commit()


def test_single_text_store() -> None:
    # The corpus text used to be stored twice: once in the FTS5 content table
    # and once in a shadow native_element_text table (observed: 7GB + 6GB of a
    # 21.6GB index). The FTS content store is the single owner now.
    conn = idx._writer_connection()
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "native_element_text" not in tables, "duplicate text store must not exist"
    assert "native_element_fts" in tables

    _seed_rows(conn, 3, text="alpha beta gamma")
    result = idx._run_readonly_sql_local(
        "SELECT m.rowid, e.text FROM native_element_meta m "
        "CROSS JOIN native_element_fts e ON e.rowid = m.rowid ORDER BY m.rowid",
        timeout_s=10.0,
    )
    assert not result.get("error"), result
    assert len(result["rows"]) == 3
    assert result["rows"][0][1].startswith("alpha beta gamma")


def test_rewriters_never_reference_dropped_text_table() -> None:
    recency = idx._rewrite_metadata_recency_sql(
        "SELECT text FROM native_element_fts WHERE sid = 'abc' ORDER BY ts_utc ASC LIMIT 5"
    )
    assert recency is not None
    assert "native_element_text" not in recency
    assert "native_element_fts e" in recency

    substr_recency = idx._rewrite_metadata_recency_sql(
        "SELECT substr(text, 1, 64) AS t FROM native_element_fts "
        "WHERE sid = 'abc' ORDER BY ts_utc ASC LIMIT 5"
    )
    assert substr_recency is not None
    assert "native_element_text" not in substr_recency
    assert "substr(e.text" in substr_recency


def test_vacuum_reclaims_free_pages() -> None:
    conn = idx._writer_connection()
    _seed_rows(conn, 400, text="x" * 4096)
    conn.execute("DELETE FROM native_element_meta")
    conn.execute("DELETE FROM native_element_fts")
    conn.execute("DELETE FROM native_element_path")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    assert freelist > 0, "deletes must leave free pages for this test"
    size_before = os.stat(idx._db_path()).st_size

    original_ratio = idx._VACUUM_FREELIST_RATIO
    original_min = idx._VACUUM_FREELIST_MIN_BYTES
    idx._VACUUM_FREELIST_RATIO = 0.01
    idx._VACUUM_FREELIST_MIN_BYTES = 1
    try:
        assert idx._maybe_vacuum(conn) is True
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size_after = os.stat(idx._db_path()).st_size
        assert size_after < size_before, (size_before, size_after)
        assert idx._maybe_vacuum(conn) is False, "no-op once free pages are reclaimed"
    finally:
        idx._VACUUM_FREELIST_RATIO = original_ratio
        idx._VACUUM_FREELIST_MIN_BYTES = original_min


def main_test() -> None:
    test_single_text_store()
    test_rewriters_never_reference_dropped_text_table()
    test_vacuum_reclaims_free_pages()
    print("PASS: native index single text store + vacuum maintenance")


if __name__ == "__main__":
    main_test()

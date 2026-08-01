from __future__ import annotations

import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import native_analytics_snapshot as nas  # noqa: E402
import native_transcript_index as nti  # noqa: E402


def _populate_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER, label TEXT)")
    conn.executemany(
        "INSERT INTO t VALUES (?, ?)",
        [(1, "a"), (2, "bb"), (3, None)],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Wire native_analytics_snapshot to a real tmp SQLite file.

    The snapshot's own logic (state transitions, query validation, byte
    budget, deadline) is what we exercise; native_transcript_index's connect
    pragmas and loop guard are stubbed so the test does not depend on them.
    """
    path = tmp_path / "snap.db"
    _populate_db(path)
    monkeypatch.setattr(nti, "_require_off_loop", lambda op: None)
    monkeypatch.setattr(nti, "_db_path", lambda: path)
    monkeypatch.setattr(
        nti,
        "_connect",
        lambda path, *, readonly: sqlite3.connect(str(path), check_same_thread=False),
    )
    return path


def _usable(value: bool, monkeypatch):
    state = {"usable": value}
    refresh: list[int] = []
    monkeypatch.setattr(nti, "quick_state", lambda: state)
    monkeypatch.setattr(nti, "request_refresh", lambda: refresh.append(1))
    return refresh


# --- __enter__ state transitions --------------------------------------------

def test_enter_path_missing(tmp_path, monkeypatch):
    # quick_state usable True but db file absent -> stays unavailable, no connect.
    _usable(True, monkeypatch)
    monkeypatch.setattr(nti, "_db_path", lambda: tmp_path / "absent.db")

    with nas.NativeAnalyticsSnapshot() as snap:
        assert snap.status == {"state": "unavailable"}
        assert snap.query("SELECT 1") == []  # conn is None -> []


def test_enter_usable_is_current_no_refresh(db_path, monkeypatch):
    refresh = _usable(True, monkeypatch)

    with nas.NativeAnalyticsSnapshot() as snap:
        assert snap.status == {"state": "current", "refresh_requested": False}
        rows = snap.query("SELECT id, label FROM t ORDER BY id")

    assert rows == [
        {"id": 1, "label": "a"},
        {"id": 2, "label": "bb"},
        {"id": 3, "label": None},  # also covers the value-is-None branch
    ]
    assert refresh == []


def test_enter_stale_requests_refresh(db_path, monkeypatch):
    refresh = _usable(False, monkeypatch)

    with nas.NativeAnalyticsSnapshot() as snap:
        assert snap.status == {"state": "stale", "refresh_requested": True}

    assert refresh == [1]


def test_enter_connect_raises_marks_unavailable(db_path, monkeypatch):
    # _connect itself raises -> _conn never assigned -> except skips close.
    _usable(True, monkeypatch)

    def boom(path, *, readonly):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(nti, "_connect", boom)

    with nas.NativeAnalyticsSnapshot() as snap:
        assert snap.status == {"state": "unavailable"}
        assert snap.query("SELECT 1") == []


def test_enter_begin_failure_closes_connection(db_path, monkeypatch):
    # _connect succeeds but execute("BEGIN") raises on a closed handle ->
    # except must close and null the non-None connection.
    _usable(True, monkeypatch)

    def returns_closed(path, *, readonly):
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.close()
        return conn

    monkeypatch.setattr(nti, "_connect", returns_closed)

    with nas.NativeAnalyticsSnapshot() as snap:
        assert snap.status == {"state": "unavailable"}
        assert snap._conn is None
        assert snap.query("SELECT 1") == []


# --- query validation --------------------------------------------------------

def test_query_rejects_non_select(db_path, monkeypatch):
    _usable(True, monkeypatch)
    with nas.NativeAnalyticsSnapshot() as snap:
        with pytest.raises(ValueError, match="read-only"):
            snap.query("DELETE FROM t")


def test_query_rejects_multistatement(db_path, monkeypatch):
    _usable(True, monkeypatch)
    with nas.NativeAnalyticsSnapshot() as snap:
        with pytest.raises(ValueError, match="read-only"):
            snap.query("SELECT 1; SELECT 2")


def test_query_allows_trailing_semicolon(db_path, monkeypatch):
    _usable(True, monkeypatch)
    with nas.NativeAnalyticsSnapshot() as snap:
        assert snap.query("SELECT 1 AS n;") == [{"n": 1}]


def test_query_allows_with_cte(db_path, monkeypatch):
    _usable(True, monkeypatch)
    with nas.NativeAnalyticsSnapshot() as snap:
        assert snap.query("WITH x AS (SELECT 1 AS n) SELECT n FROM x") == [{"n": 1}]


# --- byte budget + deadline DoS guards --------------------------------------

def test_query_byte_budget_overflow_closes_connection(db_path, monkeypatch):
    _usable(True, monkeypatch)
    with nas.NativeAnalyticsSnapshot() as snap:
        snap._conn.execute("CREATE TABLE big (payload TEXT)")
        snap._conn.execute(
            "INSERT INTO big VALUES (?)",
            ("x" * (nas._MAX_RESULT_BYTES + 10),),
        )
        snap._conn.commit()

        with pytest.raises(OverflowError, match="byte budget"):
            snap.query("SELECT payload FROM big")

        assert snap._conn is None  # except path closed the connection


def test_query_deadline_interrupts_scan(db_path, monkeypatch):
    _usable(True, monkeypatch)
    # Pre-populate via a separate connection: the snapshot's progress handler
    # is armed in __enter__, so it would abort any in-snapshot INSERT too.
    seed = sqlite3.connect(str(db_path))
    seed.execute("CREATE TABLE bulk (id INTEGER)")
    seed.executemany("INSERT INTO bulk VALUES (?)", [(i,) for i in range(50_000)])
    seed.commit()
    seed.close()

    # Negative timeout -> deadline already in the past, so the progress
    # handler returns 1 and aborts the scan.
    with nas.NativeAnalyticsSnapshot(timeout_s=-1.0) as snap:
        with pytest.raises(sqlite3.OperationalError):
            snap.query("SELECT id FROM bulk")

        assert snap._conn is None  # except path closed the connection


# --- __exit__ ----------------------------------------------------------------

def test_exit_is_idempotent_when_conn_already_none(db_path, monkeypatch):
    _usable(True, monkeypatch)
    snap = nas.NativeAnalyticsSnapshot()
    snap._conn = None  # simulate the no-connection case
    snap.__exit__(None, None, None)  # must not raise
    assert snap._conn is None

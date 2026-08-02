"""Hermetic unit tests for the cache I/O defensive branches in ``runs_dir``.

Covers the fail-closed read guards in ``_load_run_state_cache_for_sid`` and the
failed-write tmp cleanup in ``_write_run_state_cache`` that the happy-path and
signature/corruption tests leave out:

  * raw ``OSError`` during the sqlite open/query (distinct from
    ``sqlite3.DatabaseError``) -> fail-closed ``None`` (line 409-410).
  * a cache row whose ``state_path`` is a BLOB, not text -> reject (line 414).
    NOTE: SQLite TEXT-affinity coerces an inserted INTEGER to text, so an int
    value comes back as ``str`` and never reaches this guard — a BLOB is the
    only value that survives as a non-string. The sibling
    ``test_load_cache_for_sid_rejects_non_string_state_path`` inserts an int and
    actually exercises the ledger-shape reject (416), not this guard.
  * a ledger-shaped path string that nonetheless RESOLVES outside the root
    (a symlinked run-dir escaping the root) -> filtered out, loop continues
    (branch 418->412). Proves the resolve-based containment check is not
    duped by a well-shaped string.
  * the failed-write tmp cleanup itself raising ``OSError`` (the tmp file was
    never created because sqlite could not open it) -> swallowed (486-487).

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir via
``_test_home.isolate``; nothing touches a real home. Module-level caches are
reset between tests so order never matters. Every assertion is fail-on-bug.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_cache_io_defensive_unit.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-cache-io-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
from runs_dir import (  # noqa: E402
    _load_run_state_cache_for_sid,
    _run_state_ledger_signature,
    _sqlite_connect,
    _write_run_state_cache,
    ledger_state_files_for_sid,
    run_state_ledger_cache_path,
    run_state_ledger_path,
)


@pytest.fixture(autouse=True)
def _reset_module_caches():
    runs_dir._RUN_STATE_LEDGER_SEEN.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT.clear()
    yield
    runs_dir._RUN_STATE_LEDGER_SEEN.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT.clear()


@pytest.fixture
def fresh_root():
    path = Path(tempfile.mkdtemp(prefix="bc-rd-cache-io-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def _ledger(root: Path) -> Path:
    return run_state_ledger_path(root)


def _touch_ledger(root: Path) -> None:
    ledger = _ledger(root)
    if not ledger.exists():
        _write(ledger, "")


def _sig(root: Path):
    return _run_state_ledger_signature(_ledger(root))


def _empty_cache(root: Path):
    """Materialize an empty ledger cache whose meta matches the ledger stat,
    and return the signature a reader must present for it to validate."""
    _touch_ledger(root)
    sig = _sig(root)
    _write_run_state_cache(root, sig, {})
    return sig


# --------------------------------------------------------------------------- #
# _load_run_state_cache_for_sid: raw OSError during sqlite open/query
# --------------------------------------------------------------------------- #


def test_load_cache_for_sid_sqlite_connect_oserror_returns_none(fresh_root):
    """Lines 409-410: a raw ``OSError`` from the sqlite open/query path (disk
    I/O fault mid-operation — distinct from ``sqlite3.DatabaseError``) is
    fail-closed to ``None``. Proven by stubbing ``_sqlite_connect`` to raise
    ``OSError``; removing the ``except OSError`` handler would let it propagate."""
    _touch_ledger(fresh_root)
    orig = runs_dir._sqlite_connect

    def boom(_path):
        raise OSError("disk i/o fault")

    runs_dir._sqlite_connect = boom
    try:
        result = _load_run_state_cache_for_sid(fresh_root, _sig(fresh_root), "s1", fresh_root.resolve())
    finally:
        runs_dir._sqlite_connect = orig
    assert result is None


# --------------------------------------------------------------------------- #
# _load_run_state_cache_for_sid: BLOB (non-string) state_path row
# --------------------------------------------------------------------------- #


def test_load_cache_for_sid_rejects_blob_state_path(fresh_root):
    """Line 414: a cache row whose ``state_path`` is a BLOB (not text) is
    rejected -> ``None``. SQLite TEXT-affinity coerces INTEGER/REAL inserts to
    text, so only a BLOB survives as a non-string and reaches this guard.
    Removing the guard makes the next call (``str.startswith`` on bytes) raise
    ``TypeError``."""
    sig = _empty_cache(fresh_root)
    blob_path = str(fresh_root / "run-1" / "state.json")
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        conn.execute(
            "INSERT INTO entries (sid, state_path, written_at) VALUES (?, ?, ?)",
            ("s1", sqlite3.Binary(blob_path.encode()), 1.0),
        )
        conn.commit()
    assert _load_run_state_cache_for_sid(fresh_root, sig, "s1", fresh_root.resolve()) is None


# --------------------------------------------------------------------------- #
# _load_run_state_cache_for_sid: ledger-shaped path resolving outside root
# --------------------------------------------------------------------------- #


def test_load_cache_for_sid_filters_path_resolving_outside_root(fresh_root):
    """Branch 418->412: a ``state_path`` whose STRING has ledger shape (under
    ``root.absolute()``) but whose RESOLUTION escapes the root (a symlinked
    run-dir pointing outside) is filtered out and the loop continues — proving
    the resolve-based containment check is not duped by a well-shaped string.
    A missing guard would append the escaping path; the assertion demands ``[]``."""
    outside = Path(tempfile.mkdtemp(prefix="bc-rd-cache-io-out-"))
    try:
        (outside / "state.json").write_text("{}", encoding="utf-8")
        os.symlink(outside, fresh_root / "run-1")  # root/run-1 -> outside
        escaping = str(fresh_root / "run-1" / "state.json")
        sig = _empty_cache(fresh_root)
        with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
            conn.execute(
                "INSERT INTO entries (sid, state_path, written_at) VALUES (?, ?, ?)",
                ("s1", escaping, 1.0),
            )
            conn.commit()
        assert _load_run_state_cache_for_sid(fresh_root, sig, "s1", fresh_root.resolve()) == []
    finally:
        shutil.rmtree(outside, ignore_errors=True)


# --------------------------------------------------------------------------- #
# _write_run_state_cache: failed-write tmp cleanup itself raises OSError
# --------------------------------------------------------------------------- #


def test_write_cache_tmp_unlink_oserror_swallowed(fresh_root):
    """Lines 486-487: when the write fails before the tmp file is created (here
    the root does not exist, so ``_sqlite_connect`` cannot create the tmp db),
    the outer-except cleanup calls ``tmp_path.unlink()`` on a non-existent path
    -> ``FileNotFoundError`` (an ``OSError``) which must be swallowed rather than
    mask the original failure. Removing the inner ``except OSError`` lets it
    escape the function."""
    ghost_root = fresh_root / "does-not-exist"  # parent missing -> sqlite open fails
    # Must not raise; the tmp-unlink OSError is swallowed inside the handler.
    _write_run_state_cache(
        ghost_root,
        (1, 2, 3, 4, 5),
        {"s1": [(1.0, str(fresh_root / "run-1" / "state.json"))]},
    )
    assert not run_state_ledger_cache_path(ghost_root).exists()


# --------------------------------------------------------------------------- #
# Integration sanity: the BLOB / escaping-path reads compose correctly inside
# the public ledger_state_files_for_sid (which falls through to a scan when the
# cache read returns None), so the defensive guards do not corrupt lookup.
# --------------------------------------------------------------------------- #


def test_ledger_lookup_skips_blob_cache_row_and_scans(fresh_root):
    """A BLOB cache row makes ``_load_run_state_cache_for_sid`` return None, so
    ``ledger_state_files_for_sid`` falls through to a real ledger scan and still
    returns the on-disk state path — the defensive reject degrades gracefully."""
    sp = str(_write(fresh_root / "run-1" / "state.json", json.dumps({"session_id": "s1"})))
    _write(fresh_root / "run-1" / "events.jsonl", "")
    _write(_ledger(fresh_root), json.dumps({"session_id": "s1", "state_path": sp, "written_at": 1.0}) + "\n")
    sig = _sig(fresh_root)
    _write_run_state_cache(fresh_root, sig, {})
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        conn.execute(
            "INSERT INTO entries (sid, state_path, written_at) VALUES (?, ?, ?)",
            ("s1", sqlite3.Binary(sp.encode()), 1.0),
        )
        conn.commit()
    assert ledger_state_files_for_sid(fresh_root, "s1") == [Path(sp)]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

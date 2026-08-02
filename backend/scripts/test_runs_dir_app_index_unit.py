"""Hermetic unit tests for the app-session index defensive branches in ``runs_dir``.

Covers the fail-closed guards in ``run_dirs_by_app_session`` and
``_load_run_state_app_index`` that the happy-path app-index tests leave out:

  * ``run_dirs_by_app_session`` with no ledger -> ``_run_state_ledger_signature``
    returns ``None`` -> ``{}`` (line 657).
  * ``run_dirs_by_app_session`` when ``root.resolve()`` raises a raw ``OSError``
    -> ``{}`` (lines 660-661). On POSIX ``Path.resolve(strict=False)`` (the
    default since 3.6) does not raise for missing components or symlink loops,
    so this raw OS fault is not reproducible without fault injection. We trigger
    it with a ``Path`` subclass whose only override is ``resolve`` -> raise
    (scoped, no global monkeypatch); the backfill/signature code run before the
    resolve do not call ``resolve``, so the fault lands exactly on line 659.
  * ``_load_run_state_app_index`` with a cache row whose ``state_path`` is a
    BLOB (not text) -> ``None`` (line 688). SQLite TEXT-affinity coerces
    INTEGER/REAL inserts to text, so only a BLOB survives as a non-string;
    removing the guard makes ``Path(bytes_state_path)`` raise ``TypeError``.
  * ``_load_run_state_app_index`` with a cache row whose ``app_session_id`` is a
    BLOB -> the row is skipped (``continue``, line 686) and the index stays
    empty. The row passes the SQL ``app_session_id != ''`` filter (a BLOB is
    never equal to a text literal), so it is only the Python-side guard that
    rejects it; removing it would key the index by ``bytes``.

The corrupt rows are written straight into the cache sqlite file (a durability
layer) to model a hand-edited / partially-corrupt cache, which is exactly the
fault surface these guards exist to defend against.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir via
``_test_home.isolate``; nothing touches a real home. Module-level caches are
reset between tests so order never matters. Every assertion is fail-on-bug.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_app_index_unit.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path, PosixPath

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-app-index-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
from runs_dir import (  # noqa: E402
    _load_run_state_app_index,
    _run_state_ledger_signature,
    _sqlite_connect,
    _write_run_state_cache,
    run_dirs_by_app_session,
    run_state_app_index_backfill_marker_path,
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
    path = Path(tempfile.mkdtemp(prefix="bc-rd-app-index-"))
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
# run_dirs_by_app_session: no ledger -> signature None -> {}
# --------------------------------------------------------------------------- #


def test_run_dirs_by_app_session_no_ledger_returns_empty(fresh_root):
    """Line 657: when the ledger is absent, ``_run_state_ledger_signature``
    returns ``None`` and ``run_dirs_by_app_session`` short-circuits to ``{}``.

    The marker is pre-written (version 3) so ``_backfill_run_state_app_index``
    is a no-op — otherwise backfill itself creates the ledger (it opens it for
    appending), the signature would no longer be None, and line 657 would stay
    unreached. With backfill skipped the ledger stays absent -> signature None
    -> ``{}``."""
    _write(
        run_state_app_index_backfill_marker_path(fresh_root),
        json.dumps({"version": runs_dir._RUN_STATE_APP_INDEX_BACKFILL_VERSION, "backfilled_at": 1.0}),
    )
    assert not _ledger(fresh_root).exists()
    assert run_dirs_by_app_session(fresh_root) == {}


# --------------------------------------------------------------------------- #
# run_dirs_by_app_session: root.resolve() OSError -> {}
# --------------------------------------------------------------------------- #


class _UnresolvableRoot(PosixPath):
    """Path subclass whose only override is ``resolve`` -> raise ``OSError``.

    Used so the raw-OS-fault branch at lines 660-661 is exercised without a
    global monkeypatch. Everything ``run_dirs_by_app_session`` does before the
    resolve (``glob``, ``stat``, ``/``, ledger signature) is inherited unchanged
    and does not itself call ``resolve``, so the fault lands on line 659."""

    def resolve(self, strict=False):  # noqa: D401 - mirror pathlib signature
        raise OSError("simulated resolve fault")


def test_run_dirs_by_app_session_resolve_oserror_returns_empty(fresh_root):
    """Lines 660-661: when ``root.resolve()`` raises a raw ``OSError``, the
    lookup fails closed to ``{}`` rather than propagating. Proven by passing an
    unresolvable root whose ledger exists (so the signature check at 656 passes
    and execution reaches the resolve); removing the ``except OSError`` handler
    would let the fault escape the function."""
    _touch_ledger(fresh_root)  # signature non-None -> reach the resolve
    root = _UnresolvableRoot(str(fresh_root))
    assert run_dirs_by_app_session(root) == {}


# --------------------------------------------------------------------------- #
# _load_run_state_app_index: BLOB (non-string) state_path row -> None
# --------------------------------------------------------------------------- #


def test_load_app_index_rejects_blob_state_path(fresh_root):
    """Line 688: a cache row whose ``state_path`` is a BLOB (not text) makes the
    lookup fail closed to ``None``. SQLite TEXT-affinity coerces INTEGER/REAL
    inserts to text, so only a BLOB survives as a non-string and reaches this
    guard. Removing the guard makes the next line (``Path(state_path)`` on
    bytes) raise ``TypeError``."""
    sig = _empty_cache(fresh_root)
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        conn.execute(
            "INSERT INTO entries (sid, state_path, written_at, app_session_id) "
            "VALUES (?, ?, ?, ?)",
            ("s1", sqlite3.Binary(b"not-text"), 1.0, "app-1"),
        )
        conn.commit()
    assert _load_run_state_app_index(fresh_root, sig, fresh_root.resolve()) is None


# --------------------------------------------------------------------------- #
# _load_run_state_app_index: BLOB (non-string) app_session_id row -> skipped
# --------------------------------------------------------------------------- #


def test_load_app_index_skips_blob_app_session_id(fresh_root):
    """Line 686: a cache row whose ``app_session_id`` is a BLOB passes the SQL
    ``app_session_id != ''`` filter (a BLOB is never equal to a text literal)
    but is rejected by the Python-side ``isinstance`` guard with ``continue``,
    so the index stays empty. The state_path is a real on-disk ledger-shaped
    file so that, absent the guard, the row would instead land in the index
    keyed by ``bytes``; the assertion ``== {}`` therefore proves the guard."""
    sp = str(_write(fresh_root / "run-1" / "state.json", json.dumps({"session_id": "s1"})))
    sig = _empty_cache(fresh_root)
    with _sqlite_connect(run_state_ledger_cache_path(fresh_root)) as conn:
        conn.execute(
            "INSERT INTO entries (sid, state_path, written_at, app_session_id) "
            "VALUES (?, ?, ?, ?)",
            ("s1", sp, 1.0, sqlite3.Binary(b"app-1")),
        )
        conn.commit()
    assert _load_run_state_app_index(fresh_root, sig, fresh_root.resolve()) == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

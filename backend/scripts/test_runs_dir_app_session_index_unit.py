"""Hermetic unit tests for the ``runs_dir`` indexed app-session loader.

Covers the sqlite-backed fast path that ``delete_runs_for_sessions`` and the
ask/run-cleanup endpoint (``cached_run_dirs_for_app_session``) trust to find
run dirs by ``app_session_id`` without an O(N) walk. The security-critical
surface here is FAIL-CLOSED: every corrupt / stale / unresolvable condition
returns ``None`` (or ``[]`` for the public variant) so the caller falls back
to the safe exhaustive walk instead of trusting a bad index — a stale index
must never cause a dir to be missed OR mis-reaped.

Functions under test:
- ``_load_run_state_dirs_for_app_sessions`` — low-level sqlite reader.
- ``_run_dirs_for_app_sessions_indexed`` — orchestrator (backfill, signature,
  root-resolve, stale-cache rebuild).
- ``cached_run_dirs_for_app_session`` — public, never-backfill lookup used by
  the orchestrator's run-cleanup path.

What is proved here and nowhere else:
- The reader returns ``None`` (force fallback) on a non-string ``state_path``
  row, a non-ledger-shape ``state_path`` row, or a signature mismatch.
- A valid row whose resolved location is outside the resolved root is skipped
  (escape guard), never yielded.
- The reader dedups multiple rows that resolve to the same run dir.
- The orchestrator returns ``[]`` for empty sids, ``None`` when the ledger
  signature is unavailable, ``None`` when the root cannot be resolved, and
  ``None`` when a stale-cache rebuild still yields no signature.
- The public lookup mirrors those early exits (``[]``), never backfills, and
  never rebuilds.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir
via ``_test_home.isolate``; nothing touches a real home.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_app_session_index_unit.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-app-index-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402

_APP = "app-session-target"
# Arbitrary fixed signature tuple used to build a "current" cache whose meta
# rows match the signature the reader is asked to verify against.
_SIG = (101, 202, 303, 404, 505)


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset every runs_dir process-global cache and the on-disk runs root
    before AND after each test, so order never matters."""
    for cache in (
        runs_dir._RUN_STATE_LEDGER_SEEN,
        runs_dir._RUN_STATE_RECENT_INDEX_CACHE,
        runs_dir._RUN_STATE_LEDGER_CACHE,
        runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT,
        runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT,
    ):
        cache.clear()
    _rm_runs_root()
    yield
    for cache in (
        runs_dir._RUN_STATE_LEDGER_SEEN,
        runs_dir._RUN_STATE_RECENT_INDEX_CACHE,
        runs_dir._RUN_STATE_LEDGER_CACHE,
        runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT,
        runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT,
    ):
        cache.clear()
    _rm_runs_root()


def _rm_runs_root() -> None:
    root = runs_dir.runs_root()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def _seed_run(run_id: str, app_session_id: str = _APP) -> Path:
    """Create a real run dir with a shaped ``state.json`` so it passes the
    candidate-stat + ledger-shape guards when the cache points at it."""
    run_dir = runs_dir.runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "session_id": f"prov-{run_id}",
                "jsonl_path": str(run_dir / "stream.jsonl"),
                "app_session_id": app_session_id,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def _write_current_cache(
    root: Path,
    signature: tuple,
    rows: list[tuple[str, str, float, str]],
) -> None:
    """Build a run-state sqlite cache whose ``meta`` matches ``signature``.
    Each row is ``(sid, state_path, written_at, app_session_id)``. Uses the
    production writer so schema + atomic-replace stay the single source of
    truth; rows with duplicate ``(sid, state_path)`` collapse via INSERT OR
    REPLACE (callers wanting duplicate state_paths must use distinct sids)."""
    root.mkdir(parents=True, exist_ok=True)  # _write_run_state_cache assumes it
    index: dict[str, list[tuple[float, str]]] = {}
    app_map: dict[str, str] = {}
    for sid, state_path, written_at, app_session_id in rows:
        index.setdefault(sid, []).append((written_at, state_path))
        app_map[state_path] = app_session_id
    runs_dir._write_run_state_cache(root, signature, index, app_map)


def _insert_cache_row(
    root: Path, sid: str, state_path, written_at: float, app_session_id: str
) -> None:
    """Inject a single raw row into the existing cache ``entries`` table
    without touching ``meta`` (so the signature stays current). Used to plant
    malformed rows (non-string / wrong-shape state_path) the production writer
    would refuse to emit."""
    cache = runs_dir.run_state_ledger_cache_path(root)
    with runs_dir._sqlite_connect(cache) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO entries (sid, state_path, written_at, app_session_id) "
            "VALUES (?, ?, ?, ?)",
            (sid, state_path, written_at, app_session_id),
        )
        conn.commit()


# --- _load_run_state_dirs_for_app_sessions: fail-closed guards --------------


def test_load_returns_none_when_signature_not_current():
    """A cache whose meta signature does not match the expected ledger
    signature is stale -> None (caller must rebuild / fall back)."""
    root = runs_dir.runs_root()
    _write_current_cache(root, (0, 0, 0, 0, 0), [])  # wrong signature
    resolved = root.resolve()
    result = runs_dir._load_run_state_dirs_for_app_sessions(
        root, _SIG, resolved, frozenset({_APP})
    )
    assert result is None


def test_load_returns_none_for_non_string_state_path_row():
    """A corrupt row carrying a non-string ``state_path`` forces None rather
    than risking a malformed path lookup. The ``state_path`` column has TEXT
    affinity (coerces ints/bools to text), so the only way to land a genuinely
    non-string value is a BLOB that is not valid UTF-8 — sqlite keeps it as a
    BLOB and the driver returns ``bytes``. No real writer emits such a row."""
    root = runs_dir.runs_root()
    _write_current_cache(root, _SIG, [])
    _insert_cache_row(root, "sid-bad", sqlite3.Binary(b"\xff\xfe"), 1.0, _APP)
    resolved = root.resolve()
    assert runs_dir._load_run_state_dirs_for_app_sessions(
        root, _SIG, resolved, frozenset({_APP})
    ) is None


def test_load_returns_none_for_non_ledger_shape_state_path():
    """A ``state_path`` that is a string but does not live at
    ``<root>/<run>/state.json`` is not a real run-state row -> None."""
    root = runs_dir.runs_root()
    _write_current_cache(root, _SIG, [])
    _insert_cache_row(root, "sid-bad", "/etc/passwd", 1.0, _APP)
    resolved = root.resolve()
    assert runs_dir._load_run_state_dirs_for_app_sessions(
        root, _SIG, resolved, frozenset({_APP})
    ) is None


# --- _load_run_state_dirs_for_app_sessions: skipping + dedup ----------------


def test_load_skips_shaped_entry_outside_resolved_root():
    """A row whose lexical path is ledger-shaped but whose resolved location
    is outside the resolved root is skipped via ``continue`` — never yielded,
    never crashes. Exercises the under-root escape guard directly (the caller
    passes a ``root_resolved`` the entry does not live under); ``relative_to``
    is a pure path comparison, so the claimed root need not exist on disk."""
    root = runs_dir.runs_root()
    run_dir = _seed_run("r1")
    state_path = str(run_dir / "state.json")
    _write_current_cache(root, _SIG, [("prov-r1", state_path, 1.0, _APP)])
    claimed_root = root.parent / "nonexistent-elsewhere"  # entry resolves outside it
    result = runs_dir._load_run_state_dirs_for_app_sessions(
        root, _SIG, claimed_root, frozenset({_APP})
    )
    assert result == []


def test_load_returns_matching_dir_and_dedups_same_run():
    """Happy path: a current cache with a matching app_session_id yields the
    run dir exactly once even when two ledger sids point at the SAME run dir
    (one run dir per turn, many provider sids). Covers the dedup branch."""
    root = runs_dir.runs_root()
    run_dir = _seed_run("r1")
    state_path = str(run_dir / "state.json")
    # Two distinct provider sids, same state_path + app_session_id.
    _write_current_cache(
        root,
        _SIG,
        [
            ("prov-A", state_path, 1.0, _APP),
            ("prov-B", state_path, 2.0, _APP),
        ],
    )
    result = runs_dir._load_run_state_dirs_for_app_sessions(
        root, _SIG, root.resolve(), frozenset({_APP})
    )
    assert result == [run_dir]


# --- _run_dirs_for_app_sessions_indexed: orchestrator early exits -----------


def test_indexed_empty_app_sids_returns_empty_list():
    root = runs_dir.runs_root()
    root.mkdir(parents=True, exist_ok=True)
    assert runs_dir._run_dirs_for_app_sessions_indexed(root, frozenset()) == []


def test_indexed_returns_none_when_ledger_signature_unavailable(monkeypatch):
    """No usable ledger signature -> None, signaling the caller to fall back
    to the exhaustive walk. Backfill is stubbed so it cannot create a ledger
    beneath this test."""
    root = runs_dir.runs_root()
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runs_dir, "_backfill_run_state_app_index", lambda _r: None)
    # No ledger file exists -> _run_state_ledger_signature returns None.
    assert not runs_dir.run_state_ledger_path(root).exists()
    assert (
        runs_dir._run_dirs_for_app_sessions_indexed(root, frozenset({_APP}))
        is None
    )


def test_indexed_returns_none_when_root_unresolvable(monkeypatch):
    """A root whose resolve() raises OSError -> None (cannot establish a safe
    root_resolved for the under-root escape guard)."""
    root = runs_dir.runs_root()
    root.mkdir(parents=True, exist_ok=True)
    # Give it a valid ledger so the signature check passes and execution
    # reaches root.resolve().
    runs_dir.run_state_ledger_path(root).write_text("", encoding="utf-8")
    monkeypatch.setattr(runs_dir, "_backfill_run_state_app_index", lambda _r: None)

    real_resolve = runs_dir.Path.resolve

    def _boom_resolve(self):
        if self == root:
            raise OSError("simulated unresolvable root")
        return real_resolve(self)

    monkeypatch.setattr(runs_dir.Path, "resolve", _boom_resolve)
    assert (
        runs_dir._run_dirs_for_app_sessions_indexed(root, frozenset({_APP}))
        is None
    )


def test_indexed_returns_none_when_ledger_vanishes_during_rebuild(monkeypatch):
    """Stale cache -> rebuild -> if the ledger is gone when the orchestrator
    re-checks the signature after the rebuild, give up with None. Models the
    real TOCTOU (concurrent ledger removal mid-rebuild) the second signature
    guard exists for. The rebuild is exercised for real; only its side effect
    (the ledger disappearing) is simulated."""
    root = runs_dir.runs_root()
    run_dir = _seed_run("r1")
    state_path = str(run_dir / "state.json")
    ledger = runs_dir.run_state_ledger_path(root)
    # Real ledger row so the rebuild trigger has something to read.
    runs_dir._append_run_state_ledger(run_dir / "state.json", {
        "session_id": "prov-r1",
        "jsonl_path": str(run_dir / "stream.jsonl"),
        "app_session_id": _APP,
        "state_path": state_path,
    })
    assert ledger.exists()  # pre-rebuild signature will be valid
    # Mark backfill current so the orchestrator's backfill step is a no-op.
    runs_dir.write_json(
        runs_dir.run_state_app_index_backfill_marker_path(root),
        {"version": runs_dir._RUN_STATE_APP_INDEX_BACKFILL_VERSION, "backfilled_at": 0.0},
    )
    # Stale cache: meta signature (0,0,0,0,0) will not match the live ledger
    # signature, so the first load returns None and the rebuild path runs.
    _write_current_cache(root, (0, 0, 0, 0, 0), [("prov-r1", state_path, 1.0, _APP)])

    def _vanishing_rebuild(_root, _sid):
        # Simulate the ledger disappearing during the rebuild.
        ledger.unlink(missing_ok=True)
        return []

    monkeypatch.setattr(runs_dir, "ledger_state_files_for_sid", _vanishing_rebuild)
    assert (
        runs_dir._run_dirs_for_app_sessions_indexed(root, frozenset({_APP}))
        is None
    )
    assert not ledger.exists()  # proves the rebuild ran and the re-signature saw no ledger


# --- cached_run_dirs_for_app_session: public early exits --------------------


def test_cached_lookup_empty_session_returns_empty():
    root = runs_dir.runs_root()
    root.mkdir(parents=True, exist_ok=True)
    assert runs_dir.cached_run_dirs_for_app_session(root, "") == []


def test_cached_lookup_returns_empty_when_signature_unavailable(monkeypatch):
    """No ledger -> signature None -> []. The public lookup must NEVER
    backfill or rebuild (those are delete-path concerns), so an absent ledger
    is a hard []. Backfill is stubbed to assert it is never called."""
    root = runs_dir.runs_root()
    root.mkdir(parents=True, exist_ok=True)

    def _forbidden_backfill(_r):
        raise AssertionError("cached lookup must never backfill")

    monkeypatch.setattr(runs_dir, "_backfill_run_state_app_index", _forbidden_backfill)
    assert not runs_dir.run_state_ledger_path(root).exists()
    assert runs_dir.cached_run_dirs_for_app_session(root, _APP) == []


def test_cached_lookup_returns_empty_when_root_unresolvable(monkeypatch):
    """Signature available but root unresolvable -> []. The public lookup
    mirrors the orchestrator's fail-closed resolve guard."""
    root = runs_dir.runs_root()
    root.mkdir(parents=True, exist_ok=True)
    runs_dir.run_state_ledger_path(root).write_text("", encoding="utf-8")

    real_resolve = runs_dir.Path.resolve

    def _boom_resolve(self):
        if self == root:
            raise OSError("simulated unresolvable root")
        return real_resolve(self)

    monkeypatch.setattr(runs_dir.Path, "resolve", _boom_resolve)
    assert runs_dir.cached_run_dirs_for_app_session(root, _APP) == []

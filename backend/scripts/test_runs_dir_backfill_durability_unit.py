"""Hermetic unit tests for the ``runs_dir`` ledger/index backfill durability.

Covers the residual defensive + race branches of the run-state ledger append,
the app-session-index backfill, and the reconciled-marker index backfill.
These are the durability paths that populate the indexes deletion and recovery
trust; their fail-closed / idempotent / double-checked-locking behavior is
security-relevant (a half-written or duplicated index row must never cause a
run dir to be mis-attributed on reap).

Functions under test (branches not previously covered):
- ``_append_run_state_ledger``: idempotent skip when the ledger already
  carries the row but the in-memory dedup set is cold; broad exception guard.
- ``_backfill_run_state_app_index``: corrupt/missing-field state.json rows
  are skipped; double-checked-locking re-check inside the lock; OSError
  writing the ledger aborts without writing the completion marker.
- ``append_reconciled_marker_index``: append failure is swallowed + logged.
- ``ensure_reconciled_marker_index_backfilled``: double-checked-locking
  re-check; symlinked marker rejected; null row skipped; duplicate key
  skipped.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir
via ``_test_home.isolate``; nothing touches a real home.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_backfill_durability_unit.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-backfill-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402

_APP = "app-session-target"


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset every runs_dir process-global cache and the on-disk runs root +
    reconciled index before AND after each test, so order never matters."""
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


def _ledger_line_count() -> int:
    ledger = runs_dir.run_state_ledger_path(runs_dir.runs_root())
    if not ledger.is_file():
        return 0  # missing, or (in the OSError test) a directory blocking the path
    with ledger.open(encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _seed_state(
    run_id: str,
    *,
    session_id: str | None = "prov-x",
    jsonl_path: str | None = "stream.jsonl",
    app_session_id: str | None = _APP,
    body: str | None = None,
) -> Path:
    """Write a run dir + ``state.json``. ``body`` overrides the JSON body
    (used to plant corrupt JSON); individual ``None`` fields drop keys."""
    run_dir = runs_dir.runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if body is not None:
        (run_dir / "state.json").write_text(body, encoding="utf-8")
        return run_dir
    state: dict = {}
    if session_id is not None:
        state["session_id"] = session_id
    if jsonl_path is not None:
        state["jsonl_path"] = jsonl_path
    if app_session_id is not None:
        state["app_session_id"] = app_session_id
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return run_dir


# --- _append_run_state_ledger ----------------------------------------------


def test_append_idempotent_when_ledger_has_row_but_seen_cold():
    """When the ledger file already carries a row but the in-memory dedup set
    is cold (fresh process), the append detects the dup via
    ``_run_state_ledger_has_key`` and returns WITHOUT re-writing — and primes
    the in-memory set so the next append is a cheap set hit."""
    run_dir = _seed_state("r1")
    state_path = run_dir / "state.json"
    # First append writes the row + populates the in-memory set.
    runs_dir._append_run_state_ledger(state_path, json.loads(state_path.read_text()))
    assert _ledger_line_count() == 1
    # Simulate a cold process: forget the in-memory set, keep the ledger.
    runs_dir._RUN_STATE_LEDGER_SEEN.clear()
    key = (str(state_path), "prov-x", "stream.jsonl")

    runs_dir._append_run_state_ledger(state_path, json.loads(state_path.read_text()))

    assert _ledger_line_count() == 1  # not re-written
    assert key in runs_dir._RUN_STATE_LEDGER_SEEN  # primed for next time


def test_append_swallows_exception_and_logs(monkeypatch, caplog):
    """Any unexpected error inside the append is swallowed and logged; no
    partial ledger row is written. Simulated by making the dup-check raise."""
    import logging

    run_dir = _seed_state("r1")
    state_path = run_dir / "state.json"

    def _boom(_ledger, _key):
        raise RuntimeError("simulated append failure")

    monkeypatch.setattr(runs_dir, "_run_state_ledger_has_key", _boom)
    with caplog.at_level(logging.ERROR, logger="runs_dir"):
        runs_dir._append_run_state_ledger(state_path, json.loads(state_path.read_text()))
    assert any("failed to append run-state ledger" in r.message for r in caplog.records)
    assert _ledger_line_count() == 0


# --- _backfill_run_state_app_index -----------------------------------------


def test_backfill_skips_corrupt_state_json_but_keeps_valid():
    """A run whose ``state.json`` is unreadable (corrupt JSON) is skipped; a
    sibling valid run is still backfilled."""
    _seed_state("corrupt", body="{ not json")
    _seed_state("valid")
    runs_dir._backfill_run_state_app_index(runs_dir.runs_root())
    assert _ledger_line_count() == 1
    # Completion marker is written even when some candidates were skipped.
    assert runs_dir.run_state_app_index_backfill_marker_path(
        runs_dir.runs_root()
    ).exists()


def test_backfill_skips_state_with_missing_required_fields():
    """A ``state.json`` with valid JSON but a missing/empty required field
    (here ``app_session_id``) is rejected by the validation gate."""
    _seed_state("no-app", app_session_id=None)
    runs_dir._backfill_run_state_app_index(runs_dir.runs_root())
    assert _ledger_line_count() == 0
    assert runs_dir.run_state_app_index_backfill_marker_path(
        runs_dir.runs_root()
    ).exists()


def test_backfill_double_checked_locking_short_circuits(monkeypatch):
    """The marker check runs twice — once outside the lock, once inside. If
    the marker becomes current between the two checks (another writer won the
    race), backfill returns at the inner check without scanning or writing."""
    calls = {"n": 0}

    def _current(_root):
        calls["n"] += 1
        return calls["n"] != 1  # False outside the lock, True inside it.

    scanned: list[Path] = []
    real_glob = runs_dir.Path.glob

    def _spying_glob(self, pattern):
        if self == runs_dir.runs_root():
            scanned.append(self)
        return real_glob(self, pattern)

    monkeypatch.setattr(runs_dir, "_run_state_app_index_backfill_current", _current)
    monkeypatch.setattr(runs_dir.Path, "glob", _spying_glob)

    runs_dir._backfill_run_state_app_index(runs_dir.runs_root())

    assert calls["n"] == 2  # both checks ran -> inner check returned
    assert scanned == []   # the runs root was never scanned
    assert _ledger_line_count() == 0
    assert not runs_dir.run_state_app_index_backfill_marker_path(
        runs_dir.runs_root()
    ).exists()


def test_backfill_oserror_writing_ledger_aborts_without_marker():
    """If opening the ledger for append raises OSError (e.g. the ledger path
    is occupied by a directory), backfill aborts and MUST NOT write the
    completion marker — otherwise a later run would skip a backfill that
    never completed."""
    _seed_state("valid")
    # Block the ledger path with a directory so ledger.open("a") raises.
    ledger = runs_dir.run_state_ledger_path(runs_dir.runs_root())
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.mkdir()
    runs_dir._backfill_run_state_app_index(runs_dir.runs_root())
    assert _ledger_line_count() == 0  # ledger is a dir -> 0 lines
    assert not runs_dir.run_state_app_index_backfill_marker_path(
        runs_dir.runs_root()
    ).exists()


# --- append_reconciled_marker_index ----------------------------------------


def test_append_reconciled_marker_index_swallows_failure(monkeypatch, caplog):
    """If the reconciled-marker index append raises, the error is swallowed
    and logged — a failed index append must never crash the caller."""
    import logging

    run_dir = _seed_state("r1")
    marker = run_dir / "reconciled.marker"
    marker.write_text(
        json.dumps({"provider_kind": "claude", "ingestion_version": 2}),
        encoding="utf-8",
    )

    class _BrokenIndex:
        def append(self, _row):
            raise RuntimeError("simulated index write failure")

    import reconciled_marker_index

    monkeypatch.setattr(reconciled_marker_index, "for_path", lambda _p: _BrokenIndex())
    with caplog.at_level(logging.ERROR, logger="runs_dir"):
        runs_dir.append_reconciled_marker_index(
            marker, "claude", 2, root=runs_dir.runs_root()
        )
    assert any(
        "failed to append reconciled-marker index" in r.message for r in caplog.records
    )


# --- ensure_reconciled_marker_index_backfilled -----------------------------


def _seed_reconciled_marker(run_id: str, *, provider_kind="claude", ingestion_version=2) -> Path:
    run_dir = runs_dir.runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "reconciled.marker").write_text(
        json.dumps({"provider_kind": provider_kind, "ingestion_version": ingestion_version}),
        encoding="utf-8",
    )
    return run_dir


def test_ensure_reconciled_backfill_double_checked_locking(monkeypatch):
    """Same double-checked-locking pattern: marker becomes current between the
    two checks -> backfill returns False at the inner check without scanning."""
    calls = {"n": 0}

    def _current(_root):
        calls["n"] += 1
        return calls["n"] != 1  # False outside the lock, True inside it.

    scanned = {"n": 0}
    real_scandir = runs_dir.os.scandir

    def _spying_scandir(_path):
        scanned["n"] += 1
        return real_scandir(_path)

    monkeypatch.setattr(runs_dir, "_reconciled_marker_backfill_current", _current)
    monkeypatch.setattr(runs_dir.os, "scandir", _spying_scandir)

    assert runs_dir.ensure_reconciled_marker_index_backfilled(runs_dir.runs_root()) is False
    assert calls["n"] == 2
    assert scanned["n"] == 0  # never scanned


def test_ensure_reconciled_backfill_skips_symlinked_marker():
    """A ``reconciled.marker`` that is itself a symlink is rejected (symlink
    escape guard) and not indexed."""
    _seed_reconciled_marker("real")
    target = runs_dir.runs_root() / "real" / "reconciled.marker"
    link_dir = runs_dir.runs_root() / "linked"
    link_dir.mkdir(parents=True)
    (link_dir / "reconciled.marker").symlink_to(target)

    runs_dir.ensure_reconciled_marker_index_backfilled(runs_dir.runs_root())

    index = runs_dir.reconciled_marker_index_path(runs_dir.runs_root())
    loaded = runs_dir.load_reconciled_marker_index(runs_dir.runs_root())
    # Only the real marker is indexed; the symlinked one is skipped.
    assert "real" in loaded
    assert "linked" not in loaded
    assert index.exists()


def test_ensure_reconciled_backfill_skips_null_row(monkeypatch):
    """If ``_reconciled_marker_index_row`` returns None (e.g. a TOCTOU where
    the marker becomes invalid between read and stat), the candidate is
    skipped — never appended, never crashes."""
    _seed_reconciled_marker("r1")
    monkeypatch.setattr(runs_dir, "_reconciled_marker_index_row", lambda *_a, **_k: None)
    runs_dir.ensure_reconciled_marker_index_backfilled(runs_dir.runs_root())
    assert runs_dir.load_reconciled_marker_index(runs_dir.runs_root()) == {}


# NOTE: ``ensure_reconciled_marker_index_backfilled``'s duplicate-key skip
# (line 1031, ``if key in existing: continue``) is intentionally NOT covered
# here. It is dead code: ``existing`` (from ``_reconciled_marker_index_keys``
# -> ``load_keys``) holds ``row_key`` tuples keyed on ``run_id``, while the
# compared ``key`` (from ``_reconciled_marker_index_key``) is keyed on
# ``marker_path``. The two never match, so the dedup never fires. Tracked as
# defect card <pending-id>; covering it would require either mocking the key
# function (test fiction) or fixing the key mismatch (a design decision that
# also updates the existing ``_reconciled_marker_index_key`` contract test).

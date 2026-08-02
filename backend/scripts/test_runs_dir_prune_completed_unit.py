"""Hermetic unit tests for the ``runs_dir`` age-prune cluster.

Covers ``iter_run_dirs`` and ``prune_old_completed_runs`` — the startup
maintenance path that reaps run dirs carrying a stale ``complete.json``.
This is a file-deletion path, so the revalidation gate (re-stat under the
catalog lock and refuse to reap anything that changed between the inventory
scan and the locked reap) is the security-critical surface.

What is proved here and nowhere else:
- ``iter_run_dirs``: missing-root no-op, full walk yields dirs and skips
  non-dirs, and the ``run_id_filter`` fast path yields only listed dirs.
- ``prune_old_completed_runs`` scan collection: missing root -> 0; non-dir
  entries skipped; a run with no ``complete.json`` (scan OSError) skipped; a
  run whose ``complete.json`` is itself a directory (not regular) skipped; a
  young ``complete.json`` (mtime >= cutoff) skipped; a valid old
  ``complete.json`` collected AND reaped.
- The locked revalidation gate: a candidate that changed between the
  inventory scan and the locked revalidate (size/mtime identity mismatch) is
  refused; a candidate whose ``complete.json`` vanishes before revalidate
  (revalidate OSError) is refused. Both leave the dir on disk.
- ``run_catalog_lock`` default-root fallback.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir
via ``_test_home.isolate``; nothing touches a real home.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_prune_completed_unit.py -v
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-prune-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
import spawn_ledger  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset runs_dir process-global caches and the on-disk runs root + spawn
    ledger before AND after each test, so order never matters."""
    for cache in (
        runs_dir._RUN_STATE_LEDGER_SEEN,
        runs_dir._RUN_STATE_RECENT_INDEX_CACHE,
        runs_dir._RUN_STATE_LEDGER_CACHE,
        runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT,
        runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT,
    ):
        cache.clear()
    _rm_runs_root()
    spawn_ledger._path().unlink(missing_ok=True)
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
    spawn_ledger._path().unlink(missing_ok=True)


def _rm_runs_root() -> None:
    root = runs_dir.runs_root()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def _touch(path: Path, *, age_days: float, body: str = "{}") -> Path:
    """Create ``path`` (with parents) and backdate its mtime by ``age_days``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    when = time.time() - (age_days * 24 * 60 * 60)
    os.utime(path, (when, when))
    return path


# --- iter_run_dirs ---------------------------------------------------------


def test_iter_run_dirs_empty_when_root_missing():
    assert not runs_dir.runs_root().exists()
    assert list(runs_dir.iter_run_dirs()) == []


def test_iter_run_dirs_full_walk_yields_dirs_and_skips_files():
    root = runs_dir.runs_root()
    (root / "run-a").mkdir(parents=True)
    (root / "run-b").mkdir(parents=True)
    (root / "stray-file").write_text("nope", encoding="utf-8")

    yielded = sorted(p.name for p in runs_dir.iter_run_dirs())
    assert yielded == ["run-a", "run-b"]


def test_iter_run_dirs_filter_yields_only_existing_listed_dirs():
    root = runs_dir.runs_root()
    (root / "run-a").mkdir(parents=True)
    (root / "not-a-dir").write_text("nope", encoding="utf-8")

    # "run-b" does not exist, "not-a-dir" exists but is a file -> neither yielded.
    yielded = sorted(p.name for p in runs_dir.iter_run_dirs({"run-a", "run-b", "not-a-dir"}))
    assert yielded == ["run-a"]


# --- prune_old_completed_runs: scan collection -----------------------------


def test_prune_returns_zero_when_root_missing():
    assert not runs_dir.runs_root().exists()
    assert runs_dir.prune_old_completed_runs(max_age_days=7) == 0


def test_prune_reaps_old_completed_and_spares_young_incomplete_and_nondir():
    """The core contract: only old dirs carrying a regular old
    ``complete.json`` are reaped. Young-completed, old-incomplete (no
    complete.json -> scan OSError), and stray non-dir entries are all
    spared."""
    root = runs_dir.runs_root()
    old_completed = root / "old-completed"
    fresh_completed = root / "fresh-completed"
    old_incomplete = root / "old-incomplete"
    old_completed.mkdir(parents=True)
    fresh_completed.mkdir(parents=True)
    old_incomplete.mkdir(parents=True)
    _touch(old_completed / "complete.json", age_days=8)
    _touch(fresh_completed / "complete.json", age_days=1)
    # old-incomplete has only a state.json (no complete.json) -> scan OSError.
    _touch(old_incomplete / "state.json", age_days=8)
    # A non-dir entry directly in the runs root must be skipped by the scan.
    (root / "stray-file").write_text("nope", encoding="utf-8")

    removed = runs_dir.prune_old_completed_runs(max_age_days=7)
    assert removed == 1
    assert not old_completed.exists()
    assert fresh_completed.exists()
    assert old_incomplete.exists()
    assert (root / "stray-file").exists()


def test_prune_skips_dir_whose_complete_json_is_a_directory():
    """``complete.json`` must be a regular file; a directory of that name is
    rejected at scan (S_ISREG false) and never reaped."""
    root = runs_dir.runs_root()
    run_dir = root / "weird-complete"
    run_dir.mkdir(parents=True)
    # Make complete.json a DIRECTORY, then backdate it.
    (run_dir / "complete.json").mkdir()
    when = time.time() - (8 * 24 * 60 * 60)
    os.utime(run_dir / "complete.json", (when, when))

    assert runs_dir.prune_old_completed_runs(max_age_days=7) == 0
    assert run_dir.exists()


# --- prune_old_completed_runs: locked revalidation gate --------------------


def test_prune_revalidation_skips_candidate_changed_between_scan_and_revalidate(monkeypatch):
    """A candidate collected by the scan is refused if its ``complete.json``
    changes between the inventory scan and the locked revalidate (identity
    mismatch: size/mtime_ns differ). Simulated by a catalog-lock wrapper that
    rewrites ``complete.json`` on entry — the natural sync point between scan
    and revalidate. The dir must survive."""
    root = runs_dir.runs_root()
    run_dir = root / "changed-after-scan"
    run_dir.mkdir(parents=True)
    _touch(run_dir / "complete.json", age_days=8, body='{"v":1}')

    def _mutating_lock(_root):
        class _Mutating:
            def __enter__(self):
                # Rewrite + re-backdate: scan saw the old size/mtime_ns; the
                # revalidate sees these new ones -> identity mismatch -> skip.
                _touch(run_dir / "complete.json", age_days=8, body='{"v":2,"mutated":true}')
                return self

            def __exit__(self, *_args):
                return False

        return _Mutating()

    monkeypatch.setattr(runs_dir, "run_catalog_lock", _mutating_lock)
    assert runs_dir.prune_old_completed_runs(max_age_days=7) == 0
    assert run_dir.exists()


def test_prune_revalidation_oserror_skips_candidate(monkeypatch):
    """If the revalidate stat raises OSError (e.g. ``complete.json`` vanished
    between scan and revalidate), the candidate is refused, not reaped."""
    root = runs_dir.runs_root()
    run_dir = root / "vanished-after-scan"
    run_dir.mkdir(parents=True)
    _touch(run_dir / "complete.json", age_days=8)

    def _deleting_lock(_root):
        class _Deleting:
            def __enter__(self):
                (run_dir / "complete.json").unlink()
                return self

            def __exit__(self, *_args):
                return False

        return _Deleting()

    monkeypatch.setattr(runs_dir, "run_catalog_lock", _deleting_lock)
    assert runs_dir.prune_old_completed_runs(max_age_days=7) == 0
    # The dir itself survives (only complete.json was removed; reap not reached).
    assert run_dir.exists()


def test_prune_does_not_count_a_failed_reap(monkeypatch):
    """When ``reap_run_dir`` returns False (rmtree failed) for an otherwise
    valid candidate, prune must NOT count it as removed. Covers the false
    branch of the reap check."""
    root = runs_dir.runs_root()
    run_dir = root / "reap-fails"
    run_dir.mkdir(parents=True)
    _touch(run_dir / "complete.json", age_days=8)

    monkeypatch.setattr(runs_dir, "reap_run_dir", lambda _child: False)
    assert runs_dir.prune_old_completed_runs(max_age_days=7) == 0
    assert run_dir.exists()


# --- run_catalog_lock default-root fallback --------------------------------


def test_run_catalog_lock_uses_runs_root_when_root_none():
    """``run_catalog_lock(None)`` falls back to ``runs_root()`` and acquires
    the real fcntl lock. Smoke-covers the default-root branch."""
    root = runs_dir.runs_root()
    root.mkdir(parents=True, exist_ok=True)
    with runs_dir.run_catalog_lock(None):
        lock_path = root / "run_catalog.lock"
        assert lock_path.exists()
    # Reentrant within the same process (fcntl locks are per-process): a
    # second acquisition must also succeed.
    with runs_dir.run_catalog_lock(root):
        pass

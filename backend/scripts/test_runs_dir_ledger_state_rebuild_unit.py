"""Hermetic unit tests for ``runs_dir`` ledger-state rebuild defensive branches.

These cover the ``ledger_state_files_for_sid`` rebuild/error paths the cache
cluster test (``test_runs_dir_run_state_cache_unit.py``) leaves cold — the
security/concurrency-correctness guards that only fire on filesystem failure
or cache-handoff races:

* root-resolve OSError -> [] (fail-closed, never raises).
* non-owner joiner, after waiting for the rebuild owner, reuses the sqlite
  cache row the owner published (no redundant rebuild).
* non-owner joiner reuses the in-memory index the owner published during the
  joiner's wait (no redundant rebuild).
* rebuild dedup keeps the HIGHER ``written_at`` for a repeated (sid, path) key
  (a stale lower-stamped duplicate is dropped).
* ledger-open OSError on the rebuild body, as a non-owner joiner, returns []
  and does not finish a rebuild it does not own.
* signature drift between the initial and final ledger signature, as a
  non-owner joiner, recurses and ultimately stamps the cache with the FINAL
  stable signature (never a stale one).

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir via
``_test_home.isolate``; nothing touches a real home. Module-level caches are
reset between tests so order never matters.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_ledger_state_rebuild_unit.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-ledger-rebuild-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
from runs_dir import (  # noqa: E402
    _load_run_state_cache_for_sid,
    _run_state_ledger_signature,
    ledger_state_files_for_sid,
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
    path = Path(tempfile.mkdtemp(prefix="bc-rd-ledger-rebuild-root-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _write(path: Path, data: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    return path


def _ledger(root: Path) -> Path:
    return run_state_ledger_path(root)


def _touch_ledger(root: Path) -> None:
    ledger = _ledger(root)
    if not ledger.exists():
        _write(ledger, "")


def _sig(root: Path):
    return _run_state_ledger_signature(_ledger(root))


def _make_state(root: Path, run_id: str, sid: str) -> str:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {"session_id": sid, "jsonl_path": str(run_dir / "events.jsonl")}
    _write(run_dir / "state.json", json.dumps(state))
    return str(run_dir / "state.json")


# A Path whose resolve() always raises OSError — lets us exercise the
# "root unresolvable" fail-closed branch without poking the filesystem.
_UnresolvablePathBase = type(Path())


class _UnresolvablePath(_UnresolvablePathBase):
    def resolve(self, *args, **kwargs):  # type: ignore[override]
        raise OSError("simulated unresolvable root")


def _seed_non_owner(root: Path) -> threading.Event:
    """Pre-seed the rebuild-inflight flag so _claim_run_state_cache_rebuild
    returns owner=False (joiner), with an already-set event so wait() returns."""
    done = threading.Event()
    done.set()
    runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT[str(root)] = done
    return done


# --------------------------------------------------------------------------- #
# root-resolve OSError fail-closed
# --------------------------------------------------------------------------- #


def test_ledger_state_files_for_sid_unresolvable_root_returns_empty(fresh_root):
    # Ledger exists -> stat signature non-None; root.resolve() raises -> [].
    _touch_ledger(fresh_root)
    assert ledger_state_files_for_sid(_UnresolvablePath(str(fresh_root)), "s1") == []


# --------------------------------------------------------------------------- #
# non-owner joiner reuses the sqlite cache row published during its wait
# --------------------------------------------------------------------------- #


def test_joiner_reuses_sqlite_cache_row_published_after_wait(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    _touch_ledger(fresh_root)
    _seed_non_owner(fresh_root)
    # No in-memory cache, no sqlite cache on the first lookup. The owner
    # publishes a sqlite row during the joiner's wait -> second _load returns it.
    real_load = _load_run_state_cache_for_sid
    calls = {"n": 0}

    def load_side(root, signature, sid, root_resolved):
        calls["n"] += 1
        if calls["n"] == 2:
            return [Path(sp)]  # owner published while we waited
        return real_load(root, signature, sid, root_resolved)

    runs_dir._load_run_state_cache_for_sid = load_side
    try:
        # The ledger body is empty, so a (broken) fall-through rebuild would
        # yield [] rather than the owner's published path.
        assert ledger_state_files_for_sid(fresh_root, "s1") == [Path(sp)]
    finally:
        runs_dir._load_run_state_cache_for_sid = real_load


# --------------------------------------------------------------------------- #
# non-owner joiner reuses the in-memory index published during its wait
# --------------------------------------------------------------------------- #


def test_joiner_reuses_inmemory_index_published_after_wait(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    _touch_ledger(fresh_root)
    sig = _sig(fresh_root)
    _seed_non_owner(fresh_root)
    # _load always None (no sqlite cache); on the second call the owner has
    # published the in-memory index, which the joiner must reuse verbatim.
    real_load = _load_run_state_cache_for_sid
    calls = {"n": 0}

    def load_side(root, signature, sid, root_resolved):
        calls["n"] += 1
        if calls["n"] == 2:
            runs_dir._RUN_STATE_LEDGER_CACHE[str(root)] = (
                signature,
                {"s1": [(1.0, sp)]},
            )
        return None

    runs_dir._load_run_state_cache_for_sid = load_side
    try:
        # The ledger body is empty, so a (broken) fall-through rebuild would
        # yield [] rather than the owner's published index.
        assert ledger_state_files_for_sid(fresh_root, "s1") == [Path(sp)]
    finally:
        runs_dir._load_run_state_cache_for_sid = real_load


# --------------------------------------------------------------------------- #
# non-owner joiner REJECTS a stale-signature in-memory cache on second check
# --------------------------------------------------------------------------- #


def test_joiner_rejects_stale_signature_in_second_inmemory_check(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    _touch_ledger(fresh_root)
    _seed_non_owner(fresh_root)
    # _load always None; on the second call publish an in-memory cache carrying
    # a WRONG signature. The joiner's second recheck must NOT reuse it and fall
    # through to a real rebuild (which reads the empty ledger -> []).
    real_load = _load_run_state_cache_for_sid
    calls = {"n": 0}
    wrong_sig = tuple(v + 99 for v in _sig(fresh_root))

    def load_side(root, signature, sid, root_resolved):
        calls["n"] += 1
        if calls["n"] == 2:
            runs_dir._RUN_STATE_LEDGER_CACHE[str(root)] = (
                wrong_sig,
                {"s1": [(1.0, sp)]},
            )
        return None

    runs_dir._load_run_state_cache_for_sid = load_side
    try:
        # Wrong-signature cache rejected -> rebuild of empty ledger -> [].
        # (A broken reuse would return the stale [Path(sp)].)
        assert ledger_state_files_for_sid(fresh_root, "s1") == []
    finally:
        runs_dir._load_run_state_cache_for_sid = real_load


# --------------------------------------------------------------------------- #
# rebuild dedup keeps the higher written_at for a repeated (sid, path) key
# --------------------------------------------------------------------------- #


def test_rebuild_dedup_keeps_higher_written_at_for_same_key(fresh_root):
    sp = _make_state(fresh_root, "run-A", "s1")
    ledger = _ledger(fresh_root)
    # Same (sid, state_path) key, first row higher written_at, second lower.
    # The rebuild must keep the higher stamp; a broken last-writer-wins would
    # store the lower one. Observable via the in-memory cache it writes.
    _write(
        ledger,
        json.dumps({"session_id": "s1", "state_path": sp, "written_at": 100.0}) + "\n"
        + json.dumps({"session_id": "s1", "state_path": sp, "written_at": 50.0}) + "\n",
    )
    assert ledger_state_files_for_sid(fresh_root, "s1") == [Path(sp)]
    cached_index = runs_dir._RUN_STATE_LEDGER_CACHE[str(fresh_root)][1]
    assert cached_index["s1"] == [(100.0, sp)]


# --------------------------------------------------------------------------- #
# ledger-open OSError on the rebuild body, as a non-owner joiner
# --------------------------------------------------------------------------- #


def test_joiner_ledger_open_oserror_returns_empty(fresh_root):
    # Ledger path is a directory: stat succeeds (signature non-None) but the
    # rebuild's ledger.open() raises IsADirectoryError (OSError). As a joiner
    # (owner=False) the handler must NOT finish a rebuild it doesn't own and
    # just return [].
    _ledger(fresh_root).mkdir(parents=True, exist_ok=True)
    _seed_non_owner(fresh_root)
    assert ledger_state_files_for_sid(fresh_root, "s1") == []
    # Joiner never owned the rebuild -> inflight flag untouched (still set).
    assert str(fresh_root) in runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT


# --------------------------------------------------------------------------- #
# signature drift, as a non-owner joiner, recurses and stamps the final sig
# --------------------------------------------------------------------------- #


def test_joiner_signature_drift_recurses_and_stamps_final_signature(fresh_root):
    sp = _make_state(fresh_root, "run-A", "s1")
    ledger = _ledger(fresh_root)
    _write(ledger, json.dumps({"session_id": "s1", "state_path": sp, "written_at": 1.0}) + "\n")
    sig_a = _sig(fresh_root)
    sig_b = tuple(v + 1 for v in sig_a)  # drifted signature
    _seed_non_owner(fresh_root)

    # First signature read (initial) -> sig_a; every later read -> sig_b so the
    # outer call drifts and recurses, and the recursive call stabilises on sig_b.
    real_sig = _run_state_ledger_signature
    state = {"n": 0}

    def sig_side(_ledger_path):
        state["n"] += 1
        if state["n"] > 10:
            raise AssertionError("signature-drift recursion runaway")
        return sig_a if state["n"] == 1 else sig_b

    runs_dir._run_state_ledger_signature = sig_side
    try:
        assert ledger_state_files_for_sid(fresh_root, "s1") == [Path(sp)]
    finally:
        runs_dir._run_state_ledger_signature = real_sig

    # The cache must carry the FINAL stable signature (sig_b), not the stale
    # initial one (sig_a) — the observable contract of drift-safe caching.
    cached_sig, _index = runs_dir._RUN_STATE_LEDGER_CACHE[str(fresh_root)]
    assert cached_sig == sig_b


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

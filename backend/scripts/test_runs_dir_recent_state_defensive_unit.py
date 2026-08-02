"""Hermetic unit tests for ``runs_dir`` recent-state + backfill DEFENSIVE branches.

These are the security/concurrency-correctness guards the index-cluster test
(``test_runs_dir_recent_state_unit.py``) leaves cold because they only fire on
filesystem failure, cache-handoff races, or non-current backfill markers:

* ``recent_state_files_for_sid`` / ``recent_state_index_for_root`` root-resolve
  OSError (root unresolvable) -> empty, never raises.
* ``_recent_state_index_for_root`` max-age-exhausted cache entry -> must NOT be
  reused; a fresh scan + rebuild runs instead (stale-cache-poisoning defense).
* ``_recent_state_index_for_root`` scan-returns-None guard -> empty and the
  failure is NOT written into the cache (no cache poisoning on scan failure).
* ``_recent_state_index_for_root`` concurrent-owner-published-during-scan reuse
  -> a result another owner published while we held the rebuild is reused
  verbatim without a redundant rebuild.
* ``_recent_state_scan`` root-resolve OSError (root_resolved is None) -> None.
* ``_backfill_run_state_ledger`` unresolvable root -> silent return; non-dict
  valid JSON under root -> skipped (the ``isinstance(data, dict)`` guard).
* ``ensure_run_state_ledger_backfilled`` in-lock double-checked marker guard ->
  a marker that appears between the unlocked and locked checks returns False
  without doing any scan/append work.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir via
``_test_home.isolate``; nothing touches a real home. Module-level caches are
reset between tests so order never matters.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_recent_state_defensive_unit.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-recent-defensive-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
from runs_dir import (  # noqa: E402
    _backfill_run_state_ledger,
    _recent_state_scan,
    _root_signature,
    ensure_run_state_ledger_backfilled,
    recent_state_files_for_sid,
    recent_state_index_for_root,
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
    path = Path(tempfile.mkdtemp(prefix="bc-rd-defensive-root-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _make_state(root: Path, run_id: str, sid: str) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {"session_id": sid, "jsonl_path": str(run_dir / "events.jsonl")}
    sp = run_dir / "state.json"
    sp.write_text(json.dumps(state), encoding="utf-8")
    return sp


# A Path whose resolve() always raises OSError — lets us exercise the
# "root unresolvable" fail-closed branches without poking the filesystem.
_UnresolvablePathBase = type(Path())


class _UnresolvablePath(_UnresolvablePathBase):
    def resolve(self, *args, **kwargs):  # type: ignore[override]
        raise OSError("simulated unresolvable root")


# --------------------------------------------------------------------------- #
# root-resolve OSError fail-closed branches
# --------------------------------------------------------------------------- #


def test_recent_state_files_for_sid_unresolvable_root_returns_empty(fresh_root):
    # resolve() raises -> OSError branch -> [] without ever scanning.
    assert recent_state_files_for_sid(_UnresolvablePath(str(fresh_root)), "s1") == []


def test_recent_state_index_for_root_unresolvable_root_returns_empty(fresh_root):
    assert recent_state_index_for_root(_UnresolvablePath(str(fresh_root))) == {}


def test_recent_state_scan_unresolvable_root_with_unresolved_returns_none(fresh_root):
    # root_resolved=None forces the internal resolve(); OSError -> None.
    assert _recent_state_scan(_UnresolvablePath(str(fresh_root)), None) is None


# --------------------------------------------------------------------------- #
# max-age-exhausted cache entry must NOT be reused (stale-cache defense)
# --------------------------------------------------------------------------- #


def test_recent_index_beyond_max_age_is_not_reused_and_rescans(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    rsig = _root_signature(fresh_root)
    # Seed a stale cache entry past MAX_AGE carrying a WRONG index. If the
    # reuse guard wrongly honored it, we'd get the bogus path back.
    bogus = Path("/tmp/bc-rd-defensive-bogus/state.json")
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE[str(fresh_root)] = (
        time.monotonic() - runs_dir._RUN_STATE_RECENT_INDEX_MAX_AGE_S - 1.0,
        ((1, 1, str(sp)),),  # fingerprint unused once MAX_AGE exceeded
        {"s1": [bogus]},
        rsig,
        (),
    )
    # Beyond MAX_AGE -> reuse skipped -> fresh scan + rebuild returns the real path.
    assert recent_state_files_for_sid(fresh_root, "s1") == [sp]
    assert bogus != sp


# --------------------------------------------------------------------------- #
# scan-returns-None guard: empty result, no cache poisoning
# --------------------------------------------------------------------------- #


def test_recent_index_scan_none_returns_empty_without_poisoning_cache(tmp_path):
    # A regular file as "root": stat succeeds (signature non-None) but scandir
    # raises NotADirectoryError -> _recent_state_scan returns None -> the guard
    # yields {} and, critically, writes nothing into the cache.
    file_root = tmp_path / "not-a-dir"
    file_root.write_text("x", encoding="utf-8")
    assert recent_state_files_for_sid(file_root, "s1") == []
    assert str(file_root) not in runs_dir._RUN_STATE_RECENT_INDEX_CACHE


# --------------------------------------------------------------------------- #
# concurrent-owner-published-during-scan reuse
# --------------------------------------------------------------------------- #


def test_recent_index_reuses_index_published_by_concurrent_owner_during_scan(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    root_resolved = fresh_root.resolve()
    rsig = _root_signature(fresh_root)
    real_fingerprint, real_pending = _recent_state_scan(fresh_root, root_resolved)

    # Simulate another owner publishing a matching index into the cache WHILE we
    # hold the rebuild and scan. The published entry is TTL-fresh, so after our
    # scan the re-check must reuse it verbatim instead of rebuilding.
    published_index = {"s1": [sp]}

    real_scan = _recent_state_scan
    real_build = runs_dir._build_recent_state_index

    def scan_side_effect(root, root_resolved_arg=None):
        fp, pending = real_scan(root, root_resolved_arg)
        runs_dir._RUN_STATE_RECENT_INDEX_CACHE[str(root)] = (
            time.monotonic(),
            fp,
            published_index,
            rsig,
            pending,
        )
        return fp, pending

    def forbidden_build(*_a, **_k):
        raise AssertionError("must reuse published index, not rebuild")

    runs_dir._recent_state_scan = scan_side_effect
    runs_dir._build_recent_state_index = forbidden_build
    try:
        assert recent_state_files_for_sid(fresh_root, "s1") == [sp]
    finally:
        runs_dir._recent_state_scan = real_scan
        runs_dir._build_recent_state_index = real_build


# --------------------------------------------------------------------------- #
# _backfill_run_state_ledger guards
# --------------------------------------------------------------------------- #


def test_backfill_run_state_ledger_unresolvable_root_returns_silently(fresh_root):
    sp = _make_state(fresh_root, "run-1", "s1")
    # resolve() raises -> caught by the broad Exception guard -> silent return,
    # no ledger touched.
    _backfill_run_state_ledger(_UnresolvablePath(str(fresh_root)), {"s1": [sp]})
    assert not run_state_ledger_path(fresh_root).exists()


def test_backfill_run_state_ledger_skips_non_dict_valid_json(fresh_root):
    # A state.json under root holding valid-but-non-dict JSON: parses fine, so
    # the read succeeds, but the isinstance(data, dict) guard skips the append.
    nondict = fresh_root / "run-x" / "state.json"
    nondict.parent.mkdir(parents=True)
    nondict.write_text("[1, 2, 3]", encoding="utf-8")
    _backfill_run_state_ledger(fresh_root, {"s1": [nondict]})
    assert not run_state_ledger_path(fresh_root).exists()


# --------------------------------------------------------------------------- #
# ensure_run_state_ledger_backfilled in-lock double-checked marker guard
# --------------------------------------------------------------------------- #


def test_ensure_backfill_inlock_marker_appears_between_checks_returns_false(fresh_root):
    # First (unlocked) check: not current. Second (in-lock) check: current.
    # Models a marker landing between the two checks -> return False with no
    # scan/append work, proving the double-checked lock short-circuits cleanly.
    real_current = runs_dir._run_state_ledger_backfill_current
    calls = {"n": 0}

    def toctou_current(root):
        calls["n"] += 1
        return calls["n"] >= 2  # False on first check, True on second

    runs_dir._run_state_ledger_backfill_current = toctou_current
    try:
        assert ensure_run_state_ledger_backfilled(fresh_root) is False
    finally:
        runs_dir._run_state_ledger_backfill_current = real_current
    # No backfill body ran: no ledger rows, no scandir-driven marker write.
    assert not run_state_ledger_path(fresh_root).exists()
    assert calls["n"] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

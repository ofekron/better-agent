"""Hermetic unit tests for the ``ledger_state_files_for_sid`` joiner / defensive
branches in ``runs_dir``.

These cover the rebuild-concurrency and fail-closed edges the happy-path and
owner-path tests leave out:

  * ``root.resolve()`` raising ``OSError`` -> fail-closed ``[]``.
  * The rebuild JOINER path (a caller that lost the rebuild race): after waiting
    for the owner, it must consume whichever cache the owner published — either
    the sqlite cache (via ``_load_run_state_cache_for_sid``) or the in-memory
    cache (post-wait recheck) — and must NOT call ``_finish_run_state_cache_rebuild``
    (only the owner finishes).
  * The ledger scan skipping an older duplicate ``(sid, state_path)`` row so the
    newest ``written_at`` wins.
  * OSError during the scan on the JOINER path (owner=False -> no finish).
  * Signature drift on the JOINER path (owner=False -> recurse, no finish).

The joiner race windows (cache absent at the first check, present after the
owner's wait completes) are reproduced deterministically by side-effecting the
module caches from a stubbed ``_load_run_state_cache_for_sid`` — the same
stubbing style the sibling cache tests use for ``_run_state_ledger_signature``.
Every assertion is fail-on-bug: the discriminating value differs when the
guarded branch is inverted or removed.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir via
``_test_home.isolate``; nothing touches a real home. Module-level caches are
reset between tests so order never matters.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_ledger_joiner_unit.py -v
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

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-ledger-joiner-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
from runs_dir import (  # noqa: E402
    _run_state_ledger_signature,
    ledger_state_files_for_sid,
    run_state_ledger_path,
)


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """runs_dir keeps process-global caches; clear them per test for order
    independence and to keep rebuild-inflight state local to each test."""
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
    """A fresh per-test root directory (layout-controlled)."""
    path = Path(tempfile.mkdtemp(prefix="bc-rd-ledger-joiner-"))
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


def _make_state(root: Path, run_id: str, sid: str) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return _write(run_dir / "state.json", json.dumps({"session_id": sid}))


def _mark_joiner(root: Path) -> str:
    """Pre-seat a SET rebuild-inflight event so the next caller is a joiner
    (loses the rebuild race, owner=False) and its ``event.wait()`` returns
    immediately. Returns the root key."""
    key = str(root)
    done = threading.Event()
    done.set()
    runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT[key] = done
    return key


# --------------------------------------------------------------------------- #
# root.resolve() OSError -> fail-closed []
# --------------------------------------------------------------------------- #


def test_ledger_resolve_oserror_returns_empty(fresh_root):
    """The ledger exists (signature non-None) but ``root.resolve()`` raises
    ``OSError`` -> fail-closed ``[]``. Proven via a PosixPath subclass whose
    ``resolve()`` raises; every other pathlib op (``/``, ``stat``, ``str``)
    still works."""
    _touch_ledger(fresh_root)
    base = type(Path())

    class _UnresolvablePath(base):
        def resolve(self, *args, **kwargs):
            raise OSError("unresolvable root")

    unresolvable = _UnresolvablePath(base(str(fresh_root)))
    assert ledger_state_files_for_sid(unresolvable, "s1") == []


# --------------------------------------------------------------------------- #
# Joiner: post-wait sqlite cache hit
# --------------------------------------------------------------------------- #


def test_ledger_joiner_post_wait_sqlite_cache_hit(fresh_root):
    """Joiner path (line 587-589): after waiting, the owner has published the
    sqlite cache, so ``_load_run_state_cache_for_sid`` returns paths on the
    second call and the joiner returns them verbatim. The owner-published path
    does not appear in the (empty) ledger, so falling through to a scan would
    wrongly return ``[]``."""
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _touch_ledger(fresh_root)  # empty ledger -> scan would return []
    _mark_joiner(fresh_root)
    calls = {"n": 0}
    orig = runs_dir._load_run_state_cache_for_sid

    def fake_load(root, signature, agent_sid, root_resolved):
        calls["n"] += 1
        # First call (pre-wait, line 581): no cache yet. Second call
        # (post-wait, line 587): owner published the sqlite cache.
        return None if calls["n"] == 1 else [Path(sp)]

    runs_dir._load_run_state_cache_for_sid = fake_load
    try:
        result = ledger_state_files_for_sid(fresh_root, "s1")
    finally:
        runs_dir._load_run_state_cache_for_sid = orig
    assert result == [Path(sp)]
    assert calls["n"] == 2  # the post-wait recheck actually ran


# --------------------------------------------------------------------------- #
# Joiner: post-wait in-memory cache hit
# --------------------------------------------------------------------------- #


def test_ledger_joiner_post_wait_memory_cache_hit(fresh_root):
    """Joiner path (lines 590-595): after waiting, the owner published only the
    in-memory cache (sqlite still absent). The post-wait recheck finds the
    memory entry with a matching signature and returns its paths. Removing the
    recheck would fall through to a scan of the empty ledger -> ``[]``."""
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _touch_ledger(fresh_root)
    _mark_joiner(fresh_root)
    seeded = {"v": False}
    orig = runs_dir._load_run_state_cache_for_sid

    def fake_load(root, signature, agent_sid, root_resolved):
        # Owner publishes the memory cache during the joiner's wait window;
        # the sqlite cache never materializes.
        if not seeded["v"]:
            seeded["v"] = True
            runs_dir._RUN_STATE_LEDGER_CACHE[str(root)] = (signature, {"s1": [(1.0, sp)]})
        return None

    runs_dir._load_run_state_cache_for_sid = fake_load
    try:
        result = ledger_state_files_for_sid(fresh_root, "s1")
    finally:
        runs_dir._load_run_state_cache_for_sid = orig
    assert result == [Path(sp)]


# --------------------------------------------------------------------------- #
# Joiner: post-wait in-memory cache present but stale -> rescan
# --------------------------------------------------------------------------- #


def test_ledger_joiner_post_wait_memory_cache_stale_signature_rescans(fresh_root):
    """Line 594->596: the owner published a memory cache, but its signature no
    longer matches the ledger (drifted). The joiner must NOT serve the stale
    memory entry — it falls through to a fresh scan. Proven by seeding the
    stale memory with a different path (``sp2``) than the ledger (``sp``); a
    missing/inverted guard would return the stale ``[sp2]``."""
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    sp2 = str(_make_state(fresh_root, "run-2", "s1"))  # stale-memory decoy
    _write(_ledger(fresh_root), json.dumps({"session_id": "s1", "state_path": sp, "written_at": 1.0}) + "\n")
    _mark_joiner(fresh_root)
    real_sig = _sig(fresh_root)
    stale_sig = tuple(v + 9 for v in real_sig)
    seeded = {"v": False}
    orig = runs_dir._load_run_state_cache_for_sid

    def fake_load(root, signature, agent_sid, root_resolved):
        if not seeded["v"]:
            seeded["v"] = True
            # Owner published a memory cache carrying a STALE signature.
            runs_dir._RUN_STATE_LEDGER_CACHE[str(root)] = (stale_sig, {"s1": [(1.0, sp2)]})
        return None

    runs_dir._load_run_state_cache_for_sid = fake_load
    try:
        result = ledger_state_files_for_sid(fresh_root, "s1")
    finally:
        runs_dir._load_run_state_cache_for_sid = orig
    assert result == [Path(sp)]  # scan result, not the stale decoy


# --------------------------------------------------------------------------- #
# Scan: older duplicate (sid, state_path) row is skipped
# --------------------------------------------------------------------------- #


def test_ledger_scan_skips_older_duplicate_written_at(fresh_root):
    """Line 615->600: a second ledger row for the same ``(sid, state_path)``
    with an OLDER ``written_at`` is skipped, so the newest stamp wins. Proven by
    inspecting the rebuilt in-memory index: a missing/inverted guard would let
    the older stamp (2.0) overwrite the newer one (5.0)."""
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    rows = [
        json.dumps({"session_id": "s1", "state_path": sp, "written_at": 5.0}),
        json.dumps({"session_id": "s1", "state_path": sp, "written_at": 2.0}),
    ]
    _write(_ledger(fresh_root), "\n".join(rows) + "\n")
    assert ledger_state_files_for_sid(fresh_root, "s1") == [Path(sp)]
    cached_index = runs_dir._RUN_STATE_LEDGER_CACHE[str(fresh_root)][1]
    assert cached_index["s1"] == [(5.0, sp)]


# --------------------------------------------------------------------------- #
# Joiner: OSError during scan must not finish the rebuild
# --------------------------------------------------------------------------- #


def test_ledger_scan_oserror_joiner_does_not_finish(fresh_root):
    """Lines 621->623: when the ledger cannot be opened (it is a directory) on
    the JOINER path (owner=False), the joiner returns ``[]`` and must NOT call
    ``_finish_run_state_cache_rebuild`` — only the owner publishes completion.
    Proven by the inflight event still being registered after the call."""
    _ledger(fresh_root).mkdir(parents=True, exist_ok=True)  # stat ok, open() raises
    key = _mark_joiner(fresh_root)
    assert ledger_state_files_for_sid(fresh_root, "s1") == []
    assert key in runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT


# --------------------------------------------------------------------------- #
# Joiner: signature drift must recurse, not finish
# --------------------------------------------------------------------------- #


def test_ledger_signature_drift_joiner_does_not_finish(fresh_root):
    """Lines 634->637: on the JOINER path (owner=False), a signature drift
    between the pre-scan and post-scan read must recurse (re-read with the
    fresh signature) and must NOT call ``_finish_run_state_cache_rebuild``.
    Proven by the inflight event still being registered AND the recursed
    rebuild still returning the ledger paths."""
    sp = str(_make_state(fresh_root, "run-1", "s1"))
    _write(_ledger(fresh_root), json.dumps({"session_id": "s1", "state_path": sp, "written_at": 1.0}) + "\n")
    key = _mark_joiner(fresh_root)
    real_sig = _sig(fresh_root)
    calls = {"n": 0}
    orig = runs_dir._run_state_ledger_signature

    def drifting(path):
        calls["n"] += 1
        # Pre-scan read stale; every later read (post-scan + recursion) real.
        return tuple(v - 1 for v in real_sig) if calls["n"] == 1 else real_sig

    runs_dir._run_state_ledger_signature = drifting
    try:
        result = ledger_state_files_for_sid(fresh_root, "s1")
    finally:
        runs_dir._run_state_ledger_signature = orig
    assert result == [Path(sp)]
    assert key in runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

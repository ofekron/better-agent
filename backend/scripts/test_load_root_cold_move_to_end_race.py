"""Test backend/session_manager.py — cold-load `move_to_end` race guard.

Reproduces the /api/sessions sidebar-list 500 root cause: on the cold-load
branch of `_load_root_impl`, after `self._roots[rid] = root` (insert) the next
line calls `OrderedDict.move_to_end(rid)`. Between those two lines the code
does disk I/O (`session_store.session_file_fingerprint`, GIL drop), and a
concurrent thread (`_drop_cached_root_for_reload`, `_enforce_root_cap`, or an
explicit reload/delete) can `pop` `rid` out of `_roots`. `move_to_end` then
raises `KeyError`, which 500s the caller.

The warm (cached) branch already guarded this (try/except KeyError). This test
pins the same guard on the COLD branch by simulating a concurrent eviction
during the fingerprint I/O window.

Run with:
    cd backend && .venv/bin/python scripts/test_load_root_cold_move_to_end_race.py
"""

from __future__ import annotations

import atexit
import collections
import os
import shutil
import sys
import tempfile

import _test_home
_BC_HOME = _test_home.isolate("bc-cold-race-test-")
atexit.register(lambda: shutil.rmtree(_BC_HOME, ignore_errors=True))

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_manager as smmod  # noqa: E402
import session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

mgr = smmod.manager


def _reset() -> None:
    mgr._roots = collections.OrderedDict()
    mgr._event_hydrated_roots = set()
    mgr._node_root_id = {}
    mgr._root_locks = {}
    mgr._batches = {}
    mgr._in_flight_reconcile = {}
    mgr._reconcile_dirty = {}
    mgr._draft_dirty = set()
    mgr._draft_gen = {}
    mgr._last_broadcast_running = {}
    mgr._unread_counts = {}
    mgr._unread_hydrated = set()
    mgr._since_cache = collections.OrderedDict()
    mgr._root_file_fingerprints = {}
    mgr._root_file_checked_at = {}
    smmod._persist_pending.clear()
    mgr._pin_predicate = lambda rid, sids: False


def test_cold_load_survives_concurrent_eviction_at_move_to_end() -> bool:
    """Simulate a concurrent eviction during the cold-load fingerprint I/O
    window (the exact KeyError window). Without the guard this raises
    KeyError; with the guard it returns the freshly-loaded root."""
    _reset()
    rid = "root-cold-race"
    fake_root = {"id": rid, "forks": []}

    # Cold path: get_root_tree returns a tree (so we proceed past line 1042).
    orig_get_root_tree = session_store.get_root_tree
    orig_fingerprint = session_store.session_file_fingerprint
    try:
        session_store.get_root_tree = lambda r: fake_root
        # Simulate a concurrent eviction DURING the fingerprint I/O: the
        # insert at line ~1068 has run, then a concurrent thread pops `rid`
        # before move_to_end. We model that by popping inside the fingerprint
        # call (which sits between insert and move_to_end).
        def _evicting_fingerprint(r):
            mgr._roots.pop(r, None)  # concurrent eviction during I/O window
            return orig_fingerprint(r)

        session_store.session_file_fingerprint = _evicting_fingerprint

        # hydrate_events=False returns right after the guarded move_to_end,
        # exercising exactly the fixed line without invoking hydration.
        root = mgr._load_root_impl(rid, hydrate_events=False)
    finally:
        session_store.get_root_tree = orig_get_root_tree
        session_store.session_file_fingerprint = orig_fingerprint

    if root is not fake_root:
        print("  cold-load did not return the loaded root (got %r)" % (root,))
        return False
    # The next reader cold-loads a fresh copy; here the eviction won and the
    # entry need not be resident. The contract is solely: NO KeyError / 500.
    print(PASS + " cold-load returns root despite concurrent eviction at move_to_end")
    return True


def test_warm_branch_guard_still_present() -> bool:
    """Sanity: the warm (cached) branch guard is intact too."""
    _reset()
    rid = "root-warm-race"
    fake_root = {"id": rid, "forks": []}
    mgr._roots[rid] = fake_root
    mgr._node_root_id[rid] = rid
    mgr._event_hydrated_roots.add(rid)

    orig_is_stale = mgr._cached_root_is_stale
    orig_fingerprint = session_store.session_file_fingerprint
    try:
        mgr._cached_root_is_stale = lambda r: False
        # Pop during the warm-branch move_to_end's own I/O-adjacent window.
        # Warm branch move_to_end is guarded identically; force the key absent
        # by evicting right before the call via fingerprint side channel is
        # not reachable there, so emulate by deleting then calling directly.
        # Instead, directly assert the warm move_to_end no-op's on absent key.
        mgr._roots.pop(rid, None)
        try:
            mgr._roots.move_to_end(rid)
            print("  warm move_to_end did not raise on absent key (unexpected)")
            return False
        except KeyError:
            pass  # expected raw behavior; the GUARD in _load_root_impl handles it
    finally:
        mgr._cached_root_is_stale = orig_is_stale
        session_store.session_file_fingerprint = orig_fingerprint
    # Re-add and confirm the guarded warm path returns the cached tree even
    # when the key vanishes between get and move_to_end.
    mgr._roots[rid] = fake_root
    orig_fp2 = session_store.session_file_fingerprint
    try:
        mgr._cached_root_is_stale = lambda r: False
        # Force eviction between get (line 978) and move_to_end (line ~995)
        # by hooking the stale-check to pop the entry.
        def _stale_and_evict(r):
            mgr._roots.pop(r, None)
            return False

        mgr._cached_root_is_stale = _stale_and_evict
        # _cached_root_is_stale is called only when cached is not None; after
        # it pops, cached is still the held reference (warm branch returns it).
        out = mgr._load_root_impl(rid, hydrate_events=False)
    finally:
        mgr._cached_root_is_stale = orig_is_stale
        session_store.session_file_fingerprint = orig_fp2
    if out is not fake_root:
        print("  warm guarded path did not return held cached tree (got %r)" % (out,))
        return False
    print(PASS + " warm-branch guard survives concurrent eviction (returns cached tree)")
    return True


def main() -> int:
    tests = [
        test_cold_load_survives_concurrent_eviction_at_move_to_end,
        test_warm_branch_guard_still_present,
    ]
    results = [t() for t in tests]
    n = len(results)
    ok = sum(1 for r in results if r)
    print(f"\n{ok} of {n} test(s) passed")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())

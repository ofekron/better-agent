"""Test backend/session_manager.py — LRU cap on the `_roots` cache.

Locks the contract of the new eviction:
  • the cache is bounded to `_roots_max` UNPINNED roots;
  • LRU order — oldest unpinned evicted first; `move_to_end` on access
    protects a recently-used root;
  • PINNED roots (predicate or local signal) are NEVER evicted, even
    past the cap (the cap is soft against pins — active sessions are
    never starved);
  • fail-closed when the pin predicate is unbound;
  • a busy per-root lock is skipped (treated as in-use);
  • eviction tears down the root's in-memory footprint but KEEPS the
    per-root lock (reload identity).

Run with:
    cd backend && .venv/bin/python scripts/test_root_lru_cap.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import threading

import _test_home
_BC_HOME = _test_home.isolate("bc-lru-test-")
atexit.register(lambda: shutil.rmtree(_BC_HOME, ignore_errors=True))

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import collections  # noqa: E402

import session_manager as smmod  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

mgr = smmod.manager


def _fake_root(rid: str) -> dict:
    return {"id": rid, "forks": []}


def _reset(pin_predicate=None) -> None:
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
    smmod._persist_pending.clear()
    mgr._pin_predicate = pin_predicate


def _fill(n: int) -> list:
    rids = []
    for i in range(n):
        rid = f"root-{i:03d}"
        mgr._roots[rid] = _fake_root(rid)
        mgr._node_root_id[rid] = rid
        rids.append(rid)
    return rids


def test_cap_evicts_oldest_unpinned() -> bool:
    _reset(pin_predicate=lambda rid, sids: False)
    cap = mgr._roots_max
    rids = _fill(cap + 5)
    mgr._enforce_root_cap(keep_rid="__none__")
    if len(mgr._roots) != cap:
        print(f"  expected {cap} after enforce, got {len(mgr._roots)}")
        return False
    evicted, kept = rids[:5], rids[5:]
    if any(r in mgr._roots for r in evicted):
        print("  the 5 oldest were not the ones evicted")
        return False
    if not all(r in mgr._roots for r in kept):
        print("  a newer root was wrongly evicted")
        return False
    if any(r in mgr._node_root_id for r in evicted):
        print("  _node_root_id not cleared for evicted roots")
        return False
    return True


def test_lru_recency_protects_accessed_root() -> bool:
    _reset(pin_predicate=lambda rid, sids: False)
    cap = mgr._roots_max
    rids = _fill(cap + 1)
    oldest = rids[0]
    mgr._roots.move_to_end(oldest)   # simulate an access → most-recent
    mgr._enforce_root_cap(keep_rid="__none__")
    if oldest not in mgr._roots:
        print("  recently-accessed root was wrongly evicted")
        return False
    if rids[1] in mgr._roots:
        print("  expected the new-oldest root to be evicted")
        return False
    return True


def test_pinned_never_evicted() -> bool:
    cap = mgr._roots_max
    pinned = {f"root-{i:03d}" for i in range(5)}   # the 5 oldest
    _reset(pin_predicate=lambda rid, sids: rid in pinned)
    _fill(cap + 5)
    mgr._enforce_root_cap(keep_rid="__none__")
    if not all(r in mgr._roots for r in pinned):
        print("  a pinned root was evicted")
        return False
    if len(mgr._roots) != cap:
        print(f"  expected {cap} resident, got {len(mgr._roots)}")
        return False
    if any(f"root-{i:03d}" in mgr._roots for i in range(5, 10)):
        print("  expected the oldest UNPINNED roots to be evicted")
        return False
    return True


def test_all_pinned_cap_is_soft() -> bool:
    cap = mgr._roots_max
    _reset(pin_predicate=lambda rid, sids: True)
    _fill(cap + 5)
    mgr._enforce_root_cap(keep_rid="__none__")
    if len(mgr._roots) != cap + 5:
        print(f"  pinned roots were evicted (got {len(mgr._roots)})")
        return False
    return True


def test_local_pin_signal_blocks_eviction() -> bool:
    cap = mgr._roots_max
    _reset(pin_predicate=lambda rid, sids: False)
    rids = _fill(cap + 1)
    oldest = rids[0]
    mgr._in_flight_reconcile[oldest] = object()   # in-flight reconcile = pin
    mgr._enforce_root_cap(keep_rid="__none__")
    if oldest not in mgr._roots:
        print("  root with in-flight reconcile was evicted")
        return False
    if rids[1] in mgr._roots:
        print("  expected next-oldest to be evicted instead")
        return False
    return True


def test_fail_closed_when_predicate_unbound() -> bool:
    cap = mgr._roots_max
    _reset(pin_predicate=None)
    _fill(cap + 5)
    mgr._enforce_root_cap(keep_rid="__none__")
    if len(mgr._roots) != cap + 5:
        print(f"  fail-closed violated: evicted with no predicate ({len(mgr._roots)})")
        return False
    return True


def test_busy_lock_skipped() -> bool:
    cap = mgr._roots_max
    _reset(pin_predicate=lambda rid, sids: False)
    rids = _fill(cap + 1)
    oldest = rids[0]
    acquired = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        lk = mgr._lock_for_root(oldest)
        lk.acquire()
        acquired.set()
        release.wait(5)
        lk.release()

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    if not acquired.wait(5):
        print("  holder thread never acquired the lock")
        return False
    try:
        mgr._enforce_root_cap(keep_rid="__none__")
    finally:
        release.set()
        t.join(5)
    if oldest not in mgr._roots:
        print("  busy-locked root was evicted (should be skipped)")
        return False
    if rids[1] in mgr._roots:
        print("  expected next-oldest evicted instead of the busy one")
        return False
    return True


def test_eviction_clears_state_but_keeps_lock() -> bool:
    _reset(pin_predicate=lambda rid, sids: False)
    cap = mgr._roots_max
    rids = _fill(cap + 1)
    victim = rids[0]
    mgr._event_hydrated_roots.add(victim)
    mgr._since_cache[victim] = (0, {})
    mgr._unread_counts[victim] = 3
    mgr._unread_hydrated.add(victim)
    mgr._last_broadcast_running[victim] = True
    mgr._draft_gen[victim] = 1   # NOT a pin signal (unlike _draft_dirty)
    _ = mgr._lock_for_root(victim)
    mgr._enforce_root_cap(keep_rid="__none__")
    if victim in mgr._roots:
        print("  victim not evicted")
        return False
    cleared = {
        "_event_hydrated_roots": mgr._event_hydrated_roots,
        "_since_cache": mgr._since_cache,
        "_unread_counts": mgr._unread_counts,
        "_unread_hydrated": mgr._unread_hydrated,
        "_last_broadcast_running": mgr._last_broadcast_running,
        "_draft_gen": mgr._draft_gen,
        "_node_root_id": mgr._node_root_id,
    }
    for name, container in cleared.items():
        if victim in container:
            print(f"  {name} not cleared for evicted victim")
            return False
    if victim not in mgr._root_locks:
        print("  _root_locks wrongly dropped on eviction (breaks reload identity)")
        return False
    return True


def test_eviction_closes_event_ingester() -> bool:
    from event_ingester import event_ingester as ei
    _reset(pin_predicate=lambda rid, sids: False)
    rids = _fill(mgr._roots_max + 1)
    victim = rids[0]
    closed = []
    ei.close = lambda root_id: closed.append(root_id)   # spy (real clear proven elsewhere)
    try:
        mgr._enforce_root_cap(keep_rid="__none__")
    finally:
        try:
            del ei.close
        except AttributeError:
            pass
    if victim in mgr._roots:
        print("  victim not evicted")
        return False
    if victim not in closed:
        print(f"  event_ingester.close NOT called for evicted root (closed={closed})")
        return False
    return True


TESTS = [
    ("cap evicts the oldest unpinned roots", test_cap_evicts_oldest_unpinned),
    ("LRU recency protects a recently-accessed root",
     test_lru_recency_protects_accessed_root),
    ("pinned roots are never evicted", test_pinned_never_evicted),
    ("cap is soft when everything is pinned", test_all_pinned_cap_is_soft),
    ("local pin signal (in-flight reconcile) blocks eviction",
     test_local_pin_signal_blocks_eviction),
    ("fail-closed when pin predicate is unbound",
     test_fail_closed_when_predicate_unbound),
    ("a busy per-root lock is skipped", test_busy_lock_skipped),
    ("eviction clears state but keeps the per-root lock",
     test_eviction_clears_state_but_keeps_lock),
    ("eviction closes event_ingester per-root state (#2 reclaim)",
     test_eviction_closes_event_ingester),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())

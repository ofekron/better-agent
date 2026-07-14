"""recompute_state (driven by tick_running_state on every GET /api/sessions)
must NOT cold-load + hydrate an evicted root's events.jsonl. That scan runs
up to ~21s ON the caller's thread — and tick runs synchronously from async
REST handlers, so it froze the event loop and stalled every request.

`monitoring_state`/`is_running` read only in-memory run-state, so recompute
needs the tree solely for the `kind` gate — a light read, no hydration.

Run with:
    cd backend && .venv/bin/python scripts/test_recompute_no_hydrate.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-recompute-nohydrate-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from session_manager import manager as sm  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def main() -> int:
    ok = True
    try:
        sm.bind_running_check(lambda sid: True)  # always "running"

        sess = session_store.create_session(name="u", model="m", cwd="/tmp")
        sid = sess["id"]
        # Give the root an events.jsonl so an accidental hydrate has work.
        from event_ingester import event_ingester
        for i in range(5):
            event_ingester.ingest(
                sid, sid=sid, event_type="agent_message",
                data={"uuid": f"u{i}", "type": "assistant",
                      "message": {"content": []}},
                source="t", run_id=None, msg_id="m1",
            )

        fires: list[dict] = []
        sm.add_listener(
            lambda s, ch: fires.append(ch)
            if s == sid and ch.get("kind") == "monitoring_changed" else None,
        )

        # Load once so `_index_root` populates `_kind_by_sid[sid]`, then
        # evict ONLY the resident tree (pop `_roots`) — the kind cache
        # survives eviction by design. This makes the FIRST recompute a
        # genuine cache-HIT, the second (after we pop the cache) the
        # disk-fallback path, so both are exercised.
        rid = sm._root_id_for(sid)
        sm._load_root(sid)
        assert sid in sm._kind_by_sid, "setup: kind should be cached after load"
        with sm._lock_for_root(rid):
            sm._roots.pop(rid, None)

        hydrate_calls: list[str] = []
        writes: list[str] = []
        orig = sm._hydrate_cached_root_events
        owf = session_store.write_session_full
        sm._hydrate_cached_root_events = (
            lambda r, root: hydrate_calls.append(r) or orig(r, root)
        )
        session_store.write_session_full = (
            lambda *a, **k: writes.append("tree") or owf(*a, **k)
        )

        def _restore_spies():
            sm._hydrate_cached_root_events = orig
            session_store.write_session_full = owf

        try:
            sm.recompute_state(sid)  # cache-HIT path (kind cached via _load_root)
            # Cache-MISS path: forget the cached kind, force the pure-read
            # fallback — it must also do zero disk writes and not re-cache.
            sm._kind_by_sid.pop(sid, None)
            sm.recompute_state(sid)
        finally:
            _restore_spies()

        ok = _check(
            hydrate_calls == [],
            "recompute on evicted root does NOT hydrate events.jsonl",
            f"hydrate_calls={hydrate_calls}",
        ) and ok
        ok = _check(
            writes == [],
            "recompute on evicted root does ZERO disk writes (no migrate/seed)",
            f"writes={writes}",
        ) and ok
        ok = _check(
            rid not in sm._roots,
            "recompute does NOT cold-cache the evicted root",
        ) and ok
        ok = _check(
            any(c.get("value") == "active" for c in fires),
            "monitoring_changed still fires for a live user session",
            str(fires),
        ) and ok

        # A worker-kind session is gated out (no badge) without hydration.
        wsess = session_store.create_session(name="w", model="m", cwd="/tmp")
        wid = wsess["id"]
        wroot = session_store.get_root_tree(wid)
        wroot["kind"] = "delegate_fork"
        session_store.write_session_full(wroot)
        wrid = sm._root_id_for(wid)
        with sm._lock_for_root(wrid):
            sm._roots.pop(wrid, None)
        wfires: list[dict] = []
        sm.add_listener(
            lambda s, ch: wfires.append(ch) if s == wid else None,
        )
        sm._hydrate_cached_root_events = (
            lambda r, root: hydrate_calls.append(r) or orig(r, root)
        )
        try:
            sm.recompute_state(wid)
        finally:
            sm._hydrate_cached_root_events = orig
        ok = _check(
            wfires == [] and wrid not in hydrate_calls,
            "worker session gated out (no fire, no hydrate)",
            f"wfires={wfires} hydrate={hydrate_calls}",
        ) and ok

        # The eviction-surviving kind cache must NOT survive a DELETE.
        dsess = session_store.create_session(name="d", model="m", cwd="/tmp")
        did = dsess["id"]
        sm._load_root(did)
        assert did in sm._kind_by_sid
        sm.delete(did)
        ok = _check(
            did not in sm._kind_by_sid,
            "delete drops the kind-cache entry (no leak)",
            str(did in sm._kind_by_sid),
        ) and ok

        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

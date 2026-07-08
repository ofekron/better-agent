"""`session_manager.latest_assistant_finalized` (called by
`event_ingester.ingest` for every orphan event) must NOT cold-load +
hydrate the root's events.jsonl. That hydration runs
`event_ingester._scan_summaries` over the whole file (~seconds on the
largest roots; measured ~10.8s on a 591MB/100K-line file) and stalls
the per-root shard thread, indirectly blocking the asyncio loop.

The check reads only message `role`/`isStreaming`, which the on-disk
snapshot already carries — event hydration populates `msg.events`, not
the messages list, so the thin load returns the same answer without the
scan. Mirrors the precedent in test_recompute_no_hydrate.py.

Run with:
    cd backend && PYTHONPATH=. python scripts/test_orphan_latest_assistant_no_hydrate.py
"""

from __future__ import annotations

import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-orphan-nohydrate-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from session_manager import manager as sm  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def main() -> int:
    ok = True
    try:
        sess = session_store.create_session(name="u", model="m", cwd="/tmp")
        sid = sess["id"]
        # Give the root an events.jsonl so a stray hydrate would have real
        # work to do (and so the cold-load path is exercised).
        from event_ingester import event_ingester
        for i in range(5):
            event_ingester.ingest(
                sid, sid=sid, event_type="agent_message",
                data={"uuid": f"u{i}", "type": "assistant",
                      "message": {"content": []}},
                source="t", run_id=None, msg_id="m1",
            )
        # Put a finalized assistant message on the session so the check has a
        # reason to return True. Persist + evict the resident tree so the next
        # read is a genuine cold load.
        sm.append_assistant_msg(
            sid, {"id": "m-finalized", "role": "assistant", "content": [], "isStreaming": False},
        )
        sm.flush_pending_persists()  # land the message on disk before eviction
        rid = sm._root_id_for(sid)
        sm._load_root(sid)  # warm + persist
        with sm._lock_for_root(rid):
            sm._roots.pop(rid, None)  # evict resident tree -> next read cold-loads

        hydrate_calls: list[str] = []
        original = sm._hydrate_cached_root_events

        def _spy(_rid, _root):
            hydrate_calls.append(_rid)
            return original(_rid, _root)

        sm._hydrate_cached_root_events = _spy
        try:
            result = sm.latest_assistant_finalized(sid)
        finally:
            sm._hydrate_cached_root_events = original

        # Functional correctness: finalized assistant present -> True.
        ok &= _check(result is True, "returns True for a finalized assistant")
        # The fix: the orphan check must NOT trigger event hydration.
        ok &= _check(
            not hydrate_calls,
            "does NOT cold-load + hydrate events.jsonl",
            f"_hydrate_cached_root_events called for {hydrate_calls}",
        )
    finally:
        pass
    print(("\n" + PASS + " all passed") if ok else (("\n" + FAIL + " failed")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Locks the O(N²) → O(N) apply_event dedup contract.

Pre-fix: linear scan through `evs` per call at `orchs/base.py:619`
and `:679` → cold-load hydration of 5268 events on session
`4ddbd4d7` took 5968 ms (~28 M comparisons).

Post-fix: `_uid_idx: dict[uuid, int]` cached on the events-list owner
(msg / panel) → O(1) lookup. Measured: 5000 events in
~60 ms inside a phantom batch (cold-load context).

This test asserts the perf invariant + correctness:

  1. 5000 unique apply_event(source_is_provider_stream=False) calls inside a phantom batch
     completes in < 1 s (was ~6 s).
  2. msg.events ends with 5000 entries (no double-append, no drop).
  3. uid_idx mirrors msg.events 1:1.
  4. Replay of the same event is idempotent (no growth).
  5. Same-uuid streaming replace preserves uid_idx (same uuid → same
     idx, data swapped in place).
  6. Manager mode also gets uid_idx (on the flat `msg`).
  7. set_native_events invalidates uid_idx (subsequent apply_event
     rebuilds before reading).

Run with:
    cd backend && .venv/bin/python scripts/test_apply_event_uid_idx_perf.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-uid-idx-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _ev(uid: str, text: str = "x") -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def _mgr_ev(uid: str, text: str = "x") -> dict:
    return {
        "type": "manager_event",
        "data": {
            "event": {
                "type": "agent_message",
                "data": {
                    "uuid": uid,
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": text}]},
                },
            },
        },
    }


def _mk_native() -> tuple[str, dict, str]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/uid-idx",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["id"] = "msg-1"
    scaffold["role"] = "assistant"
    scaffold["seq"] = 1
    session_manager.append_assistant_msg(sid, scaffold)
    msg = session_manager.get_ref(sid)["messages"][-1]
    return sid, msg, "msg-1"


def _mk_manager() -> tuple[str, dict, str]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/uid-idx",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["id"] = "msg-mgr-1"
    scaffold["role"] = "assistant"
    scaffold["seq"] = 1
    session_manager.append_assistant_msg(sid, scaffold)
    msg = session_manager.get_ref(sid)["messages"][-1]
    return sid, msg, "msg-mgr-1"


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    strategy = get_strategy("native")

    # 1) 5000 events inside phantom batch < 1 s.
    sid, msg, mid = _mk_native()
    rid = session_manager._root_id_for(sid)
    ctx = ApplyEventCtx(root_id=sid, run_id="r")
    N = 5000
    session_manager._batches[rid] = {"_phantom": True, "bump_updated_at": False}
    try:
        t0 = time.perf_counter()
        for i in range(N):
            strategy.apply_event(
                app_session_id=sid, msg=msg,
                event=_ev(f"u-{i}"), ctx=ctx, source_is_provider_stream=False,
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
    finally:
        session_manager._batches.pop(rid, None)
    results.append(
        (f"5000 apply_event(source_is_provider_stream=False) inside phantom batch < 1000ms",
         elapsed_ms < 1000.0, f"got {elapsed_ms:.1f}ms"))
    results.append(
        ("msg.events has 5000 entries",
         len(msg["events"]) == N, f"got {len(msg['events'])}"))
    uid_idx = msg.get("_uid_idx")
    results.append(
        ("_uid_idx mirrors msg.events 1:1",
         isinstance(uid_idx, dict) and len(uid_idx) == N,
         f"uid_idx len={len(uid_idx or {})}"))

    # 2) Idempotent replay.
    session_manager._batches[rid] = {"_phantom": True, "bump_updated_at": False}
    try:
        for i in range(N):
            strategy.apply_event(
                app_session_id=sid, msg=msg,
                event=_ev(f"u-{i}"), ctx=ctx, source_is_provider_stream=False,
            )
    finally:
        session_manager._batches.pop(rid, None)
    results.append(
        ("idempotent replay leaves msg.events unchanged",
         len(msg["events"]) == N, f"got {len(msg['events'])}"))

    # 3) Same-uuid streaming replace preserves uid_idx.
    sid2, msg2, _ = _mk_native()
    rid2 = session_manager._root_id_for(sid2)
    ctx2 = ApplyEventCtx(root_id=sid2, run_id="r")
    strategy.apply_event(
        app_session_id=sid2, msg=msg2,
        event=_ev("u-stream", text="chunk-1"), ctx=ctx2, source_is_provider_stream=False,
    )
    pre_idx = dict(msg2.get("_uid_idx") or {})
    strategy.apply_event(
        app_session_id=sid2, msg=msg2,
        event=_ev("u-stream", text="chunk-2"), ctx=ctx2, source_is_provider_stream=False,
    )
    post_idx = msg2.get("_uid_idx") or {}
    results.append(
        ("streaming replace: same uid, same idx, no len growth",
         pre_idx == post_idx and len(msg2["events"]) == 1,
         f"pre={pre_idx} post={post_idx} events_len={len(msg2['events'])}"))
    results.append(
        ("streaming replace: data is chunk-2",
         msg2["events"][0]["data"]["message"]["content"][0]["text"] == "chunk-2",
         "data not replaced"))

    # 4) Manager mode owner is the flat msg (same as native).
    mgr_strategy = get_strategy("manager")
    sidm, msgm, _ = _mk_manager()
    ridm = session_manager._root_id_for(sidm)
    ctxm = ApplyEventCtx(root_id=sidm, run_id="r")
    session_manager._batches[ridm] = {"_phantom": True, "bump_updated_at": False}
    try:
        for i in range(50):
            mgr_strategy.apply_event(
                app_session_id=sidm, msg=msgm,
                event=_mgr_ev(f"m-{i}"), ctx=ctxm, source_is_provider_stream=False,
            )
    finally:
        session_manager._batches.pop(ridm, None)
    results.append(
        ("manager-mode uid_idx lives on the flat msg",
         isinstance(msgm.get("_uid_idx"), dict)
         and len(msgm.get("_uid_idx", {})) == 50,
         f"got uid_idx={msgm.get('_uid_idx')}"))
    results.append(
        ("manager-mode events land on flat msg.events",
         len(msgm.get("events") or []) == 50,
         f"events_len={len(msgm.get('events') or [])}"))

    # 5) set_native_events invalidates uid_idx.
    sid3, msg3, mid3 = _mk_native()
    rid3 = session_manager._root_id_for(sid3)
    ctx3 = ApplyEventCtx(root_id=sid3, run_id="r")
    strategy.apply_event(
        app_session_id=sid3, msg=msg3,
        event=_ev("x-1"), ctx=ctx3, source_is_provider_stream=False,
    )
    assert isinstance(msg3.get("_uid_idx"), dict)
    # External mutation: replace the whole list with a different set.
    session_manager.set_native_events(sid3, mid3, [_ev("y-1"), _ev("y-2")])
    results.append(
        ("set_native_events invalidates _uid_idx",
         "_uid_idx" not in msg3,
         f"uid_idx survived external mutation: {msg3.get('_uid_idx')}"))

    # 6) After invalidation, next apply_event rebuilds correctly.
    strategy.apply_event(
        app_session_id=sid3, msg=msg3,
        event=_ev("y-3"), ctx=ctx3, source_is_provider_stream=False,
    )
    rebuilt = msg3.get("_uid_idx") or {}
    results.append(
        ("rebuilt uid_idx after invalidation tracks all 3 uids",
         set(rebuilt.keys()) == {"y-1", "y-2", "y-3"},
         f"got {set(rebuilt.keys())}"))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg_ in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg_}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

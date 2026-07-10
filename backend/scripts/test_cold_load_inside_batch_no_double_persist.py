"""Locks the v8 phantom-batch contract for cold-load hydration:

When `_load_root` runs from inside an outer `batch()` caller, the
hydrate phase MUST NOT fire its own `write_session_full` — otherwise
both the inner (hydrate-coalesced) AND the outer batch each fire one
write per cold-load. Round 2 of adversarial review found this exact
double-persist; the fix is the phantom-batch marker installed by
`_load_root` BEFORE calling hydrate.

This test asserts:

  1. Cold-load inside an outer `batch()` produces EXACTLY ONE
     `write_session_full` call (the outer's exit-persist).
  2. Cold-load called from a non-batch context (e.g.
     `get_root_tree_paginated`) produces ZERO `write_session_full`
     calls during hydrate — because msg.events are reconstructed from
     events.jsonl which is already durable; the snapshot on disk
     doesn't need to be re-stamped.
  3. The phantom batch entry is REMOVED from `self._batches` after
     hydrate exits — no leak, even on hydrate-exception.

Run with:
    cd backend && .venv/bin/python scripts/test_cold_load_inside_batch_no_double_persist.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-phantom-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _native_event(uuid: str, text: str = "x") -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def _build_session_with_events(n: int) -> str:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/test-phantom",
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
    ctx = ApplyEventCtx(root_id=sid, run_id="run-phantom")
    for i in range(n):
        ev = _native_event(f"u-{i}", f"text-{i}")
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=ev, ctx=ctx, source_is_provider_stream=True,
        )
    session_manager.flush_pending_persists()
    return sid


def _patch_count_writes() -> tuple[list, callable]:
    """Replace `session_store.write_session_full` with a counter that
    delegates to the real impl. Returns (call_list, restore_fn)."""
    calls: list[tuple] = []
    original = session_store.write_session_full

    def counting(root, *, bump_updated_at: bool = True, **kwargs):
        calls.append((root.get("id"), bump_updated_at))
        return original(root, bump_updated_at=bump_updated_at, **kwargs)

    session_store.write_session_full = counting

    def restore():
        session_store.write_session_full = original
    return calls, restore


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    sid = _build_session_with_events(20)

    # Case 1: cold-load from non-batch context.
    session_manager._roots.pop(sid, None)
    calls, restore = _patch_count_writes()
    try:
        _ = session_manager.get_root_tree_paginated(sid, msg_limit=50)
    finally:
        restore()
    results.append(
        ("non-batch cold-load fires ZERO write_session_full",
         len(calls) == 0, f"got {len(calls)} writes: {calls}"))

    # Case 2: cold-load INSIDE outer batch — exactly one write at outer
    # batch exit. We force the outer batch's leading-edge persist to
    # fire by sleeping past `PERSIST_DEBOUNCE_S` so the debounce window
    # has already lapsed (otherwise the outer's persist queues a tail
    # flush onto a Timer thread and the test exits before it fires).
    import time
    from session_manager import PERSIST_DEBOUNCE_S
    session_manager._roots.pop(sid, None)
    time.sleep(PERSIST_DEBOUNCE_S * 1.5)
    calls, restore = _patch_count_writes()
    try:
        with session_manager.batch(sid, bump_updated_at=False):
            # The batch() context invokes `_cached(sid)` internally,
            # which triggers `_load_root` → hydrate. Phantom batch
            # should suppress all writes during hydrate. The body
            # below is empty — only the outer batch's exit persist
            # fires.
            pass
        session_manager.flush_pending_persists()
    finally:
        restore()
    results.append(
        ("cold-load inside outer batch() fires EXACTLY 1 write",
         len(calls) == 1, f"got {len(calls)} writes: {calls}"))

    # Case 3: phantom batch entry is GONE after _load_root returns.
    session_manager._roots.pop(sid, None)
    _ = session_manager._cached(sid)
    results.append(
        ("phantom batch entry is popped after _load_root returns",
         session_manager._batches.get(session_manager._root_id_for(sid))
         is None,
         f"got {session_manager._batches.get(session_manager._root_id_for(sid))}"))

    # Case 4: phantom batch is removed even on hydrate exception.
    # Force render_tree_hydrate.hydrate_msg_events_from_jsonl to raise.
    session_manager._roots.pop(sid, None)
    import render_tree_hydrate
    original_hydrate = render_tree_hydrate.decode_prepared_hydration

    def boom(prepared):
        raise RuntimeError("simulated hydrate failure")

    render_tree_hydrate.decode_prepared_hydration = boom
    try:
        _ = session_manager.get_root_tree(sid)
    finally:
        render_tree_hydrate.decode_prepared_hydration = original_hydrate
    results.append(
        ("phantom batch entry is popped after hydrate exception",
         session_manager._batches.get(session_manager._root_id_for(sid))
         is None,
         f"got {session_manager._batches.get(session_manager._root_id_for(sid))}"))

    # Case 5: phantom-batched mutators do NOT fire listeners.
    # Subscribe a listener; cold-load; expect zero `running_content_updated`
    # / event-append events captured.
    session_manager._roots.pop(sid, None)
    fires: list[dict] = []

    def listener(sid_arg: str, change: dict) -> None:
        fires.append({"sid": sid_arg, **change})

    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        session_manager.add_listener(listener)
    try:
        _ = session_manager.get_root_tree_paginated(sid, msg_limit=50)
    finally:
        # `add_listener` has no `remove`; the listener stays for the
        # rest of this test process. That's fine — we capture the
        # fires inside this scope and assert.
        pass
    # Cold-load fires up to 20 hydration apply_event calls (one per
    # event) and possibly an `update_running_content`. With the
    # phantom-batch fire suppression they should all be silent.
    spam_kinds = {"native_event_appended", "running_content_updated"}
    spam = [f for f in fires if f.get("kind") in spam_kinds]
    results.append(
        ("phantom batch suppresses listener fan-out during hydrate",
         len(spam) == 0,
         f"got {len(spam)} listener fires: {spam[:3]}"))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
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

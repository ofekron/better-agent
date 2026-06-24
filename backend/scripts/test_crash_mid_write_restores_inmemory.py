"""Locks the v8 strip/restore atomicity:

If `os.replace` raises after `_strip_volatile_from_tree` runs but
before the rename, the `finally` block MUST restore the in-memory
tree to its pre-strip state (events + isStreaming both back).

After the crash, the on-disk file is unchanged (atomic-against-torn-
reads — readers see the prior version), AND on next cold load,
hydration from events.jsonl produces the same render-tree state.

Run with:
    cd backend && .venv/bin/python scripts/test_crash_mid_write_restores_inmemory.py
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-crash-")

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


def _build() -> str:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/test-crash",
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
    ctx = ApplyEventCtx(root_id=sid, run_id="run-crash")
    for i in range(20):
        ev = _native_event(f"u-{i}", f"text-{i}")
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=ev, ctx=ctx, source_is_provider_stream=True,
        )
    session_manager.flush_pending_persists()
    return sid


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    sid = _build()

    # Snapshot in-memory pre-crash.
    pre_root = session_manager.get_ref(sid)
    pre_events_count = len(pre_root["messages"][-1].get("events") or [])
    pre_dump = json.dumps(pre_root, sort_keys=True)
    assert pre_events_count > 0

    # Snapshot on-disk pre-crash (the just-flushed thin snapshot).
    pre_disk = open(session_store._session_path(sid)).read()

    # Monkey-patch os.replace to raise during the next write.
    original_replace = os.replace

    def _failing_replace(*a, **kw):
        # Restore for any subsequent writes, then raise this once.
        os.replace = original_replace  # type: ignore
        raise OSError("simulated crash mid-write")

    os.replace = _failing_replace  # type: ignore

    raised = False
    try:
        session_store.write_session_full(pre_root, bump_updated_at=False)
    except OSError as e:
        raised = "simulated crash" in str(e)
    finally:
        os.replace = original_replace  # type: ignore

    results.append(("write_session_full propagated the simulated OSError",
                    raised, "expected OSError"))

    # 1) In-memory tree restored.
    post_dump = json.dumps(pre_root, sort_keys=True)
    results.append(
        ("in-memory tree byte-identical after crash + finally restore",
         post_dump == pre_dump, "tree diverged after failed write"))

    # 2) On-disk file unchanged (the tmp file path is unlinked; the
    # canonical file kept its previous content because os.replace never ran).
    post_disk = open(session_store._session_path(sid)).read()
    results.append(("on-disk snapshot unchanged after crash",
                    post_disk == pre_disk,
                    "on-disk content mutated even though replace raised"))

    # 3) Drop cache, cold-load — hydration from events.jsonl rebuilds.
    session_manager._roots.pop(sid, None)
    tree = session_manager.get_root_tree_paginated(sid, msg_limit=50)
    assert tree is not None
    hydrated_count = len(tree["messages"][-1].get("events") or [])
    results.append(
        ("cold-load hydration rebuilds events from events.jsonl",
         hydrated_count == pre_events_count,
         f"got {hydrated_count} vs {pre_events_count}"))

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

"""Locks the thin-snapshot invariant:

1. After `write_session_full`, the raw on-disk JSON omits
   `msg.events` / `msg.workers[*].events` lists — the events live
   exclusively in `events.jsonl`.
2. Loading the session via `session_manager.get_root_tree_paginated`
   returns a tree whose `msg.events` lists are POPULATED and per-
   event `uuid`+`data` are byte-identical to the pre-write
   in-memory state.
3. `_schema_version == session_store.SCHEMA_VERSION` on disk.

Run with:
    cd backend && .venv/bin/python scripts/test_session_snapshot_thin.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-thin-")

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


def _mk_session_with_events(n: int) -> tuple[str, list[dict]]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/test-thin",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["id"] = "msg-1"
    scaffold["role"] = "assistant"
    scaffold["seq"] = 1
    session_manager.append_assistant_msg(sid, scaffold)
    ctx = ApplyEventCtx(root_id=sid, run_id="run-1")
    msg = session_manager.get_ref(sid)["messages"][-1]
    raw_events = []
    for i in range(n):
        ev = _native_event(f"u-{i}", f"text-{i}")
        raw_events.append(ev)
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=ev, ctx=ctx, source_is_provider_stream=True,
        )
    return sid, raw_events


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    sid, raw_events = _mk_session_with_events(50)

    # Capture pre-write in-memory state.
    pre_tree = session_manager.get_ref(sid)
    pre_events = list(pre_tree["messages"][-1]["events"])
    pre_uuids = [e["data"]["uuid"] for e in pre_events]
    assert pre_uuids, "in-memory events empty before write"

    # Force a fresh write_session_full (synchronous flush).
    session_manager.flush_pending_persists()

    # 1) Raw on-disk omits events.
    on_disk_path = session_store._session_path(sid)
    with open(on_disk_path) as f:
        raw = json.load(f)
    msg = raw["messages"][-1]
    ok = "events" not in msg
    results.append(("on-disk msg.events is absent", ok,
                    f"keys={sorted(msg.keys())}"))

    # 2) _schema_version is current on disk.
    ok = raw.get("_schema_version") == session_store.SCHEMA_VERSION
    results.append(("on-disk _schema_version is current", ok,
                    f"got {raw.get('_schema_version')}"))

    # 3) get_root_tree_paginated returns hydrated msg.events.
    # Simulate restart by clearing cache.
    session_manager._roots.pop(sid, None)
    tree = session_manager.get_root_tree_paginated(sid, msg_limit=50)
    assert tree is not None, "get_root_tree_paginated returned None"
    hydrated_msg = tree["messages"][-1]
    hydrated_events = hydrated_msg.get("events") or []
    ok = len(hydrated_events) == len(pre_events)
    results.append(
        ("hydrated tree has all events", ok,
         f"len {len(hydrated_events)} vs {len(pre_events)}"))
    hydrated_uuids = [
        e["data"]["uuid"] for e in hydrated_events
        if isinstance(e.get("data"), dict)
    ]
    ok = set(hydrated_uuids) == set(pre_uuids)
    results.append(("hydrated uuids match pre-write set", ok,
                    f"missing {set(pre_uuids) - set(hydrated_uuids)}"))

    # 4) Per-event data byte-identical (modulo ordering — apply_event
    # may re-order, but uuid+data dict equality must hold).
    pre_by_uid = {e["data"]["uuid"]: e["data"] for e in pre_events}
    hydrated_by_uid = {
        e["data"]["uuid"]: e["data"] for e in hydrated_events
        if isinstance(e.get("data"), dict)
    }
    all_match = all(
        pre_by_uid[u] == hydrated_by_uid.get(u) for u in pre_by_uid
    )
    results.append(("per-event data dict equality", all_match,
                    "some data dicts differ"))

    # Report
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

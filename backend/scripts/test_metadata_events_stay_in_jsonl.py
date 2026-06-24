"""Locks the metadata-event narrow-render-tree invariant in the v8
world: events whose normalized inner `data.type` is `ai-title` or
`file-history-snapshot` MUST NOT land on `msg.events`, even though
they are persisted to events.jsonl for audit / recovery replay.

The rule is enforced inside `apply_event` (see `backend/orchs/
base.py:497-506`). The hydration path delegates to `apply_event` so
the rule applies symmetrically to cold-load hydration. This test
locks that symmetry.

Run with:
    cd backend && .venv/bin/python scripts/test_metadata_events_stay_in_jsonl.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-meta-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_writer  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from paths import ba_home  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _native_event(uuid: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "x"}]},
        },
    }


def _ai_title_event(title: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "type": "ai-title",
            "aiTitle": title,
        },
    }


def _file_history_event() -> dict:
    return {
        "type": "agent_message",
        "data": {
            "type": "file-history-snapshot",
            "snapshot": {"foo.py": "abc"},
        },
    }


def _events_jsonl_count(sid: str) -> int:
    p = ba_home() / "sessions" / sid / "events.jsonl"
    if not p.exists():
        return 0
    with open(p) as f:
        return sum(1 for line in f if line.strip())


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/meta",
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
    ctx = ApplyEventCtx(root_id=sid, run_id="run-meta")

    # Two real events + one ai-title + one file-history-snapshot.
    real_uids = ["u-1", "u-2"]
    for u in real_uids:
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=_native_event(u),
            ctx=ctx, source_is_provider_stream=True,
        )
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_ai_title_event("My new title"),
        ctx=ctx, source_is_provider_stream=True,
    )
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_file_history_event(),
        ctx=ctx, source_is_provider_stream=True,
    )

    event_journal_writer.barrier_sync(sid)
    session_manager.flush_pending_persists()

    # 1) Live ingest path: msg.events has ONLY the 2 real events.
    msg_after = session_manager.get_ref(sid)["messages"][-1]
    real_in_msg = [
        e for e in (msg_after.get("events") or [])
        if isinstance(e.get("data"), dict)
        and e["data"].get("type") in {"assistant", "user", "tool_use", "tool_result"}
    ]
    results.append(
        ("live: msg.events has the 2 real events",
         len(real_in_msg) == 2, f"got {len(real_in_msg)}"))

    bad_in_msg = [
        e for e in (msg_after.get("events") or [])
        if isinstance(e.get("data"), dict)
        and e["data"].get("type") in {"ai-title", "file-history-snapshot"}
    ]
    results.append(
        ("live: msg.events excludes ai-title and file-history-snapshot",
         len(bad_in_msg) == 0,
         f"found {len(bad_in_msg)} metadata entries leaked"))

    # 2) events.jsonl has ALL 4 entries (audit trail).
    jsonl_count = _events_jsonl_count(sid)
    results.append(
        ("events.jsonl preserves metadata for audit / replay",
         jsonl_count >= 4, f"got {jsonl_count} rows (expected >=4)"))

    # 3) ai-title was applied as a rename (side-effect verification).
    results.append(
        ("ai-title fired the rename side-effect",
         session_manager.get(sid)["name"] == "My new title",
         f"got name={session_manager.get(sid)['name']}"))

    # 4) Cold-load hydration: same exclusion holds.
    session_manager._roots.pop(sid, None)
    tree = session_manager.get_root_tree_paginated(sid, msg_limit=50)
    assert tree is not None
    hydrated = tree["messages"][-1].get("events") or []
    leaked = [
        e for e in hydrated
        if isinstance(e.get("data"), dict)
        and e["data"].get("type") in {"ai-title", "file-history-snapshot"}
    ]
    results.append(
        ("hydrated: msg.events excludes ai-title and file-history-snapshot",
         len(leaked) == 0,
         f"hydrated leaked {len(leaked)} metadata entries"))

    real_hydrated = [
        e for e in hydrated
        if isinstance(e.get("data"), dict)
        and e["data"].get("type") in {"assistant", "user", "tool_use", "tool_result"}
    ]
    results.append(
        ("hydrated: 2 real events rebuilt",
         len(real_hydrated) == 2,
         f"got {len(real_hydrated)}"))

    strategy.ingest_orphan(
        app_session_id=sid,
        event=_ai_title_event("Late orphan title"),
        ctx=ctx,
        source_is_provider_stream=True,
    )
    event_journal_writer.barrier_sync(sid)
    session_manager.flush_pending_persists()
    msg_after_orphan = session_manager.get_ref(sid)["messages"][-1]
    orphan_leaked = [
        e for e in (msg_after_orphan.get("events") or [])
        if isinstance(e.get("data"), dict)
        and e["data"].get("type") == "ai-title"
    ]
    results.append(
        ("orphan ai-title fired the rename side-effect",
         session_manager.get(sid)["name"] == "Late orphan title",
         f"got name={session_manager.get(sid)['name']}"))
    results.append(
        ("orphan ai-title stays out of msg.events",
         len(orphan_leaked) == 0,
         f"found {len(orphan_leaked)} metadata entries leaked"))

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

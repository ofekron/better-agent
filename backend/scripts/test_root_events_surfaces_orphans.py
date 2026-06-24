"""Locks the 'root-detached orphans' render fix.

Render-tree events that were written to events.jsonl with msg_id=None
(no owning assistant message — e.g. a final line the provider flushed
after the turn finalized) used to be silently dropped by the native
per-message render path. They are now surfaced on the tree as
`root_events` (detached 'root children'), with two guards:
  - render types only (agent_message / manager_event) — internal frames
    (run_state, command_received, …) are NOT surfaced.
  - dedup against stamped uuids — an orphan whose uuid is already stamped
    onto a message (the dual-writer re-emit) is NOT surfaced (no
    double-render).

Run with:
    cd backend && .venv/bin/python scripts/test_root_events_surfaces_orphans.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-root-events-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _agent_msg(uid: str, text: str) -> dict:
    return {
        "uuid": uid, "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/root-events",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    from orchs import get_strategy
    sc = get_strategy("native").build_assistant_scaffold()
    sc["id"] = "asst-1"
    sc["seq"] = 1
    session_manager.append_assistant_msg(sid, sc)

    # 1) Stamped render event (uuid A) — owned by the message.
    event_ingester.ingest(
        sid, sid=sid, event_type="agent_message",
        data=_agent_msg("A", "streamed chunk"), source="test", msg_id="asst-1",
    )
    # 2) Duplicate orphan of the SAME uuid A (dual-writer re-emit) — must
    #    be deduped (not surfaced).
    event_ingester.ingest(
        sid, sid=sid, event_type="agent_message",
        data=_agent_msg("A", "streamed chunk"), source="test", msg_id=None,
    )
    # 3) Orphan-only render event (uuid B) — the late report. Must surface.
    event_ingester.ingest(
        sid, sid=sid, event_type="agent_message",
        data=_agent_msg("B", "THE FINAL REPORT"), source="test", msg_id=None,
    )
    # 4) Internal orphan (run_state) — must NOT surface.
    event_ingester.ingest(
        sid, sid=sid, event_type="run_state",
        data={"uuid": "C", "running": False}, source="test", msg_id=None,
    )
    # 5) Metadata orphans (agent_message with a non-render data.type, no
    #    uuid) — must NOT surface as empty root children.
    for mtype in ("last-prompt", "ai-title", "file-history-snapshot"):
        event_ingester.ingest(
            sid, sid=sid, event_type="agent_message",
            data={"type": mtype, "sessionId": sid}, source="test", msg_id=None,
        )

    tree = session_manager.get_root_tree_stubbed(sid)
    root_events = (tree or {}).get("root_events") or []
    uuids = {(e.get("data") or {}).get("uuid") for e in root_events}

    results.append((
        "orphan-only render event (report, B) IS surfaced as a root child",
        "B" in uuids,
        f"root_events uuids={sorted(u for u in uuids if u)}",
    ))
    results.append((
        "duplicate orphan of a stamped uuid (A) is NOT surfaced (dedup)",
        "A" not in uuids,
        "A double-rendered at root",
    ))
    results.append((
        "internal orphan (run_state, C) is NOT surfaced (render-gate)",
        "C" not in uuids,
        "run_state leaked into root_events",
    ))
    results.append((
        "metadata orphans (ai-title/last-prompt/file-history) NOT surfaced",
        all(
            (e.get("data") or {}).get("type")
            not in ("last-prompt", "ai-title", "file-history-snapshot")
            for e in root_events
        ),
        f"metadata leaked: {[(e.get('data') or {}).get('type') for e in root_events]}",
    ))
    results.append((
        "exactly one root event (only the report)",
        len(root_events) == 1,
        f"got {len(root_events)}",
    ))
    results.append((
        "root events are normalized to agent_message shape",
        all(e.get("type") == "agent_message" for e in root_events),
        f"types={[e.get('type') for e in root_events]}",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + detail}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        return 0 if _run() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

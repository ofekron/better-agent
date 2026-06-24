"""Locks the merged manager/native shape invariant.

After consolidating manager → native:
  - `get_strategy("manager")` and `get_strategy("native")` return the
    SAME single strategy object (mode no longer selects shape).
  - The assistant scaffold has NO `manager` key; it has flat `events`,
    `workers`, and `agent_session_id`.
  - A manager-mode turn's primary events land on `msg["events"]` (flat,
    like native) — never on a `manager` sub-dict.
  - `finalize_turn` pins the primary CLI sid onto `msg["agent_session_id"]`
    for manager mode (was `msg["manager"]["session_id"]`).

Run with:
    cd backend && .venv/bin/python scripts/test_manager_native_unified_shape.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-unified-shape-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _ev(uid: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hi"}]},
        },
    }


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    # 1) One strategy for both modes.
    s_mgr = get_strategy("manager")
    s_nat = get_strategy("native")
    results.append((
        "get_strategy('manager') is get_strategy('native') (single strategy)",
        s_mgr is s_nat,
        "distinct objects — modes still select different strategies",
    ))

    # 2) Scaffold is flat — no manager key.
    scaffold = s_mgr.build_assistant_scaffold()
    results.append((
        "scaffold has no 'manager' key",
        "manager" not in scaffold,
        f"keys={sorted(scaffold)}",
    ))
    results.append((
        "scaffold has flat events/workers/agent_session_id",
        all(k in scaffold for k in ("events", "workers", "agent_session_id")),
        f"keys={sorted(scaffold)}",
    ))

    # 3) A manager-mode turn writes primary events to msg['events'].
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/unified",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    scaffold = s_mgr.build_assistant_scaffold()
    scaffold["id"] = "msg-1"
    scaffold["seq"] = 1
    session_manager.append_assistant_msg(sid, scaffold)
    msg = session_manager.get_ref(sid)["messages"][-1]

    rid = session_manager._root_id_for(sid)
    session_manager._batches[rid] = {"_phantom": True, "bump_updated_at": False}
    try:
        ctx = ApplyEventCtx(root_id=sid, run_id="r")
        s_mgr.apply_event(
            app_session_id=sid, msg=msg, event=_ev("u-1"),
            ctx=ctx, source_is_provider_stream=False,
        )
        s_mgr.finalize_turn(
            app_session_id=sid,
            assistant_msg=msg,
            primary_result={"session_id": "cli-sid-XYZ"},
        )
    finally:
        session_manager._batches.pop(rid, None)

    live_msg = session_manager.get_ref(sid)["messages"][-1]
    results.append((
        "manager-mode event landed on flat msg['events']",
        len(live_msg.get("events") or []) == 1
        and "manager" not in live_msg,
        f"events={len(live_msg.get('events') or [])} "
        f"has_manager={'manager' in live_msg}",
    ))
    results.append((
        "finalize_turn pinned msg['agent_session_id']",
        live_msg.get("agent_session_id") == "cli-sid-XYZ",
        f"got {live_msg.get('agent_session_id')!r}",
    ))

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

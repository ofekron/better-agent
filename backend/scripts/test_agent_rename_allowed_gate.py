"""Locks the `agent_rename_allowed` session setting: by default (False),
an agent-issued "ai-title" event must NOT rename the session. Only when
the setting is explicitly turned on does the ai-title side effect fire.

Run with:
    cd backend && .venv/bin/python scripts/test_agent_rename_allowed_gate.py
"""

from __future__ import annotations

import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-agent-rename-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_journal import event_journal_writer  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _ai_title_event(uuid: str, title: str) -> dict:
    return {
        "type": "agent_message",
        "data": {"uuid": uuid, "type": "ai-title", "aiTitle": title},
    }


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    sess = session_manager.create(
        name="original", model="sonnet", cwd="/tmp/agent-rename",
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
    ctx = ApplyEventCtx(root_id=sid, run_id="run-agent-rename")

    results.append((
        "agent_rename_allowed defaults to falsy",
        not session_manager.get_lite(sid).get("agent_rename_allowed"),
        f"got {session_manager.get_lite(sid).get('agent_rename_allowed')!r}",
    ))

    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_ai_title_event("u-1", "Agent Chosen Title"),
        ctx=ctx, source_is_provider_stream=True,
    )
    event_journal_writer.barrier_sync(sid)
    session_manager.flush_pending_persists()

    results.append((
        "ai-title does NOT rename when agent_rename_allowed is False",
        session_manager.get(sid)["name"] == "original",
        f"got name={session_manager.get(sid)['name']!r}",
    ))

    session_manager.set_agent_rename_allowed(sid, True)
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_ai_title_event("u-2", "Agent Chosen Title"),
        ctx=ctx, source_is_provider_stream=True,
    )
    event_journal_writer.barrier_sync(sid)
    session_manager.flush_pending_persists()

    results.append((
        "ai-title renames once agent_rename_allowed is True",
        session_manager.get(sid)["name"] == "Agent Chosen Title",
        f"got name={session_manager.get(sid)['name']!r}",
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

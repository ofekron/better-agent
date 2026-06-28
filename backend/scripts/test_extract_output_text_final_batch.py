"""Regression tests for assistant content extraction.

`assistant.content` is a plain-text snapshot of the final answer, not a
concatenation of every assistant text block written before/between tool
calls. The extractor must return the last contiguous batch of assistant
text blocks, with non-text blocks acting as boundaries.

Run with:
    cd backend && .venv/bin/python scripts/test_extract_output_text_final_batch.py
"""

from __future__ import annotations

import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-extract-final-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_shape import extract_output_text  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _assistant(
    content,
    uuid: str | None = None,
    parent_tool_use_id: str | None = None,
) -> dict:
    data = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4",
            "content": content,
        },
    }
    if uuid:
        data["uuid"] = uuid
    if parent_tool_use_id:
        data["parent_tool_use_id"] = parent_tool_use_id
    return {"type": "agent_message", "data": data}


def _user_tool_result() -> dict:
    return {
        "type": "agent_message",
        "data": {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "ok",
                    }
                ],
            },
        },
    }


def _text(value: str) -> dict:
    return {"type": "text", "text": value}


def _tool() -> dict:
    return {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}


def _thinking() -> dict:
    return {"type": "thinking", "thinking": "internal"}


def _run_case(name: str, events: list[dict], expected: str) -> bool:
    got = extract_output_text(events)
    ok = got == expected
    print(f"{PASS if ok else FAIL}  {name}")
    if not ok:
        print(f"    expected: {expected!r}")
        print(f"    got:      {got!r}")
    return ok


def main() -> int:
    cases = [
        (
            "final text after tool only",
            [
                _assistant([_text("planning"), _tool()]),
                _user_tool_result(),
                _assistant([_text("final answer")]),
            ],
            "final answer",
        ),
        (
            "trailing non-text means no final text snapshot",
            [
                _assistant([_text("pre"), _tool()]),
                _assistant([_text("final one")]),
                _assistant([_text("final two")]),
                _assistant([_thinking()]),
            ],
            "",
        ),
        (
            "text before a tool in the same message is not final content",
            [
                _assistant([_text("before tool"), _tool(), _text("after tool")]),
            ],
            "after tool",
        ),
        (
            "text before a trailing tool is progress narration",
            [
                _assistant([_text("before tool"), _tool()]),
                _user_tool_result(),
            ],
            "",
        ),
        (
            "cumulative snapshots keep last snapshot within final batch",
            [
                _assistant([_text("p")], uuid="u1"),
                _assistant([_text("po")], uuid="u1"),
                _assistant([_text("pong")], uuid="u1"),
            ],
            "pong",
        ),
        (
            "different-uuid text events: inter-message boundary",
            [
                _assistant([_text("first")], uuid="m1"),
                _assistant([_text("second")], uuid="m2"),
            ],
            "second",
        ),
        (
            "three different uuids: only last survives",
            [
                _assistant([_text("a")], uuid="m1"),
                _assistant([_text("b")], uuid="m2"),
                _assistant([_text("c")], uuid="m3"),
            ],
            "c",
        ),
        (
            "same-uuid snapshots then different uuid",
            [
                _assistant([_text("a")], uuid="m1"),
                _assistant([_text("ab")], uuid="m1"),
                _assistant([_text("final")], uuid="m2"),
            ],
            "final",
        ),
        (
            "subagent child text is not parent final content",
            [
                _assistant([
                    {
                        "type": "tool_use",
                        "id": "call_agent",
                        "name": "Agent",
                        "input": {"subagent_type": "wait"},
                    }
                ], uuid="parent-tool"),
                _assistant(
                    [_text("child final should stay nested")],
                    uuid="child-final",
                    parent_tool_use_id="call_agent",
                ),
                _user_tool_result(),
            ],
            "",
        ),
        (
            "top-level final after subagent wins",
            [
                _assistant([
                    {
                        "type": "tool_use",
                        "id": "call_agent",
                        "name": "Agent",
                        "input": {"subagent_type": "wait"},
                    }
                ], uuid="parent-tool"),
                _assistant(
                    [_text("child final should stay nested")],
                    uuid="child-final",
                    parent_tool_use_id="call_agent",
                ),
                _user_tool_result(),
                _assistant([_text("primary final")], uuid="primary-final"),
            ],
            "primary final",
        ),
    ]
    ok = all(_run_case(*case) for case in cases)
    ok = test_non_streaming_projection_clears_stale_content() and ok
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if ok else 1


def test_non_streaming_projection_clears_stale_content() -> bool:
    sess = session_manager.create(
        name="projection",
        model="sonnet",
        cwd="/tmp/projection",
        orchestration_mode="native",
        source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    msg = strategy.build_assistant_scaffold()
    msg["id"] = "msg-stale"
    msg["content"] = "stale progress"
    session_manager.append_assistant_msg(sid, msg)
    live_msg = session_manager.get_ref(sid)["messages"][-1]
    ctx = ApplyEventCtx(root_id=sid)
    strategy.apply_event(
        app_session_id=sid,
        msg=live_msg,
        event=_assistant([_text("progress"), _tool()], uuid="tool-step"),
        ctx=ctx,
        source_is_provider_stream=True,
    )
    strategy.apply_event(
        app_session_id=sid,
        msg=live_msg,
        event=_user_tool_result(),
        ctx=ctx,
        source_is_provider_stream=True,
    )
    session_manager.set_streaming(sid, "msg-stale", False)
    event_ingester.close(sid)
    session_manager.refresh_message_content_from_events(sid, sid, "msg-stale")
    projected = next(
        m for m in session_manager.get_ref(sid)["messages"]
        if m.get("id") == "msg-stale"
    )
    ok = projected.get("content") == ""
    print(f"{PASS if ok else FAIL}  non-streaming projection clears stale content")
    if not ok:
        print(f"    got: {projected.get('content')!r}")
    return ok


if __name__ == "__main__":
    raise SystemExit(main())

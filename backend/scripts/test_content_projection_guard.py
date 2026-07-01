"""Regression tests for the guarded content projection
(`event_shape.project_content_snapshot`).

Pins the fix for the blank-assistant-bubble bug: a finalized message's
non-empty content snapshot must survive late trailing non-text events
(babysitter-linger late flush, a continuation turn cut off mid-tools).
Before the fix, `_refresh_message_content_from_event_projection`
overwrote content with '' because `extract_output_text` projects the
TRAILING text run, which is empty when events end on a tool/thinking
boundary.

Covers:
  1. Helper semantics — empty projection keeps current content;
     non-empty projection overwrites.
  2. apply_event path — text event then trailing tool_use/thinking
     events on the same msg: content retains the final answer.
  3. session_manager.refresh_message_content_from_events — journal
     re-projection does not blank content.

Run with:
    cd backend && .venv/bin/python scripts/test_content_projection_guard.py
"""

from __future__ import annotations

import os
import sys
import uuid as uuid_mod

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-content-guard-")

from event_shape import has_assistant_text, project_content_snapshot  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"{PASS if ok else FAIL}: {name}" + (f" ({detail})" if detail else ""))


def _text_event(text: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "uuid": str(uuid_mod.uuid4()),
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        },
    }


def _tool_event() -> dict:
    return {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "uuid": str(uuid_mod.uuid4()),
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            },
        },
    }


def _thinking_event() -> dict:
    return {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "uuid": str(uuid_mod.uuid4()),
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "hmm"}],
            },
        },
    }


def test_helper_semantics() -> None:
    text, tool = _text_event("final answer"), _tool_event()
    _check(
        "helper: trailing tool run keeps current content",
        project_content_snapshot([text, tool], "final answer") == "final answer",
    )
    _check(
        "helper: non-empty projection overwrites current",
        project_content_snapshot([text], "stale") == "final answer",
    )
    _check(
        "helper: empty events keep current",
        project_content_snapshot([], "kept") == "kept",
    )
    _check(
        "helper: nothing anywhere yields empty string",
        project_content_snapshot([_tool_event()], None) == "",
    )


def test_strip_synthetic_semantics() -> None:
    synthetic = _text_event("synthetic")
    synthetic["data"]["message"]["model"] = "<synthetic>"
    _check(
        "strip semantics: synthetic-only events report no assistant text",
        not has_assistant_text([synthetic, _tool_event()]),
    )
    _check(
        "strip semantics: real text among tools reports assistant text",
        has_assistant_text([_text_event("real"), _tool_event()]),
    )


def test_recovery_and_strip_tree_guards_wired() -> None:
    """Source-pattern locks for the two call sites outside the shared
    helper: run_recovery must not write an empty extraction, and the
    strip-synthetic tree pass must use has_assistant_text +
    project_content_snapshot (synthetic-only blanks; trailing-tools keep)."""
    recovery_src = open(os.path.join(_BACKEND, "run_recovery.py")).read()
    _check(
        "run_recovery guards empty extraction before update_running_content",
        "if extracted:\n        session_manager.update_running_content(" in recovery_src,
    )
    main_src = open(os.path.join(_BACKEND, "main.py")).read()
    _check(
        "main strip-tree pass routes through has_assistant_text guard",
        "if has_assistant_text(cleaned):" in main_src
        and "project_content_snapshot(" in main_src,
    )


def test_apply_event_trailing_tools_keep_content() -> None:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    msg = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, msg)
    ctx = ApplyEventCtx(root_id=sid)

    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_text_event("the real answer"),
        ctx=ctx, source_is_provider_stream=True,
    )
    _check(
        "apply_event: text event sets content",
        msg.get("content") == "the real answer",
        f"content={msg.get('content')!r}",
    )

    for late in (_thinking_event(), _tool_event()):
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=late,
            ctx=ctx, source_is_provider_stream=True,
        )
    _check(
        "apply_event: trailing thinking/tool events keep content",
        msg.get("content") == "the real answer",
        f"content={msg.get('content')!r}",
    )

    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_text_event("continuation report"),
        ctx=ctx, source_is_provider_stream=True,
    )
    _check(
        "apply_event: later final text replaces content",
        msg.get("content") == "continuation report",
        f"content={msg.get('content')!r}",
    )


def test_journal_refresh_keeps_content() -> None:
    sess = session_manager.create(
        name="t2", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    msg = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, msg)
    session_manager.update_running_content(sid, msg["id"], "durable answer")
    # Journal holds only trailing tool rows for this msg (the late-flush
    # shape): the re-projection must not blank the durable content.
    session_manager.refresh_message_content_from_events(sid, sid, msg["id"])
    fresh = session_manager.get(sid) or {}
    m = next(
        (mm for mm in fresh.get("messages") or [] if mm.get("id") == msg["id"]),
        {},
    )
    _check(
        "journal refresh: empty projection keeps durable content",
        m.get("content") == "durable answer",
        f"content={m.get('content')!r}",
    )


def main() -> int:
    test_helper_semantics()
    test_strip_synthetic_semantics()
    test_recovery_and_strip_tree_guards_wired()
    test_apply_event_trailing_tools_keep_content()
    test_journal_refresh_keeps_content()
    failed = [r for r in _results if not r[1]]
    if failed:
        print(f"FAILED: {len(failed)}")
        return 1
    print("OK: content projection guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

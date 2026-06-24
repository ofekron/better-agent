"""Focused regression test for runner_agy parent-conversation extraction.

Locks the fix for the bug where the agy/antigravity runner rendered the
assistant response as ONE giant stdout block instead of ordered, separated
text turns + tool calls. The parent DB's `steps` table carries the real
ordered turn structure; _extract_parent_main_events / _agy_worker_events
must walk it and emit:

  (a) text turns as separate ordered agent_message events, with agy's
      protobuf-ish wrappers (leading field-marker char, trailing
      "2(bot-<uuid>)" suffix) stripped,
  (b) each logical tool call as exactly one tool_use event — NOT
      duplicated even though agy writes the same call as BOTH a type-15
      (model turn) step AND a type-127 step,
  (c) invoke_subagent payloads routed to the worker-panel path
      (worker_start/worker_event/worker_complete), NOT also emitted as a
      main-thread tool_use that would double-render.

Run with:
    cd backend && .venv/bin/python scripts/test_runner_agy_parent_extraction.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state BEFORE importing any backend module.
import _test_home

_TMP_HOME = _test_home.isolate("bc-test-agy-parent-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runner_agy  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_PARENT_SID = "aaaaaaaa-0000-0000-0000-000000000001"
_CHILD_SID = "bbbbbbbb-0000-0000-0000-000000000002"

# agy step_type taxonomy (verified against a real conversation DB):
#   14  = user prompt
#   15  = model turn (assistant prose OR a tool-call JSON object)
#   127 = invoke_subagent tool call (duplicates a type-15 payload)
#   132 = send_message / action tool call (duplicates a type-15 payload)
#   101 = tool/subagent RESULT


def _write_steps_db(path: Path, rows: list[tuple[int, int, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "create table steps (idx integer, step_type integer, status integer, "
            "has_subtrajectory integer, metadata blob, step_payload blob, render_info blob)"
        )
        con.executemany(
            "insert into steps values (?, ?, 0, 0, ?, ?, ?)",
            [(idx, step_type, b"", payload, b"") for idx, step_type, payload in rows],
        )
        con.commit()
    finally:
        con.close()


def _build_parent_db(home: Path) -> Path:
    """A parent conversation with: user prompt, a protobuf-wrapped text turn,
    a duplicated tool-call pair (type 15 + type 127), an invoke_subagent
    pair (type 15 + type 127), and a tool result."""
    db_path = runner_agy._conversation_db(home, _PARENT_SID)
    _write_steps_db(db_path, [
        # type 14: user prompt
        (0, 14, b"Please analyze the SDK"),
        # type 15: assistant prose wrapped in protobuf field markers. The
        # leading \x01 is a field-marker char; the trailing "2(bot-<uuid>)"
        # is agy's bot-identifier suffix. Both must be stripped.
        (
            1,
            15,
            b"\x01I am waiting for the research subagent to analyze the SDK"
            b" and extensions.2(bot-550e8400-e29b-41d4-a716-446655440000)",
        ),
        # type 15: a tool call (Action read_file). The same call appears again
        # as type 127 below — the extractor must emit it ONCE.
        (
            2,
            15,
            b'tool_abc read_file {"Action":"read_file","Path":"/tmp/x.txt"}:',
        ),
        # type 127: DUPLICATE of the read_file call above. Must be deduped.
        (
            3,
            127,
            b'tool_abc read_file {"Action":"read_file","Path":"/tmp/x.txt"}:',
        ),
        # type 15: invoke_subagent (Subagents). Routed to the worker-panel
        # path, NOT emitted as a main-thread tool_use.
        (
            4,
            15,
            b'tool_sub invoke_subagent {"Subagents":[{"Prompt":"Find files",'
            b'"Role":"Researcher","TypeName":"research"}]}:',
        ),
        # type 127: the invoke_subagent declaration the worker path consumes.
        (
            5,
            127,
            b'tool_sub invoke_subagent {"Subagents":[{"Prompt":"Find files",'
            b'"Role":"Researcher","TypeName":"research"}]}:',
        ),
        # type 101: tool/subagent result.
        (
            6,
            101,
            b"Located the SDK and extension files. Here is the analysis.",
        ),
    ])
    return db_path


def _build_child_db(home: Path) -> None:
    """A minimal child subagent conversation DB so _agy_worker_events can
    attach its events + worker_complete envelope."""
    db_path = runner_agy._conversation_db(home, _CHILD_SID)
    _write_steps_db(db_path, [
        (0, 15, b"child subagent analysis text"),
    ])


def _content(event: dict) -> dict:
    return event["data"]["message"]["content"][0]


def _main_thread_agent_messages(events: list[dict]) -> list[dict]:
    """Top-level agent_message events (the parent main thread), excluding
    worker_event/worker_start/worker_complete envelopes."""
    return [e for e in events if e.get("type") == "agent_message"]


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="bc-test-agy-parent-home-"))
    failures = 0
    try:
        parent_db = _build_parent_db(home)
        _build_child_db(home)

        # ----- _extract_parent_main_events: ordered text + tool_use only -----
        main_events = runner_agy._extract_parent_main_events(parent_db, "root")
        msgs = _main_thread_agent_messages(main_events)

        # (a) Assistant text turns are separate ordered agent_message events
        #     with protobuf artifacts stripped. The user prompt (type 14) is
        #     NOT re-emitted — it is already the session's user message, so
        #     emitting it would duplicate the user bubble.
        texts = [
            (_content(e).get("text") or "")
            for e in msgs
            if _content(e).get("type") == "text"
        ]
        user_msgs = [
            e for e in msgs
            if e["data"]["type"] == "user" and _content(e).get("type") == "text"
        ]
        if texts and not user_msgs and msgs[0]["data"]["type"] == "assistant":
            print(f"{PASS}  assistant text emitted as separate ordered events, "
                  f"user prompt not duplicated")
        else:
            print(f"{FAIL}  expected assistant text first and NO user-prompt "
                  f"event; got {len(msgs)} msgs ({len(user_msgs)} user-prompt)")
            failures += 1

        assistant_texts = [t for t in texts if "analyze the SDK" in t]
        if assistant_texts and "2(bot-" not in assistant_texts[0] and not assistant_texts[0].startswith("\x01"):
            print(f"{PASS}  protobuf artifacts stripped from assistant prose")
        else:
            print(f"{FAIL}  protobuf artifacts not stripped: {assistant_texts!r}")
            failures += 1

        # (b) The read_file tool call is a single tool_use (deduped despite
        #     the type-15 + type-127 duplicate pair).
        tool_uses = [
            e for e in msgs
            if _content(e).get("type") == "tool_use"
        ]
        read_file_calls = [e for e in tool_uses if _content(e).get("name") == "read_file"]
        if len(read_file_calls) == 1:
            tu = _content(read_file_calls[0])
            if tu.get("id") == "tool_abc" and tu.get("input", {}).get("Path") == "/tmp/x.txt":
                print(f"{PASS}  read_file emitted as exactly one tool_use (type-15/127 deduped)")
            else:
                print(f"{FAIL}  read_file tool_use has wrong id/input: {tu!r}")
                failures += 1
        else:
            print(f"{FAIL}  expected 1 read_file tool_use, got {len(read_file_calls)} "
                  f"(type-15/127 duplicate not deduped)")
            failures += 1

        # (c) invoke_subagent is NOT emitted as a main-thread tool_use.
        invoke_on_main = [e for e in tool_uses if _content(e).get("name") == "invoke_subagent"]
        if not invoke_on_main:
            print(f"{PASS}  invoke_subagent not duplicated on main thread (worker-panel owned)")
        else:
            print(f"{FAIL}  invoke_subagent leaked to main thread as tool_use ({len(invoke_on_main)})")
            failures += 1

        # ----- _agy_worker_events: worker envelope for invoke_subagent -----
        worker_events = runner_agy._agy_worker_events(
            agy_home=home, conversation_id=_PARENT_SID, parent_uuid="root",
        )
        types = [e["type"] for e in worker_events]
        # Parent main events appear first (user + text + tool_use + result),
        # then the worker panel is created only when a [Message] line is seen.
        # This parent DB has no [Message] line, so no worker_start fires here;
        # the invoke_subagent routing is exercised in test_agy_provider's
        # test_native_subagent_events_from_agy_db. Here we assert the main
        # thread events survive the interleaving into _agy_worker_events.
        main_in_worker = _main_thread_agent_messages(worker_events)
        if len(main_in_worker) == len(msgs):
            print(f"{PASS}  _agy_worker_events preserves all {len(msgs)} main-thread events")
        else:
            print(f"{FAIL}  _agy_worker_events has {len(main_in_worker)} main events, "
                  f"expected {len(msgs)}")
            failures += 1

        # Verify invoke_subagent worker routing with a [Message]-line DB too,
        # so the worker_start/worker_event/worker_complete path is covered.
        home2 = Path(tempfile.mkdtemp(prefix="bc-test-agy-parent-msg-"))
        try:
            parent_db2 = runner_agy._conversation_db(home2, _PARENT_SID)
            _write_steps_db(parent_db2, [
                (
                    0,
                    127,
                    b'tool123 invoke_subagent {"Subagents":[{"Prompt":"Find files",'
                    b'"Role":"Researcher","TypeName":"research"}]}:',
                ),
                (
                    1,
                    101,
                    b"[Message] timestamp=2026-06-21T10:00:00Z "
                    b"sender=bbbbbbbb-0000-0000-0000-000000000002 "
                    b"priority=MESSAGE_PRIORITY_HIGH content=subagent result text",
                ),
            ])
            _build_child_db(home2)
            w2 = runner_agy._agy_worker_events(
                agy_home=home2, conversation_id=_PARENT_SID, parent_uuid="root",
            )
            t2 = [e["type"] for e in w2]
            if t2.count("worker_start") == 1 and "worker_complete" in t2:
                payload = json.dumps(w2)
                if "Researcher" in payload and "subagent result text" in payload:
                    print(f"{PASS}  invoke_subagent still produces worker_start/result/complete envelope")
                else:
                    print(f"{FAIL}  worker envelope missing role/result: {payload[:120]}")
                    failures += 1
            else:
                print(f"{FAIL}  worker envelope regressed: types={t2}")
                failures += 1
        finally:
            shutil.rmtree(home2, ignore_errors=True)
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    if failures:
        print(f"\nFAILED: {failures} check(s)")
        return 1
    print("\nAll parent-extraction checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

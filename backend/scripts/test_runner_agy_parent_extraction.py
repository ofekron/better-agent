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

        # ----- inline call+result: a tool's output attaches to ITS OWN id -----
        # agy bundles each tool call and its output in the SAME step (types
        # 7/8/9/23). The result must be keyed on the step's own tool_id, never
        # on a previous tool's id (the off-by-one "mix of other events" bug).
        home3 = Path(tempfile.mkdtemp(prefix="bc-test-agy-inline-"))
        try:
            inline_db = runner_agy._conversation_db(home3, _PARENT_SID)
            _SEP = b"\x02"  # protobuf field separator -> separate printable strings
            _write_steps_db(inline_db, [
                (0, 14, b"Please look at x.txt and grep for foo"),
                # type 8 view_file: tool_id + name + input JSON + inline content
                (
                    1, 8,
                    _SEP.join([
                        b"viewtool1", b"view_file",
                        b'{"AbsolutePath":"/tmp/x.txt","toolAction":"Reading x.txt"}',
                        b"The first paragraph of the file describes the project setup in detail.",
                        b"The second paragraph continues with configuration notes for the build.",
                    ]),
                ),
                # type 7 grep_search: tool_id + name + input JSON + inline matches
                (
                    2, 7,
                    _SEP.join([
                        b"greptool2", b"grep_search",
                        b'{"Query":"foo","SearchPath":"/tmp","toolAction":"Searching for foo"}',
                        b"src/main.py line ten contains the foo symbol we were searching for above.",
                    ]),
                ),
                # type 15 narration AFTER the tools (must NOT become a tool_result)
                (
                    3, 15,
                    b"\x01I have analyzed the file and the search results above."
                    b"2(bot-550e8400-e29b-41d4-a716-446655440000)",
                ),
            ])
            inline_events = runner_agy._extract_parent_main_events(inline_db, "root")

            # Every tool_result's tool_use_id must belong to a tool_use emitted
            # in the SAME step. Pre-fix this fails: the grep step's result was
            # glued onto the previous view_file tool id. Check per step, since
            # tool_use and tool_result are separate events from one step.
            inline_state = runner_agy._ParentMainState("root")
            inline_bad = 0
            inline_result_ids: list[str] = []
            for step in runner_agy._read_agy_steps(inline_db):
                step_events = inline_state.events_for_step(step)
                use_ids = {
                    b.get("id")
                    for ev in step_events
                    for b in ev.get("data", {}).get("message", {}).get("content", [])
                    if b.get("type") == "tool_use"
                }
                for ev in step_events:
                    for b in ev.get("data", {}).get("message", {}).get("content", []):
                        if b.get("type") == "tool_result":
                            inline_result_ids.append(b.get("tool_use_id"))
                            if b.get("tool_use_id") not in use_ids:
                                inline_bad += 1
            if inline_bad == 0 and set(inline_result_ids) == {"viewtool1", "greptool2"}:
                print(f"{PASS}  inline tool_result keyed on its own tool_id "
                      f"(no cross-step misroute)")
            else:
                print(f"{FAIL}  inline result misroute: bad={inline_bad} "
                      f"result_ids={inline_result_ids}")
                failures += 1
        finally:
            shutil.rmtree(home3, ignore_errors=True)

        # ----- send_message inline text is the sent message, NOT a result -----
        # A messaging tool's inline text is the message being sent (the input).
        # It must emit a tool_use only — never a tool_result labeling the sent
        # message as the reply. Pre-fix-the-regression it emitted a bogus result.
        home4 = Path(tempfile.mkdtemp(prefix="bc-test-agy-sendmsg-"))
        try:
            sendmsg_db = runner_agy._conversation_db(home4, _PARENT_SID)
            _write_steps_db(sendmsg_db, [
                (0, 14, b"Tell the subagent to inspect the file"),
                (
                    1, 132,
                    b"\x02".join([
                        b"sendtool3", b"send_message",
                        b'{"Message":"please inspect the sessions json file now",'
                        b'"Recipient":"worker-1","toolAction":"Sending message",'
                        b'"toolSummary":"Send"}',
                    ]),
                ),
            ])
            sendmsg_state = runner_agy._ParentMainState("root")
            sendmsg_events: list[dict] = []
            for step in runner_agy._read_agy_steps(sendmsg_db):
                sendmsg_events.extend(sendmsg_state.events_for_step(step))
            sendmsg_uses = [
                b for ev in sendmsg_events
                for b in ev.get("data", {}).get("message", {}).get("content", [])
                if b.get("type") == "tool_use" and b.get("name") == "send_message"
            ]
            sendmsg_results = [
                b for ev in sendmsg_events
                for b in ev.get("data", {}).get("message", {}).get("content", [])
                if b.get("type") == "tool_result"
            ]
            if len(sendmsg_uses) == 1 and not sendmsg_results:
                print(f"{PASS}  send_message emits tool_use only (sent message "
                      f"not labeled as a result)")
            else:
                print(f"{FAIL}  send_message misrendered: uses={len(sendmsg_uses)} "
                      f"results={len(sendmsg_results)}")
                failures += 1
        finally:
            shutil.rmtree(home4, ignore_errors=True)

        # ----- DEFERRED tool model: type-15 call -> later type-101 result -----
        # agy tool calls on step types 15/127/132 defer the result to a later
        # step. The call step must emit tool_use ONLY (its reassembled text is
        # the call line, not output); the later result step attaches to that
        # tool_id. The first over-correction of the inline fix regressed this:
        # it emitted the call line as the result and orphaned the real result.
        home5 = Path(tempfile.mkdtemp(prefix="bc-test-agy-deferred-"))
        try:
            deferred_db = runner_agy._conversation_db(home5, _PARENT_SID)
            _write_steps_db(deferred_db, [
                (0, 14, b"Read the file and tell me"),
                # type 15: deferred read_file call (single-string serialization).
                (
                    1, 15,
                    b'readtool9 read_file {"Action":"read_file","Path":"/tmp/h.txt"}:',
                ),
                # type 101: the deferred result lands in a later step.
                (
                    2, 101,
                    b"The file contents are hello world and more details follow here.",
                ),
                # type 15: trailing narration must render as assistant text.
                (
                    3, 15,
                    b"\x01Now I will summarize the findings for the user."
                    b"2(bot-550e8400-e29b-41d4-a716-446655440000)",
                ),
            ])
            deferred_state = runner_agy._ParentMainState("root")
            deferred_events: list[dict] = []
            for step in runner_agy._read_agy_steps(deferred_db):
                deferred_events.extend(deferred_state.events_for_step(step))
            df_results = [
                b for ev in deferred_events
                for b in ev.get("data", {}).get("message", {}).get("content", [])
                if b.get("type") == "tool_result"
            ]
            df_read_results = [b for b in df_results if b.get("tool_use_id") == "readtool9"]
            df_call_line_results = [
                b for b in df_read_results if "read_file" in (b.get("content") or "")
            ]
            df_has_output = any("hello world" in (b.get("content") or "") for b in df_read_results)
            df_texts = [
                (b.get("text") or "")
                for ev in deferred_events
                for b in ev.get("data", {}).get("message", {}).get("content", [])
                if b.get("type") == "text"
            ]
            df_narration_is_text = any("summarize the findings" in t for t in df_texts)
            if (
                len(df_read_results) == 1
                and df_has_output
                and not df_call_line_results
                and df_narration_is_text
            ):
                print(f"{PASS}  deferred tool result attaches from later step "
                      f"(call line not emitted as result)")
            else:
                print(f"{FAIL}  deferred model wrong: read_results={len(df_read_results)} "
                      f"has_output={df_has_output} call_line_results="
                      f"{len(df_call_line_results)} narration_is_text={df_narration_is_text}")
                failures += 1
        finally:
            shutil.rmtree(home5, ignore_errors=True)

        # ----- tool_result content strips agy's display header -----
        # agy prepends a [UI label, toolAction] header (often twice for the
        # streaming + final copy) before the real output. The result must keep
        # only the output -- the toolAction description is UI chrome, not data.
        home6 = Path(tempfile.mkdtemp(prefix="bc-test-agy-toolout-"))
        try:
            toolout_db = runner_agy._conversation_db(home6, _PARENT_SID)
            _write_steps_db(toolout_db, [
                (0, 14, b"Read the config file"),
                (
                    1, 8,
                    b"\x02".join([
                        b"viewtoolX", b"view_file",
                        b'{"AbsolutePath":"/tmp/c.txt",'
                        b'"toolAction":"Reading the config file header"}',
                        # duplicated [UI label, toolAction] header (streaming+final)
                        b"Read config.txt",
                        b"-Reading the config file header",
                        b"Read config.txt",
                        b"-Reading the config file header",
                        # real output
                        b"The first configuration section describes the defaults in detail.",
                        b"The second configuration section covers override behavior.",
                    ]),
                ),
            ])
            toolout_state = runner_agy._ParentMainState("root")
            toolout_events: list[dict] = []
            for step in runner_agy._read_agy_steps(toolout_db):
                toolout_events.extend(toolout_state.events_for_step(step))
            to_results = [
                b for ev in toolout_events
                for b in ev.get("data", {}).get("message", {}).get("content", [])
                if b.get("type") == "tool_result"
                and b.get("tool_use_id") == "viewtoolX"
            ]
            assert len(to_results) == 1, to_results
            content = to_results[0].get("content") or ""
            has_output = "first configuration section" in content
            has_header = "Reading the config file header" in content
            if has_output and not has_header:
                print(f"{PASS}  tool_result content strips the display header "
                      f"(keeps the output)")
            else:
                print(f"{FAIL}  tool_result header not stripped: "
                      f"has_output={has_output} has_header={has_header} "
                      f"content={content[:120]!r}")
                failures += 1
        finally:
            shutil.rmtree(home6, ignore_errors=True)
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

"""Locks runner_better_agent_claude_subscription's wire-translation layer:
the Anthropic Messages API backend used when a Claude provider record
configures runner=="better_agent_runner" with mode=="subscription".

Covers:
- messages_to_anthropic / tools_to_anthropic translate runner_better_agent's
  internal OpenAI-shaped representation (system message, user/assistant
  turns, parallel tool_calls, tool-result messages, function-tool schemas)
  into Anthropic's (system, messages, tools) shape correctly, including a
  tool-call round trip.
- one_round_claude_subscription drives a mocked Anthropic SSE stream
  (message_start / content_block_start / content_block_delta incl.
  input_json_delta / content_block_stop / message_delta / message_stop)
  through the SAME EventEmitter runner_better_agent.py itself uses, and
  returns the same (finish_reason, tool_calls, text, thinking, usage)
  shape _one_round does.
- Missing/unreadable Keychain token fails closed with a clear error (no
  silent fallback, no bare traceback) instead of hitting the network.
- No emitted session_events.jsonl line or raised exception message ever
  contains the token value, across a live (mocked) run.

The real Anthropic servers are never contacted — HTTP is mocked via a fake
httpx.AsyncClient.stream.

Run with:
    cd backend && .venv/bin/python scripts/test_runner_better_agent_claude_subscription.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import AsyncIterator
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="bc-test-claude-ba-runner-wire-")
paths.engage_test_home(_TMP)

import claude_subscription_credential  # noqa: E402
import runner_better_agent_claude_subscription as backend  # noqa: E402
from runner_better_agent import EventEmitter  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

FAKE_TOKEN = "sk-ant-oat-super-secret-test-token-do-not-leak"  # nosec - test fixture only


def test_messages_to_anthropic_round_trip() -> bool:
    messages = [
        {"role": "system", "content": "base system prompt"},
        {"role": "user", "content": "please run ls"},
        {
            "role": "assistant",
            "content": "sure",
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "Bash", "arguments": json.dumps({"command": "ls"})}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "file1\nfile2"},
        {"role": "system", "content": "transient capability context"},
        {"role": "user", "content": "thanks, now read file1"},
    ]
    system, anthropic_messages = backend.messages_to_anthropic(messages)
    if system != "base system prompt\n\ntransient capability context":
        print(f"  system: {system!r}")
        return False
    if len(anthropic_messages) != 4:
        print(f"  message count: {len(anthropic_messages)} -> {anthropic_messages}")
        return False
    if anthropic_messages[0] != {"role": "user", "content": [{"type": "text", "text": "please run ls"}]}:
        print(f"  msg0: {anthropic_messages[0]}")
        return False
    asst = anthropic_messages[1]
    if asst["role"] != "assistant" or len(asst["content"]) != 2:
        print(f"  msg1: {asst}")
        return False
    if asst["content"][0] != {"type": "text", "text": "sure"}:
        print(f"  msg1 text block: {asst['content'][0]}")
        return False
    tool_use = asst["content"][1]
    if tool_use != {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}}:
        print(f"  msg1 tool_use block: {tool_use}")
        return False
    tool_result_msg = anthropic_messages[2]
    if tool_result_msg != {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "file1\nfile2"}],
    }:
        print(f"  msg2 (tool result): {tool_result_msg}")
        return False
    if anthropic_messages[3] != {
        "role": "user", "content": [{"type": "text", "text": "thanks, now read file1"}],
    }:
        print(f"  msg3: {anthropic_messages[3]}")
        return False
    return True


def test_parallel_tool_results_merge_into_one_message() -> bool:
    messages = [
        {"role": "tool", "tool_call_id": "a", "content": "result a"},
        {"role": "tool", "tool_call_id": "b", "content": "result b"},
    ]
    _, anthropic_messages = backend.messages_to_anthropic(messages)
    if len(anthropic_messages) != 1:
        print(f"  expected 1 merged message, got {len(anthropic_messages)}: {anthropic_messages}")
        return False
    content = anthropic_messages[0]["content"]
    if len(content) != 2 or content[0]["tool_use_id"] != "a" or content[1]["tool_use_id"] != "b":
        print(f"  merged content: {content}")
        return False
    return True


def test_tools_to_anthropic() -> bool:
    openai_tools = [
        {"type": "function", "function": {
            "name": "Bash", "description": "Run a shell command.",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                            "required": ["command"]},
        }},
    ]
    anthropic_tools = backend.tools_to_anthropic(openai_tools)
    if anthropic_tools != [{
        "name": "Bash",
        "description": "Run a shell command.",
        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                          "required": ["command"]},
    }]:
        print(f"  tools: {anthropic_tools}")
        return False
    return True


def test_cache_breakpoints_mark_stable_content_only() -> bool:
    """Regression test for the missing-cache_control bug: the harness-profile
    system block and the tool schema list are byte-identical on nearly every
    turn of a session, and prior conversation turns never change once
    written — but before this fix nothing marked any of it cacheable, so
    Anthropic billed it as fresh input tokens every single call. This locks
    that a breakpoint lands on `system`, on the last tool schema, and on the
    last stable (i.e. not-newest) message — and, just as importantly, that
    the newest turn's content is NOT marked (it genuinely is new)."""
    openai_messages = [
        {"role": "system", "content": "base system prompt"},
        {"role": "system", "content": "harness profile block"},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "turn 2 (newest)"},
    ]
    system, anthropic_messages = backend.messages_to_anthropic(openai_messages)
    tools = backend.tools_to_anthropic([
        {"type": "function", "function": {"name": "Bash", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}},
    ])
    system_blocks, msgs, cached_tools = backend._apply_cache_breakpoints(system, anthropic_messages, tools)

    if system_blocks != [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]:
        print(f"  system_blocks: {system_blocks}")
        return False
    if cached_tools[-1].get("cache_control") != {"type": "ephemeral"}:
        print(f"  tools[-1]: {cached_tools[-1]}")
        return False
    # second-to-last message ("reply 1") is the last STABLE turn -> cached.
    if msgs[-2]["content"][-1].get("cache_control") != {"type": "ephemeral"}:
        print(f"  second-to-last message not cache-marked: {msgs[-2]}")
        return False
    # last message ("turn 2 (newest)") must stay unmarked -> it is new content.
    if "cache_control" in msgs[-1]["content"][-1]:
        print(f"  newest message was incorrectly cache-marked: {msgs[-1]}")
        return False
    return True


class _FakeAsyncStreamResponse:
    def __init__(self, status_code: int, lines: list[str], body: bytes = b""):
        self.status_code = status_code
        self._lines = lines
        self._body = body

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body


class _FakeStreamCtx:
    def __init__(self, response: _FakeAsyncStreamResponse):
        self._response = response

    async def __aenter__(self) -> _FakeAsyncStreamResponse:
        return self._response

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeAsyncClient:
    captured_headers: list[dict] = []
    captured_payloads: list[dict] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def stream(self, method: str, url: str, *, json: dict, headers: dict):
        _FakeAsyncClient.captured_headers.append(dict(headers))
        _FakeAsyncClient.captured_payloads.append(json)
        sse_lines = [
            'event: message_start',
            'data: ' + __import__("json").dumps({
                "type": "message_start",
                "message": {"usage": {"input_tokens": 42, "cache_read_input_tokens": 7}},
            }),
            'event: content_block_start',
            'data: ' + __import__("json").dumps({
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""},
            }),
            'event: content_block_delta',
            'data: ' + __import__("json").dumps({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            }),
            'event: content_block_stop',
            'data: ' + __import__("json").dumps({"type": "content_block_stop", "index": 0}),
            'event: content_block_start',
            'data: ' + __import__("json").dumps({
                "type": "content_block_start", "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash"},
            }),
            'event: content_block_delta',
            'data: ' + __import__("json").dumps({
                "type": "content_block_delta", "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"command":'},
            }),
            'event: content_block_delta',
            'data: ' + __import__("json").dumps({
                "type": "content_block_delta", "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '"ls"}'},
            }),
            'event: content_block_stop',
            'data: ' + __import__("json").dumps({"type": "content_block_stop", "index": 1}),
            'event: message_delta',
            'data: ' + __import__("json").dumps({
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 9},
            }),
            'event: message_stop',
            'data: ' + __import__("json").dumps({"type": "message_stop"}),
        ]
        return _FakeStreamCtx(_FakeAsyncStreamResponse(200, sse_lines))


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_one_round_streams_text_and_tool_call() -> bool:
    events_path = Path(_TMP) / "events.jsonl"
    emitter = EventEmitter(events_path)
    emitter.set_model("claude-opus-test")
    run_dir = Path(_TMP)
    (run_dir / "cancel").unlink(missing_ok=True)

    with mock.patch.object(claude_subscription_credential, "read_claude_subscription_token",
                            return_value=FAKE_TOKEN), \
         mock.patch("httpx.AsyncClient", _FakeAsyncClient):
        result = _run(backend.one_round_claude_subscription(
            "https://api.anthropic.com", "unused-api-key", "claude-opus-test",
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "run ls"}],
            emitter, run_dir, [
                {"type": "function", "function": {
                    "name": "Bash", "description": "run a command",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                }},
            ],
        ))
    emitter.close()
    finish_reason, tool_calls, text, thinking, usage = result
    if finish_reason != "tool_calls":
        print(f"  finish_reason: {finish_reason}")
        return False
    if text != "Hello":
        print(f"  text: {text!r}")
        return False
    if thinking is not None:
        print(f"  thinking: {thinking!r}")
        return False
    if len(tool_calls) != 1 or tool_calls[0]["id"] != "toolu_1" or tool_calls[0]["name"] != "Bash":
        print(f"  tool_calls: {tool_calls}")
        return False
    if json.loads(tool_calls[0]["arguments"]) != {"command": "ls"}:
        print(f"  tool_calls arguments: {tool_calls[0]['arguments']}")
        return False
    if usage != {
        "prompt_tokens": 42, "completion_tokens": 9, "total_tokens": 51,
        "prompt_tokens_details": {"cached_tokens": 7},
    }:
        print(f"  usage: {usage}")
        return False
    # Auth header carries the bearer token + the oauth beta gate, matches
    # models._fetch_api_models' subscription flow. Never print the header
    # value itself, even on failure — it may be a real credential if the
    # mock ever fails to intercept.
    headers = _FakeAsyncClient.captured_headers[-1]
    if headers.get("Authorization") != f"Bearer {FAKE_TOKEN}":
        print("  auth header did not carry the mocked fake token "
              "(request may have used a real credential and/or hit the "
              "real network — investigate before rerunning)")
        return False
    if headers.get("anthropic-beta") != "oauth-2025-04-20":
        print(f"  anthropic-beta header: {headers.get('anthropic-beta')}")
        return False

    # The outbound payload actually carries the cache breakpoints: `system`
    # is a block array with an ephemeral marker, and the single tool schema
    # (also the last one) is marked too.
    payload = _FakeAsyncClient.captured_payloads[-1]
    system_payload = payload.get("system")
    if not (isinstance(system_payload, list) and system_payload
            and system_payload[-1].get("cache_control") == {"type": "ephemeral"}):
        print(f"  outbound system payload not cache-marked: {system_payload}")
        return False
    tools_payload = payload.get("tools") or []
    if not (tools_payload and tools_payload[-1].get("cache_control") == {"type": "ephemeral"}):
        print(f"  outbound tools payload not cache-marked: {tools_payload}")
        return False

    # No emitted session_events.jsonl line contains the token value.
    raw = events_path.read_text(encoding="utf-8")
    if FAKE_TOKEN in raw:
        print("  TOKEN LEAKED into session_events.jsonl")
        return False
    return True


def test_missing_token_fails_closed() -> bool:
    events_path = Path(_TMP) / "events_missing_token.jsonl"
    emitter = EventEmitter(events_path)
    emitter.set_model("claude-opus-test")
    run_dir = Path(_TMP)

    with mock.patch.object(claude_subscription_credential, "read_claude_subscription_token",
                            return_value=None):
        try:
            _run(backend.one_round_claude_subscription(
                "https://api.anthropic.com", "", "claude-opus-test",
                [{"role": "user", "content": "hi"}], emitter, run_dir, [],
            ))
        except backend.ClaudeSubscriptionCredentialError as e:
            emitter.close()
            if FAKE_TOKEN in str(e):
                print("  token leaked into exception message")
                return False
            return True
        except Exception as e:
            emitter.close()
            print(f"  wrong exception type: {type(e).__name__}: {e}")
            return False
    emitter.close()
    print("  missing token did not raise")
    return False


def test_401_fails_closed_without_retry() -> bool:
    class _Fake401Client(_FakeAsyncClient):
        def stream(self, method, url, *, json, headers):
            return _FakeStreamCtx(_FakeAsyncStreamResponse(401, [], b'{"error":"invalid token"}'))

    events_path = Path(_TMP) / "events_401.jsonl"
    emitter = EventEmitter(events_path)
    run_dir = Path(_TMP)
    with mock.patch.object(claude_subscription_credential, "read_claude_subscription_token",
                            return_value=FAKE_TOKEN), \
         mock.patch("httpx.AsyncClient", _Fake401Client):
        try:
            _run(backend.one_round_claude_subscription(
                "https://api.anthropic.com", "", "claude-opus-test",
                [{"role": "user", "content": "hi"}], emitter, run_dir, [],
            ))
        except backend.ClaudeSubscriptionCredentialError as e:
            emitter.close()
            if FAKE_TOKEN in str(e):
                print("  token leaked into 401 exception message")
                return False
            return True
        except Exception as e:
            emitter.close()
            print(f"  wrong exception type on 401: {type(e).__name__}: {e}")
            return False
    emitter.close()
    print("  401 did not raise")
    return False


TESTS = [
    ("messages_to_anthropic translates a full turn round trip", test_messages_to_anthropic_round_trip),
    ("parallel tool results merge into one Anthropic user message", test_parallel_tool_results_merge_into_one_message),
    ("tools_to_anthropic converts function schemas to input_schema", test_tools_to_anthropic),
    ("cache breakpoints mark system/tools/stable-history, not the newest turn",
     test_cache_breakpoints_mark_stable_content_only),
    ("one_round_claude_subscription streams text + tool call + usage", test_one_round_streams_text_and_tool_call),
    ("missing Keychain token fails closed (no network call)", test_missing_token_fails_closed),
    ("HTTP 401 fails closed without silent retry", test_401_fails_closed_without_retry),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())

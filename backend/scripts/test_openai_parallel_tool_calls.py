"""Regression: Better Agent runner enables parallel tool calls.

The runner dispatches an assistant response's tool calls concurrently via
`_dispatch_tool_batch`, with a `barrier` tool as an explicit join point for
calls fired earlier in the same batch. Resume-safety is preserved by
`_resume_safe_messages` dropping an entire unbalanced multi-call block and
history being persisted only once a batch is fully balanced. The request
must therefore opt INTO multi-tool rounds.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="openai_parallel_tool_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import runner_better_agent  # noqa: E402


class _Resp:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        yield 'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}'
        yield "data: [DONE]"

    async def aread(self):
        return b""


class _Client:
    payloads: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, *, json=None, headers=None):
        self.payloads.append(json or {})
        return _Resp()


def test_stream_chat_enables_parallel_tool_calls() -> None:
    original = runner_better_agent.httpx.AsyncClient
    _Client.payloads.clear()
    try:
        runner_better_agent.httpx.AsyncClient = _Client  # type: ignore[assignment]

        async def _go() -> None:
            async for _ in runner_better_agent._stream_chat(
                "http://stub",
                "key",
                "model",
                [{"role": "user", "content": "hi"}],
                [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}}],
            ):
                pass

        asyncio.run(_go())
    finally:
        runner_better_agent.httpx.AsyncClient = original

    assert _Client.payloads, "_stream_chat did not issue a request"
    assert _Client.payloads[0].get("parallel_tool_calls") is True


if __name__ == "__main__":
    test_stream_chat_enables_parallel_tool_calls()
    print("PASS openai parallel tool calls enabled")

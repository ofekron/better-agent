"""Regression: OpenAI runner disables parallel tool calls.

Incremental history persistence trims incomplete assistant tool-call blocks so
stored history is always resume-safe. If the provider can emit multiple tool
calls in one assistant message, a restart after the first tool result would have
to drop the whole block and could replay the completed side effect. The request
must therefore force single-tool rounds.
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

import runner_openai  # noqa: E402


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


def test_stream_chat_disables_parallel_tool_calls() -> None:
    original = runner_openai.httpx.AsyncClient
    _Client.payloads.clear()
    try:
        runner_openai.httpx.AsyncClient = _Client  # type: ignore[assignment]

        async def _go() -> None:
            async for _ in runner_openai._stream_chat(
                "http://stub",
                "key",
                "model",
                [{"role": "user", "content": "hi"}],
                [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}}],
            ):
                pass

        asyncio.run(_go())
    finally:
        runner_openai.httpx.AsyncClient = original

    assert _Client.payloads, "_stream_chat did not issue a request"
    assert _Client.payloads[0].get("parallel_tool_calls") is False


if __name__ == "__main__":
    test_stream_chat_disables_parallel_tool_calls()
    print("PASS openai parallel tool calls disabled")

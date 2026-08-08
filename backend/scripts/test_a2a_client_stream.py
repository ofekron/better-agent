"""Outbound A2A JSON-RPC client: message/send and the message/stream SSE
lifecycle. HTTP is mocked via httpx.MockTransport — no real network
calls, mirroring scripts/test_runner_better_agent_codex_subscription.py's
convention."""
from __future__ import annotations

import asyncio
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import httpx
import pytest

from a2a.client import A2ARpcError, send_message, stream_message
from a2a.url_policy import A2AUrlPolicyError

_RealAsyncClient = httpx.AsyncClient


def _mock_async_client(handler):
    return lambda *a, **k: _RealAsyncClient(*a, transport=httpx.MockTransport(handler), **k)


def _sse_body(results: list[dict]) -> bytes:
    lines = []
    for index, result in enumerate(results):
        envelope = {"jsonrpc": "2.0", "id": f"req-{index}", "result": result}
        lines.append(f"data: {json.dumps(envelope)}\n\n")
    return "".join(lines).encode("utf-8")


def _collect_stream(**kwargs) -> list[dict]:
    async def _go():
        return [result async for result in stream_message(**kwargs)]
    return asyncio.run(_go())


def test_stream_message_completes_with_status_updates(monkeypatch):
    results = [
        {"status": {"state": "submitted"}},
        {"status": {"state": "working"}},
        {"status": {"state": "completed", "message": {"parts": [{"type": "text", "text": "done"}]}}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "text/event-stream"
        body = json.loads(request.content)
        assert body["method"] == "message/stream"
        return httpx.Response(200, content=_sse_body(results),
                               headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    collected = _collect_stream(
        base_url="https://agent.example.com", auth_header_name="", auth_secret="",
        text="what's the weather",
    )
    assert len(collected) == 3
    assert collected[-1]["status"]["state"] == "completed"


def test_stream_message_applies_auth_header(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "s3cr3t"
        return httpx.Response(
            200, content=_sse_body([{"status": {"state": "completed"}}]),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    _collect_stream(
        base_url="https://agent.example.com", auth_header_name="x-api-key",
        auth_secret="s3cr3t", text="hi",
    )


def test_stream_message_accumulates_artifact_chunks(monkeypatch):
    results = [
        {"artifact": {"artifactId": "a1", "parts": [{"type": "text", "text": "Hello "}]}, "append": False},
        {"artifact": {"artifactId": "a1", "parts": [{"type": "text", "text": "world"}]}, "append": True},
        {"status": {"state": "completed"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body(results),
                               headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    collected = _collect_stream(
        base_url="https://agent.example.com", auth_header_name="", auth_secret="", text="hi",
    )
    artifacts = [r for r in collected if "artifact" in r]
    assert len(artifacts) == 2
    assert collected[-1]["status"]["state"] == "completed"


@pytest.mark.parametrize("state", ["failed", "canceled", "rejected", "input-required", "auth-required"])
def test_stream_message_stops_on_paused_or_terminal_state(monkeypatch, state):
    results = [{"status": {"state": "working"}}, {"status": {"state": state}}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body(results),
                               headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    collected = _collect_stream(
        base_url="https://agent.example.com", auth_header_name="", auth_secret="", text="hi",
    )
    assert collected[-1]["status"]["state"] == state


def test_stream_message_raises_when_stream_ends_without_terminal(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_sse_body([{"status": {"state": "working"}}]),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    with pytest.raises(A2ARpcError, match="without a terminal"):
        _collect_stream(
            base_url="https://agent.example.com", auth_header_name="", auth_secret="", text="hi",
        )


def test_stream_message_raises_on_rpc_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        envelope = {"jsonrpc": "2.0", "id": "req-0", "error": {"code": -32000, "message": "boom"}}
        return httpx.Response(
            200, content=f"data: {json.dumps(envelope)}\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    with pytest.raises(A2ARpcError, match="boom"):
        _collect_stream(
            base_url="https://agent.example.com", auth_header_name="", auth_secret="", text="hi",
        )


def test_stream_message_rejects_non_loopback_http():
    with pytest.raises(A2AUrlPolicyError):
        asyncio.run(stream_message(
            base_url="http://agent.example.com", auth_header_name="", auth_secret="", text="hi",
        ).__anext__())


def test_send_message_returns_result(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "message/send"
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"status": {"state": "completed"}},
        })

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    result = asyncio.run(send_message(
        base_url="https://agent.example.com", auth_header_name="", auth_secret="", text="hi",
    ))
    assert result["status"]["state"] == "completed"


def test_send_message_raises_on_rpc_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "x",
            "error": {"code": -32000, "message": "nope"},
        })

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    with pytest.raises(A2ARpcError, match="nope"):
        asyncio.run(send_message(
            base_url="https://agent.example.com", auth_header_name="", auth_secret="", text="hi",
        ))

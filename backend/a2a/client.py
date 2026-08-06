"""Hand-rolled outbound A2A JSON-RPC 2.0 client: `message/send` and the
streaming `message/stream` (SSE) task-update loop.

Design decision (recorded on board card 7dfd091d7fa1): hand-rolled over
the `a2a-python` SDK. The SDK bundles a full server framework (Starlette
app, push-notification receiver, task store abstractions) we don't need
for an outbound-only client, and pulls in dependencies Better Agent has
no other use for. This module only ever calls out to a URL the user
explicitly registered — no inbound surface.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Optional

import httpx

from a2a.url_policy import validate_base_url

_REQUEST_TIMEOUT = 30.0
_STREAM_TIMEOUT = httpx.Timeout(10.0, read=300.0)

TERMINAL_TASK_STATES = frozenset({"completed", "failed", "canceled", "rejected"})
PAUSED_TASK_STATES = frozenset({"input-required", "auth-required"})


class A2ARpcError(RuntimeError):
    pass


def _headers(auth_header_name: str, auth_secret: str, accept: str) -> dict:
    headers = {"content-type": "application/json", "accept": accept}
    if auth_header_name and auth_secret:
        headers[auth_header_name] = auth_secret
    return headers


def _message_params(text: str, task_id: Optional[str], context_id: Optional[str]) -> dict:
    message: dict[str, Any] = {
        "role": "user",
        "parts": [{"type": "text", "text": text}],
        "messageId": uuid.uuid4().hex,
    }
    if task_id:
        message["taskId"] = task_id
    if context_id:
        message["contextId"] = context_id
    return {"message": message}


def _rpc_body(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}


def _raise_on_rpc_error(envelope: dict, base_url: str) -> dict:
    if "error" in envelope:
        error = envelope.get("error") or {}
        raise A2ARpcError(
            f"{base_url} returned JSON-RPC error "
            f"{error.get('code')}: {error.get('message')}"
        )
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise A2ARpcError(f"{base_url} returned a JSON-RPC response with no result object")
    return result


async def send_message(
    *,
    base_url: str,
    auth_header_name: str,
    auth_secret: str,
    text: str,
    task_id: Optional[str] = None,
    context_id: Optional[str] = None,
) -> dict:
    """Non-streaming `message/send`. Returns the JSON-RPC `result`
    (a Task or a Message object per the A2A spec)."""
    normalized = validate_base_url(base_url)
    body = _rpc_body("message/send", _message_params(text, task_id, context_id))
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        response = await client.post(
            normalized,
            content=json.dumps(body),
            headers=_headers(auth_header_name, auth_secret, "application/json"),
        )
    response.raise_for_status()
    return _raise_on_rpc_error(response.json(), normalized)


def _task_terminal(result: dict) -> tuple[bool, Optional[str]]:
    """Returns (is_stream_terminal, state) for one streamed JSON-RPC result."""
    status = result.get("status")
    if isinstance(status, dict):
        state = status.get("state")
        if isinstance(state, str) and (
            state in TERMINAL_TASK_STATES or state in PAUSED_TASK_STATES
        ):
            return True, state
        if result.get("final") is True:
            return True, state if isinstance(state, str) else None
        return False, state if isinstance(state, str) else None
    if result.get("kind") == "message" or (result.get("role") and result.get("parts")):
        # A bare Message result with no task means the agent answered
        # directly without creating a trackable task — treat as complete.
        return True, "completed"
    return False, None


async def stream_message(
    *,
    base_url: str,
    auth_header_name: str,
    auth_secret: str,
    text: str,
    task_id: Optional[str] = None,
    context_id: Optional[str] = None,
) -> AsyncIterator[dict]:
    """Streaming `message/stream`. Yields each JSON-RPC `result` object
    (Task / Message / TaskStatusUpdateEvent / TaskArtifactUpdateEvent) as
    it arrives over SSE. Raises A2ARpcError if the stream ends without
    ever reaching a terminal or paused task state."""
    normalized = validate_base_url(base_url)
    body = _rpc_body("message/stream", _message_params(text, task_id, context_id))
    reached_terminal = False
    async with httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as client:
        async with client.stream(
            "POST",
            normalized,
            content=json.dumps(body),
            headers=_headers(auth_header_name, auth_secret, "text/event-stream"),
        ) as response:
            response.raise_for_status()
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line == "":
                    if not data_lines:
                        continue
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        envelope = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    result = _raise_on_rpc_error(envelope, normalized)
                    yield result
                    is_terminal, _state = _task_terminal(result)
                    if is_terminal:
                        reached_terminal = True
                        return
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip())
    if not reached_terminal:
        raise A2ARpcError(
            f"{normalized} closed the stream without a terminal or paused task state"
        )

"""Anthropic Messages API backend for `runner_better_agent.py`.

Used when a Claude provider record configures `runner=="better_agent_runner"`
with `mode=="subscription"`: speaks Anthropic's native Messages API
(`POST {base_url}/v1/messages`) over the SAME OAuth bearer token the
`claude` CLI's own login produces (read via
`claude_subscription_credential.read_claude_subscription_token()` — the
same Keychain entry `models._fetch_api_models` already reads for
model-list refresh).

This is NOT the generic OpenAI Chat-Completions path (`_stream_chat` in
runner_better_agent.py) — Anthropic's wire shape differs structurally:
`system` is a separate top-level field (not a role in `messages`), tool
schemas use `input_schema` (not `parameters`), and streaming uses named SSE
event types (`message_start`/`content_block_start`/`content_block_delta`/
`content_block_stop`/`message_delta`/`message_stop`) instead of
Chat Completions' uniform `delta` chunks.

Reuses runner_better_agent.py's `EventEmitter` and its internal
OpenAI-shaped `messages` list representation verbatim — only the wire
format in/out differs. `one_round_claude_subscription` is a drop-in
alternative to `_one_round` with the identical call signature (accepting
and ignoring the unused `api_key` parameter, since auth is a bearer token
read fresh from the Keychain here, not an API key), so `_run`'s main tool
loop needs no other changes to dispatch to it.

Security: the bearer token is held only in local variables for the life of
a single HTTP call and is never logged, never written to
session_events.jsonl, and never included in a raised exception message.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator, Optional

import httpx

import claude_subscription_credential

logger = logging.getLogger("runner_better_agent_claude_subscription")

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_BETA_OAUTH = "oauth-2025-04-20"
# Subscription mode never honors a record-configured base_url — matches
# provider_claude.py's native-runner precedent, which pops
# ANTHROPIC_BASE_URL entirely for subscription mode (a corporate-proxy
# override is a native-runner/api_key-mode-only escape hatch).
DEFAULT_BASE_URL = "https://api.anthropic.com"
# Anthropic requires max_tokens; Chat Completions leaves it optional. High
# enough for long agentic replies without being unbounded.
DEFAULT_MAX_TOKENS = 8192


class ClaudeSubscriptionCredentialError(RuntimeError):
    """Raised when the Claude subscription OAuth token cannot be read or is
    rejected by Anthropic. Never carries the token value itself."""


def _require_token() -> str:
    token = claude_subscription_credential.read_claude_subscription_token()
    if not token:
        raise ClaudeSubscriptionCredentialError(
            "Claude subscription credentials are unavailable (no OAuth "
            'token found in the macOS Keychain for "Claude Code-'
            'credentials"). Log in with the claude CLI, then retry.'
        )
    return token


_DATA_URL_RE = re.compile(r"^data:(?P<media_type>[^;]+);base64,(?P<data>.+)$", re.DOTALL)


def _openai_content_part_to_anthropic(part: dict) -> Optional[dict]:
    ptype = part.get("type")
    if ptype == "text":
        return {"type": "text", "text": str(part.get("text") or "")}
    if ptype == "image_url":
        url = str((part.get("image_url") or {}).get("url") or "")
        match = _DATA_URL_RE.match(url)
        if not match:
            return None
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": match.group("media_type"),
                "data": match.group("data"),
            },
        }
    return None


def _openai_content_to_anthropic_blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks: list[dict] = []
        for part in content:
            if isinstance(part, dict):
                block = _openai_content_part_to_anthropic(part)
                if block:
                    blocks.append(block)
        return blocks
    return []


def _assistant_message_to_anthropic(msg: dict) -> dict:
    blocks: list[dict] = list(_openai_content_to_anthropic_blocks(msg.get("content")))
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            tool_input = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            tool_input = {}
        blocks.append({
            "type": "tool_use",
            "id": call.get("id") or "",
            "name": fn.get("name") or "",
            "input": tool_input,
        })
    return {"role": "assistant", "content": blocks}


def messages_to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """Translate runner_better_agent's internal OpenAI-shaped message list
    into Anthropic's (system, messages) shape.

    - Every role=="system" entry (the base system prompt inserted at index
      0, plus a transient per-turn capability-context message some turns
      append right before the latest user prompt) is concatenated into the
      single top-level `system` string Anthropic expects — Anthropic has no
      interleaved system-message concept. Order is preserved; capability
      context still lands after the base prompt textually, it is just no
      longer positioned inside the `messages` array.
    - Consecutive role=="tool" entries (one per call in a parallel tool
      batch) are merged into a single Anthropic user message carrying one
      `tool_result` block per call — Anthropic requires every tool_result
      answering one assistant turn's tool_use blocks to arrive in one user
      message, unlike Chat Completions' one-message-per-result shape.
    - `reasoning_content` (thinking) is intentionally NOT replayed back
      into outgoing assistant messages: Anthropic's extended-thinking
      replay contract requires an opaque `signature` field on each
      thinking block that this runner does not currently capture. This is
      a documented limitation, not a data-loss bug — thinking is still
      streamed live to the user via `feed_thinking_delta` when produced.
    """
    system_parts: list[str] = []
    anthropic_messages: list[dict] = []
    pending_tool_results: list[dict] = []

    def _flush_tool_results() -> None:
        if pending_tool_results:
            anthropic_messages.append({
                "role": "user",
                "content": list(pending_tool_results),
            })
            pending_tool_results.clear()

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            content = msg.get("content")
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue
        if role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id") or "",
                "content": str(msg.get("content") or ""),
            })
            continue
        _flush_tool_results()
        if role == "user":
            anthropic_messages.append({
                "role": "user",
                "content": _openai_content_to_anthropic_blocks(msg.get("content")),
            })
        elif role == "assistant":
            anthropic_messages.append(_assistant_message_to_anthropic(msg))
    _flush_tool_results()
    return "\n\n".join(system_parts), anthropic_messages


def tools_to_anthropic(tool_schemas: list[dict]) -> list[dict]:
    """Convert this runner's OpenAI-shaped function-tool schemas
    (`{"type":"function","function":{"name","description","parameters"}}`)
    into Anthropic's tool shape (`{"name","description","input_schema"}`)."""
    tools: list[dict] = []
    for schema in tool_schemas:
        fn = schema.get("function") if schema.get("type") == "function" else schema
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        tools.append({
            "name": fn["name"],
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return tools


def _anthropic_usage_to_chat_completions_usage(usage: dict, previous: Optional[dict]) -> dict:
    """Anthropic splits usage across `message_start` (input/cache tokens)
    and `message_delta` (cumulative output tokens) instead of Chat
    Completions' single trailing usage chunk. Merge onto `previous` so
    `_run`'s accumulator (which reads Chat-Completions field names) sees a
    complete, consistently-shaped snapshot after either event."""
    merged = dict(previous or {})
    if "input_tokens" in usage:
        merged["prompt_tokens"] = usage.get("input_tokens", 0) or 0
        merged["prompt_tokens_details"] = {
            "cached_tokens": usage.get("cache_read_input_tokens", 0) or 0,
        }
    if "output_tokens" in usage:
        merged["completion_tokens"] = usage.get("output_tokens", 0) or 0
    merged["total_tokens"] = merged.get("prompt_tokens", 0) + merged.get("completion_tokens", 0)
    return merged


async def _stream_messages(
    base_url: str, model: str, system: str, messages: list[dict],
    tools: list[dict],
) -> AsyncIterator[dict]:
    """Yield parsed Anthropic Messages API SSE event dicts."""
    token = _require_token()
    url = base_url.rstrip("/") + "/v1/messages"
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "messages": messages,
        "stream": True,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": ANTHROPIC_BETA_OAUTH,
        "content-type": "application/json",
    }
    del token  # not needed past request construction; never logged either way
    timeout = httpx.Timeout(connect=15.0, read=1800.0, write=30.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")[:500]
                if resp.status_code in (401, 403):
                    # Fail closed: no silent retry with (possibly stale)
                    # credentials. The caller surfaces this through the
                    # runner's normal error path (`emitter.emit_error` +
                    # `complete.json.error`).
                    raise ClaudeSubscriptionCredentialError(
                        f"Claude subscription auth rejected (HTTP {resp.status_code}); "
                        "the OAuth token may have expired mid-turn. Log in again "
                        "with the claude CLI, then retry."
                    )
                raise RuntimeError(f"HTTP {resp.status_code}: {body}")
            event_name: Optional[str] = None
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data:
                    continue
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event_name and "type" not in parsed:
                    parsed["type"] = event_name
                yield parsed


async def one_round_claude_subscription(
    base_url: str, api_key: str, model: str, messages: list[dict],
    emitter: Any, run_dir: Any, tool_schemas: list[dict],
    reasoning_effort: Optional[str] = None,
    codex_home: Optional[Any] = None, app_session_id: str = "",
) -> tuple[Optional[str], list[dict], Optional[str], Optional[str], Optional[dict]]:
    """Drop-in equivalent of `runner_better_agent._one_round` for the
    Anthropic Messages API backend, matching its call signature exactly
    (including the codex-only trailing params) so `_run`'s single dispatch
    call site (`round_fn(...)`) can call either backend interchangeably.

    `api_key` is accepted but unused (auth is a bearer token read fresh
    from the Keychain per round, not a static key). `reasoning_effort` is
    likewise accepted for signature parity but not yet mapped onto
    Claude's extended-thinking token budget — a documented limitation, not
    an oversight; wiring it up needs a decision on how BA's five-level
    `reasoning_effort` enum maps onto a `budget_tokens` value, which is out
    of scope for this backend's first pass. `codex_home`/`app_session_id`
    are codex-subscription-only parameters (see
    runner_better_agent_codex_subscription.py); accepted here purely so
    this function's signature matches `_one_round`'s, never used.
    """
    del api_key, reasoning_effort, codex_home, app_session_id
    system, anthropic_messages = messages_to_anthropic(messages)
    tools = tools_to_anthropic(tool_schemas)
    finish_reason: Optional[str] = None
    usage: Optional[dict] = None
    async for event in _stream_messages(base_url, model, system, anthropic_messages, tools):
        if (run_dir / "cancel").exists():
            break
        etype = event.get("type")
        if etype == "message_start":
            msg_usage = (event.get("message") or {}).get("usage")
            if msg_usage:
                usage = _anthropic_usage_to_chat_completions_usage(msg_usage, usage)
        elif etype == "content_block_start":
            idx = event.get("index", 0)
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                emitter.feed_tool_call_delta(idx, block.get("id"), block.get("name"), None)
        elif etype == "content_block_delta":
            idx = event.get("index", 0)
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                emitter.feed_text_delta(delta.get("text") or "")
            elif dtype == "thinking_delta":
                emitter.feed_thinking_delta(delta.get("thinking") or "")
            elif dtype == "input_json_delta":
                emitter.feed_tool_call_delta(idx, None, None, delta.get("partial_json") or "")
        elif etype == "message_delta":
            delta = event.get("delta") or {}
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                finish_reason = "tool_calls" if stop_reason == "tool_use" else "stop"
            delta_usage = event.get("usage")
            if delta_usage:
                usage = _anthropic_usage_to_chat_completions_usage(delta_usage, usage)
        elif etype == "message_stop":
            break
        elif etype == "error":
            err = event.get("error") or {}
            raise RuntimeError(f"Anthropic stream error: {err.get('type')}: {err.get('message')}")

    thinking = emitter.close_thinking()
    text = emitter.close_text()
    tool_calls = emitter.finalize_tool_calls()
    if finish_reason is None:
        finish_reason = "tool_calls" if tool_calls else "stop"
    return finish_reason, tool_calls, text, thinking, usage

"""OpenAI Codex ChatGPT-subscription wire backend for `runner_better_agent.py`.

Speaks OpenAI's internal Codex "ResponsesAPI" protocol directly (POST
`{BASE_URL}/responses`, SSE response) using the OAuth credential the `codex`
CLI's own `codex login` produces at `{CODEX_HOME}/auth.json`. This is a
separate wire protocol from the generic OpenAI-compatible Chat Completions
path in `runner_better_agent.py` (different auth: ChatGPT OAuth vs API key;
different request/response shape; different, non-overridable base URL) but
plugs into the SAME internal turn representation — `stream_chat` below
yields Chat-Completions-shaped chunks (`{"choices": [{"delta": {...},
"finish_reason": ...}], "usage": {...}}`) so `_one_round` in
runner_better_agent.py consumes it identically to `_stream_chat`, with zero
changes to the EventEmitter / tool-dispatch / persistence code downstream.

Protocol confirmed from the public github.com/openai/codex repo
(`codex-rs/`) at the time this was written:
  - Credential shape (`auth.json`): `codex-rs/login/src/auth/storage.rs`
    (`AuthDotJson`), `codex-rs/login/src/token_data.rs` (`TokenData`,
    `IdTokenInfo`) — `auth_mode` is `"chatgpt"` (lowercase) for a
    ChatGPT-subscription login (`codex-rs/protocol/src/auth.rs`).
  - Request shape (`ResponsesApiRequest`) and `ResponseItem`/`ContentItem`
    variants: `codex-rs/codex-api/src/common.rs`,
    `codex-rs/protocol/src/models.rs`. `ResponsesApiTools` itself is an
    opaque pre-serialized JSON blob in Rust (`Arc<RawValue>`) — the
    flattened `{"type":"function","name":...,"description":...,
    "parameters":...}` tool shape used here matches OpenAI's publicly
    documented Responses API function-tool shape (Chat Completions' nested
    `{"type":"function","function":{...}}` shape does NOT apply here).
  - SSE event names: CONFIRMED from literal test fixtures in
    `codex-rs/codex-api/src/sse/responses.rs` (`process_responses_event`
    matches `"response.output_item.done"`, `"response.output_text.delta"`,
    `"response.completed"`, `"response.failed"`, `"response.incomplete"`,
    `"response.created"`, `"response.output_item.added"`,
    `"response.reasoning_summary_text.delta/done"`,
    `"response.reasoning_text.delta"`,
    `"response.custom_tool_call_input.delta"` — these are NOT a guess.
  - OAuth refresh: `codex-rs/login/src/auth/manager.rs`
    (`request_chatgpt_token_refresh`, `CLIENT_ID`, `REFRESH_TOKEN_URL`).
  - Bearer/account headers: `codex-rs/model-provider/src/bearer_auth_provider.rs`
    (`Authorization: Bearer {token}`, `ChatGPT-Account-ID: {account_id}`).

What was NOT independently re-derived from source for this pass (flagged
per the brief, not presented as confirmed): the exact `/responses` endpoint
path prefix (`https://chatgpt.com/backend-api/codex`) and the full set of
`x-codex-*` turn-correlation headers were taken from the task brief's prior
research rather than re-verified against `codex-rs/backend-client` in this
pass; they are internally consistent for this runner's own use (per-turn
correlation only) rather than required to bit-for-bit match the real CLI.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx

import portable_lock
from json_store import write_json_durable

logger = logging.getLogger("runner_better_agent_codex_subscription")

# Locked to the real ChatGPT backend — unlike the generic OpenAI-compatible
# path, this wire protocol never accepts a user-supplied base_url override.
CODEX_SUBSCRIPTION_BASE_URL = "https://chatgpt.com/backend-api/codex"

_REFRESH_TOKEN_URL = "https://auth.openai.com/oauth/token"
_REFRESH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_TOKEN_EXPIRY_MARGIN_S = 5 * 60
_LAST_REFRESH_MAX_AGE_S = 8 * 24 * 3600
_REFRESH_TIMEOUT_S = 30.0
# Honest, non-impersonating client identity. `originator` is a required
# protocol field the backend uses to route Codex-CLI-only traffic (not an
# identity claim we forge for detection evasion), so it is set to the
# value the protocol expects; `User-Agent` identifies this runner truthfully
# rather than copying the real Codex CLI's string byte-for-byte.
_ORIGINATOR = "codex_cli_rs"
_USER_AGENT = "BetterAgentCodexSubscription/1.0"
# Per-chunk silence bound, mirroring `_stream_chat`'s Chat Completions
# timeout — the model-facing safety net for a turn that never completes.
_READ_TIMEOUT_S = 1800.0


class CodexSubscriptionAuthError(RuntimeError):
    """Unrecoverable credential problem: missing/invalid auth.json, wrong
    auth_mode, or a failed OAuth refresh. Callers must fail the turn closed
    — never fall back to a different auth mode or retry silently."""


# --------------------------------------------------------------------------
# auth.json — load / refresh / persist
# --------------------------------------------------------------------------

def _auth_json_path(codex_home: Path) -> Path:
    return codex_home / "auth.json"


def _lock_path(codex_home: Path) -> Path:
    return codex_home / "auth.json.lock"


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _decode_jwt_claims(jwt: str) -> dict[str, Any]:
    parts = jwt.split(".")
    if len(parts) != 3:
        raise CodexSubscriptionAuthError("malformed token in codex auth.json")
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception as exc:
        raise CodexSubscriptionAuthError("failed to decode codex token claims") from exc
    if not isinstance(payload, dict):
        raise CodexSubscriptionAuthError("malformed codex token claims")
    return payload


def load_auth(codex_home: Path) -> dict[str, Any]:
    """Read and validate `{codex_home}/auth.json`. Raises
    `CodexSubscriptionAuthError` for anything short of a well-formed
    ChatGPT-subscription credential (fail closed — never a partial/guessed
    fallback)."""
    path = _auth_json_path(codex_home)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CodexSubscriptionAuthError(
            f"codex auth.json is unavailable under {codex_home}; run `codex "
            "login` to sign in with your ChatGPT subscription"
        ) from exc
    try:
        auth = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexSubscriptionAuthError("codex auth.json is not valid JSON") from exc
    if not isinstance(auth, dict):
        raise CodexSubscriptionAuthError("codex auth.json has an unexpected shape")
    if auth.get("auth_mode") != "chatgpt":
        raise CodexSubscriptionAuthError(
            "codex auth.json is not signed in with a ChatGPT subscription "
            f"(auth_mode={auth.get('auth_mode')!r}); run `codex login`"
        )
    tokens = auth.get("tokens")
    if (
        not isinstance(tokens, dict)
        or not tokens.get("access_token")
        or not tokens.get("refresh_token")
    ):
        raise CodexSubscriptionAuthError("codex auth.json is missing OAuth tokens")
    return auth


def _account_id(tokens: dict[str, Any]) -> str:
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id:
        return account_id
    try:
        claims = _decode_jwt_claims(tokens["access_token"])
    except CodexSubscriptionAuthError:
        return ""
    auth_claims = claims.get("https://api.openai.com/auth")
    value = auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, dict) else None
    return value if isinstance(value, str) else ""


def _seconds_since(iso_timestamp: str) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _needs_refresh(auth: dict[str, Any]) -> bool:
    tokens = auth["tokens"]
    try:
        claims = _decode_jwt_claims(tokens["access_token"])
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            if exp - time.time() <= _TOKEN_EXPIRY_MARGIN_S:
                return True
        else:
            return True
    except CodexSubscriptionAuthError:
        return True
    last_refresh = auth.get("last_refresh")
    if not isinstance(last_refresh, str):
        return True
    age = _seconds_since(last_refresh)
    return age is None or age >= _LAST_REFRESH_MAX_AGE_S


def _refresh_tokens(refresh_token: str) -> dict[str, Any]:
    try:
        resp = httpx.post(
            _REFRESH_TOKEN_URL,
            json={
                "client_id": _REFRESH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/json"},
            timeout=_REFRESH_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise CodexSubscriptionAuthError(
            "network error refreshing codex subscription token"
        ) from exc
    if resp.status_code != 200:
        # Never include the response body: it may echo back request
        # fields, and in any case carries no information safe to log.
        raise CodexSubscriptionAuthError(
            f"codex subscription token refresh failed with HTTP {resp.status_code}"
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise CodexSubscriptionAuthError(
            "codex subscription token refresh returned a non-JSON response"
        ) from exc
    if not isinstance(body, dict):
        raise CodexSubscriptionAuthError("codex subscription token refresh returned an unexpected shape")
    return body


def _persist_refreshed_auth(
    codex_home: Path, auth: dict[str, Any], refreshed: dict[str, Any],
) -> dict[str, Any]:
    tokens = dict(auth.get("tokens") or {})
    for key in ("id_token", "access_token", "refresh_token"):
        value = refreshed.get(key)
        if isinstance(value, str) and value:
            tokens[key] = value
    updated = {**auth, "tokens": tokens, "last_refresh": datetime.now(timezone.utc).isoformat()}
    # Atomic tmp-write + os.replace (write_json_durable), 0600 via
    # tempfile.mkstemp — matches this repo's credential-file safety norms.
    # An advisory exclusive lock (see ensure_fresh_credentials) serializes
    # this against a concurrent native `codex` CLI run refreshing the same
    # file, so neither writer can observe/produce a torn read.
    write_json_durable(_auth_json_path(codex_home), updated)
    return updated


def ensure_fresh_credentials(codex_home: Path) -> tuple[str, str]:
    """Return `(access_token, account_id)` for `codex_home`'s
    ChatGPT-subscription credentials, refreshing and persisting to
    `auth.json` first if the access token is expiring within 5 minutes or
    hasn't been refreshed in over 8 days.

    Fails closed (raises `CodexSubscriptionAuthError`) on any credential or
    refresh problem — never falls back to a different auth mode, never
    retries silently."""
    codex_home.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(_lock_path(codex_home)), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        portable_lock.lock_ex(lock_fd)
        auth = load_auth(codex_home)
        if _needs_refresh(auth):
            refreshed = _refresh_tokens(auth["tokens"]["refresh_token"])
            auth = _persist_refreshed_auth(codex_home, auth, refreshed)
        tokens = auth["tokens"]
        return tokens["access_token"], _account_id(tokens)
    finally:
        try:
            portable_lock.unlock(lock_fd)
        finally:
            os.close(lock_fd)


# --------------------------------------------------------------------------
# ba-runner internal history (Chat-Completions-shaped) -> ResponsesAPI input
# --------------------------------------------------------------------------

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return ""


def _user_content_items(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    items: list[dict] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text":
                items.append({"type": "input_text", "text": str(part.get("text") or "")})
            elif part_type == "image_url":
                url = ((part.get("image_url") or {}).get("url")
                       if isinstance(part.get("image_url"), dict) else None)
                if url:
                    items.append({"type": "input_image", "image_url": url})
    return items or [{"type": "input_text", "text": ""}]


def messages_to_responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
    """Translate ba-runner's OpenAI-Chat-Completions-shaped history into
    (instructions, input_items) for the ResponsesAPI request. The leading
    system message becomes `instructions` (the Responses API convention);
    every other message becomes a `ResponseItem`.

    Known, documented limitation: prior-turn `reasoning_content` (assistant
    reasoning text captured from a previous ResponsesAPI or Chat Completions
    round) is NOT re-sent as a `Reasoning` ResponseItem. The Responses API's
    native reasoning item carries provider-issued `encrypted_content` needed
    for faithful replay, which a Chat-Completions-shaped history does not
    retain — sending a synthetic, unencrypted reasoning item back would not
    be a faithful reconstruction. Only the assistant's final text and tool
    calls are replayed for prior turns, matching what a Chat Completions
    history already does for reasoning content today (it is not fed back as
    a distinct input either)."""
    rest = list(messages)
    instructions = ""
    if rest and rest[0].get("role") == "system":
        instructions = _text_of(rest[0].get("content"))
        rest = rest[1:]

    items: list[dict] = []
    for msg in rest:
        role = msg.get("role")
        if role == "system":
            items.append({"type": "message", "role": "system",
                          "content": [{"type": "input_text", "text": _text_of(msg.get("content"))}]})
        elif role == "user":
            items.append({"type": "message", "role": "user",
                          "content": _user_content_items(msg.get("content"))})
        elif role == "assistant":
            content = msg.get("content")
            if content:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": _text_of(content)}]})
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "arguments": str(fn.get("arguments") or "{}"),
                })
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(msg.get("tool_call_id") or ""),
                "output": str(msg.get("content") or ""),
            })
    return instructions, items


def tools_to_responses_tools(tool_schemas: list[dict]) -> list[dict]:
    """ba-runner's Chat-Completions-nested tool schemas
    (`{"type":"function","function":{"name":...}}`) -> the Responses API's
    flattened function-tool shape (`{"type":"function","name":...}`)."""
    out: list[dict] = []
    for schema in tool_schemas or []:
        fn = schema.get("function") if isinstance(schema, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({
            "type": "function",
            "name": fn["name"],
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _build_request_body(
    *, model: str, messages: list[dict], tools: list[dict],
    reasoning_effort: Optional[str], session_id: str,
) -> dict[str, Any]:
    instructions, input_items = messages_to_responses_input(messages)
    body: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "store": False,
        "stream": True,
        "include": [],
    }
    responses_tools = tools_to_responses_tools(tools)
    if responses_tools:
        body["tools"] = responses_tools
    if reasoning_effort and reasoning_effort != "none":
        body["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
    if session_id:
        body["prompt_cache_key"] = session_id
    return body


def _installation_id(codex_home: Path) -> str:
    # Deterministic per-CODEX_HOME surrogate — this runner has no local
    # installation-id file of its own; only used for per-turn/per-session
    # request correlation, not identity verification.
    return hashlib.sha256(str(codex_home).encode("utf-8")).hexdigest()[:32]


def _turn_headers(
    *, access_token: str, account_id: str, codex_home: Path,
    session_id: str, turn_id: str,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "originator": _ORIGINATOR,
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-client-request-id": turn_id,
        "x-codex-installation-id": _installation_id(codex_home),
        "x-codex-window-id": session_id or turn_id,
        "x-codex-turn-metadata": json.dumps({"session_id": session_id, "turn_id": turn_id}),
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


# --------------------------------------------------------------------------
# SSE response -> Chat-Completions-shaped chunks
# --------------------------------------------------------------------------

def _usage_from_responses_usage(usage: dict[str, Any]) -> dict[str, Any]:
    input_details = usage.get("input_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("input_tokens") or 0,
        "completion_tokens": usage.get("output_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
        "prompt_tokens_details": {
            "cached_tokens": input_details.get("cached_tokens") or 0,
        },
    }


async def stream_chat(
    *, codex_home: Path, model: str, messages: list[dict], tools: list[dict],
    reasoning_effort: Optional[str] = None, session_id: str = "",
) -> AsyncIterator[dict]:
    """Yield Chat-Completions-shaped chunks from a streaming Codex
    ResponsesAPI turn, so `_one_round` in runner_better_agent.py can consume
    it identically to `_stream_chat`.

    SSE `event:`/`data:` frames are parsed defensively: unrecognized or
    malformed frames are logged and skipped rather than raising, so a
    protocol addition on the server side degrades gracefully instead of
    hard-failing the whole turn. If the stream ends without ever producing a
    recognized `response.completed` (or hard error), that IS treated as a
    turn failure — this loop must never silently report success/hang."""
    access_token, account_id = await asyncio.to_thread(ensure_fresh_credentials, codex_home)
    body = _build_request_body(
        model=model, messages=messages, tools=tools,
        reasoning_effort=reasoning_effort, session_id=session_id,
    )
    turn_id = uuid.uuid4().hex
    headers = _turn_headers(
        access_token=access_token, account_id=account_id, codex_home=codex_home,
        session_id=session_id, turn_id=turn_id,
    )
    url = CODEX_SUBSCRIPTION_BASE_URL.rstrip("/") + "/responses"
    timeout = httpx.Timeout(connect=15.0, read=_READ_TIMEOUT_S, write=30.0, pool=15.0)

    saw_terminal = False
    tool_call_index = 0
    emitted_text_len = 0

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                raw = (await resp.aread()).decode("utf-8", "replace")[:500]
                raise RuntimeError(f"codex subscription HTTP {resp.status_code}: {raw}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    logger.debug("codex subscription: non-JSON SSE data frame, skipping")
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")

                if kind == "response.output_text.delta":
                    delta = event.get("delta")
                    if delta:
                        emitted_text_len += len(delta)
                        yield {"choices": [{"delta": {"content": delta}}]}

                elif kind in (
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                ):
                    delta = event.get("delta")
                    if delta:
                        yield {"choices": [{"delta": {"reasoning_content": delta}}]}

                elif kind == "response.output_item.done":
                    item = event.get("item")
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "function_call":
                        idx = tool_call_index
                        tool_call_index += 1
                        yield {"choices": [{"delta": {"tool_calls": [{
                            "index": idx,
                            "id": str(item.get("call_id") or item.get("id") or ""),
                            "function": {
                                "name": str(item.get("name") or ""),
                                "arguments": str(item.get("arguments") or ""),
                            },
                        }]}}]}
                    elif item_type == "message":
                        full_text = "".join(
                            str(part.get("text") or "")
                            for part in (item.get("content") or [])
                            if isinstance(part, dict) and part.get("type") == "output_text"
                        )
                        # Reconcile against already-streamed deltas: only emit
                        # the suffix we haven't sent yet. This is also the
                        # ONLY text source if the server ever sends the final
                        # message without any preceding output_text.delta
                        # frames (full_text then equals the remainder).
                        if len(full_text) > emitted_text_len:
                            remainder = full_text[emitted_text_len:]
                            emitted_text_len = len(full_text)
                            yield {"choices": [{"delta": {"content": remainder}}]}

                elif kind == "response.completed":
                    saw_terminal = True
                    response = event.get("response") or {}
                    usage = response.get("usage") or {}
                    finish_reason = "tool_calls" if tool_call_index else "stop"
                    yield {
                        "choices": [{"delta": {}, "finish_reason": finish_reason}],
                        "usage": _usage_from_responses_usage(usage) if usage else {},
                    }
                    return

                elif kind in ("response.failed", "response.incomplete"):
                    saw_terminal = True
                    response = event.get("response") or {}
                    error = response.get("error") or {}
                    message = (
                        error.get("message")
                        or (response.get("incomplete_details") or {}).get("reason")
                        or kind
                    )
                    raise RuntimeError(f"codex subscription turn failed: {message}")

                elif kind in (
                    "response.created",
                    "response.output_item.added",
                    "response.reasoning_summary_part.added",
                    "response.reasoning_summary_text.done",
                    "response.custom_tool_call_input.delta",
                    "response.metadata",
                ):
                    # Recognized but not needed for the adapted turn
                    # representation (custom/non-function tool-call deltas
                    # are not produced by ba-runner's own tool schemas).
                    continue

                else:
                    logger.debug("codex subscription: unhandled SSE event type=%s", kind)

    if not saw_terminal:
        raise RuntimeError(
            "codex subscription stream ended without a recognized completion "
            "event (response.completed/response.failed/response.incomplete)"
        )

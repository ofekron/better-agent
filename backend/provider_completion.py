from __future__ import annotations

from datetime import datetime
import re
from typing import Any


RECOVERABLE_PARTIAL_OUTCOME = "recoverable_partial"
TRANSPORT_TRUNCATED_AFTER_PROGRESS_ERROR = "transport_truncated_after_progress"
_AMBIGUOUS_TRANSPORT_ERROR_PATTERN = re.compile(
    r"(?:"
    r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|EPIPE|"
    r"ENOTFOUND|EAI_NONAME|getaddrinfo|could not resolve|"
    r"socket hang up|network error|connection reset|connection refused|"
    r"websocket.*(?:closed|reset|refused)|"
    r"connect ETIMEDOUT|connect ECONNREFUSED|"
    r"TLS handshake|SSL handshake|"
    r"HTTP (?:500|502|503|529)|HTTP 429|"
    r"server_error|internal server error|"
    r"rate.?limit|overloaded|temporarily unavailable|"
    r"service unavailable|bad gateway"
    r")",
    re.IGNORECASE,
)


def recoverable_partial_payload(
    *,
    session_id: str,
    cause: str | None = None,
    token_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "outcome": RECOVERABLE_PARTIAL_OUTCOME,
        "recoverable": True,
        "session_id": session_id,
        "error": TRANSPORT_TRUNCATED_AFTER_PROGRESS_ERROR,
        "cause": cause or None,
        "token_usage": token_usage or None,
        "finished_at": datetime.now().isoformat(),
    }


def is_ambiguous_transport_failure(error: str | None) -> bool:
    return bool(error and _AMBIGUOUS_TRANSPORT_ERROR_PATTERN.search(error))


def should_preserve_recoverable_partial(
    *,
    error: str | None,
    cancelled: bool,
    native_session_id: str | None,
    progress_seen: bool,
) -> bool:
    return bool(
        not cancelled
        and native_session_id
        and progress_seen
        and is_ambiguous_transport_failure(error)
    )


def completion_has_progress(
    payload: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
) -> bool:
    used_tools = payload.get("used_tools")
    if isinstance(used_tools, (list, tuple, set)) and bool(used_tools):
        return True

    usage = payload.get("token_usage")
    if isinstance(usage, dict):
        output_tokens = usage.get("output_tokens")
        if isinstance(output_tokens, (int, float)) and output_tokens > 0:
            return True

    sdk_output = payload.get("sdk_output")
    if isinstance(sdk_output, str) and sdk_output.strip():
        error = str(payload.get("error") or "").strip()
        output = sdk_output.strip()
        if output != error and not (
            output.lower().startswith("api error:")
            and len(output) < 512
        ):
            return True

    for event in events or []:
        data = event.get("data") if isinstance(event, dict) else None
        inner = (
            data.get("event")
            if isinstance(data, dict) and isinstance(data.get("event"), dict)
            else data
        )
        if not isinstance(inner, dict):
            continue
        message = inner.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"tool_use", "tool_result"}:
                return True
            text = block.get("text")
            if (
                isinstance(text, str)
                and text.strip()
                and not inner.get("isApiErrorMessage")
            ):
                return True
    return False


def classify_completion_after_progress(
    payload: dict[str, Any],
    *,
    progress_seen: bool,
) -> dict[str, Any]:
    if payload.get("success") or payload.get("recoverable") is True:
        return payload
    session_id = str(payload.get("session_id") or "").strip()
    error = str(payload.get("error") or "").strip() or None
    if not should_preserve_recoverable_partial(
        error=error,
        cancelled=bool(payload.get("cancelled")),
        native_session_id=session_id,
        progress_seen=progress_seen,
    ):
        return payload
    return recoverable_partial_payload(
        session_id=session_id,
        cause=error,
        token_usage=(
            payload.get("token_usage")
            if isinstance(payload.get("token_usage"), dict)
            else None
        ),
    )

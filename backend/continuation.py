from __future__ import annotations

from typing import Iterable, Optional

from prompt_templates import render_prompt

CONTEXT_OVERFLOW_ERROR = "context_window_exceeded"

_OVERFLOW_MARKERS = (
    "context_window_exceeded",
    "model_context_window_exceeded",
    "context_length_exceeded",
    "context length exceeded",
    "context window exceeded",
    "context window",
    "context limit",
    "token limit",
    "maximum context",
    "maximum tokens",
)


def normalize_context_overflow_error(message: Optional[str]) -> Optional[str]:
    if not message:
        return None
    lower = message.lower()
    if any(marker in lower for marker in _OVERFLOW_MARKERS):
        return CONTEXT_OVERFLOW_ERROR
    if "context" in lower and any(word in lower for word in ("exceed", "length", "limit", "window")):
        return CONTEXT_OVERFLOW_ERROR
    return None


def is_context_overflow_error(message: Optional[str]) -> bool:
    return normalize_context_overflow_error(message) is not None


def build_continuation_prompt(
    *,
    prompt: str,
    app_session_id: str,
    continuation_chain: Iterable[str],
    reason: str = "context_exceeded",
) -> str:
    provider_session_ids = [
        str(item).strip()
        for item in continuation_chain
        if str(item).strip()
    ]
    provider_session_ids_block = ""
    if provider_session_ids:
        provider_session_ids_block = (
            "\n\nPrevious provider session ids: " + ", ".join(provider_session_ids)
        )

    context_message = "Context window was exceeded"
    if reason == "selector_changed":
        context_message = "Session provider or model changed"
    elif reason == "agent_requested":
        context_message = "The agent requested a fresh context window"

    return render_prompt(
        "continuation/context_exceeded.md",
        {
            "context_message": context_message,
            "app_session_id": app_session_id,
            "provider_session_ids_block": provider_session_ids_block,
            "prompt": prompt,
        },
    ).rstrip("\n")

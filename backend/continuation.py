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


def format_recall_results(results: Iterable[dict], *, limit: int = 8) -> str:
    lines: list[str] = []
    for idx, item in enumerate(results):
        if idx >= limit:
            break
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        role = item.get("role") or "unknown"
        message_index = item.get("message_index")
        source = item.get("source_session_id") or item.get("sid") or ""
        source_part = f" session={str(source)[:8]}" if source else ""
        index_part = f" message={message_index}" if message_index is not None else ""
        lines.append(f"- [{role}{source_part}{index_part}] {text}")
    return "\n".join(lines)


def build_continuation_prompt(
    *,
    prompt: str,
    app_session_id: str,
    continuation_chain: Iterable[str],
    recall_results: Iterable[dict],
    has_recall_tool: bool,
    reason: str = "context_exceeded",
) -> str:
    recall_text = format_recall_results(recall_results)
    provider_session_ids = [
        str(item).strip()
        for item in continuation_chain
        if str(item).strip()
    ]
    recall_hint = (
        "Use `recall_history` to search previous transcripts when needed."
        if has_recall_tool
        else "Relevant previous transcript excerpts are included below."
    )
    provider_session_ids_block = ""
    if provider_session_ids:
        provider_session_ids_block = (
            "\n\nPrevious provider session ids: " + ", ".join(provider_session_ids)
        )
    recall_text_block = ""
    if recall_text:
        recall_text_block = "\n\nPrevious transcript excerpts:\n" + recall_text

    context_message = "Context window was exceeded"
    if reason == "selector_changed":
        context_message = "Session provider or model changed"

    return render_prompt(
        "continuation/context_exceeded.md",
        {
            "context_message": context_message,
            "recall_hint": recall_hint,
            "app_session_id": app_session_id,
            "provider_session_ids_block": provider_session_ids_block,
            "recall_text_block": recall_text_block,
            "prompt": prompt,
        },
    ).rstrip("\n")

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from continuation import (  # noqa: E402
    CONTEXT_OVERFLOW_ERROR,
    build_continuation_prompt,
    normalize_context_overflow_error,
)
from runner_codex import (  # noqa: E402
    _build_recall_dynamic_tool,
    _build_recall_tool_handler,
)


def test_overflow_normalization_covers_provider_phrases() -> None:
    cases = (
        "model_context_window_exceeded",
        "context_length_exceeded",
        "Context window exceeded",
        "maximum context length is 1,000 tokens",
        "input exceeds the token limit",
    )
    for case in cases:
        assert normalize_context_overflow_error(case) == CONTEXT_OVERFLOW_ERROR
    assert normalize_context_overflow_error("rate limit exceeded") is None


def test_continuation_prompt_includes_recall_without_tool() -> None:
    prompt = build_continuation_prompt(
        prompt="finish the refactor",
        app_session_id="bc-session-123",
        continuation_chain=["provider-session-123"],
        recall_results=[{
            "role": "assistant",
            "message_index": 2,
            "source_session_id": "provider-session-123",
            "text": "The migration target is session_store v2.",
        }],
        has_recall_tool=False,
    )
    assert "Better Agent session id: bc-session-123" in prompt
    assert "Previous provider session ids: provider-session-123" in prompt
    assert "Relevant previous transcript excerpts" in prompt
    assert "provider" in prompt
    assert "The migration target is session_store v2." in prompt
    assert prompt.endswith("finish the refactor")


def test_continuation_prompt_mentions_recall_tool_when_available() -> None:
    prompt = build_continuation_prompt(
        prompt="continue",
        app_session_id="bc-session-456",
        continuation_chain=["provider-session-456"],
        recall_results=[],
        has_recall_tool=True,
    )
    assert "Use `recall_history`" in prompt


def test_codex_recall_tool_registered_shape() -> None:
    tool = _build_recall_dynamic_tool()
    handler = _build_recall_tool_handler(
        app_session_id="app-1",
        backend_url="http://127.0.0.1:9",
        internal_token="tok",
    )
    assert tool["name"] == "recall_history"
    assert "query" in tool["inputSchema"]["required"]
    assert callable(handler)


if __name__ == "__main__":
    test_overflow_normalization_covers_provider_phrases()
    test_continuation_prompt_includes_recall_without_tool()
    test_continuation_prompt_mentions_recall_tool_when_available()
    test_codex_recall_tool_registered_shape()
    print("ALL TESTS PASSED")

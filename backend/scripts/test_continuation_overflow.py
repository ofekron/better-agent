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


def test_continuation_prompt_renders_without_recall() -> None:
    prompt = build_continuation_prompt(
        prompt="keep going",
        app_session_id="abc123",
        continuation_chain=["oldsid1", "oldsid2"],
        reason="selector_changed",
    )
    assert "fresh subprocess" in prompt
    assert "abc123" in prompt
    assert "oldsid1" in prompt and "oldsid2" in prompt
    assert "query_provider_native_transcript_index" in prompt
    assert "native_element_fts" in prompt
    assert "agent_session_id" in prompt
    assert "supervisor_agent_session_id" in prompt
    assert "already native ids" in prompt
    assert "keep going" in prompt
    # No recall machinery leaks into the rendered prompt.
    assert "recall_history" not in prompt
    assert "transcript excerpts" not in prompt


def test_continuation_prompt_renders_all_reasons() -> None:
    reasons = (
        "context_exceeded",
        "selector_changed",
        "agent_requested",
        "moved_project",
    )
    for reason in reasons:
        prompt = build_continuation_prompt(
            prompt="continue",
            app_session_id=f"session-{reason}",
            continuation_chain=["previous"],
            reason=reason,
        )
        assert "continue" in prompt
        assert f"session-{reason}" in prompt
        assert "previous" in prompt


if __name__ == "__main__":
    test_overflow_normalization_covers_provider_phrases()
    test_continuation_prompt_renders_without_recall()
    test_continuation_prompt_renders_all_reasons()
    print("ok")

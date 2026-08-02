from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-last-user-prompt-source-gate-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import session_store  # noqa: E402


def test_delegate_task_message_does_not_bump_last_user_prompt_at() -> None:
    root = {
        "messages": [
            {"role": "user", "timestamp": "2026-01-01T00:00:00Z"},
            {"role": "assistant", "timestamp": "2026-01-01T00:00:01Z"},
            {
                "role": "user",
                "timestamp": "2026-01-02T00:00:00Z",
                "source": "delegate_task",
            },
        ],
    }
    assert (
        session_store._last_user_prompt_timestamp(root)  # type: ignore[attr-defined]
        == "2026-01-01T00:00:00Z"
    )


def test_manual_web_message_after_delegate_task_bumps_last_user_prompt_at() -> None:
    root = {
        "messages": [
            {"role": "user", "timestamp": "2026-01-01T00:00:00Z"},
            {
                "role": "user",
                "timestamp": "2026-01-02T00:00:00Z",
                "source": "delegate_task",
            },
            {"role": "user", "timestamp": "2026-01-03T00:00:00Z"},
        ],
    }
    assert (
        session_store._last_user_prompt_timestamp(root)  # type: ignore[attr-defined]
        == "2026-01-03T00:00:00Z"
    )


def test_no_manual_message_returns_empty_string() -> None:
    root = {
        "messages": [
            {
                "role": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "source": "cli",
            },
            {
                "role": "user",
                "timestamp": "2026-01-02T00:00:00Z",
                "source": "supervisor",
            },
        ],
    }
    assert session_store._last_user_prompt_timestamp(root) == ""  # type: ignore[attr-defined]

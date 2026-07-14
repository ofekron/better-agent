from __future__ import annotations

from typing import Optional

ReasoningEffort = str

ALL_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
CLAUDE_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
# "minimal" is rejected outright by Codex's gpt-5.5 model (OpenAI: "Supported
# values are: 'none', 'low', 'medium', 'high', and 'xhigh'") — offering it
# produces a ghost completion (task_complete with no assistant output) that
# our retry guard can't recover from since the failure is deterministic.
CODEX_REASONING_EFFORTS = tuple(e for e in ALL_REASONING_EFFORTS if e != "minimal")

DEFAULT_REASONING_EFFORT = "medium"


def normalize_reasoning_effort(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    effort = value.strip().lower()
    if effort == "max":
        return "xhigh"
    if effort in ALL_REASONING_EFFORTS:
        return effort
    return None


def require_reasoning_effort(value: object) -> str:
    effort = normalize_reasoning_effort(value)
    if effort is None:
        allowed = ", ".join(ALL_REASONING_EFFORTS)
        raise ValueError(f"reasoning_effort must be one of: {allowed}")
    return effort


def claude_sdk_effort(value: object) -> Optional[str]:
    effort = normalize_reasoning_effort(value)
    if effort is None:
        return None
    if effort == "xhigh":
        return "max"
    if effort in ("low", "medium", "high"):
        return effort
    allowed = ", ".join(CLAUDE_REASONING_EFFORTS)
    raise ValueError(f"Claude reasoning_effort must be one of: {allowed}")

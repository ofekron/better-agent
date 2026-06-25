"""Shared turn-finalization guards used by the provider runners.

Both the Claude (runner.py) and Codex (runner_codex.py) runners must
detect a provider "ghost completion" — a turn that reports success but
produced no assistant output for a non-empty prompt with zero token
usage — and fail it closed as a retryable ``prompt_not_executed`` instead
of binding a fake empty reply. This module owns that logic so the two
runners cannot drift.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# Bounded retry for ``prompt_not_executed`` ghost completions. The provider
# intermittently swallows an empty/failed upstream response as a successful
# zero-usage turn (codex-cli emits ``task_complete`` with
# ``last_agent_message`` null); a fresh attempt usually succeeds, so retry
# this many times before failing the turn. Shared so the runners cannot drift.
GHOST_RETRY_MAX = 2
GHOST_RETRY_BACKOFF_S = 3.0


def token_usage_is_zero(usage: Any) -> bool:
    """True when a normalized token-usage dict carries no tokens at all
    (missing/empty counts as zero). Sums numeric leaves recursively so
    nested cache-token breakdowns are covered."""
    def _numeric_sum(value: Any) -> float:
        if isinstance(value, bool):
            return 0.0
        if isinstance(value, (int, float)):
            return abs(value)
        if isinstance(value, dict):
            return sum(_numeric_sum(v) for v in value.values())
        return 0.0

    if not isinstance(usage, dict) or not usage:
        return True
    return _numeric_sum(usage) == 0.0


def apply_ghost_completion_guard(
    *,
    success: bool,
    cancelled: bool,
    error: Optional[str],
    prompt: str,
    assistant_seen: bool,
    total_usage: Any,
    result_seen: bool,
) -> tuple[bool, Optional[str]]:
    """Fail closed when a turn reports success but produced no assistant
    output for a non-empty prompt with zero token usage — a provider
    ghost completion (e.g. a second CLI spawned behind a still-live
    instance, or a Codex ``task_complete`` with ``last_agent_message``
    null and no response items). Returns ``(success, error)``; a turn
    already failing or cancelled is left alone."""
    if (
        result_seen
        and success
        and not cancelled
        and not error
        and prompt.strip()
        and not assistant_seen
        and token_usage_is_zero(total_usage)
    ):
        log.warning(
            "ghost completion: zero-usage success with no assistant "
            "output for a non-empty prompt — marking prompt_not_executed",
        )
        return False, "prompt_not_executed"
    return success, error


def should_retry_ghost(
    error: Optional[str], *, cancelled: bool, attempts: int,
) -> bool:
    """True when a ``prompt_not_executed`` ghost completion should be
    retried, given how many ghost retries have already run. The provider
    intermittently returns an empty/failed response it logs as a
    successful zero-usage turn; a fresh attempt usually succeeds, so the
    runner retries up to ``GHOST_RETRY_MAX`` times before failing closed.

    ``attempts`` is the count of ghost retries already performed (0 on the
    first ghost). Non-ghost errors, cancels, and an exhausted budget all
    return False."""
    return (
        error == "prompt_not_executed"
        and not cancelled
        and attempts < GHOST_RETRY_MAX
    )

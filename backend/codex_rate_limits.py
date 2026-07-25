"""Reads Codex quota state as the cause of an empty turn.

Codex reports quota state only as an `event_msg`/`token_count` payload's
`rate_limits` field, and reports an unaffordable turn by completing it
with `last_agent_message: null` and no usage — never as an error. Without
this predicate an out-of-credits turn is indistinguishable from a
transient provider ghost completion, so the runner labelled it the
generic `prompt_not_executed` and retried it.

Only the credits bucket counts as exhausted: on a plan-governed turn
`primary`/`secondary` carry the window state and `credits.has_credits` is
false for every turn, healthy ones included, so the credits fields alone
do not separate a dead turn from a working one.

`runner_codex._scan_rollout_rows` owns reading the payload out of the
rollout; this module owns only what the payload means.
"""

from __future__ import annotations

from typing import Any, Optional

from i18n import t

# Stable machine code, kept separate from the localized sentence so the
# cause survives translation. Non-retryable: waiting cannot restore a zero
# balance, and the code deliberately carries none of the substrings
# `turn_helpers._is_rate_limit_attempt` matches, so the turn fails with
# this truthful cause instead of entering the rate-limit retry loop and
# showing a reset time no provider supplied.
CREDITS_EXHAUSTED_CODE = "codex_credits_exhausted"


def _balance_is_zero(credits: dict) -> bool:
    raw = credits.get("balance")
    if raw is None:
        return False
    try:
        return float(str(raw)) == 0.0
    except (TypeError, ValueError):
        return False


def empty_turn_error(rate_limits: Any) -> Optional[str]:
    """Terminal cause for a Codex turn that produced no assistant output,
    given the attempt's last `token_count.rate_limits` payload. Returns
    None when quota state does not explain the empty turn, leaving the
    caller's generic ghost-completion handling in place."""
    if not isinstance(rate_limits, dict):
        return None
    if rate_limits.get("primary") is not None or rate_limits.get("secondary") is not None:
        return None
    credits = rate_limits.get("credits")
    if not isinstance(credits, dict):
        return None
    if credits.get("has_credits") is not False or credits.get("unlimited"):
        return None
    if not _balance_is_zero(credits):
        return None
    return f"{CREDITS_EXHAUSTED_CODE}: {t('runner.codex_credits_exhausted')}"

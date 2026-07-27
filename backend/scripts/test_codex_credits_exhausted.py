"""Regression tests for Codex quota-caused empty turns.

Bug: with the account's credits balance at 0, codex-cli rejected every
turn before running it, yet still emitted a normal ``task_complete`` with
``last_agent_message: null`` and no usage. The runner only parsed
``token_count.info``, never ``token_count.rate_limits``, so the turn was
indistinguishable from a transient ghost completion: it failed as the
generic ``prompt_not_executed``, burned two ghost retries, and — because
that literal matches no rate-limit pattern — never reached any quota
handling. The user was told the prompt was not executed while the real
cause (no credits) sat in the rollout.

Locks the discriminator too: ``credits.has_credits`` is false on HEALTHY
turns as well, so credits fields alone must NOT convict a turn. Only the
credits bucket (no ``primary``/``secondary`` window) with a zero balance
counts as exhausted; a plan-governed empty turn stays a retryable ghost.

Run with:
    cd backend && .venv/bin/python scripts/test_codex_credits_exhausted.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-codex-credits-")  # noqa: E402

import codex_rate_limits  # noqa: E402
import runner_codex  # noqa: E402
from codex_rate_limits import CREDITS_EXHAUSTED_CODE  # noqa: E402
from runner_guard import (  # noqa: E402
    GHOST_ERROR,
    apply_ghost_completion_guard,
    should_retry_ghost,
)
from turn_helpers import _is_rate_limit_attempt, _is_transient_error  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[str, bool, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))


# Verbatim shapes from ~/.codex/sessions rollouts.
EXHAUSTED_CREDITS = {
    "limit_id": "premium", "limit_name": None, "primary": None, "secondary": None,
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "individual_limit": None, "plan_type": None, "rate_limit_reached_type": None,
}
HEALTHY_PLAN = {
    "limit_id": "codex", "limit_name": None,
    "primary": {"used_percent": 12.5, "resets_in_seconds": 3600},
    "secondary": None,
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "individual_limit": None, "plan_type": None, "rate_limit_reached_type": None,
}
NO_CREDITS_FIELD = {
    "limit_id": "codex", "primary": None, "secondary": None, "credits": None,
    "rate_limit_reached_type": None,
}
CREDITS_EXHAUSTED_ERROR = codex_rate_limits.empty_turn_error(EXHAUSTED_CREDITS)


def _token_count(rate_limits: dict, *, info=None) -> str:
    return json.dumps({
        "timestamp": "2026-07-25T12:44:20.688Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": info, "rate_limits": rate_limits},
    })


def _guard(empty_turn_error):
    return apply_ghost_completion_guard(
        success=True, cancelled=False, error=None, prompt="do the thing",
        assistant_seen=False, total_usage={}, result_seen=True,
        empty_turn_error=empty_turn_error,
    )


def test_payload_classification() -> None:
    print("\ntoken_count.rate_limits classification")
    _check(
        "exhausted credits bucket convicts, under a stable machine code",
        (CREDITS_EXHAUSTED_ERROR or "").startswith(f"{CREDITS_EXHAUSTED_CODE}: ")
        and len(CREDITS_EXHAUSTED_ERROR or "") > len(CREDITS_EXHAUSTED_CODE) + 2,
        f"got {CREDITS_EXHAUSTED_ERROR!r}",
    )
    _check(
        "healthy plan window does NOT convict (has_credits false is normal)",
        codex_rate_limits.empty_turn_error(HEALTHY_PLAN) is None,
        f"got {codex_rate_limits.empty_turn_error(HEALTHY_PLAN)!r}",
    )
    _check(
        "absent credits does not convict",
        codex_rate_limits.empty_turn_error(NO_CREDITS_FIELD) is None,
    )
    _check(
        "an empty window dict still counts as a plan window, not the credits bucket",
        codex_rate_limits.empty_turn_error({**EXHAUSTED_CREDITS, "primary": {}}) is None,
    )
    _check(
        "unlimited credits never convict",
        codex_rate_limits.empty_turn_error({
            **EXHAUSTED_CREDITS,
            "credits": {"has_credits": False, "unlimited": True, "balance": "0"},
        }) is None,
    )
    _check(
        "non-zero balance never convicts",
        codex_rate_limits.empty_turn_error({
            **EXHAUSTED_CREDITS,
            "credits": {"has_credits": False, "unlimited": False, "balance": "5"},
        }) is None,
    )
    _check("missing payload is inert", codex_rate_limits.empty_turn_error(None) is None)


def test_guard_and_retry() -> None:
    print("\nghost guard + retry policy")
    success, error, retry_ghost = _guard(CREDITS_EXHAUSTED_ERROR)
    _check("exhausted credits fails the turn with the real cause",
           (success, error) == (False, CREDITS_EXHAUSTED_ERROR), f"got {(success, error)!r}")
    _check("exhausted credits is terminal — the guard marks it non-retryable",
           retry_ghost is False)
    _check("terminality is carried by the classification, not the error text",
           should_retry_ghost(retry_ghost, cancelled=False, attempts=0) is False)

    success, error, retry_ghost = _guard(None)
    _check("undiagnosed empty turn is still the generic ghost",
           (success, error) == (False, GHOST_ERROR), f"got {(success, error)!r}")
    _check("undiagnosed ghost still retries",
           should_retry_ghost(retry_ghost, cancelled=False, attempts=0) is True)

    kept_success, kept_error, kept_retry = apply_ghost_completion_guard(
        success=True, cancelled=False, error=None, prompt="do the thing",
        assistant_seen=True, total_usage={"input_tokens": 10}, result_seen=True,
        empty_turn_error=CREDITS_EXHAUSTED_ERROR,
    )
    _check("a turn WITH output stays successful even when credits are exhausted",
           (kept_success, kept_error, kept_retry) == (True, None, False),
           f"got {(kept_success, kept_error, kept_retry)!r}")


def test_downstream_classification() -> None:
    print("\ndownstream turn_manager classification")
    _check("credits exhaustion is NOT a rate limit (no fabricated reset/retry)",
           _is_rate_limit_attempt(CREDITS_EXHAUSTED_ERROR, []) is False)
    _check("credits exhaustion is NOT transient",
           _is_transient_error(CREDITS_EXHAUSTED_ERROR, []) is False)
    _check("the machine code alone stays clear of the rate-limit patterns",
           not _is_rate_limit_attempt(CREDITS_EXHAUSTED_CODE, [])
           and not _is_transient_error(CREDITS_EXHAUSTED_CODE, []))
    _check("the generic ghost is still neither",
           not _is_rate_limit_attempt(GHOST_ERROR, [])
           and not _is_transient_error(GHOST_ERROR, []))


def test_rollout_scan() -> None:
    """The canonical rollout scanner is the single reader of the payload;
    `codex_rate_limits` only judges what it read."""
    print("\nrollout scan (attempt-scoped, canonical scanner)")
    with tempfile.TemporaryDirectory() as tmp:
        rollout = Path(tmp) / "rollout.jsonl"
        prior = _token_count(HEALTHY_PLAN, info={"total_token_usage": {"total_tokens": 10}}) + "\n"
        rollout.write_text(prior + _token_count(EXHAUSTED_CREDITS) + "\n", encoding="utf-8")

        limits = runner_codex._rollout_terminal_state(
            str(rollout), byte_offset=len(prior.encode()),
        ).rate_limits
        _check("the attempt slice reports this attempt's quota state",
               codex_rate_limits.empty_turn_error(limits) == CREDITS_EXHAUSTED_ERROR,
               f"got {limits!r}")

        whole = runner_codex._rollout_terminal_state(str(rollout)).rate_limits
        _check("the last payload wins when a slice spans several turns",
               codex_rate_limits.empty_turn_error(whole) == CREDITS_EXHAUSTED_ERROR)

        healthy = Path(tmp) / "healthy.jsonl"
        healthy.write_text(_token_count(HEALTHY_PLAN) + "\n", encoding="utf-8")
        healthy_limits = runner_codex._rollout_terminal_state(str(healthy)).rate_limits
        _check("a plan-governed empty attempt yields no cause (stays a ghost)",
               codex_rate_limits.empty_turn_error(healthy_limits) is None)

        _check("a rollout with no token_count fails open to the generic ghost",
               codex_rate_limits.empty_turn_error(
                   runner_codex._rollout_terminal_state(str(Path(tmp) / "nope.jsonl")).rate_limits,
               ) is None)
        _check("no rollout path is inert",
               runner_codex._rollout_terminal_state(None).rate_limits is None)


def main() -> int:
    print("=" * 70)
    print("Codex credits-exhausted classification")
    print("=" * 70)
    test_payload_classification()
    test_guard_and_retry()
    test_downstream_classification()
    test_rollout_scan()
    failed = [r for r in _results if not r[1]]
    print("\n" + "=" * 70)
    print(f"{len(_results) - len(failed)}/{len(_results)} passed")
    for name, _, detail in failed:
        print(f"  {FAIL} {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Owner test for `runner_guard.py` — the shared turn-finalization guards.

The three referencing tests (`test_codex_ghost_completion`,
`test_codex_credits_exhausted`, `test_ghost_retry`) drive the guard through
the codex runner end to end and cover `apply_ghost_completion_guard` and
`should_retry_ghost`. But they never construct the pathological token-usage
shapes the predicate exists to catch, so `token_usage_is_zero` sat at 0%
direct coverage (baseline 62% module coverage). This file closes that gap by
exercising every branch of every public function with real assertions — no
line-touchers.

`runner_guard` is pure (no filesystem, no event loop, no app state), so it
needs no fixtures beyond the autouse `BETTER_AGENT_HOME` tempdir the shared
conftest already engages.
"""

from __future__ import annotations

import pytest

import runner_guard
from runner_guard import (
    GHOST_ERROR,
    GHOST_RETRY_MAX,
    GuardResult,
    apply_ghost_completion_guard,
    should_retry_ghost,
    token_usage_is_zero,
)


# ---- token_usage_is_zero ---------------------------------------------------


def test_token_usage_is_zero_treats_missing_and_non_dict_as_zero():
    """The guard fires on a turn that carried no real usage record, so
    anything that is not a populated dict counts as zero tokens."""
    assert token_usage_is_zero(None) is True
    assert token_usage_is_zero({}) is True
    assert token_usage_is_zero("not a dict") is True
    assert token_usage_is_zero([1, 2, 3]) is True
    assert token_usage_is_zero(5) is True


def test_token_usage_is_zero_flat_counts():
    assert token_usage_is_zero({"input": 0}) is True
    assert token_usage_is_zero({"input": 0, "output": 0}) is True
    assert token_usage_is_zero({"input": 5}) is False
    assert token_usage_is_zero({"input": 5, "output": 0}) is False
    assert token_usage_is_zero({"output": 3}) is False


def test_token_usage_is_zero_sums_nested_cache_breakdowns():
    """Providers nest cache-token breakdowns; the sum must recurse so a
    turn that only reports cached tokens is NOT misread as zero usage."""
    assert token_usage_is_zero({"a": {"b": 0}}) is True
    assert token_usage_is_zero({"a": {"b": {"c": 0}}}) is True
    assert token_usage_is_zero({"a": {"b": 3}}) is False
    assert token_usage_is_zero({"cache": {"read": 4, "write": 0}, "input": 0}) is False


def test_token_usage_is_zero_ignores_bools_and_uses_absolute_value():
    """A boolean-only usage dict must count as zero (a usage record carrying
    only a flag is not real token spend -> the ghost guard still fires), and
    negative provider-reported counts must not cancel out real spend."""
    assert token_usage_is_zero({"flag": True}) is True
    assert token_usage_is_zero({"flag": True, "ok": False}) is True
    # A bool alongside real spend still counts the spend.
    assert token_usage_is_zero({"flag": True, "input": 4}) is False
    # Absolute value: a negative report is still non-zero spend.
    assert token_usage_is_zero({"input": -7}) is False
    assert token_usage_is_zero({"a": -2, "b": 2}) is False  # abs sum = 4, not 0
    # Floats are numeric leaves too.
    assert token_usage_is_zero({"input": 1.5}) is False


def test_token_usage_is_zero_ignores_non_numeric_leaves():
    """String/list leaves contribute nothing; only numeric leaves decide."""
    assert token_usage_is_zero({"model": "gpt", "n": 0}) is True
    assert token_usage_is_zero({"model": "gpt", "n": 2}) is False


# ---- apply_ghost_completion_guard ------------------------------------------


def _ghost_inputs(**overrides):
    """Baseline kwargs that satisfy every ghost condition; callers flip one
    condition to prove the guard stops short of a false positive."""
    base = dict(
        success=True,
        cancelled=False,
        error=None,
        prompt="do something",
        assistant_seen=False,
        total_usage={"input": 0},
        result_seen=True,
        empty_turn_error=None,
    )
    base.update(overrides)
    return base


def test_ghost_completion_fires_retryable_when_undiagnosed():
    """A zero-usage success with no assistant output for a real prompt is the
    generic ghost: failed closed, marked retryable."""
    result = apply_ghost_completion_guard(**_ghost_inputs())
    assert result == GuardResult(success=False, error=GHOST_ERROR, retry_ghost=True)


def test_ghost_completion_is_terminal_when_cause_diagnosed():
    """When the caller supplies the diagnosed cause (e.g. exhausted credits),
    the same empty turn is terminal — not retried — with that cause as error."""
    result = apply_ghost_completion_guard(
        **_ghost_inputs(empty_turn_error="credits_exhausted")
    )
    assert result == GuardResult(
        success=False, error="credits_exhausted", retry_ghost=False
    )


@pytest.mark.parametrize(
    "flip",
    [
        # Each flip breaks exactly one term of the ghost `and` chain.
        {"result_seen": False},
        {"success": False},
        {"cancelled": True},
        {"error": "upstream failed"},
        {"prompt": ""},
        {"prompt": "   \n  "},
        {"assistant_seen": True},
        {"total_usage": {"input": 9}},
    ],
)
def test_ghost_completion_passthrough_when_any_condition_unmet(flip):
    """If even one ghost condition is unmet, the turn is NOT a ghost: the
    guard returns the caller's own success/error untouched, never retrying."""
    result = apply_ghost_completion_guard(**_ghost_inputs(**flip))
    expected_success = flip.get("success", True)
    expected_error = flip.get("error", None)
    assert result == GuardResult(
        success=expected_success, error=expected_error, retry_ghost=False
    )


def test_ghost_completion_passthrough_preserves_failure_state():
    """A turn that was already failing must be left alone — the guard must
    not rewrite a real error into a ghost."""
    result = apply_ghost_completion_guard(
        **_ghost_inputs(success=False, error="rate_limited")
    )
    assert result == GuardResult(success=False, error="rate_limited", retry_ghost=False)


# ---- should_retry_ghost ----------------------------------------------------


def test_should_retry_ghost_true_within_budget():
    assert should_retry_ghost(True, cancelled=False, attempts=0) is True
    assert should_retry_ghost(True, cancelled=False, attempts=1) is True


def test_should_retry_ghost_false_at_and_over_budget():
    assert should_retry_ghost(True, cancelled=False, attempts=GHOST_RETRY_MAX) is False
    assert (
        should_retry_ghost(True, cancelled=False, attempts=GHOST_RETRY_MAX + 1) is False
    )


def test_should_retry_ghost_false_when_not_classified_as_ghost():
    assert should_retry_ghost(False, cancelled=False, attempts=0) is False


def test_should_retry_ghost_false_when_cancelled():
    """A cancel arriving mid-retry must stop further attempts."""
    assert should_retry_ghost(True, cancelled=True, attempts=0) is False


def test_retry_budget_constant_matches_documented_contract():
    """The runners depend on this exact budget; pin it so a drift is caught."""
    assert GHOST_RETRY_MAX == 2

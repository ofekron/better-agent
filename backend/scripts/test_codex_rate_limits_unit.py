"""100% unit coverage of codex_rate_limits.

Pure predicate that turns a Codex turn's last ``token_count.rate_limits``
payload into a terminal cause (or None). Only the credits bucket, fully
exhausted (zero balance, ``has_credits is False``, no ``unlimited``), with
no ``primary``/``secondary`` window state, counts; everything else leaves
the caller's generic ghost-completion handling in place.

Run with:
    ./scripts/run-backend-tests.sh -- scripts/test_codex_rate_limits_unit.py
"""

from __future__ import annotations

import os
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-codex-rate-limits-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from codex_rate_limits import (  # noqa: E402
    CREDITS_EXHAUSTED_CODE,
    _balance_is_zero,
    empty_turn_error,
)


# --------------------------------------------------------------------------- #
# _balance_is_zero
# --------------------------------------------------------------------------- #


def test_balance_is_zero_for_int_zero() -> None:
    assert _balance_is_zero({"balance": 0}) is True


def test_balance_is_zero_for_float_zero() -> None:
    assert _balance_is_zero({"balance": 0.0}) is True


def test_balance_is_zero_for_numeric_string_zero() -> None:
    assert _balance_is_zero({"balance": "0"}) is True
    assert _balance_is_zero({"balance": "0.00"}) is True


def test_balance_is_zero_for_negative_zero() -> None:
    # -0.0 == 0.0 is True; this is still an exhausted balance.
    assert _balance_is_zero({"balance": "-0.0"}) is True


def test_balance_is_zero_false_for_nonzero() -> None:
    assert _balance_is_zero({"balance": 5}) is False
    assert _balance_is_zero({"balance": "0.01"}) is False


def test_balance_is_zero_false_when_balance_absent() -> None:
    # No balance key at all -> nothing to declare exhausted.
    assert _balance_is_zero({}) is False


def test_balance_is_zero_false_for_non_numeric_string() -> None:
    assert _balance_is_zero({"balance": "abc"}) is False


def test_balance_is_zero_false_for_unstringifiable_value() -> None:
    # str(value) succeeds but float(str) raises -> False.
    assert _balance_is_zero({"balance": [1, 2, 3]}) is False


def test_balance_is_zero_false_when_str_raises_type_error() -> None:
    # Exercises the TypeError half of the except clause: an object whose
    # __str__ itself raises propagates out of str() as TypeError.
    class _BadStr:
        def __str__(self) -> str:
            raise TypeError("boom")

    assert _balance_is_zero({"balance": _BadStr()}) is False


# --------------------------------------------------------------------------- #
# empty_turn_error — terminal / exhausted path
# --------------------------------------------------------------------------- #


def test_terminal_credits_exhausted_int_balance() -> None:
    rl = {"primary": None, "secondary": None, "credits": {"has_credits": False, "balance": 0}}
    result = empty_turn_error(rl)
    assert result is not None
    assert result.startswith(CREDITS_EXHAUSTED_CODE + ": ")
    # The localized suffix must be non-empty.
    assert result.split(":", 1)[1].strip()


def test_terminal_credits_exhausted_string_balance() -> None:
    rl = {"credits": {"has_credits": False, "balance": "0.00"}}
    assert empty_turn_error(rl) is not None


def test_terminal_credits_exhausted_without_balance_key_is_not_exhausted() -> None:
    # Terminal requires a concrete zero balance; has_credits=False alone is
    # ambiguous with a healthy-but-uncounted turn, so it must NOT match.
    rl = {"credits": {"has_credits": False}}
    assert empty_turn_error(rl) is None


# --------------------------------------------------------------------------- #
# empty_turn_error — early-return (None) branches
# --------------------------------------------------------------------------- #


def test_none_when_payload_not_a_dict() -> None:
    assert empty_turn_error(None) is None
    assert empty_turn_error("not-a-dict") is None
    assert empty_turn_error([{"credits": {}}]) is None


def test_none_when_primary_window_present() -> None:
    # A plan-governed turn carries window state in primary/secondary; the
    # credits bucket is not the cause.
    rl = {"primary": {"window": "x"}, "credits": {"has_credits": False, "balance": 0}}
    assert empty_turn_error(rl) is None


def test_none_when_secondary_window_present() -> None:
    rl = {
        "primary": None,
        "secondary": {"window": "y"},
        "credits": {"has_credits": False, "balance": 0},
    }
    assert empty_turn_error(rl) is None


def test_none_when_credits_absent() -> None:
    assert empty_turn_error({}) is None


def test_none_when_credits_not_a_dict() -> None:
    assert empty_turn_error({"credits": "x"}) is None


def test_none_when_has_credits_true() -> None:
    rl = {"credits": {"has_credits": True, "balance": 0}}
    assert empty_turn_error(rl) is None


def test_none_when_has_credits_absent() -> None:
    # Absent has_credits is None, and None is not False -> not exhausted.
    rl = {"credits": {"balance": 0}}
    assert empty_turn_error(rl) is None


def test_none_when_unlimited_set() -> None:
    rl = {"credits": {"has_credits": False, "unlimited": True, "balance": 0}}
    assert empty_turn_error(rl) is None


def test_none_when_balance_nonzero() -> None:
    rl = {"credits": {"has_credits": False, "balance": 5}}
    assert empty_turn_error(rl) is None

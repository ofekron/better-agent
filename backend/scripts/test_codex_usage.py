"""Full unit coverage for codex_usage.py.

``token_usage_from_codex_usage`` normalizes the Codex provider's raw usage
payload into Better Agent's canonical token-usage dict. It is pure logic with
no I/O, so every branch is covered deterministically. The existing
``test_codex_recovery.py`` exercises the happy path only (85%); these tests
close the null-input path and the strict type-check edges (non-int, bool,
float, negative, missing keys) that guard the accounting against bad data.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import codex_usage  # noqa: E402


def test_none_for_non_dict_payloads():
    # Non-dict or empty payload -> None (both arms of the `or` on L7).
    for bad in (None, "tokens:5", 5, 5.0, [1, 2], object(), {}):
        assert codex_usage.token_usage_from_codex_usage(bad) is None


def test_full_usage_sums_correctly():
    result = codex_usage.token_usage_from_codex_usage(
        {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 6}
    )
    assert result == {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_read_input_tokens": 6,
        "total_tokens": 14,
    }


def test_missing_keys_default_zero():
    assert codex_usage.token_usage_from_codex_usage({"input_tokens": 7}) == {
        "input_tokens": 7,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 7,
    }


def test_strict_int_type_check_rejects_non_int_values():
    # `type(value) is int` is strict identity: str, bool, float all -> 0.
    result = codex_usage.token_usage_from_codex_usage(
        {
            "input_tokens": "5",
            "output_tokens": True,
            "cached_input_tokens": 2.0,
        }
    )
    assert result == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 0,
    }


def test_negative_values_default_zero():
    assert codex_usage.token_usage_from_codex_usage(
        {"input_tokens": -3, "output_tokens": -1, "cached_input_tokens": -2}
    ) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 0,
    }


def test_zero_values_pass_through():
    result = codex_usage.token_usage_from_codex_usage(
        {"input_tokens": 0, "output_tokens": 0}
    )
    assert result == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 0,
    }


if __name__ == "__main__":
    for fn in (
        test_none_for_non_dict_payloads,
        test_full_usage_sums_correctly,
        test_missing_keys_default_zero,
        test_strict_int_type_check_rejects_non_int_values,
        test_negative_values_default_zero,
        test_zero_values_pass_through,
    ):
        fn()
    print("PASS test_codex_usage")

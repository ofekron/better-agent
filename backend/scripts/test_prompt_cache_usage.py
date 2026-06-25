"""Regression tests for the Claude prompt-cache work.

Pins three contracts:

  1. Every Claude turn's ClaudeAgentOptions carries the CLI env knob
     that force-enables the 1-hour prompt-cache TTL
     (ENABLE_PROMPT_CACHING_1H=1) — without it the stable prefix
     (system prompt + MCP tool defs + skills) re-bills at full price
     whenever the idle gap between turns exceeds the 5-minute default
     TTL.
  2. trace_collector usage normalization extracts the Anthropic
     nested cache-write TTL breakdown (usage.cache_creation.
     ephemeral_5m/1h_input_tokens) into the flat
     cache_creation_5m_tokens / cache_creation_1h_tokens keys, sums
     it across merges, and OMITS the keys (never zero-fills) when the
     provider does not report the split.
  3. session_manager.add_session_token_usage carries the breakdown
     into token_usage_total / token_usage_last only when present.

Run with:
    cd backend && .venv/bin/python scripts/test_prompt_cache_usage.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, _BACKEND)

import paths  # noqa: E402

_TEST_HOME = tempfile.mkdtemp(prefix="ba_test_prompt_cache_")
paths.engage_test_home(_TEST_HOME)

import runner  # noqa: E402
import trace_collector as tc  # noqa: E402
from session_manager import SessionManager  # noqa: E402


def test_claude_cache_env_knob() -> None:
    env = runner._claude_cache_env()
    assert env == {"ENABLE_PROMPT_CACHING_1H": "1"}, env
    # The knob must actually be wired into the options construction —
    # source-level pin so a refactor can't silently drop it.
    src = open(os.path.join(_BACKEND, "runner.py"), encoding="utf-8").read()
    assert "env=_claude_cache_env()" in src, (
        "ClaudeAgentOptions no longer passes env=_claude_cache_env()"
    )


def test_normalize_extracts_nested_breakdown() -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 100,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 20,
            "ephemeral_5m_input_tokens": 10,
        },
    }
    norm = tc._normalize_token_usage(usage)
    assert norm is not None
    assert norm["cache_creation_5m_tokens"] == 10, norm
    assert norm["cache_creation_1h_tokens"] == 20, norm
    # Invariant: split sums to the aggregate when both are reported.
    assert (
        norm["cache_creation_5m_tokens"] + norm["cache_creation_1h_tokens"]
        == norm["cache_creation_input_tokens"]
    ), norm


def test_normalize_prefers_flat_keys() -> None:
    usage = {
        "input_tokens": 1,
        "cache_creation_5m_tokens": 7,
        "cache_creation_1h_tokens": 3,
    }
    norm = tc._normalize_token_usage(usage)
    assert norm is not None
    assert norm["cache_creation_5m_tokens"] == 7, norm
    assert norm["cache_creation_1h_tokens"] == 3, norm


def test_normalize_omits_breakdown_when_absent() -> None:
    usage = {"input_tokens": 10, "output_tokens": 5}
    norm = tc._normalize_token_usage(usage)
    assert norm is not None
    assert "cache_creation_5m_tokens" not in norm, norm
    assert "cache_creation_1h_tokens" not in norm, norm


def test_merge_sums_breakdown_only_when_present() -> None:
    with_split = {
        "input_tokens": 1,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 4,
            "ephemeral_5m_input_tokens": 2,
        },
    }
    without_split = {"input_tokens": 2}
    merged = tc._merge_usage([with_split, with_split, without_split])
    assert merged is not None
    assert merged["input_tokens"] == 4, merged
    assert merged["cache_creation_5m_tokens"] == 4, merged
    assert merged["cache_creation_1h_tokens"] == 8, merged

    merged_none = tc._merge_usage([without_split, without_split])
    assert merged_none is not None
    assert "cache_creation_1h_tokens" not in merged_none, merged_none


def test_primary_plus_worker_merge_keeps_breakdown() -> None:
    # Mirrors orchestrator's combine step: primary turn + worker turns
    # merge through the canonical merge_token_usages, so worker 5m/1h
    # writes survive into the session total.
    primary = {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 5,
        "cache_creation_5m_tokens": 10,
        "cache_creation_1h_tokens": 20,
    }
    worker_with_split = {
        "input_tokens": 3,
        "output_tokens": 2,
        "cache_creation_input_tokens": 8,
        "cache_read_input_tokens": 1,
        "cache_creation_5m_tokens": 3,
        "cache_creation_1h_tokens": 5,
    }
    codex_worker = {
        "input_tokens": 7,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 9,
    }
    merged = tc.merge_token_usages([primary, worker_with_split, codex_worker])
    assert merged is not None
    assert merged["cache_creation_input_tokens"] == 38, merged
    assert merged["cache_creation_5m_tokens"] == 13, merged
    assert merged["cache_creation_1h_tokens"] == 25, merged
    assert (
        merged["cache_creation_5m_tokens"] + merged["cache_creation_1h_tokens"]
        == merged["cache_creation_input_tokens"]
    ), merged


def test_session_totals_carry_breakdown() -> None:
    sm = SessionManager()
    sid = sm.create(cwd=_TEST_HOME)["id"]
    sm.add_session_token_usage(sid, {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 0,
        "cache_creation_5m_tokens": 10,
        "cache_creation_1h_tokens": 20,
    })
    sm.add_session_token_usage(sid, {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 60,
    })
    s = sm.get(sid)
    sm.flush_pending_persists()
    total = s["token_usage_total"]
    assert total["cache_creation_5m_tokens"] == 10, total
    assert total["cache_creation_1h_tokens"] == 20, total
    assert total["cache_read_input_tokens"] == 60, total
    # Last turn had no breakdown → keys absent, not zero.
    last = s["token_usage_last"]
    assert "cache_creation_1h_tokens" not in last, last


def main() -> int:
    tests = [
        test_claude_cache_env_knob,
        test_normalize_extracts_nested_breakdown,
        test_normalize_prefers_flat_keys,
        test_normalize_omits_breakdown_when_absent,
        test_merge_sums_breakdown_only_when_present,
        test_primary_plus_worker_merge_keeps_breakdown,
        test_session_totals_carry_breakdown,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-token-usage-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from trace_collector import (  # noqa: E402
    aggregate_claude_turn_usage,
    aggregate_claude_usage_snapshots,
    extract_provider_result_token_usage,
    extract_token_usage,
)
from claude_agent_sdk import AssistantMessage  # noqa: E402
from runner import _message_id  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _usage(inp: int, out: int, create: int, read: int) -> dict:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": create,
        "cache_read_input_tokens": read,
    }


def _agent_event(message_id: str | None, usage: dict) -> dict:
    message = {"usage": usage}
    if message_id is not None:
        message["id"] = message_id
    return {
        "type": "agent_message",
        "data": {"type": "assistant", "message": message},
    }


def test_exact_duplicate_message_id_counts_once() -> bool:
    usage = _usage(10, 2, 3, 4)
    got = extract_token_usage([
        _agent_event("msg-a", usage),
        _agent_event("msg-a", usage),
    ])
    return got == usage


def test_streaming_update_keeps_latest_snapshot() -> bool:
    got = extract_token_usage([
        _agent_event("msg-a", _usage(10, 1, 3, 4)),
        _agent_event("msg-a", _usage(10, 5, 3, 4)),
    ])
    return got == _usage(10, 5, 3, 4)


def test_distinct_message_ids_sum() -> bool:
    got = extract_token_usage([
        _agent_event("msg-a", _usage(10, 2, 3, 4)),
        _agent_event("msg-b", _usage(1, 20, 30, 40)),
    ])
    return got == _usage(11, 22, 33, 44)


def test_result_rollup_preferred_over_event_fallback() -> bool:
    got = extract_provider_result_token_usage({
        "token_usage": _usage(7, 8, 9, 10),
        "events": [
            _agent_event("msg-a", _usage(10, 2, 3, 4)),
            _agent_event("msg-b", _usage(1, 20, 30, 40)),
        ],
    })
    return got == _usage(7, 8, 9, 10)


def test_missing_message_id_counts_each_snapshot_once() -> bool:
    got = extract_token_usage([
        _agent_event(None, _usage(1, 2, 3, 4)),
        _agent_event(None, _usage(10, 20, 30, 40)),
    ])
    return got == _usage(11, 22, 33, 44)


def test_runner_snapshot_helper_matches_trace_fallback_semantics() -> bool:
    snapshots = [
        ("msg-a", _usage(10, 1, 3, 4)),
        ("msg-a", _usage(10, 5, 3, 4)),
        ("msg-b", _usage(1, 20, 30, 40)),
    ]
    got = aggregate_claude_usage_snapshots(snapshots)
    return got == _usage(11, 25, 33, 44)


def test_runner_turn_helper_prefers_result_rollup() -> bool:
    got = aggregate_claude_turn_usage(
        [
            ("msg-a", _usage(10, 2, 3, 4)),
            ("msg-b", _usage(1, 20, 30, 40)),
        ],
        _usage(7, 8, 9, 10),
    )
    return got == _usage(7, 8, 9, 10)


def test_runner_message_id_reads_sdk_assistant_message_id() -> bool:
    first = AssistantMessage(
        content=[],
        model="test",
        message_id="msg-a",
        usage=_usage(10, 1, 3, 4),
    )
    updated = AssistantMessage(
        content=[],
        model="test",
        message_id="msg-a",
        usage=_usage(10, 5, 3, 4),
    )
    got = aggregate_claude_turn_usage([
        (_message_id(first), first.usage),
        (_message_id(updated), updated.usage),
    ])
    return got == _usage(10, 5, 3, 4)


TESTS = [
    ("exact duplicate message id counts once", test_exact_duplicate_message_id_counts_once),
    ("streaming update keeps latest snapshot", test_streaming_update_keeps_latest_snapshot),
    ("distinct message ids sum", test_distinct_message_ids_sum),
    ("result rollup preferred over event fallback", test_result_rollup_preferred_over_event_fallback),
    ("missing message id counts each snapshot once", test_missing_message_id_counts_each_snapshot_once),
    ("runner snapshot helper matches trace fallback semantics", test_runner_snapshot_helper_matches_trace_fallback_semantics),
    ("runner turn helper prefers result rollup", test_runner_turn_helper_prefers_result_rollup),
    ("runner message id reads SDK assistant message_id", test_runner_message_id_reads_sdk_assistant_message_id),
]


def main_run() -> int:
    ok = True
    for name, fn in TESTS:
        try:
            passed = bool(fn())
        except Exception as exc:
            passed = False
            print(f"{FAIL} {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"{PASS if passed else FAIL} {name}")
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main_run())

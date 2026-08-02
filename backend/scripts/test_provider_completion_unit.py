"""Unit tests for ``provider_completion`` — recoverable-progress classification.

Pure-logic module (regex + dict classification) deciding whether a provider
turn that died mid-flight after making progress should be preserved as a
recoverable partial instead of reported as a total failure. Host-runnable:
no provider subprocess, no live model, no backend state.

Pins:
  1. ``recoverable_partial_payload``: fixed shape, ``cause`` / ``token_usage``
     ``or None`` semantics, ISO ``finished_at``.
  2. ``is_ambiguous_transport_failure``: None / empty / non-matching -> False;
     every transport-error family (case-insensitive) -> True.
  3. ``should_preserve_recoverable_partial``: AND of not-cancelled, native
     session id present, progress seen, ambiguous transport error.
  4. ``completion_has_progress``: signal from ``used_tools``, ``token_usage``
     output tokens, ``sdk_output`` (excluding echoed errors / short API-error
     strings), and streamed ``events`` content blocks; malformed shapes fall
     through to False.
  5. ``classify_completion_after_progress``: success / recoverable /
     not-preservable payloads pass through untouched; preservable payloads are
     rebuilt as a recoverable partial.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_provider_completion_unit.py -v
"""

from __future__ import annotations

import os
import sys

import _test_home

_TEST_HOME = _test_home.isolate("bc-test-provider-completion-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provider_completion as pc  # noqa: E402
from provider_completion import (  # noqa: E402
    RECOVERABLE_PARTIAL_OUTCOME,
    TRANSPORT_TRUNCATED_AFTER_PROGRESS_ERROR,
    classify_completion_after_progress,
    completion_has_progress,
    is_ambiguous_transport_failure,
    recoverable_partial_payload,
    should_preserve_recoverable_partial,
)


# --------------------------------------------------------------------------- #
# recoverable_partial_payload
# --------------------------------------------------------------------------- #
def test_recoverable_partial_payload_defaults_cause_and_token_usage_none():
    payload = recoverable_partial_payload(session_id="s1")
    assert payload == {
        "success": False,
        "outcome": RECOVERABLE_PARTIAL_OUTCOME,
        "recoverable": True,
        "session_id": "s1",
        "error": TRANSPORT_TRUNCATED_AFTER_PROGRESS_ERROR,
        "cause": None,
        "token_usage": None,
        "finished_at": payload["finished_at"],
    }


def test_recoverable_partial_payload_carries_cause_and_token_usage():
    usage = {"input_tokens": 10, "output_tokens": 5}
    payload = recoverable_partial_payload(
        session_id="s2", cause="upstream reset", token_usage=usage
    )
    assert payload["cause"] == "upstream reset"
    assert payload["token_usage"] == usage


def test_recoverable_partial_payload_finished_at_is_iso():
    payload = recoverable_partial_payload(session_id="s")
    # datetime.fromisoformat round-trips the value the module produced.
    from datetime import datetime

    datetime.fromisoformat(payload["finished_at"])


def test_recoverable_partial_payload_empty_string_cause_folds_to_none():
    # ``cause or None`` -> an empty cause is recorded as None, never "".
    assert recoverable_partial_payload(session_id="s", cause="")["cause"] is None


# --------------------------------------------------------------------------- #
# is_ambiguous_transport_failure
# --------------------------------------------------------------------------- #
def test_transport_failure_none_and_empty_is_false():
    assert is_ambiguous_transport_failure(None) is False
    assert is_ambiguous_transport_failure("") is False


def test_transport_failure_non_matching_is_false():
    assert is_ambiguous_transport_failure("the model said no") is False
    assert is_ambiguous_transport_failure("invalid api key") is False


# One assertion per transport-error family so a regression in any single
# alternative is named clearly.
def test_transport_failure_matches_each_family():
    cases = [
        "ECONNREFUSED",
        "ECONNRESET",
        "ETIMEDOUT",
        "EPIPE",
        "ENOTFOUND",
        "EAI_NONAME",
        "getaddrinfo failed",
        "could not resolve host",
        "socket hang up",
        "network error",
        "connection reset by peer",
        "connection refused",
        "websocket was closed",
        "websocket reset",
        "connect ETIMEDOUT 10.0.0.1:443",
        "connect ECONNREFUSED 127.0.0.1:80",
        "TLS handshake failed",
        "SSL handshake error",
        "HTTP 500",
        "HTTP 502",
        "HTTP 503",
        "HTTP 529",
        "HTTP 429",
        "server_error: boom",
        "internal server error",
        "rate limit exceeded",
        "overloaded",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
    ]
    for text in cases:
        assert is_ambiguous_transport_failure(text), text


def test_transport_failure_is_case_insensitive():
    assert is_ambiguous_transport_failure("Connection Refused") is True
    assert is_ambiguous_transport_failure("OVERLOADED") is True


def test_transport_failure_matches_inside_larger_message():
    assert is_ambiguous_transport_failure("stream aborted: connect ECONNRESET x") is True


# --------------------------------------------------------------------------- #
# should_preserve_recoverable_partial
# --------------------------------------------------------------------------- #
def _amb_error() -> str:
    return "connect ECONNREFUSED"


def test_preserve_when_all_conditions_hold():
    assert (
        should_preserve_recoverable_partial(
            error=_amb_error(),
            cancelled=False,
            native_session_id="s",
            progress_seen=True,
        )
        is True
    )


def test_no_preserve_when_cancelled():
    assert (
        should_preserve_recoverable_partial(
            error=_amb_error(), cancelled=True, native_session_id="s", progress_seen=True
        )
        is False
    )


def test_no_preserve_when_no_native_session_id():
    assert (
        should_preserve_recoverable_partial(
            error=_amb_error(), cancelled=False, native_session_id=None, progress_seen=True
        )
        is False
    )
    assert (
        should_preserve_recoverable_partial(
            error=_amb_error(), cancelled=False, native_session_id="", progress_seen=True
        )
        is False
    )


def test_no_preserve_when_no_progress_seen():
    assert (
        should_preserve_recoverable_partial(
            error=_amb_error(), cancelled=False, native_session_id="s", progress_seen=False
        )
        is False
    )


def test_no_preserve_when_error_is_not_transport_ambiguous():
    assert (
        should_preserve_recoverable_partial(
            error="context length exceeded",
            cancelled=False,
            native_session_id="s",
            progress_seen=True,
        )
        is False
    )


def test_no_preserve_when_error_none():
    assert (
        should_preserve_recoverable_partial(
            error=None, cancelled=False, native_session_id="s", progress_seen=True
        )
        is False
    )


# --------------------------------------------------------------------------- #
# completion_has_progress
# --------------------------------------------------------------------------- #
def test_progress_from_used_tools_list():
    assert completion_has_progress({"used_tools": ["Read"]}) is True


def test_progress_from_used_tools_tuple_and_set():
    assert completion_has_progress({"used_tools": ("Edit",)}) is True
    assert completion_has_progress({"used_tools": {"Bash"}}) is True


def test_no_progress_from_empty_used_tools():
    assert completion_has_progress({"used_tools": []}) is False
    assert completion_has_progress({"used_tools": ()}) is False


def test_progress_from_output_tokens_int():
    assert completion_has_progress({"token_usage": {"output_tokens": 7}}) is True


def test_progress_from_output_tokens_float():
    assert completion_has_progress({"token_usage": {"output_tokens": 0.5}}) is True


def test_no_progress_from_zero_output_tokens():
    assert completion_has_progress({"token_usage": {"output_tokens": 0}}) is False


def test_no_progress_when_token_usage_not_dict():
    assert completion_has_progress({"token_usage": "lots"}) is False


def test_progress_from_sdk_output_distinct_from_error():
    assert completion_has_progress(
        {"sdk_output": "Here is the answer.", "error": "boom"}
    ) is True


def test_no_progress_when_sdk_output_echoes_error():
    assert completion_has_progress(
        {"sdk_output": "boom", "error": "boom"}
    ) is False


def test_no_progress_when_sdk_output_is_short_api_error():
    assert completion_has_progress({"sdk_output": "API Error: 500"}) is False


def test_progress_when_api_error_string_is_long():
    long_msg = "API Error: " + ("x" * 600)
    assert completion_has_progress({"sdk_output": long_msg}) is True


def test_no_progress_from_whitespace_sdk_output():
    assert completion_has_progress({"sdk_output": "   "}) is False


def test_no_progress_from_empty_payload():
    assert completion_has_progress({}) is False


# --- events branch -------------------------------------------------------- #
def _event(inner: dict) -> dict:
    return {"data": inner}


def test_progress_from_event_tool_use_block():
    event = _event({"message": {"content": [{"type": "tool_use", "name": "Bash"}]}})
    assert completion_has_progress({}, [event]) is True


def test_progress_from_event_tool_result_block():
    event = _event({"message": {"content": [{"type": "tool_result"}]}})
    assert completion_has_progress({}, [event]) is True


def test_progress_from_event_text_block():
    event = _event({"message": {"content": [{"type": "text", "text": "hello"}]}})
    assert completion_has_progress({}, [event]) is True


def test_no_progress_from_event_empty_text():
    event = _event({"message": {"content": [{"type": "text", "text": "  "}]}})
    assert completion_has_progress({}, [event]) is False


def test_no_progress_from_event_api_error_message():
    event = _event(
        {
            "isApiErrorMessage": True,
            "message": {"content": [{"type": "text", "text": "err"}]},
        }
    )
    assert completion_has_progress({}, [event]) is False


def test_no_progress_from_malformed_events():
    # Each one of these must fall through without raising.
    assert completion_has_progress({}, ["not-a-dict"]) is False
    assert completion_has_progress({}, [{"data": "scalar"}]) is False
    assert completion_has_progress({}, [{"data": {"event": "scalar"}}]) is False
    assert completion_has_progress({}, [{"data": {"message": "scalar"}}]) is False
    assert completion_has_progress({}, [{"data": {"message": {"content": "nope"}}}]) is False
    assert (
        completion_has_progress({}, [{"data": {"message": {"content": [object()]}}}])
        is False
    )


def test_progress_from_wrapped_event_inner_dict():
    # events may be {"data": {"event": {<message-here>}}}.
    event = {
        "data": {
            "event": {"message": {"content": [{"type": "text", "text": "hi"}]}}
        }
    }
    assert completion_has_progress({}, [event]) is True


# --------------------------------------------------------------------------- #
# classify_completion_after_progress
# --------------------------------------------------------------------------- #
def test_classify_passes_through_successful_payload():
    payload = {"success": True, "session_id": "s", "error": None}
    assert classify_completion_after_progress(payload, progress_seen=True) is payload


def test_classify_passes_through_already_recoverable_payload():
    payload = {"recoverable": True, "session_id": "s"}
    assert classify_completion_after_progress(payload, progress_seen=True) is payload


def test_classify_passes_through_when_not_preservable():
    # No native session id -> not preservable -> original payload returned.
    payload = {"success": False, "session_id": "", "error": _amb_error()}
    assert classify_completion_after_progress(payload, progress_seen=True) is payload


def test_classify_passes_through_when_no_progress_seen():
    payload = {
        "success": False,
        "session_id": "s",
        "error": _amb_error(),
    }
    # progress_seen False -> not preservable.
    assert classify_completion_after_progress(payload, progress_seen=False) is payload


def test_classify_rebuilds_preservable_payload_as_recoverable_partial():
    usage = {"output_tokens": 3}
    payload = {
        "success": False,
        "session_id": "  s9  ",  # classify strips whitespace on session_id
        "error": "  connect ECONNREFUSED  ",
        "cancelled": False,
        "token_usage": usage,
    }
    result = classify_completion_after_progress(payload, progress_seen=True)
    assert result is not payload
    assert result["success"] is False
    assert result["recoverable"] is True
    assert result["outcome"] == RECOVERABLE_PARTIAL_OUTCOME
    assert result["session_id"] == "s9"
    assert result["cause"] == "connect ECONNREFUSED"
    assert result["token_usage"] == usage
    assert result["error"] == TRANSPORT_TRUNCATED_AFTER_PROGRESS_ERROR


def test_classify_recoverable_partial_token_usage_only_when_dict():
    payload = {
        "success": False,
        "session_id": "s",
        "error": _amb_error(),
        "token_usage": "not-a-dict",
    }
    result = classify_completion_after_progress(payload, progress_seen=True)
    assert result["token_usage"] is None

"""Unit coverage for credential_broker.output_guard — the fail-closed gate
that prevents a bound secret value from echoing back to the caller via any
of the result's text fields (body / stderr / error).

The guard is the single thing standing between an executor result and the
agent context, so the tests lock each branch directly and deterministically:
str vs dict secret, all three scanned fields, None/empty field handling,
empty-secret skip, identity return on safety, and the fail-closed raise.

Pure logic: imports only the ExecResult dataclass, no state, no subprocess.

Run:
    cd backend && .venv/bin/python -m pytest scripts/test_credential_broker_output_guard.py \
        --cov=credential_broker.output_guard --cov-report=term-missing --cov-branch
"""
from __future__ import annotations

import pytest

from credential_broker.executors.base import ExecResult
from credential_broker.output_guard import OutputEchoError, guard


def _result(**kw) -> ExecResult:
    base = dict(ok=True, status=200, body="", stderr="", error="")
    base.update(kw)
    return ExecResult(**base)


# --- _contains / empty-secret skip ---------------------------------------

def test_safe_result_returned_unchanged_same_identity():
    res = _result(body="public output", stderr="minor warn", error="")
    out = guard(res, "super-secret")
    assert out is res  # unchanged, same object


def test_empty_string_secret_is_skipped_never_matches():
    # bool(secret) is False -> _contains returns False even if "" in anything.
    res = _result(body="anything at all")
    assert guard(res, "") is res


def test_whitespace_secret_still_matches_when_present():
    res = _result(body="token abc")
    # Non-empty secret, present verbatim -> refused.
    with pytest.raises(OutputEchoError):
        guard(res, "abc")


# --- str secret: each of the three scanned fields ------------------------

@pytest.mark.parametrize(
    "field",
    ["body", "stderr", "error"],
)
def test_secret_in_any_field_is_refused(field):
    res = _result(**{field: "here is the hunter2 token"})
    with pytest.raises(OutputEchoError):
        guard(res, "hunter2")


def test_secret_in_body_does_not_reach_stderr_or_error_check_only():
    # Secret only in body; stderr/error clean. Must still raise on body.
    res = _result(body="leak: s3cr3t", stderr="clean", error="clean")
    with pytest.raises(OutputEchoError):
        guard(res, "s3cr3t")


# --- None fields ----------------------------------------------------------

def test_none_fields_do_not_crash_or_false_positive():
    res = ExecResult(ok=False, status=None, body=None, stderr=None, error=None)
    assert guard(res, "whatever") is res  # None -> "" -> no match


def test_none_field_with_secret_in_another_field_still_raises():
    res = ExecResult(ok=False, body=None, stderr=None, error="boom: pw")
    with pytest.raises(OutputEchoError):
        guard(res, "pw")


# --- dict secret ----------------------------------------------------------

def test_dict_secret_matches_any_value():
    res = _result(body="user=alice key=KEY123")
    with pytest.raises(OutputEchoError):
        guard(res, {"username": "alice", "api_key": "KEY123"})


def test_dict_secret_safe_when_no_value_present():
    res = _result(body="unrelated text")
    secrets = {"username": "alice", "api_key": "KEY123"}
    assert guard(res, secrets) is res


def test_dict_secret_second_value_matches_stderr():
    res = _result(stderr="trace: KEY123")
    with pytest.raises(OutputEchoError):
        guard(res, {"username": "alice", "api_key": "KEY123"})


def test_dict_secret_empty_values_are_skipped():
    # Empty-string values must not raise on arbitrary body content.
    res = _result(body="anything")
    assert guard(res, {"a": "", "b": ""}) is res


def test_dict_secret_mixed_empty_and_present():
    res = _result(body="has REAL")
    with pytest.raises(OutputEchoError):
        guard(res, {"empty": "", "real": "REAL"})


# --- substring semantics --------------------------------------------------

def test_partial_substring_of_secret_does_not_match():
    # "hunter" alone is not the secret "hunter2".
    res = _result(body="saw a hunter today")
    assert guard(res, "hunter2") is res


def test_secret_is_substring_of_body_still_matches():
    res = _result(body="xxhunter2yy")
    with pytest.raises(OutputEchoError):
        guard(res, "hunter2")


# --- error message content ------------------------------------------------

def test_raise_message_is_fail_closed_constant():
    res = _result(body="leak SECRET")
    with pytest.raises(OutputEchoError, match="fail-closed"):
        guard(res, "SECRET")

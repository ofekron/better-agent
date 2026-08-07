"""Hermetic unit owner for continuation_flow.

Owns the capability-restart referent builder, the bounded exchange truncation,
ISO-timestamp validation, and the continuation-start orchestrator. Every branch
is a real behavior: truncation keeps the payload under the wire limit, the
restart target validator rejects every malformed/forbidden adjacency, and the
orchestrator threads the continuation chain correctly.

session_manager is an injected boundary and is replaced by a recorder; no real
session store is touched. conftest engages an isolated per-module ba_home().
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-continuation-flow-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import continuation_flow as cf  # noqa: E402
from continuation import PROVIDER_CAPABILITIES_CHANGED_ERROR  # noqa: E402
from continuation_flow import ContinuationStart  # noqa: E402


_EMPTY_JSON_LEN = len(json.dumps({"user": "", "assistant": ""}))


# ===========================================================================
# _bounded_exchange_json
# ===========================================================================


def test_bounded_exchange_rejects_limit_below_empty_payload_floor():
    with pytest.raises(ValueError):
        cf._bounded_exchange_json("u", "a", _EMPTY_JSON_LEN - 1)


def test_bounded_exchange_no_truncation_when_under_limit():
    rendered = cf._bounded_exchange_json("hi", "yo", _EMPTY_JSON_LEN + 100)
    assert json.loads(rendered) == {"user": "hi", "assistant": "yo"}


def test_bounded_exchange_truncates_the_longer_value_with_ellipsis():
    user = "x" * 100
    assistant = "y" * 5
    # limit loose enough that the longer value keeps >=1 char (keep > 0),
    # which is the branch that appends the ellipsis marker.
    limit = _EMPTY_JSON_LEN + 10
    rendered = cf._bounded_exchange_json(user, assistant, limit)

    assert len(rendered) <= limit
    decoded = json.loads(rendered)
    # the longer (user) value was cut and marked with an ellipsis
    assert decoded["user"].endswith("…")
    assert 0 < len(decoded["user"]) < len(user)
    # the shorter (assistant) value was untouched
    assert decoded["assistant"] == assistant


def test_bounded_exchange_fully_consumes_values_at_minimum_limit():
    # limit == empty-payload floor forces keep==0 (no ellipsis) for long values.
    user = "x" * 100
    assistant = "y" * 100
    rendered = cf._bounded_exchange_json(user, assistant, _EMPTY_JSON_LEN)

    assert len(rendered) <= _EMPTY_JSON_LEN
    decoded = json.loads(rendered)
    assert decoded == {"user": "", "assistant": ""}


# ===========================================================================
# _is_iso_timestamp
# ===========================================================================


@pytest.mark.parametrize(
    "value",
    [None, 123, "", "   ", "2024-01-01"],
)
def test_is_iso_timestamp_rejects_non_iso_shapes(value):
    assert cf._is_iso_timestamp(value) is False


def test_is_iso_timestamp_accepts_z_suffix():
    assert cf._is_iso_timestamp("2024-01-01T00:00:00Z") is True


def test_is_iso_timestamp_accepts_plain_iso():
    assert cf._is_iso_timestamp("2024-01-01T00:00:00") is True


def test_is_iso_timestamp_rejects_invalid_after_z_normalization():
    assert cf._is_iso_timestamp("2024TbadZ") is False


def test_is_iso_timestamp_rejects_invalid_plain():
    assert cf._is_iso_timestamp("2024-13-99T00:00:00") is False


# ===========================================================================
# _capability_restart_prompt
# ===========================================================================


def _target_id() -> str:
    return "assistant-target"


def _valid_restart_messages():
    """Return [prior_user, prior_assistant, current_user, target_assistant]."""
    return [
        {"role": "user", "content": "what is the plan"},
        {
            "role": "assistant",
            "id": "other",
            "content": "the plan is to ship",
            "completed_at": "2024-01-01T00:00:00Z",
        },
        {"role": "user", "content": "ship it now"},
        {"role": "assistant", "id": _target_id()},
    ]


@pytest.mark.parametrize("bad_target", [None, "", 123])
def test_restart_rejects_missing_target(bad_target):
    with pytest.raises(ValueError, match="target assistant message"):
        cf._capability_restart_prompt({"messages": []}, bad_target)


def test_restart_rejects_session_without_messages():
    with pytest.raises(ValueError, match="no messages"):
        cf._capability_restart_prompt({}, _target_id())


def test_restart_rejects_messages_that_are_not_a_list():
    with pytest.raises(ValueError, match="no messages"):
        cf._capability_restart_prompt({"messages": "nope"}, _target_id())


def test_restart_rejects_when_target_absent():
    with pytest.raises(ValueError, match="immediate prior exchange"):
        cf._capability_restart_prompt(
            {"messages": _valid_restart_messages()}, "not-present"
        )


def test_restart_rejects_when_target_ambiguous():
    messages = _valid_restart_messages()
    messages.append({"role": "assistant", "id": _target_id()})
    with pytest.raises(ValueError, match="immediate prior exchange"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_target_too_early_in_history():
    messages = [
        {"role": "assistant", "id": _target_id()},
    ]
    with pytest.raises(ValueError, match="immediate prior exchange"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_malformed_prior_messages():
    messages = _valid_restart_messages()
    messages[0] = "not-a-dict"
    with pytest.raises(ValueError, match="malformed"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_subturn_boundary_crossing():
    messages = _valid_restart_messages()
    messages[0]["source"] = "worker"
    with pytest.raises(ValueError, match="subturn boundary"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_wrong_user_roles():
    messages = _valid_restart_messages()
    messages[0]["role"] = "system"  # prior_user not a user
    with pytest.raises(ValueError, match="adjacent to user messages"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_current_message_not_from_user():
    messages = _valid_restart_messages()
    messages[2]["role"] = "assistant"  # current_user not a user
    with pytest.raises(ValueError, match="adjacent to user messages"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_incomplete_prior_assistant_role():
    messages = _valid_restart_messages()
    messages[1]["role"] = "user"  # prior_assistant not assistant
    with pytest.raises(ValueError, match="prior assistant is incomplete"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_prior_assistant_without_completed_at():
    messages = _valid_restart_messages()
    del messages[1]["completed_at"]
    with pytest.raises(ValueError, match="prior assistant is incomplete"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_errored_prior_assistant():
    messages = _valid_restart_messages()
    messages[1]["error"] = "boom"
    with pytest.raises(ValueError, match="prior assistant is incomplete"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_stopped_prior_assistant():
    messages = _valid_restart_messages()
    messages[1]["stopped_at"] = "2024-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="prior assistant is incomplete"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_missing_user_content():
    messages = _valid_restart_messages()
    messages[0]["content"] = "   "
    with pytest.raises(ValueError, match="user content is missing"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_rejects_missing_assistant_content():
    messages = _valid_restart_messages()
    messages[1]["content"] = "  "
    with pytest.raises(ValueError, match="assistant content is missing"):
        cf._capability_restart_prompt({"messages": messages}, _target_id())


def test_restart_builds_prompt_from_valid_exchange():
    prompt = cf._capability_restart_prompt(
        {"messages": _valid_restart_messages()}, _target_id()
    )
    assert prompt.startswith("Authoritative immediate conversation")
    assert "ship it now" in prompt  # current user content inlined
    assert "what is the plan" in prompt  # prior user content inside the exchange json
    # the bounded exchange is valid JSON
    exchange_json = prompt.split("\n", 1)[1].split("\n\nCurrent user message:")[0]
    assert isinstance(json.loads(exchange_json), dict)


# ===========================================================================
# start_continuation_for
# ===========================================================================


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session
        self.chain_sets: list[tuple[str, list[str]]] = []

    def get(self, app_session_id):
        return self.session

    def set_continuation_chain(self, app_session_id, chain):
        self.chain_sets.append((app_session_id, list(chain)))


def test_start_continuation_threads_old_provider_sid_into_chain():
    sm = _FakeSessionManager({"continuation_chain": ["sid-a"]})
    result = cf.start_continuation_for(
        session_manager=sm,
        app_session_id="app-1",
        prompt="continue",
        old_provider_sid="sid-b",
    )
    assert isinstance(result, ContinuationStart)
    assert result.continuation_chain == ["sid-a", "sid-b"]
    assert result.chain_depth == 2
    assert sm.chain_sets == [("app-1", ["sid-a", "sid-b"])]
    assert "continue" in result.prompt


def test_start_continuation_without_old_provider_sid_does_not_persist_chain():
    sm = _FakeSessionManager({})
    result = cf.start_continuation_for(
        session_manager=sm,
        app_session_id="app-1",
        prompt="continue",
        old_provider_sid=None,
    )
    assert result.continuation_chain == []
    assert result.chain_depth == 0
    assert sm.chain_sets == []


def test_start_continuation_capability_restart_uses_restart_prompt():
    sm = _FakeSessionManager({"messages": _valid_restart_messages()})
    result = cf.start_continuation_for(
        session_manager=sm,
        app_session_id="app-1",
        prompt="ignored-by-restart",
        old_provider_sid="sid-b",
        reason=PROVIDER_CAPABILITIES_CHANGED_ERROR,
        target_assistant_msg_id=_target_id(),
    )
    assert "Authoritative immediate conversation" in result.prompt
    assert result.continuation_chain == ["sid-b"]
    assert sm.chain_sets == [("app-1", ["sid-b"])]


def test_start_continuation_handles_missing_chain_key():
    sm = _FakeSessionManager({"continuation_chain": None})
    result = cf.start_continuation_for(
        session_manager=sm,
        app_session_id="app-1",
        prompt="continue",
        old_provider_sid="sid-x",
    )
    assert result.continuation_chain == ["sid-x"]

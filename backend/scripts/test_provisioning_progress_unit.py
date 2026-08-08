from __future__ import annotations

import logging
import os
import sys

import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_test_home.isolate("bc-test-provisioning-progress-unit-")

from provisioning.progress import (  # noqa: E402
    _DELEGATION_STAGE_MILESTONES,
    delegation_milestone,
    emit_milestone,
)


# ── emit_milestone ────────────────────────────────────────────────────────


def test_emit_milestone_none_callback_is_a_noop():
    assert emit_milestone(None, "started", {"pid": 1}) is None


def test_emit_milestone_invokes_callback_with_a_copy_of_fields():
    seen: dict = {}
    original = {"pid": 7, "run": "r1"}

    def cb(name, fields):
        seen["name"] = name
        seen["fields"] = fields

    emit_milestone(cb, "started", original)
    assert seen["name"] == "started"
    assert seen["fields"] == {"pid": 7, "run": "r1"}
    # A defensive copy is passed, so later mutation of the original cannot leak.
    assert seen["fields"] is not original


def test_emit_milestone_passes_empty_dict_when_fields_is_none():
    seen: dict = {}

    def cb(name, fields):
        seen["fields"] = fields

    emit_milestone(cb, "started", None)
    assert seen["fields"] == {}


def test_emit_milestone_swallows_callback_exception_and_logs(caplog):
    def cb(name, fields):
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        # Must not propagate the callback's exception.
        emit_milestone(cb, "started", {})

    assert any(
        "provisioning milestone callback failed" in rec.message
        and rec.levelno == logging.ERROR
        for rec in caplog.records
    )


# ── delegation_milestone: native_session_started cascade head ─────────────


def test_delegation_native_session_started_carries_file_path():
    name, fields = delegation_milestone(
        {"fork_agent_sid": "sid-1", "jsonl_path": "/tmp/run.jsonl", "provider_id": "claude"}
    )
    assert name == "native_session_started"
    assert fields["native_session_id"] == "sid-1"
    assert fields["native_session_file_path"] == "/tmp/run.jsonl"
    assert fields["provider_id"] == "claude"


def test_delegation_native_session_started_without_file_path_key():
    # jsonl_path absent → skip the file-path line, still native_session_started.
    name, fields = delegation_milestone({"fork_agent_sid": "sid-1"})
    assert name == "native_session_started"
    assert fields["native_session_id"] == "sid-1"
    assert "native_session_file_path" not in fields


def test_delegation_native_session_started_with_empty_file_path():
    name, fields = delegation_milestone({"fork_agent_sid": "sid-1", "jsonl_path": ""})
    assert name == "native_session_started"
    assert "native_session_file_path" not in fields


def test_delegation_native_session_started_with_non_string_file_path():
    name, fields = delegation_milestone({"fork_agent_sid": "sid-1", "jsonl_path": 42})
    assert name == "native_session_started"
    assert "native_session_file_path" not in fields


def test_delegation_empty_native_session_id_is_not_a_native_session():
    # An empty fork_agent_sid is falsy → falls through the native head.
    assert delegation_milestone({"fork_agent_sid": ""}) is None


def test_delegation_non_string_native_session_id_falls_through():
    assert delegation_milestone({"fork_agent_sid": None}) is None


# ── delegation_milestone: runner_started cascade ──────────────────────────


def test_delegation_runner_started_on_positive_int_worker_pid():
    name, fields = delegation_milestone({"worker_pid": 123, "worker_agent_session_id": "w-1"})
    assert name == "runner_started"
    assert fields == {"worker_pid": 123, "worker_agent_session_id": "w-1"}


def test_delegation_bool_worker_pid_is_excluded_and_falls_through():
    # bool is an int subclass but must NOT be treated as a runner pid.
    assert delegation_milestone({"worker_pid": True}) is None


def test_delegation_zero_or_negative_worker_pid_falls_through():
    assert delegation_milestone({"worker_pid": 0}) is None
    assert delegation_milestone({"worker_pid": -3}) is None


def test_delegation_non_int_worker_pid_falls_through():
    assert delegation_milestone({"worker_pid": "7"}) is None


# ── delegation_milestone: stage cascade ───────────────────────────────────


def test_delegation_every_known_stage_emits_itself():
    for stage in _DELEGATION_STAGE_MILESTONES:
        name, fields = delegation_milestone({"stage": stage})
        assert name == stage
        assert fields == {}


def test_delegation_unknown_stage_falls_through():
    assert delegation_milestone({"stage": "not-a-real-stage"}) is None


# ── delegation_milestone: phase cascade + terminal None ───────────────────


def test_delegation_queued_phase():
    name, fields = delegation_milestone({"status": "queued"})
    assert name == "delegation_queued"
    assert fields == {}


def test_delegation_resolving_phase():
    name, fields = delegation_milestone({"status": "resolving"})
    assert name == "delegation_resolving"
    assert fields == {}


def test_delegation_unknown_phase_returns_none():
    assert delegation_milestone({"status": "idle"}) is None


def test_delegation_empty_status_returns_none():
    assert delegation_milestone({}) is None


# ── field filtering contract ──────────────────────────────────────────────


def test_delegation_drops_none_valued_identity_fields():
    # Only non-None identity keys are retained in the emitted fields.
    name, fields = delegation_milestone(
        {"provider_id": "claude", "provider_run_id": None, "worker_pid": 5}
    )
    assert name == "runner_started"
    assert fields == {"provider_id": "claude", "worker_pid": 5}
    assert "provider_run_id" not in fields
    assert "worker_agent_session_id" not in fields

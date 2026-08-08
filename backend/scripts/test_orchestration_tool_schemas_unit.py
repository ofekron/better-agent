"""Hermetic unit owner for orchestration_tool_schemas.

Owns the four behavior functions every session-creating backend route shares:
runtime-profile runner normalization/validation (untrusted-input boundary),
the runner input-property builder, tool-cwd resolution, and harness-profile
wire-field normalization. The static JSON-schema constants are asserted for
the invariants that protect their REST/MCP boundaries (required keys,
additionalProperties, the runner enum) rather than merely imported.

conftest engages an isolated per-module ba_home(); no real home is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-orch-schemas-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from orchestration_tool_schemas import (  # noqa: E402
    CREATE_SESSION_CWD_INPUT_PROPERTY,
    DELEGATE_TASK_INPUT_SCHEMA,
    ENSURE_NAMED_WORKER_INPUT_SCHEMA,
    HARNESS_PROFILE_INPUT_PROPERTIES,
    INBOX_INPUT_SCHEMA,
    LIST_AVAILABLE_PROVIDER_MODELS_INPUT_SCHEMA,
    READ_INBOX_HISTORY_INPUT_SCHEMA,
    RUNTIME_PROFILE_RUNNER_IDS,
    SESSION_ORGANIZATION_INPUT_PROPERTIES,
    STOP_TURN_INPUT_SCHEMA,
    harness_profile_wire_fields,
    normalize_runtime_profile_runner,
    resolve_tool_cwd,
    runtime_profile_runner_input_property,
)

_VALID_RUNNER = RUNTIME_PROFILE_RUNNER_IDS[0]


# ===========================================================================
# runtime_profile_runner_input_property
# ===========================================================================


def test_runner_property_non_nullable_excludes_none():
    prop = runtime_profile_runner_input_property(description="the runner")
    assert prop["type"] == "string"
    assert prop["description"] == "the runner"
    assert prop["enum"] == list(RUNTIME_PROFILE_RUNNER_IDS)
    assert None not in prop["enum"]


def test_runner_property_nullable_appends_none_and_null_type():
    prop = runtime_profile_runner_input_property(nullable=True, description="d")
    assert prop["type"] == ["string", "null"]
    assert prop["enum"][-1] is None
    assert prop["enum"].count(None) == 1
    assert set(RUNTIME_PROFILE_RUNNER_IDS).issubset(prop["enum"])


def test_runner_property_enum_is_deduplicated():
    prop = runtime_profile_runner_input_property(description="d")
    assert len(prop["enum"]) == len(set(prop["enum"]))


# ===========================================================================
# normalize_runtime_profile_runner
# ===========================================================================


@pytest.mark.parametrize("value", [None, ""])
def test_normalize_runner_none_and_empty_return_none(value):
    assert normalize_runtime_profile_runner(value) is None


def test_normalize_runner_whitespace_only_returns_none():
    assert normalize_runtime_profile_runner("   ") is None


def test_normalize_runner_valid_id_returns_stripped():
    assert normalize_runtime_profile_runner(_VALID_RUNNER) == _VALID_RUNNER


def test_normalize_runner_strips_surrounding_whitespace():
    assert normalize_runtime_profile_runner(f"  {_VALID_RUNNER}  ") == _VALID_RUNNER


def test_normalize_runner_unknown_id_raises_with_available_list():
    with pytest.raises(ValueError, match="available:"):
        normalize_runtime_profile_runner("not-a-real-runner-id")


def test_normalize_runner_non_string_int_raises():
    with pytest.raises(ValueError, match="runner must be a string or null"):
        normalize_runtime_profile_runner(123)


def test_normalize_runner_non_string_list_raises():
    with pytest.raises(ValueError, match="runner must be a string or null"):
        normalize_runtime_profile_runner([_VALID_RUNNER])


# ===========================================================================
# resolve_tool_cwd
# ===========================================================================


def test_resolve_cwd_falls_back_when_key_missing():
    assert resolve_tool_cwd({}, "/inherited") == "/inherited"


def test_resolve_cwd_falls_back_when_none():
    assert resolve_tool_cwd({"cwd": None}, "/inherited") == "/inherited"


def test_resolve_cwd_falls_back_when_empty():
    assert resolve_tool_cwd({"cwd": ""}, "/inherited") == "/inherited"


def test_resolve_cwd_falls_back_when_whitespace():
    assert resolve_tool_cwd({"cwd": "   "}, "/inherited") == "/inherited"


def test_resolve_cwd_falls_back_when_falsy_non_string():
    assert resolve_tool_cwd({"cwd": 0}, "/inherited") == "/inherited"


def test_resolve_cwd_returns_explicit_path():
    assert resolve_tool_cwd({"cwd": "/explicit"}, "/inherited") == "/explicit"


def test_resolve_cwd_strips_explicit_path():
    assert resolve_tool_cwd({"cwd": "  /explicit  "}, "/inherited") == "/explicit"


def test_resolve_cwd_stringifies_truthy_non_string():
    assert resolve_tool_cwd({"cwd": 5}, "/inherited") == "5"


# ===========================================================================
# harness_profile_wire_fields
# ===========================================================================


def test_harness_fields_default_blank_is_none():
    assert harness_profile_wire_fields() == {"harness_profile_id": None}


def test_harness_fields_explicit_none_is_none():
    assert harness_profile_wire_fields(None) == {"harness_profile_id": None}


def test_harness_fields_whitespace_is_none():
    assert harness_profile_wire_fields("   ") == {"harness_profile_id": None}


def test_harness_fields_falsy_non_string_is_none():
    assert harness_profile_wire_fields(0) == {"harness_profile_id": None}


def test_harness_fields_named_profile():
    assert harness_profile_wire_fields("prof-1") == {"harness_profile_id": "prof-1"}


def test_harness_fields_strips_named_profile():
    assert harness_profile_wire_fields("  prof-1  ") == {"harness_profile_id": "prof-1"}


def test_harness_fields_stringifies_truthy_non_string():
    assert harness_profile_wire_fields(5) == {"harness_profile_id": "5"}


# ===========================================================================
# static JSON-schema boundary invariants
# ===========================================================================


def test_stop_turn_schema_requires_target_and_forbids_extra():
    assert STOP_TURN_INPUT_SCHEMA["type"] == "object"
    assert STOP_TURN_INPUT_SCHEMA["required"] == ["target_session_id"]
    assert STOP_TURN_INPUT_SCHEMA["additionalProperties"] is False
    assert "target_session_id" in STOP_TURN_INPUT_SCHEMA["properties"]


def test_inbox_schema_optional_both_directions():
    assert INBOX_INPUT_SCHEMA["required"] == []
    assert INBOX_INPUT_SCHEMA["additionalProperties"] is False
    assert INBOX_INPUT_SCHEMA["properties"]["message"]["maxLength"] == 10000
    assert "recipient_session_id" in INBOX_INPUT_SCHEMA["properties"]


def test_read_inbox_history_schema_optional_filters():
    schema = READ_INBOX_HISTORY_INPUT_SCHEMA
    assert schema["required"] == []
    assert set(schema["properties"]) == {"limit", "before_seq"}
    assert schema["properties"]["before_seq"]["type"] == ["integer", "null"]


def test_delegate_task_requires_task_and_merges_org_profile_fields():
    schema = DELEGATE_TASK_INPUT_SCHEMA
    assert schema["required"] == ["task"]
    props = schema["properties"]
    assert "target_session_id" in props
    # merged-in shared blocks
    assert set(SESSION_ORGANIZATION_INPUT_PROPERTIES).issubset(props)
    assert set(HARNESS_PROFILE_INPUT_PROPERTIES).issubset(props)


def test_ensure_named_worker_requires_name_and_mode_with_enum():
    schema = ENSURE_NAMED_WORKER_INPUT_SCHEMA
    assert set(schema["required"]) == {"name", "orchestration_mode"}
    assert schema["properties"]["orchestration_mode"]["enum"] == ["team", "native"]
    assert "provision_prompt" in schema["properties"]


def test_session_organization_properties_shape():
    assert set(SESSION_ORGANIZATION_INPUT_PROPERTIES) == {"folder_id", "tag_ids"}
    assert SESSION_ORGANIZATION_INPUT_PROPERTIES["tag_ids"]["items"] == {"type": "string"}


def test_harness_profile_input_properties_shape():
    assert set(HARNESS_PROFILE_INPUT_PROPERTIES) == {"harness_profile_id"}
    assert HARNESS_PROFILE_INPUT_PROPERTIES["harness_profile_id"]["type"] == "string"


def test_create_session_cwd_property_nullable():
    assert CREATE_SESSION_CWD_INPUT_PROPERTY["cwd"]["type"] == ["string", "null"]


def test_list_providers_schema_all_optional_filters():
    props = LIST_AVAILABLE_PROVIDER_MODELS_INPUT_SCHEMA["properties"]
    assert set(props) == {"provider", "model", "reasoning_effort", "runner"}
    assert "required" not in LIST_AVAILABLE_PROVIDER_MODELS_INPUT_SCHEMA


def test_runner_ids_constant_nonempty_and_deduplicated():
    assert RUNTIME_PROFILE_RUNNER_IDS
    assert len(RUNTIME_PROFILE_RUNNER_IDS) == len(set(RUNTIME_PROFILE_RUNNER_IDS))
    assert isinstance(RUNTIME_PROFILE_RUNNER_IDS, tuple)

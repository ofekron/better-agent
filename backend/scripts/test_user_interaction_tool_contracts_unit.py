from __future__ import annotations

import os
import sys

import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_test_home.isolate("bc-test-user-interaction-tool-contracts-unit-")

import user_interaction_tool_contracts as mod  # noqa: E402


def test_schema_is_a_closed_object():
    schema = mod.REQUEST_USER_APPROVAL_SCHEMA
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


def test_schema_declares_only_prompt_and_timeout():
    assert set(mod.REQUEST_USER_APPROVAL_SCHEMA["properties"]) == {
        "prompt",
        "timeout_seconds",
    }


def test_prompt_is_a_required_string():
    prompt = mod.REQUEST_USER_APPROVAL_SCHEMA["properties"]["prompt"]
    assert prompt["type"] == "string"
    assert "description" in prompt
    assert mod.REQUEST_USER_APPROVAL_SCHEMA["required"] == ["prompt"]


def test_timeout_seconds_is_an_optional_number():
    timeout = mod.REQUEST_USER_APPROVAL_SCHEMA["properties"]["timeout_seconds"]
    assert timeout["type"] == "number"
    assert "description" in timeout
    assert "timeout_seconds" not in mod.REQUEST_USER_APPROVAL_SCHEMA["required"]


def test_description_documents_the_approval_contract():
    desc = mod.REQUEST_USER_APPROVAL_DESCRIPTION
    assert isinstance(desc, str) and desc
    lowered = desc.lower()
    # The contract: one concrete action, user approves or redirects.
    assert "approve" in lowered
    # Documents the return shape so callers cannot drift on it.
    assert "approved=true" in lowered
    assert "approved=false" in lowered

"""Locks the REVIEWER session preset: preset resolution, persistence of
per-session capability exclusions at creation, the per-run union of
disabled builtin tools, and per-session runtime-skill filtering.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_session_presets.py
"""

from __future__ import annotations

import os
import sys

import pytest

import _test_home

_test_home.isolate("bc-test-session-presets-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402
import session_presets  # noqa: E402
from extension_run_policy import (  # noqa: E402
    disabled_builtin_tools_for_run,
    disabled_runtime_skills_for_run,
)
from runtime_skills import _filter_disabled  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def test_unknown_preset_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown session preset"):
        session_presets.normalize_preset("nope")
    assert session_presets.normalize_preset("") == ""
    assert session_presets.normalize_preset(None) == ""
    assert session_presets.normalize_preset(" Reviewer ") == "reviewer"


def test_apply_preset_unions_with_caller_exclusions() -> None:
    fields = session_presets.apply_preset("reviewer", {
        "disabled_builtin_tools": ["mssg"],
        "disabled_builtin_extensions": None,
        "disabled_runtime_skills": ["command-adv"],
    })
    assert set(fields["disabled_builtin_tools"]) == {
        "mssg", "ask", "create_session", "create_sub_session", "delegate_task",
    }
    assert extension_store.BUILTIN_SESSION_BRIDGE_EXTENSION_ID in (
        fields["disabled_builtin_extensions"]
    )
    assert set(fields["disabled_runtime_skills"]) == {"command-adv", "*"}


def test_reviewer_preset_keeps_async_reply_channel() -> None:
    fields = session_presets.apply_preset("reviewer", {})
    assert "mssg" not in fields["disabled_builtin_tools"]


def test_create_sub_session_with_preset_persists_exclusions() -> None:
    root = session_manager.create(
        name="preset-root", model="sonnet", cwd="/tmp/preset-test",
        orchestration_mode="native", source="cli",
    )
    child = session_manager.create_sub_session(
        parent_session_id=root["id"], name="reviewer", preset="reviewer",
    )
    assert "ask" in child["disabled_builtin_tools"]
    assert "delegate_task" in child["disabled_builtin_tools"]
    assert "mssg" not in child["disabled_builtin_tools"]
    assert child["disabled_runtime_skills"] == ["*"]
    assert extension_store.BUILTIN_SESSION_BRIDGE_EXTENSION_ID in (
        child["disabled_builtin_extensions"]
    )

    run_disabled = disabled_builtin_tools_for_run(session_record=child)
    assert {"ask", "create_session", "create_sub_session", "delegate_task"} <= set(run_disabled)
    assert disabled_runtime_skills_for_run(session_record=child) == ["*"]


def test_create_sub_session_unknown_preset_rejected() -> None:
    root = session_manager.create(
        name="preset-root-2", model="sonnet", cwd="/tmp/preset-test",
        orchestration_mode="native", source="cli",
    )
    with pytest.raises(ValueError, match="unknown session preset"):
        session_manager.create_sub_session(
            parent_session_id=root["id"], name="x", preset="bogus",
        )


def test_runtime_skill_filter_star_and_names() -> None:
    skills = [{"name": "a"}, {"name": "b"}]
    assert _filter_disabled(skills, None) == skills
    assert _filter_disabled(skills, ["b"]) == [{"name": "a"}]
    assert _filter_disabled(skills, ["*"]) == []

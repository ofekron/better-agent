from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-role-llm-tasks-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extension_store  # noqa: E402


def _record(*roles: str) -> dict:
    return {"manifest": {"id": "example.extension", "core_roles": list(roles)}}


def test_role_internal_llm_tasks():
    requirements = extension_store.extension_internal_llm_tasks(_record("requirements"))
    assert requirements == ["requirement_analysis"]
    assert extension_store.extension_provisioned_internal_llm_tasks(
        _record("requirements")
    ) == ["requirement_analysis"]

    team = extension_store.extension_internal_llm_tasks(_record("team-orchestration"))
    assert team == ["delegation_task", "delegation_message", "delegation_ask"]

    keys = extension_store.all_internal_llm_task_keys()
    assert "requirement_analysis" in keys
    assert set(team).issubset(keys)
    assert set(requirements + team).issubset(extension_store.extension_internal_llm_task_keys())

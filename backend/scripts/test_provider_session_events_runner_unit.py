#!/usr/bin/env python3
"""100% unit coverage for provider_session_events_runner.

Covers runtime capability hydration validation (valid + every invalid form),
provider SDK materialization (stubbed side effect), the session-events runner
restore orchestration (materialize on/off + resume present/absent), and the
frozen MCP delivery validation (valid + plan-not-list + every invalid
delivery branch). materialize_sdk() is stubbed because real materialization is
RED under the Docker test image (ELF $ORIGIN relocatability); at the unit tier
this isolates the adapter logic under test without touching the real SDK.
"""
from __future__ import annotations

import atexit
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-provider-session-events-runner-")
atexit.register(TEST_HOME.release)

import provider_session_events_runner as pse  # noqa: E402
from codex_execution_common import ExecutionContractError  # noqa: E402


def _runtime(*, skill_dirs=None, prewarm_status="ready", resume=None):
    return SimpleNamespace(
        capabilities=SimpleNamespace(
            skill_dirs=skill_dirs if skill_dirs is not None else {},
            prewarm_status=prewarm_status,
        ),
        launch=SimpleNamespace(
            materialize_sdk=lambda: SimpleNamespace(executable_path="/sdk/bin"),
            config=SimpleNamespace(
                root=SimpleNamespace(resolved_path="/cfg/root"),
                resume=resume,
            ),
        ),
        inputs={},
    )


def _valid_hydration(*, skill_dirs=None, prewarm_status="ready"):
    expected = {name: str(path) for name, path in (skill_dirs or {}).items()}
    return {
        "capability_plan": {"k": "v"},
        "prewarm_status": prewarm_status,
        "skill_dirs": expected,
    }


# --- _runtime_capabilities -----------------------------------------------

def test_runtime_capabilities_valid_returns_plan(monkeypatch):
    seen = {}

    def fake_hydrate(plan, broker):
        seen["plan"], seen["broker"] = plan, broker
        return "hydrated-plan"

    monkeypatch.setattr(pse, "hydrate_runner_runtime_plan", fake_hydrate)
    monkeypatch.setattr(pse, "get_env", lambda _name: " unix:/b.sock ")

    skill_dirs = {"a": Path("/a")}
    runtime = _runtime(skill_dirs=skill_dirs)
    inputs = {"_runtime_hydration": _valid_hydration(skill_dirs=skill_dirs)}

    result = pse._runtime_capabilities(inputs, runtime)

    assert result == "hydrated-plan"
    assert seen == {"plan": {"k": "v"}, "broker": "unix:/b.sock"}
    assert "_runtime_hydration" not in inputs


@pytest.mark.parametrize(
    "hydration, skill_dirs, prewarm_status",
    [
        # hydration missing -> None -> not dict
        (None, {}, "ready"),
        # hydration not a dict
        (["not", "dict"], {}, "ready"),
        # hydration wrong keys (missing capability_plan)
        ({"prewarm_status": "ready", "skill_dirs": {}}, {}, "ready"),
        # capability_plan not a dict
        ({**_valid_hydration(), "capability_plan": "nope"}, {}, "ready"),
        # prewarm_status mismatch
        (_valid_hydration(prewarm_status="ready"), {}, "other"),
        # skill_dirs mismatch (hydration empty vs runtime non-empty)
        (_valid_hydration(), {"a": Path("/a")}, "ready"),
    ],
)
def test_runtime_capabilities_invalid_raises(hydration, skill_dirs, prewarm_status):
    runtime = _runtime(skill_dirs=skill_dirs, prewarm_status=prewarm_status)
    inputs = {} if hydration is None else {"_runtime_hydration": hydration}
    with pytest.raises(ExecutionContractError, match="hydration is invalid"):
        pse._runtime_capabilities(inputs, runtime)


# --- _materialize_provider -----------------------------------------------

def test_materialize_provider_returns_executable_path():
    assert pse._materialize_provider(_runtime()) == "/sdk/bin"


# --- restore_session_events_runner ---------------------------------------

def test_restore_session_events_runner_materializes(monkeypatch, tmp_path):
    runtime = _runtime()
    monkeypatch.setattr(pse, "restore_family_runner_runtime", lambda run_dir: runtime)
    monkeypatch.setattr(
        "runner_operation_host.hydrate_runner_inputs",
        lambda inputs, run_dir: {**inputs, "hydrated": True},
    )
    monkeypatch.setattr(pse, "_runtime_capabilities", lambda inputs, rt: "cap-plan")
    monkeypatch.setattr(pse, "_materialize_provider", lambda rt: "/exe/path")

    execution = pse.restore_session_events_runner(tmp_path, materialize_provider=True)

    assert isinstance(execution, pse.SessionEventsRunnerExecution)
    assert execution.runtime is runtime
    assert execution.capability_plan == "cap-plan"
    assert execution.provider_executable == "/exe/path"
    assert execution.inputs["hydrated"] is True
    assert execution.inputs["_capability_plan"] == "cap-plan"
    assert execution.inputs["_provider_executable"] == "/exe/path"
    assert execution.inputs["_config_root"] == "/cfg/root"
    assert execution.inputs["_resume_path"] is None


def test_restore_session_events_runner_skips_materialize_with_resume(monkeypatch, tmp_path):
    runtime = _runtime(resume=SimpleNamespace(resolved_path="/resume/path"))
    monkeypatch.setattr(pse, "restore_family_runner_runtime", lambda run_dir: runtime)
    monkeypatch.setattr(
        "runner_operation_host.hydrate_runner_inputs",
        lambda inputs, run_dir: inputs,
    )
    monkeypatch.setattr(pse, "_runtime_capabilities", lambda inputs, rt: "cap-plan")
    monkeypatch.setattr(pse, "_materialize_provider", lambda rt: pytest.fail("must not materialize"))

    execution = pse.restore_session_events_runner(tmp_path, materialize_provider=False)

    assert execution.provider_executable is None
    assert execution.inputs["_capability_plan"] == "cap-plan"
    assert execution.inputs["_provider_executable"] is None
    assert execution.inputs["_config_root"] == "/cfg/root"
    assert execution.inputs["_resume_path"] == "/resume/path"


# --- effective_mcp_servers -----------------------------------------------

def test_effective_mcp_servers_valid():
    plan = {"mcp_servers": [
        {"name": "a", "config": {"effective": {"command": "x"}}},
        {"name": "b", "config": {"effective": {"command": "y"}}},
    ]}
    assert pse.effective_mcp_servers(plan) == {
        "a": {"command": "x"},
        "b": {"command": "y"},
    }


def test_effective_mcp_servers_plan_not_list():
    with pytest.raises(ExecutionContractError, match="MCP plan is invalid"):
        pse.effective_mcp_servers({"mcp_servers": {"not": "list"}})


@pytest.mark.parametrize(
    "servers",
    [
        # server entry not a dict -> name/variants None -> name not str
        ["notdict"],
        # empty name
        [{"name": "", "config": {"effective": {}}}],
        # duplicate name
        [
            {"name": "a", "config": {"effective": {}}},
            {"name": "a", "config": {"effective": {"x": 1}}},
        ],
        # variants not a dict -> selected None -> selected not dict
        [{"name": "a", "config": "nope"}],
        # selected not a dict
        [{"name": "a", "config": {"effective": "nope"}}],
    ],
)
def test_effective_mcp_servers_invalid_delivery_raises(servers):
    with pytest.raises(ExecutionContractError, match="MCP delivery is invalid"):
        pse.effective_mcp_servers({"mcp_servers": servers})

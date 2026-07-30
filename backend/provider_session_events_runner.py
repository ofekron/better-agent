from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_execution_common import ExecutionContractError
from env_compat import get_env
from provider_family_execution_runtime import (
    FamilyExecutionRuntime,
    restore_family_runner_runtime,
)
from provider_runtime_plan_source import hydrate_runner_operation_broker
from run_execution_payloads import publish_execution_payload_manifest


@dataclass(frozen=True)
class SessionEventsRunnerExecution:
    runtime: FamilyExecutionRuntime
    inputs: dict[str, Any]
    capability_plan: dict[str, Any]
    provider_executable: str | None


def _runtime_capabilities(
    inputs: dict[str, Any],
    runtime: FamilyExecutionRuntime,
) -> dict[str, Any]:
    hydration = inputs.pop("_runtime_hydration", None)
    expected_skills = {
        name: str(path)
        for name, path in runtime.capabilities.skill_dirs.items()
    }
    if (
        type(hydration) is not dict
        or set(hydration) != {
            "capability_plan",
            "prewarm_status",
            "skill_dirs",
        }
        or type(hydration["capability_plan"]) is not dict
        or hydration["prewarm_status"] != runtime.capabilities.prewarm_status
        or hydration["skill_dirs"] != expected_skills
    ):
        raise ExecutionContractError(
            "session-events runtime capability hydration is invalid",
        )
    return hydrate_runner_operation_broker(
        hydration["capability_plan"],
        get_env("BETTER_CLAUDE_RUNTIME_BROKER").strip(),
    )


def _materialize_provider(
    runtime: FamilyExecutionRuntime,
    run_dir: Path,
) -> str:
    root = run_dir / "provider-cli"
    root.mkdir(mode=0o700)
    materialized = runtime.launch.materialize_sdk(root)
    publish_execution_payload_manifest(
        run_dir,
        "provider-cli",
        materialized.payload_files,
    )
    return materialized.executable_path


def restore_session_events_runner(
    run_dir: Path,
    *,
    materialize_provider: bool = True,
) -> SessionEventsRunnerExecution:
    runtime = restore_family_runner_runtime(run_dir)
    inputs = dict(runtime.inputs)
    from runner_operation_host import hydrate_runner_inputs

    inputs = hydrate_runner_inputs(inputs, run_dir)
    capability_plan = _runtime_capabilities(inputs, runtime)
    provider_executable = (
        _materialize_provider(runtime, run_dir)
        if materialize_provider
        else None
    )
    inputs["_capability_plan"] = capability_plan
    inputs["_provider_executable"] = provider_executable
    inputs["_config_root"] = runtime.launch.config.root.resolved_path
    inputs["_resume_path"] = (
        runtime.launch.config.resume.resolved_path
        if runtime.launch.config.resume is not None
        else None
    )
    return SessionEventsRunnerExecution(
        runtime=runtime,
        inputs=inputs,
        capability_plan=capability_plan,
        provider_executable=provider_executable,
    )


def effective_mcp_servers(
    plan: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    servers = plan.get("mcp_servers")
    if type(servers) is not list:
        raise ExecutionContractError("frozen MCP plan is invalid")
    configs: dict[str, dict[str, Any]] = {}
    for server in servers:
        variants = server.get("config") if type(server) is dict else None
        name = server.get("name") if type(server) is dict else None
        selected = (
            variants.get("effective")
            if type(variants) is dict
            else None
        )
        if (
            type(name) is not str
            or not name
            or name in configs
            or type(selected) is not dict
        ):
            raise ExecutionContractError("frozen MCP delivery is invalid")
        configs[name] = selected
    return configs


__all__ = [
    "SessionEventsRunnerExecution",
    "effective_mcp_servers",
    "restore_session_events_runner",
]

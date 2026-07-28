from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from codex_execution_common import ExecutionContractError
from execution_artifact_io import load_execution_artifact
from execution_template import (
    ExecutionArtifact,
    PreparedExecution,
    prepare_execution,
)
from provider_execution_contract import provider_family_contract
from provider_family_launch_attestation import FamilyLaunchAttestation
from provider_family_runtime_capabilities import (
    PreparedRuntimeCapabilities,
    RunLocalCapabilities,
    cleanup_installed_family_runtime_capabilities,
    cleanup_staged_family_runtime_capabilities,
    family_runtime_capability_payload,
    install_staged_family_runtime_capabilities,
    resolve_run_local_capabilities,
    runtime_capability_manifest_from_payload,
    stage_family_runtime_capabilities,
)


@dataclass(frozen=True)
class FamilyExecutionRuntime:
    artifact: ExecutionArtifact
    launch: FamilyLaunchAttestation
    capabilities: RunLocalCapabilities
    inputs: dict[str, Any]


def _contract_payload(artifact: ExecutionArtifact) -> dict[str, Any]:
    contract = artifact.provider_contract
    if (
        type(contract) is not dict
        or contract.get("type") != artifact.provider_kind
        or type(contract.get("contract")) is not dict
        or type(contract["contract"].get("payload")) is not dict
    ):
        raise ExecutionContractError(
            "family execution contract is unavailable",
        )
    return contract["contract"]["payload"]


def family_launch_from_artifact(
    artifact: ExecutionArtifact,
) -> FamilyLaunchAttestation:
    launch = FamilyLaunchAttestation.from_payload(
        _contract_payload(artifact),
    )
    if launch.family != artifact.provider_kind or not launch.attest():
        raise ExecutionContractError("family launch authority mismatch")
    return launch


def family_capability_manifest_from_artifact(
    artifact: ExecutionArtifact,
) -> dict[str, Any]:
    manifest = runtime_capability_manifest_from_payload(
        _contract_payload(artifact),
    )
    if manifest["family"] != artifact.provider_kind:
        raise ExecutionContractError("family capability authority mismatch")
    return manifest


def prepare_family_execution(
    provider: Mapping[str, Any],
    *,
    start_arguments: Mapping[str, Any],
    runner_input: Mapping[str, Any],
    launch: FamilyLaunchAttestation,
    capabilities: PreparedRuntimeCapabilities,
) -> PreparedExecution:
    if (
        type(provider) is not dict
        or type(start_arguments) is not dict
        or type(runner_input) is not dict
        or provider.get("kind") != launch.family
        or capabilities.manifest["family"] != launch.family
    ):
        raise ExecutionContractError("invalid family execution preparation")
    payload = {
        **launch.to_payload(),
        **family_runtime_capability_payload(capabilities),
    }
    execution = prepare_execution(
        provider,
        runtime_policy={"runner_input": dict(runner_input)},
        provider_contract=provider_family_contract(
            provider,
            payload=payload,
        ),
        **dict(start_arguments),
    )
    run_id = execution.artifact.template.arguments()["run_id"]
    try:
        stage_family_runtime_capabilities(run_id, capabilities)
    except BaseException:
        cleanup_staged_family_runtime_capabilities(run_id)
        raise
    return execution


def install_family_execution_payload(
    execution: PreparedExecution,
    run_dir: Path,
) -> None:
    run_id = execution.artifact.template.arguments()["run_id"]
    install_staged_family_runtime_capabilities(
        run_dir,
        run_id=run_id,
        manifest=family_capability_manifest_from_artifact(
            execution.artifact,
        ),
    )


def release_staged_family_execution(
    execution: PreparedExecution,
) -> None:
    cleanup_staged_family_runtime_capabilities(
        execution.artifact.template.arguments()["run_id"],
    )


def cleanup_failed_family_execution(
    execution: PreparedExecution,
    run_dir: Path,
) -> None:
    release_staged_family_execution(execution)
    cleanup_installed_family_runtime_capabilities(run_dir)


def resolve_family_execution_payload(
    artifact: ExecutionArtifact,
    run_dir: Path,
) -> tuple[FamilyLaunchAttestation, RunLocalCapabilities]:
    launch = family_launch_from_artifact(artifact)
    capabilities = resolve_run_local_capabilities(
        run_dir,
        family_capability_manifest_from_artifact(artifact),
    )
    return launch, capabilities


def restore_family_runner_runtime(
    run_dir: Path,
) -> FamilyExecutionRuntime:
    artifact = load_execution_artifact(run_dir, validate_input=False)
    launch, capabilities = resolve_family_execution_payload(
        artifact,
        run_dir,
    )
    try:
        input_projection = json.loads(
            (run_dir / "input.json").read_text(encoding="utf-8"),
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionContractError("family runner input is invalid") from exc
    runner_input = artifact.runtime_policy.get("runner_input")
    if type(runner_input) is not dict or type(input_projection) is not dict:
        raise ExecutionContractError("frozen family runner input is unavailable")
    if input_projection != runner_input:
        raise ExecutionContractError(
            "family runner input conflicts with execution authority",
        )
    return FamilyExecutionRuntime(
        artifact=artifact,
        launch=launch,
        capabilities=capabilities,
        inputs=runner_input,
    )


__all__ = [
    "FamilyExecutionRuntime",
    "cleanup_failed_family_execution",
    "family_capability_manifest_from_artifact",
    "family_launch_from_artifact",
    "install_family_execution_payload",
    "prepare_family_execution",
    "release_staged_family_execution",
    "resolve_family_execution_payload",
    "restore_family_runner_runtime",
]

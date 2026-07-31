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
from provider_runner_launch import retarget_runner_launch
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
from provider_manifest import artifact_family_kinds

import runtime_profile


@dataclass(frozen=True)
class FamilyExecutionRuntime:
    artifact: ExecutionArtifact
    launch: FamilyLaunchAttestation
    capabilities: RunLocalCapabilities
    inputs: dict[str, Any]


def family_execution_kind(artifact: ExecutionArtifact) -> str:
    """The runtime family an artifact executes through.

    `artifact.provider_kind` is the configured record's kind (the
    provider AUTHORITY); better_agent_runner delegation executes
    codex/fugu records through the openai family, and the runtime family
    is frozen into the fingerprinted runner input as `provider_kind`.
    """
    runner_input = artifact.runtime_policy.get("runner_input")
    family = (
        runner_input.get("provider_kind")
        if type(runner_input) is dict
        else None
    )
    if (
        type(family) is not str
        or family not in artifact_family_kinds()
        or not runtime_profile.family_binds_to_kind(
            family,
            artifact.provider_kind,
        )
    ):
        raise ExecutionContractError("family execution kind is unavailable")
    return family


def _contract_payload(artifact: ExecutionArtifact) -> dict[str, Any]:
    contract = artifact.provider_contract
    if (
        type(contract) is not dict
        or contract.get("type") != family_execution_kind(artifact)
        or type(contract.get("contract")) is not dict
        or type(contract["contract"].get("payload")) is not dict
    ):
        raise ExecutionContractError(
            "family execution contract is unavailable",
        )
    return contract["contract"]["payload"]


def family_launch_from_artifact(
    artifact: ExecutionArtifact,
    *,
    max_retries: int = 1,
) -> FamilyLaunchAttestation:
    launch = FamilyLaunchAttestation.from_payload(
        _contract_payload(artifact),
    )
    if launch.family != family_execution_kind(artifact):
        raise ExecutionContractError("family launch authority mismatch")
    
    if launch.attest():
        return launch

    # Bounded 1-shot retry for transient live file edits during prepare->spawn window
    for attempt in range(max_retries):
        import logging
        logging.warning(
            "family_launch_from_artifact attestation mismatch (attempt %d/%d), retrying attestation...",
            attempt + 1,
            max_retries,
        )
        if launch.attest():
            return launch

    raise ExecutionContractError("family launch authority mismatch")



def family_capability_manifest_from_artifact(
    artifact: ExecutionArtifact,
) -> dict[str, Any]:
    manifest = runtime_capability_manifest_from_payload(
        _contract_payload(artifact),
    )
    if manifest["family"] != family_execution_kind(artifact):
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
        or runtime_profile.runtime_kind(provider) != launch.family
        or capabilities.manifest["family"] != launch.family
    ):
        raise ExecutionContractError("invalid family execution preparation")
    payload = {
        **launch.to_payload(),
        **family_runtime_capability_payload(capabilities),
    }
    # The artifact keeps the configured record's kind (the provider
    # AUTHORITY — require_authority re-checks it against the hydrated
    # record at spawn). The contract envelope carries the runtime FAMILY:
    # better_agent_runner delegation executes codex/fugu records through
    # the openai family, and the family codec is keyed by envelope type.
    execution = prepare_execution(
        provider,
        runtime_policy={"runner_input": dict(runner_input)},
        provider_contract=provider_family_contract(
            {**provider, "kind": launch.family},
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


def retry_family_execution(
    execution: PreparedExecution,
    **overrides: Any,
) -> PreparedExecution:
    if not isinstance(execution, PreparedExecution):
        raise ExecutionContractError("invalid family retry execution")
    artifact = execution.artifact
    family = family_execution_kind(artifact)
    runtime_policy = artifact.runtime_policy
    runner_input = runtime_policy.get("runner_input")
    if type(runner_input) is not dict:
        raise ExecutionContractError(
            "frozen family runner input is unavailable",
        )
    arguments = execution.start_arguments()
    arguments.update(overrides)
    from runs_dir import runs_root

    target_run_id = arguments["run_id"]
    launch = family_launch_from_artifact(artifact)
    retry_launch = FamilyLaunchAttestation.capture(
        family=launch.family,
        runner=retarget_runner_launch(
            launch.runner,
            runs_root() / target_run_id,
        ),
        downstream=launch.downstream,
        config=launch.config,
        critical_packages=launch.critical_packages,
    )
    contract_payload = _contract_payload(artifact)
    contract_payload.update(retry_launch.to_payload())
    updated_input = json.loads(json.dumps(runner_input))
    for key, value in overrides.items():
        if key in updated_input:
            updated_input[key] = json.loads(json.dumps(value))
    return prepare_execution(
        {
            "id": artifact.provider_id,
            "kind": artifact.provider_kind,
            "generation": artifact.provider_generation,
            "revision": artifact.provider_revision,
        },
        routing_session_id=artifact.routing_session_id,
        runtime_policy={
            **runtime_policy,
            "runner_input": updated_input,
        },
        provider_contract=provider_family_contract(
            {
                "id": artifact.provider_id,
                "kind": family,
                "generation": artifact.provider_generation,
                "revision": artifact.provider_revision,
            },
            payload=contract_payload,
        ),
        **arguments,
    )


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
    artifact = load_execution_artifact(run_dir, validate_input=True)
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
    input_projection.pop("execution_fingerprint", None)
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
    "retry_family_execution",
    "restore_family_runner_runtime",
]

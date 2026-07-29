from __future__ import annotations

from pathlib import Path

from codex_execution_common import timed_contract_step
from execution_template import ExecutionArtifact, ExecutionAuthorityError
from provider_manifest import artifact_family_kinds


def attest_execution_spawn_authority(
    artifact: ExecutionArtifact,
) -> None:
    try:
        with timed_contract_step("provider.execution.spawn_authority.attest"):
            _attest_execution_spawn_authority(artifact)
    except ExecutionAuthorityError:
        raise
    except Exception as exc:
        raise ExecutionAuthorityError(
            "execution spawn authority is invalid",
        ) from exc


def _attest_execution_spawn_authority(
    artifact: ExecutionArtifact,
) -> None:
    if artifact.provider_kind in {"codex", "fugu"}:
        from codex_execution_runtime import (
            codex_contract_from_artifact,
            codex_runner_launch_from_artifact,
            codex_runtime_agent_manifest,
        )

        contract = codex_contract_from_artifact(artifact)
        if not contract.attest():
            raise ExecutionAuthorityError(
                "Codex process authority changed before admission",
            )
        codex_runner_launch_from_artifact(artifact)
        codex_runtime_agent_manifest(artifact)
        return
    if artifact.provider_kind in artifact_family_kinds():
        from provider_family_execution_runtime import (
            family_capability_manifest_from_artifact,
            family_launch_from_artifact,
        )

        family_launch_from_artifact(artifact)
        family_capability_manifest_from_artifact(artifact)
        return
    raise ExecutionAuthorityError("execution provider has no spawn authority")


def consume_execution_spawn_authority(
    artifact: ExecutionArtifact,
    run_dir: Path,
) -> None:
    from execution_artifact_io import load_execution_artifact

    persisted = load_execution_artifact(run_dir, validate_input=True)
    if persisted != artifact:
        raise ExecutionAuthorityError(
            "persisted execution authority changed before spawn",
        )
    attest_execution_spawn_authority(persisted)

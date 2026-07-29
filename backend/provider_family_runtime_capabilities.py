from __future__ import annotations

from typing import Any, Mapping

from codex_execution_common import ExecutionContractError
from provider_runtime_capability_model import (
    PreparedRuntimeCapabilities,
    RunLocalCapabilities,
)
from provider_runtime_capability_snapshot import (
    snapshot_family_runtime_capabilities,
)
from provider_runtime_hydration import (
    HydratedSpawnCapabilities,
    PrewarmConnectionHydration,
    RuntimeHydrationRefs,
    SecretHydrationRef,
    hydrate_spawn_capabilities,
)
from provider_runtime_payload_store import (
    cleanup_staged_family_runtime_capabilities,
    clone_family_runtime_capabilities,
    install_staged_family_runtime_capabilities,
    stage_family_runtime_capabilities,
)
from provider_runtime_payload_codec import (
    validate_runtime_capability_manifest,
)
from provider_runtime_resolver import (
    cleanup_installed_family_runtime_capabilities,
    resolve_run_local_capabilities,
)


def family_runtime_capability_payload(
    prepared: PreparedRuntimeCapabilities,
) -> dict[str, Any]:
    if not isinstance(prepared, PreparedRuntimeCapabilities):
        raise ExecutionContractError("invalid prepared runtime capabilities")
    return {"runtime_capabilities": prepared.manifest}


def runtime_capability_manifest_from_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        type(payload) is not dict
        or type(payload.get("runtime_capabilities")) is not dict
    ):
        raise ExecutionContractError(
            "runtime capability manifest is unavailable",
        )
    return validate_runtime_capability_manifest(
        payload["runtime_capabilities"],
    )


__all__ = [
    "HydratedSpawnCapabilities",
    "PrewarmConnectionHydration",
    "PreparedRuntimeCapabilities",
    "RunLocalCapabilities",
    "RuntimeHydrationRefs",
    "SecretHydrationRef",
    "cleanup_installed_family_runtime_capabilities",
    "cleanup_staged_family_runtime_capabilities",
    "clone_family_runtime_capabilities",
    "family_runtime_capability_payload",
    "hydrate_spawn_capabilities",
    "install_staged_family_runtime_capabilities",
    "resolve_run_local_capabilities",
    "runtime_capability_manifest_from_payload",
    "snapshot_family_runtime_capabilities",
    "stage_family_runtime_capabilities",
]

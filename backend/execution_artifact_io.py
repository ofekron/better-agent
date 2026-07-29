from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from execution_template import (
    ExecutionArtifact,
    ExecutionAuthorityError,
    validate_recovery_input,
)


_REQUIRED_INPUT_PROJECTION = frozenset({
    "app_session_id",
    "cwd",
    "execution_fingerprint",
    "mode",
    "model",
    "prompt",
    "provider_id",
    "session_id",
})


def bind_execution_input(
    artifact: ExecutionArtifact,
    input_payload: Mapping[str, Any],
) -> dict[str, Any]:
    if type(input_payload) is not dict:
        raise ExecutionAuthorityError("run input projection must be an object")
    if "execution_fingerprint" in input_payload:
        raise ExecutionAuthorityError(
            "run input already contains execution authority",
        )
    bound = dict(input_payload)
    bound.setdefault("provider_id", artifact.provider_id)
    bound["execution_fingerprint"] = artifact.fingerprint
    validate_execution_input_projection(artifact, bound)
    return bound


def validate_execution_input_projection(
    artifact: ExecutionArtifact,
    input_payload: Mapping[str, Any],
) -> None:
    if type(input_payload) is not dict:
        raise ExecutionAuthorityError("run input projection must be an object")
    if _REQUIRED_INPUT_PROJECTION - set(input_payload):
        raise ExecutionAuthorityError("run input projection is incomplete")
    if input_payload["execution_fingerprint"] != artifact.fingerprint:
        raise ExecutionAuthorityError(
            "run input conflicts with execution authority: fingerprint",
        )
    validate_recovery_input(
        artifact,
        {
            key: input_payload[key]
            for key in _REQUIRED_INPUT_PROJECTION
            if key != "execution_fingerprint"
        },
    )


def load_execution_artifact(
    run_dir: Path,
    *,
    validate_input: bool = False,
) -> ExecutionArtifact:
    execution_path = run_dir / "execution.json"
    if execution_path.is_symlink():
        raise ExecutionAuthorityError("execution artifact must not be a symlink")
    try:
        raw = json.loads(execution_path.read_text(encoding="utf-8"))
        artifact = ExecutionArtifact.from_dict(raw)
    except ExecutionAuthorityError:
        raise
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ExecutionAuthorityError("execution artifact is invalid") from exc
    if not validate_input:
        return artifact
    input_path = run_dir / "input.json"
    if input_path.is_symlink():
        raise ExecutionAuthorityError("run input must not be a symlink")
    try:
        input_payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionAuthorityError("run input is invalid") from exc
    validate_execution_input_projection(artifact, input_payload)
    return artifact


def requires_execution_artifact(provider_kind: str) -> bool:
    from provider_manifest import execution_artifact_kinds

    return provider_kind in execution_artifact_kinds()

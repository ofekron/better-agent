"""Unit tests for ``execution_artifact_io`` — execution-authority binding/validation.

This is the security boundary that admits a run: it binds a run's input
projection to a frozen execution artifact's fingerprint and rejects any input
that conflicts with execution authority (tampered fingerprint, incomplete
projection, recovery-field mismatch) or arrives via a forbidden channel
(non-object payload, symlinked execution/input files). The existing
``test_claude_execution_artifact.py`` is a heavy Docker-only integration test
(spawns a real claude subprocess) that exercises only the happy paths; these
host-runnable unit tests cover every error branch deterministically.

Pins:
  1. ``bind_execution_input``: non-object rejection, double-bind rejection,
     provider_id default + override, fingerprint stamp, validation propagation.
  2. ``validate_execution_input_projection``: non-object, incomplete,
     fingerprint conflict, recovery-field conflict (session_id / provider_id).
  3. ``load_execution_artifact``: happy (with/without input validation),
     symlink rejection for both execution.json and input.json, invalid-JSON
     wrapping, and that a genuine ``ExecutionAuthorityError`` from the artifact
     parse is re-raised unwrapped (not collapsed into "invalid").
  4. ``requires_execution_artifact``: membership against the provider-manifest
     execution-artifact kinds.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_execution_artifact_io_unit.py -v
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import _test_home

_TEST_HOME = _test_home.isolate("bc-test-execution-artifact-io-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from execution_artifact_io import (  # noqa: E402
    bind_execution_input,
    load_execution_artifact,
    requires_execution_artifact,
    validate_execution_input_projection,
)
from execution_template import (  # noqa: E402
    ExecutionArtifact,
    ExecutionAuthorityError,
    ExecutionTemplate,
)


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
def _provider() -> dict:
    return {
        "id": "claude",
        "kind": "claude",
        "generation": str(uuid.uuid4()),
        "revision": 1,
    }


def _template_arguments(**overrides) -> dict:
    base = {
        "run_id": "run-1",
        "prompt": "do the thing",
        "cwd": "/tmp/work",
        "model": None,
        "reasoning_effort": None,
        "session_id": None,
        "mode": "native",
        "app_session_id": "app-session-1",
    }
    base.update(overrides)
    return base


def _artifact(**template_overrides) -> ExecutionArtifact:
    return ExecutionArtifact.create(
        _provider(),
        ExecutionTemplate.create(_template_arguments(**template_overrides)),
    )


def _template_args(artifact: ExecutionArtifact) -> dict:
    return artifact.template.arguments()


def _valid_projection(artifact: ExecutionArtifact) -> dict:
    """The 8 required input-projection keys, all matching the artifact."""
    args = _template_args(artifact)
    return {
        "app_session_id": args["app_session_id"],
        "cwd": args["cwd"],
        "mode": args["mode"],
        "model": args["model"],
        "prompt": args["prompt"],
        "session_id": args["session_id"],
        "provider_id": artifact.provider_id,
        "execution_fingerprint": artifact.fingerprint,
    }


def _valid_bind_input(artifact: ExecutionArtifact) -> dict:
    """Keys accepted by ``bind_execution_input`` (no fingerprint yet)."""
    args = _template_args(artifact)
    return {
        "app_session_id": args["app_session_id"],
        "cwd": args["cwd"],
        "mode": args["mode"],
        "model": args["model"],
        "prompt": args["prompt"],
        "session_id": args["session_id"],
        "provider_id": artifact.provider_id,
    }


# --------------------------------------------------------------------------- #
# bind_execution_input
# --------------------------------------------------------------------------- #
def test_bind_stamps_fingerprint_and_defaults_provider_id():
    artifact = _artifact()
    bound = bind_execution_input(artifact, _valid_bind_input(artifact))

    assert bound["execution_fingerprint"] == artifact.fingerprint
    assert bound["provider_id"] == artifact.provider_id
    # Original caller keys are preserved.
    assert bound["prompt"] == _template_args(artifact)["prompt"]


def test_bind_preserves_explicit_provider_id():
    artifact = _artifact()
    payload = _valid_bind_input(artifact)
    payload["provider_id"] = artifact.provider_id  # explicit, not defaulted
    bound = bind_execution_input(artifact, payload)
    assert bound["provider_id"] == artifact.provider_id


def test_bind_rejects_non_object_input():
    with pytest.raises(ExecutionAuthorityError, match="must be an object"):
        bind_execution_input(_artifact(), [("not", "a dict")])  # type: ignore[arg-type]


def test_bind_rejects_already_bound_input():
    artifact = _artifact()
    payload = _valid_bind_input(artifact)
    payload["execution_fingerprint"] = "pre-existing"
    with pytest.raises(ExecutionAuthorityError, match="already contains execution authority"):
        bind_execution_input(artifact, payload)


def test_bind_propagates_incomplete_projection():
    artifact = _artifact()
    payload = _valid_bind_input(artifact)
    del payload["session_id"]
    with pytest.raises(ExecutionAuthorityError, match="incomplete"):
        bind_execution_input(artifact, payload)


def test_bind_does_not_mutate_caller_payload():
    artifact = _artifact()
    payload = _valid_bind_input(artifact)
    bind_execution_input(artifact, payload)
    assert "execution_fingerprint" not in payload


# --------------------------------------------------------------------------- #
# validate_execution_input_projection
# --------------------------------------------------------------------------- #
def test_validate_accepts_matching_projection():
    artifact = _artifact()
    validate_execution_input_projection(artifact, _valid_projection(artifact))


def test_validate_rejects_non_object():
    with pytest.raises(ExecutionAuthorityError, match="must be an object"):
        validate_execution_input_projection(_artifact(), "nope")  # type: ignore[arg-type]


def test_validate_rejects_incomplete_projection():
    projection = _valid_projection(_artifact())
    del projection["mode"]
    with pytest.raises(ExecutionAuthorityError, match="incomplete"):
        validate_execution_input_projection(_artifact(), projection)


def test_validate_rejects_fingerprint_conflict():
    artifact = _artifact()
    projection = _valid_projection(artifact)
    projection["execution_fingerprint"] = "tampered"
    with pytest.raises(ExecutionAuthorityError, match="fingerprint"):
        validate_execution_input_projection(artifact, projection)


def test_validate_rejects_recovery_session_conflict():
    # session_id is a recovery-validated field: a mismatch with the frozen
    # template's authority must be rejected.
    artifact = _artifact(session_id="frozen-session")
    projection = _valid_projection(artifact)
    projection["session_id"] = "different-session"
    with pytest.raises(ExecutionAuthorityError, match="session_id"):
        validate_execution_input_projection(artifact, projection)


def test_validate_rejects_recovery_provider_id_conflict():
    artifact = _artifact()
    projection = _valid_projection(artifact)
    projection["provider_id"] = "not-claude"
    with pytest.raises(ExecutionAuthorityError, match="provider_id"):
        validate_execution_input_projection(artifact, projection)


# --------------------------------------------------------------------------- #
# load_execution_artifact
# --------------------------------------------------------------------------- #
def _write_run(tmp_path, artifact: ExecutionArtifact, *, input_payload=None) -> None:
    (tmp_path / "execution.json").write_text(
        json.dumps(artifact.to_dict()), encoding="utf-8"
    )
    if input_payload is not None:
        (tmp_path / "input.json").write_text(
            json.dumps(input_payload), encoding="utf-8"
        )


def test_load_without_input_validation_returns_artifact(tmp_path):
    artifact = _artifact()
    _write_run(tmp_path, artifact)
    loaded = load_execution_artifact(tmp_path)
    assert loaded == artifact


def test_load_with_input_validation_accepts_matching_input(tmp_path):
    artifact = _artifact()
    _write_run(tmp_path, artifact, input_payload=_valid_projection(artifact))
    loaded = load_execution_artifact(tmp_path, validate_input=True)
    assert loaded == artifact


def test_load_rejects_symlinked_execution_artifact(tmp_path):
    artifact = _artifact()
    real = tmp_path / "real-execution.json"
    real.write_text(json.dumps(artifact.to_dict()), encoding="utf-8")
    (tmp_path / "execution.json").symlink_to(real)
    with pytest.raises(ExecutionAuthorityError, match="execution artifact must not be a symlink"):
        load_execution_artifact(tmp_path)


def test_load_wraps_missing_execution_artifact(tmp_path):
    with pytest.raises(ExecutionAuthorityError, match="execution artifact is invalid"):
        load_execution_artifact(tmp_path)


def test_load_wraps_invalid_json_execution_artifact(tmp_path):
    (tmp_path / "execution.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ExecutionAuthorityError, match="execution artifact is invalid"):
        load_execution_artifact(tmp_path)


def test_load_reraises_authority_error_unwrapped(tmp_path):
    # A genuine ExecutionAuthorityError from the artifact parse (fingerprint
    # tampering) must surface unwrapped, not collapsed into "is invalid".
    artifact = _artifact()
    payload = artifact.to_dict()
    payload["fingerprint"] = "tampered"
    (tmp_path / "execution.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ExecutionAuthorityError, match="fingerprint mismatch"):
        load_execution_artifact(tmp_path)


def test_load_rejects_symlinked_input_when_validating(tmp_path):
    artifact = _artifact()
    _write_run(tmp_path, artifact)
    real_input = tmp_path / "real-input.json"
    real_input.write_text(json.dumps(_valid_projection(artifact)), encoding="utf-8")
    (tmp_path / "input.json").symlink_to(real_input)
    with pytest.raises(ExecutionAuthorityError, match="run input must not be a symlink"):
        load_execution_artifact(tmp_path, validate_input=True)


def test_load_wraps_invalid_input_json(tmp_path):
    artifact = _artifact()
    _write_run(tmp_path, artifact)
    (tmp_path / "input.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ExecutionAuthorityError, match="run input is invalid"):
        load_execution_artifact(tmp_path, validate_input=True)


def test_load_validates_input_projection(tmp_path):
    artifact = _artifact()
    projection = _valid_projection(artifact)
    del projection["model"]  # incomplete -> rejected through the projection validator
    _write_run(tmp_path, artifact, input_payload=projection)
    with pytest.raises(ExecutionAuthorityError, match="incomplete"):
        load_execution_artifact(tmp_path, validate_input=True)


# --------------------------------------------------------------------------- #
# requires_execution_artifact
# --------------------------------------------------------------------------- #
def test_requires_execution_artifact_for_known_kind():
    assert requires_execution_artifact("claude") is True


def test_requires_execution_artifact_for_unknown_kind():
    assert requires_execution_artifact("not-a-real-provider") is False

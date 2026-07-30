from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from codex_execution_common import SHA256_RE, canonical_json


MODEL_ADMISSION_SCHEMA = 1
# Fugu's CLI catalog is unverifiable (codex_model_discovery rejects it),
# so its authority is the curated FUGU_MODELS constant enforced by
# FuguProvider — only codex has a D3 catalog authority to admit against.
_CATALOG_KINDS = frozenset({"codex"})
_DESCRIPTOR_KEYS = frozenset({
    "schema",
    "provider_id",
    "provider_kind",
    "provider_generation",
    "provider_revision",
    "selected_model",
    "provider_contract_fingerprint",
})


class ModelAdmissionError(RuntimeError):
    pass


def selected_model_from_policy(
    runtime_policy: Mapping[str, Any],
    template_model: str | None,
) -> str | None:
    runner_input = runtime_policy.get("runner_input")
    if type(runner_input) is dict and "model" in runner_input:
        return runner_input["model"]
    return template_model


def _contract_fingerprint(contract: Mapping[str, Any]) -> str:
    if type(contract) is not dict:
        raise ModelAdmissionError(
            "provider execution contract is unavailable for model admission",
        )
    return hashlib.sha256(canonical_json(contract)).hexdigest()


def issue_model_admission(
    *,
    provider: Mapping[str, Any],
    selected_model: str | None,
    provider_contract: Mapping[str, Any],
) -> dict[str, Any]:
    from provider_manifest import SPECS

    provider_id = provider.get("id")
    provider_kind = provider.get("kind")
    generation = provider.get("generation")
    revision = provider.get("revision")
    if (
        type(provider_id) is not str
        or not provider_id
        or type(provider_kind) is not str
        or provider_kind not in SPECS
        or SPECS[provider_kind].virtual
        or type(generation) is not str
        or not generation
        or type(revision) is not int
        or revision < 0
        or (
            selected_model is not None
            and (
                type(selected_model) is not str
                or selected_model.strip() != selected_model
                or len(selected_model) > 512
                or "\x00" in selected_model
                or "\n" in selected_model
            )
        )
    ):
        raise ModelAdmissionError("model admission authority is invalid")
    return {
        "schema": MODEL_ADMISSION_SCHEMA,
        "provider_id": provider_id,
        "provider_kind": provider_kind,
        "provider_generation": generation,
        "provider_revision": revision,
        "selected_model": selected_model,
        "provider_contract_fingerprint": _contract_fingerprint(
            provider_contract,
        ),
    }


def _descriptor(artifact: Any) -> dict[str, Any]:
    runtime_policy = artifact.runtime_policy
    raw = runtime_policy.get("model_admission")
    if type(raw) is not dict or set(raw) != _DESCRIPTOR_KEYS:
        raise ModelAdmissionError(
            "execution artifact lacks model admission authority",
        )
    expected = (
        MODEL_ADMISSION_SCHEMA,
        artifact.provider_id,
        artifact.provider_kind,
        artifact.provider_generation,
        artifact.provider_revision,
        selected_model_from_policy(
            runtime_policy,
            artifact.template.arguments().get("model"),
        ),
        _contract_fingerprint(artifact.provider_contract),
    )
    actual = (
        raw.get("schema"),
        raw.get("provider_id"),
        raw.get("provider_kind"),
        raw.get("provider_generation"),
        raw.get("provider_revision"),
        raw.get("selected_model"),
        raw.get("provider_contract_fingerprint"),
    )
    if (
        actual != expected
        or type(actual[-1]) is not str
        or not SHA256_RE.fullmatch(actual[-1])
    ):
        raise ModelAdmissionError(
            "execution model admission authority mismatch",
        )
    return json.loads(canonical_json(raw))


def _admit_catalog_model(artifact: Any, descriptor: Mapping[str, Any]) -> None:
    import model_catalog_read_projection
    from codex_execution import CodexExecutionContract

    state = model_catalog_read_projection.runtime_snapshot(
        artifact.provider_id,
        artifact.provider_generation,
    )
    if state is None:
        raise ModelAdmissionError("model catalog is warming")
    projection = state.projection
    snapshot = state.snapshot
    if (
        projection.status != "current"
        or not projection.models_current
        or projection.reason
        or snapshot is None
        or snapshot.last_fetch_state != "ok"
    ):
        raise ModelAdmissionError("model catalog is not current")
    authority = snapshot.authority
    if (
        authority.provider_id != artifact.provider_id
        or authority.provider_generation != artifact.provider_generation
        or authority.provider_revision != artifact.provider_revision
        or projection.authority_fingerprint != authority.fingerprint
    ):
        raise ModelAdmissionError("model catalog authority mismatch")
    provider_contract = artifact.provider_contract
    if (
        type(provider_contract) is not dict
        or provider_contract.get("type") != "codex"
        or type(provider_contract.get("contract")) is not dict
    ):
        raise ModelAdmissionError("Codex model contract is unavailable")
    contract = CodexExecutionContract.from_dict(
        provider_contract["contract"],
    )
    if (
        contract.catalog_fingerprint
        != authority.execution_contract.catalog_fingerprint
    ):
        raise ModelAdmissionError("model catalog execution contract mismatch")
    selected = descriptor["selected_model"]
    if type(selected) is not str or not selected:
        raise ModelAdmissionError("default-only model selection is not runnable")
    if selected in {record.id for record in snapshot.retired}:
        raise ModelAdmissionError("retired model is not runnable")
    if selected not in snapshot.models:
        raise ModelAdmissionError("model is absent from the runnable catalog")


def _delegated_family_contract(artifact: Any) -> bool:
    """True when the artifact executes through another family's runtime
    (better_agent_runner delegation): its contract envelope carries a
    family kind that legitimately binds to the record kind. A missing or
    codex-typed contract is NOT delegation, so the catalog gate stays
    closed for native codex runs."""
    from runtime_profile import family_binds_to_kind

    contract = artifact.provider_contract
    contract_type = contract.get("type") if type(contract) is dict else None
    return (
        type(contract_type) is str
        and contract_type != artifact.provider_kind
        and contract_type != "codex"
        and family_binds_to_kind(contract_type, artifact.provider_kind)
    )


def admit_execution_model(artifact: Any) -> None:
    from provider_manifest import SPECS

    spec = SPECS.get(artifact.provider_kind)
    if spec is None or spec.virtual:
        return
    descriptor = _descriptor(artifact)
    if (
        artifact.provider_kind in _CATALOG_KINDS
        and not _delegated_family_contract(artifact)
    ):
        _admit_catalog_model(artifact, descriptor)

#!/usr/bin/env python3
"""Dedicated unit coverage for model_execution_admission.py.

The module is the security gate that admits a provider model selection into a
frozen execution artifact: it issues the admission authority, re-derives it
from the artifact at execution time, and (for codex) cross-checks the live
model catalog. Existing tests barely exercise it (~20%); this file drives
every branch directly.

Real collaborators (provider_manifest.SPECS, codex_execution_common) are used
as-is. Heavy/lazy collaborators (model_catalog_read_projection.runtime_snapshot,
codex_execution.CodexExecutionContract.from_dict, runtime_profile.family_binds_to_kind)
are patched because their parsing/IO is tested in their own modules; the unit
under test here is the admission BRANCHING that consumes their results.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_TEST_HOME = tempfile.mkdtemp(prefix="ba-model-admission-unit-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402

paths.engage_test_home(_TEST_HOME)

import codex_execution  # noqa: E402
import model_catalog_read_projection as mcr  # noqa: E402
import model_execution_admission as mea  # noqa: E402
import runtime_profile  # noqa: E402
from model_execution_admission import ModelAdmissionError  # noqa: E402

_CAT_FINGERPRINT = "a" * 64
_AUTH_FINGERPRINT = "b" * 64
_UNSET = object()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _provider(kind: str = "codex") -> dict:
    return {"id": "prov-1", "kind": kind, "generation": "gen-1", "revision": 3}


def _contract() -> dict:
    return {"type": "codex", "contract": {"catalog": "body"}}


def _artifact(
    *,
    provider_kind: str = "codex",
    provider_contract: object = _UNSET,
    runtime_policy: dict | None = None,
    provider_id: str = "prov-1",
    provider_generation: str = "gen-1",
    provider_revision: int = 3,
    template_model: str | None = "codex-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        provider_id=provider_id,
        provider_kind=provider_kind,
        provider_generation=provider_generation,
        provider_revision=provider_revision,
        provider_contract=_contract() if provider_contract is _UNSET else provider_contract,
        runtime_policy=runtime_policy if runtime_policy is not None else {},
        template=SimpleNamespace(
            arguments=lambda: {"model": template_model} if template_model else {}
        ),
    )


def _admission_descriptor(
    provider_contract: dict, selected_model: str | None = "codex-1", kind: str = "codex"
) -> dict:
    return mea.issue_model_admission(
        provider=_provider(kind),
        selected_model=selected_model,
        provider_contract=provider_contract,
    )


def _policy_with_admission(
    provider_contract: dict, selected_model: str | None = "codex-1", kind: str = "codex"
) -> dict:
    """Runtime policy carrying a VALID model_admission descriptor for the artifact."""
    return {
        "runner_input": {"model": selected_model} if selected_model else {},
        "model_admission": _admission_descriptor(provider_contract, selected_model, kind),
    }


def _catalog_state(
    *,
    status: str = "current",
    models_current: bool = True,
    reason: str | None = None,
    last_fetch_state: str = "ok",
    authority_provider_revision: int = 3,
    authority_fingerprint: str = _AUTH_FINGERPRINT,
    catalog_fingerprint: str = _CAT_FINGERPRINT,
    retired: tuple = (),
    models=("codex-1",),
    snapshot_present: bool = True,
) -> SimpleNamespace:
    authority = SimpleNamespace(
        provider_id="prov-1",
        provider_generation="gen-1",
        provider_revision=authority_provider_revision,
        # The snapshot's own authority fingerprint is fixed; the projection's
        # belief (param below) is what may diverge to simulate a mismatch.
        fingerprint=_AUTH_FINGERPRINT,
        execution_contract=SimpleNamespace(catalog_fingerprint=catalog_fingerprint),
    )
    snapshot = (
        None
        if not snapshot_present
        else SimpleNamespace(
            last_fetch_state=last_fetch_state,
            authority=authority,
            retired=tuple(SimpleNamespace(id=r) for r in retired),
            models=set(models),
        )
    )
    projection = SimpleNamespace(
        status=status,
        models_current=models_current,
        reason=reason,
        authority_fingerprint=authority_fingerprint,
    )
    return SimpleNamespace(projection=projection, snapshot=snapshot)


def _wire_contract_from_dict(monkeypatch, *, catalog_fingerprint: str = _CAT_FINGERPRINT) -> None:
    monkeypatch.setattr(
        codex_execution.CodexExecutionContract,
        "from_dict",
        classmethod(lambda cls, _raw: SimpleNamespace(catalog_fingerprint=catalog_fingerprint)),
    )


# ---------------------------------------------------------------------------
# selected_model_from_policy
# ---------------------------------------------------------------------------


def test_policy_runner_input_model_wins_over_template():
    assert mea.selected_model_from_policy({"runner_input": {"model": "runner-model"}, "model": "x"}, "tmpl") == "runner-model"


def test_policy_falls_back_to_template_when_no_runner_model():
    assert mea.selected_model_from_policy({}, "tmpl-model") == "tmpl-model"
    assert mea.selected_model_from_policy({"runner_input": "not-a-dict"}, "tmpl-model") == "tmpl-model"
    assert mea.selected_model_from_policy({"runner_input": {}}, None) is None


# ---------------------------------------------------------------------------
# _contract_fingerprint
# ---------------------------------------------------------------------------


def test_contract_fingerprint_is_stable_sha256():
    import hashlib

    from codex_execution_common import canonical_json

    contract = {"b": 2, "a": 1}
    assert mea._contract_fingerprint(contract) == hashlib.sha256(canonical_json(contract)).hexdigest()


def test_contract_fingerprint_rejects_non_dict():
    with pytest.raises(ModelAdmissionError, match="unavailable for model admission"):
        mea._contract_fingerprint("not-a-dict")


# ---------------------------------------------------------------------------
# issue_model_admission
# ---------------------------------------------------------------------------


def test_issue_model_admission_valid_with_and_without_selected_model():
    contract = _contract()
    admitted = mea.issue_model_admission(
        provider=_provider(), selected_model="codex-1", provider_contract=contract
    )
    assert admitted["schema"] == mea.MODEL_ADMISSION_SCHEMA
    assert admitted["provider_id"] == "prov-1"
    assert admitted["provider_kind"] == "codex"
    assert admitted["provider_generation"] == "gen-1"
    assert admitted["provider_revision"] == 3
    assert admitted["selected_model"] == "codex-1"
    assert admitted["provider_contract_fingerprint"] == mea._contract_fingerprint(contract)

    none_selected = mea.issue_model_admission(
        provider=_provider("claude"), selected_model=None, provider_contract=contract
    )
    assert none_selected["selected_model"] is None
    assert none_selected["provider_kind"] == "claude"


@pytest.mark.parametrize(
    "provider, selected_model",
    [
        ({"id": "", "kind": "codex", "generation": "g", "revision": 0}, None),
        ({"id": 1, "kind": "codex", "generation": "g", "revision": 0}, None),
        ({"id": "p", "kind": "", "generation": "g", "revision": 0}, None),
        ({"id": "p", "kind": "no-such-kind", "generation": "g", "revision": 0}, None),
        ({"id": "p", "kind": "claude-remote", "generation": "g", "revision": 0}, None),  # virtual
        ({"id": "p", "kind": "codex", "generation": "", "revision": 0}, None),
        ({"id": "p", "kind": "codex", "generation": "g", "revision": "0"}, None),  # revision not int
        ({"id": "p", "kind": "codex", "generation": "g", "revision": -1}, None),
        (_provider(), " trailing"),  # strip() != selected
        (_provider(), "x" * 513),  # too long
        (_provider(), "has\x00null"),
        (_provider(), "has\nnewline"),
        (_provider(), 123),  # not a str
    ],
)
def test_issue_model_admission_rejects_invalid_authority(provider, selected_model):
    with pytest.raises(ModelAdmissionError, match="authority is invalid"):
        mea.issue_model_admission(
            provider=provider, selected_model=selected_model, provider_contract=_contract()
        )


# ---------------------------------------------------------------------------
# _descriptor
# ---------------------------------------------------------------------------


def test_descriptor_accepts_matching_authority():
    contract = _contract()
    artifact = _artifact(provider_contract=contract, runtime_policy=_policy_with_admission(contract))

    descriptor = mea._descriptor(artifact)

    assert descriptor["selected_model"] == "codex-1"
    assert descriptor["provider_contract_fingerprint"] == mea._contract_fingerprint(contract)


def test_descriptor_rejects_missing_or_malformed_block():
    contract = _contract()
    # no model_admission key at all
    with pytest.raises(ModelAdmissionError, match="lacks model admission authority"):
        mea._descriptor(_artifact(provider_contract=contract, runtime_policy={}))
    # wrong key set
    with pytest.raises(ModelAdmissionError, match="lacks model admission authority"):
        mea._descriptor(
            _artifact(provider_contract=contract, runtime_policy={"model_admission": {"schema": 1}})
        )


def test_descriptor_rejects_mismatched_authority():
    contract = _contract()
    policy = _policy_with_admission(contract)
    policy["model_admission"]["selected_model"] = "tampered"  # diverges from runner_input/expected
    artifact = _artifact(provider_contract=contract, runtime_policy=policy)

    with pytest.raises(ModelAdmissionError, match="authority mismatch"):
        mea._descriptor(artifact)


# ---------------------------------------------------------------------------
# _delegated_family_contract
# ---------------------------------------------------------------------------


def test_delegated_family_contract_truth_table(monkeypatch):
    monkeypatch.setattr(runtime_profile, "family_binds_to_kind", lambda _family, _kind: True)

    # contract not a dict -> False
    assert mea._delegated_family_contract(_artifact(provider_kind="codex", provider_contract=None)) is False  # type: ignore[arg-type]
    # type == provider_kind -> False
    assert mea._delegated_family_contract(_artifact(provider_kind="codex", provider_contract={"type": "codex"})) is False
    # type == "codex" -> False (never delegation)
    assert mea._delegated_family_contract(_artifact(provider_kind="kimi", provider_contract={"type": "codex"})) is False
    # different, non-codex, family binds -> True
    assert (
        mea._delegated_family_contract(_artifact(provider_kind="kimi", provider_contract={"type": "claude"}))
        is True
    )


def test_delegated_family_contract_false_when_family_does_not_bind(monkeypatch):
    monkeypatch.setattr(runtime_profile, "family_binds_to_kind", lambda _family, _kind: False)
    assert (
        mea._delegated_family_contract(_artifact(provider_kind="kimi", provider_contract={"type": "claude"}))
        is False
    )


# ---------------------------------------------------------------------------
# _admit_catalog_model
# ---------------------------------------------------------------------------


def _catalog_artifact() -> SimpleNamespace:
    contract = _contract()
    return _artifact(provider_contract=contract, runtime_policy=_policy_with_admission(contract))


def test_admit_catalog_model_happy_path(monkeypatch):
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: _catalog_state())
    _wire_contract_from_dict(monkeypatch)

    # Returns None on success; no exception is the assertion.
    assert mea._admit_catalog_model(_catalog_artifact(), _admission_descriptor(_contract())) is None


def test_admit_catalog_model_rejects_warming_catalog(monkeypatch):
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: None)

    with pytest.raises(ModelAdmissionError, match="catalog is warming"):
        mea._admit_catalog_model(_catalog_artifact(), _admission_descriptor(_contract()))


@pytest.mark.parametrize(
    "state_kwarg",
    [
        dict(status="stale"),
        dict(status="refreshing"),
        dict(status="error"),
        dict(models_current=False),
        dict(reason="fetching"),
    ],
)
def test_admit_catalog_model_admits_stale_projection_with_good_snapshot(monkeypatch, state_kwarg):
    """A non-"current" projection (background refresh pending/degraded) must not
    block admission as long as the last successfully committed snapshot still
    validates (authority/fingerprint/contract/model checks below still apply) —
    availability must not regress on mere staleness."""
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: _catalog_state(**state_kwarg))
    _wire_contract_from_dict(monkeypatch)

    assert mea._admit_catalog_model(_catalog_artifact(), _admission_descriptor(_contract())) is None


@pytest.mark.parametrize(
    "state_kwarg, match",
    [
        (dict(snapshot_present=False), "catalog is warming"),
        (dict(last_fetch_state="error"), "catalog is warming"),
        (dict(last_fetch_state="failing"), "catalog is warming"),
    ],
)
def test_admit_catalog_model_rejects_when_no_committed_snapshot(monkeypatch, state_kwarg, match):
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: _catalog_state(**state_kwarg))

    with pytest.raises(ModelAdmissionError, match=match):
        mea._admit_catalog_model(_catalog_artifact(), _admission_descriptor(_contract()))


@pytest.mark.parametrize(
    "state_kwarg, match",
    [
        (dict(authority_provider_revision=99), "authority mismatch"),
        (dict(authority_fingerprint="c" * 64), "authority mismatch"),
    ],
)
def test_admit_catalog_model_rejects_authority_mismatch(monkeypatch, state_kwarg, match):
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: _catalog_state(**state_kwarg))

    with pytest.raises(ModelAdmissionError, match=match):
        mea._admit_catalog_model(_catalog_artifact(), _admission_descriptor(_contract()))


@pytest.mark.parametrize(
    "contract, match",
    [
        (None, "contract is unavailable"),  # type: ignore[arg-type]
        ("nope", "contract is unavailable"),  # type: ignore[arg-type]
        ({"type": "claude"}, "contract is unavailable"),
        ({"type": "codex", "contract": "not-a-dict"}, "contract is unavailable"),
    ],
)
def test_admit_catalog_model_rejects_bad_provider_contract(monkeypatch, contract, match):
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: _catalog_state())
    artifact = _artifact(provider_contract=contract, runtime_policy=_policy_with_admission(_contract()))

    with pytest.raises(ModelAdmissionError, match=match):
        mea._admit_catalog_model(artifact, _admission_descriptor(_contract()))


def test_admit_catalog_model_rejects_execution_contract_mismatch(monkeypatch):
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: _catalog_state())
    _wire_contract_from_dict(monkeypatch, catalog_fingerprint="d" * 64)  # diverges from authority

    with pytest.raises(ModelAdmissionError, match="execution contract mismatch"):
        mea._admit_catalog_model(_catalog_artifact(), _admission_descriptor(_contract()))


def test_admit_catalog_model_rejects_default_only_selection(monkeypatch):
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: _catalog_state())
    _wire_contract_from_dict(monkeypatch)
    contract = _contract()
    artifact = _artifact(provider_contract=contract, runtime_policy=_policy_with_admission(contract, selected_model=None))

    with pytest.raises(ModelAdmissionError, match="default-only model selection is not runnable"):
        mea._admit_catalog_model(artifact, _admission_descriptor(contract, selected_model=None))


def test_admit_catalog_model_rejects_retired_model(monkeypatch):
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: _catalog_state(retired=("codex-1",), models=("codex-1",)))
    _wire_contract_from_dict(monkeypatch)

    with pytest.raises(ModelAdmissionError, match="retired model is not runnable"):
        mea._admit_catalog_model(_catalog_artifact(), _admission_descriptor(_contract()))


def test_admit_catalog_model_rejects_absent_model(monkeypatch):
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: _catalog_state(models=("other-model",)))
    _wire_contract_from_dict(monkeypatch)

    with pytest.raises(ModelAdmissionError, match="absent from the runnable catalog"):
        mea._admit_catalog_model(_catalog_artifact(), _admission_descriptor(_contract()))


# ---------------------------------------------------------------------------
# admit_execution_model (entry point)
# ---------------------------------------------------------------------------


def test_admit_execution_model_skips_unknown_kind():
    # SPECS has no such kind -> spec is None -> early return.
    assert mea.admit_execution_model(_artifact(provider_kind="no-such-kind")) is None


def test_admit_execution_model_skips_virtual_kind():
    assert mea.admit_execution_model(
        _artifact(provider_kind="claude-remote", runtime_policy={})
    ) is None


def test_admit_execution_model_non_catalog_kind_bypasses_catalog_gate():
    contract = _contract()
    artifact = _artifact(
        provider_kind="claude",
        provider_contract=contract,
        runtime_policy=_policy_with_admission(contract, kind="claude"),
    )
    # claude is not in _CATALOG_KINDS -> no catalog lookup; descriptor alone admits.
    assert mea.admit_execution_model(artifact) is None


def test_admit_execution_model_codex_delegated_bypasses_catalog_gate(monkeypatch):
    monkeypatch.setattr(runtime_profile, "family_binds_to_kind", lambda _f, _k: True)
    contract = {"type": "claude", "contract": {}}  # delegated family contract
    artifact = _artifact(
        provider_kind="codex",
        provider_contract=contract,
        runtime_policy={  # descriptor fingerprint derives from this contract
            "runner_input": {"model": "codex-1"},
            "model_admission": mea.issue_model_admission(
                provider=_provider(), selected_model="codex-1", provider_contract=contract
            ),
        },
    )
    # delegated -> catalog gate skipped; runtime_snapshot must not be called.
    calls = []

    def _spy(*_a):
        calls.append(1)
        return _catalog_state()

    monkeypatch.setattr(mcr, "runtime_snapshot", _spy)
    assert mea.admit_execution_model(artifact) is None
    assert calls == []


def test_admit_execution_model_codex_runs_catalog_gate(monkeypatch):
    monkeypatch.setattr(mcr, "runtime_snapshot", lambda *_a: _catalog_state())
    _wire_contract_from_dict(monkeypatch)
    artifact = _catalog_artifact()
    assert mea.admit_execution_model(artifact) is None

#!/usr/bin/env python3
"""Locks two family execution contract behaviors:

1. `_decode_family` delegates the embedded runtime-capability manifest to
   its structural whitelist validator instead of the heuristic secret key
   scan. The manifest is identifier-keyed in places (prewarm_status keyed
   by MCP server name), so a server whose name contains a secret word
   (e.g. a credential-broker) froze every claude-family turn with
   'provider execution contract must be secret-free'. Everything outside
   the manifest stays under the heuristic scan.

2. `prepare_family_execution` admits better_agent_runner delegation:
   a codex/fugu record executing through the openai family runtime must
   pass the family guard via runtime_kind and stamp the execution-side
   authority record with the runtime family kind.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_home

_test_home.isolate(prefix="contract-family-")

import provider_family_execution_runtime as runtime  # noqa: E402
from codex_execution_common import ExecutionContractError  # noqa: E402
from provider_execution_contract import (  # noqa: E402
    ProviderExecutionContractError,
    provider_family_contract,
)
from provider_family_runtime_capabilities import (  # noqa: E402
    family_runtime_capability_payload,
)
from test_provider_family_runtime_capabilities import _snapshot  # noqa: E402

PROVIDER = {
    "id": "p-1",
    "kind": "claude",
    "generation": "0d4dc3f2-6f95-4c5c-b0c3-1a2b3c4d5e6f",
    "revision": 0,
    "execution_revision": 0,
}

BROKER = "identity-credential-broker"


def _broker_plan() -> dict:
    return {
        "harness": {
            "instructions": ["Better Agent runtime"],
            "tool_policy": {"allow": ["Read", f"{BROKER}.issue"]},
        },
        "tools": ["Read", f"{BROKER}.issue"],
        "mcp_servers": [
            {
                "name": BROKER,
                "transport": "stdio",
                "config": {
                    "argv": ["/opt/better-agent/broker", "--stdio"],
                    "env_refs": {"IDENTITY": "extension:scheduler"},
                },
                "tool_names": [f"{BROKER}.issue"],
                "prewarm": {
                    "eligible": True,
                    "readiness_required": False,
                },
            },
        ],
    }


def _broker_payload() -> dict:
    with tempfile.TemporaryDirectory(prefix="contract-broker-") as raw:
        prepared, _ = _snapshot(
            Path(raw),
            plan=_broker_plan(),
            prewarm_results={
                BROKER: {"status": "ready", "error": None},
            },
        )
        return {
            "launch_attestation": {"fingerprint": "aa" * 32},
            **family_runtime_capability_payload(prepared),
        }


def test_identifier_keyed_manifest_passes_secret_scan() -> None:
    payload = _broker_payload()
    assert BROKER in payload["runtime_capabilities"]["prewarm_status"]
    envelope = provider_family_contract(PROVIDER, payload=payload)
    frozen = envelope["contract"]["payload"]["runtime_capabilities"]
    assert BROKER in frozen["prewarm_status"]


def test_secret_key_outside_manifest_still_fails_closed() -> None:
    payload = _broker_payload()
    payload["launch_attestation"] = {"api_key": "sk-nope"}
    try:
        provider_family_contract(PROVIDER, payload=payload)
    except ProviderExecutionContractError:
        return
    raise AssertionError("expected secret-free rejection")


def test_malformed_manifest_fails_closed() -> None:
    payload = _broker_payload()
    payload["runtime_capabilities"] = {"schema": 999}
    try:
        provider_family_contract(PROVIDER, payload=payload)
    except ProviderExecutionContractError:
        return
    raise AssertionError("expected manifest validation rejection")


def _run_prepare(record: dict, family: str):
    seen: dict = {}

    def fake_prepare_execution(provider, **kwargs):
        seen["provider"] = provider
        return SimpleNamespace(
            artifact=SimpleNamespace(
                template=SimpleNamespace(arguments=lambda: {"run_id": "r1"}),
            ),
        )

    def fake_contract(provider, *, payload):
        seen["contract_provider"] = provider
        return {"type": family, "contract": {}}

    saved = (
        runtime.prepare_execution,
        runtime.provider_family_contract,
        runtime.family_runtime_capability_payload,
        runtime.stage_family_runtime_capabilities,
    )
    runtime.prepare_execution = fake_prepare_execution
    runtime.provider_family_contract = fake_contract
    runtime.family_runtime_capability_payload = lambda prepared: {}
    runtime.stage_family_runtime_capabilities = lambda run_id, caps: None
    try:
        runtime.prepare_family_execution(
            record,
            start_arguments={},
            runner_input={},
            launch=SimpleNamespace(family=family, to_payload=lambda: {}),
            capabilities=SimpleNamespace(manifest={"family": family}),
        )
    finally:
        (
            runtime.prepare_execution,
            runtime.provider_family_contract,
            runtime.family_runtime_capability_payload,
            runtime.stage_family_runtime_capabilities,
        ) = saved
    return seen


def test_ba_runner_delegation_admits_runtime_family() -> None:
    record = {**PROVIDER, "kind": "codex", "runner": "better_agent_runner"}
    seen = _run_prepare(record, "openai")
    # Artifact authority keeps the configured record kind; the contract
    # envelope carries the runtime family.
    assert seen["provider"]["kind"] == "codex"
    assert seen["contract_provider"]["kind"] == "openai"
    assert record["kind"] == "codex"


def test_family_execution_kind_reads_frozen_runner_input() -> None:
    artifact = SimpleNamespace(
        provider_kind="codex",
        runtime_policy={"runner_input": {"provider_kind": "openai"}},
    )
    assert runtime.family_execution_kind(artifact) == "openai"
    native = SimpleNamespace(
        provider_kind="claude",
        runtime_policy={"runner_input": {"provider_kind": "claude"}},
    )
    assert runtime.family_execution_kind(native) == "claude"


def test_family_execution_kind_rejects_unrelated_family() -> None:
    artifact = SimpleNamespace(
        provider_kind="codex",
        runtime_policy={"runner_input": {"provider_kind": "claude"}},
    )
    try:
        runtime.family_execution_kind(artifact)
    except ExecutionContractError:
        return
    raise AssertionError("expected unrelated family rejection")


def test_freeze_binds_family_contract_to_delegating_record() -> None:
    import json as _json

    from provider_execution_contract import freeze_provider_contract

    envelope = provider_family_contract(
        {**PROVIDER, "kind": "openai"},
        payload=_broker_payload(),
    )
    frozen = freeze_provider_contract({**PROVIDER, "kind": "codex"}, envelope)
    assert _json.loads(frozen)["contract"]["provider_kind"] == "openai"
    try:
        freeze_provider_contract({**PROVIDER, "kind": "claude"}, envelope)
    except ProviderExecutionContractError:
        return
    raise AssertionError("expected authority mismatch for unrelated record")


def test_native_record_must_match_family() -> None:
    record = {**PROVIDER, "kind": "codex", "runner": "native"}
    try:
        _run_prepare(record, "openai")
    except ExecutionContractError:
        return
    raise AssertionError("expected family mismatch rejection")


def test_matching_kind_passes_unchanged() -> None:
    record = {**PROVIDER, "kind": "claude", "runner": "native"}
    seen = _run_prepare(record, "claude")
    assert seen["provider"]["kind"] == "claude"


def test_helpers_reject_invalid_input() -> None:
    from provider_execution_contract import (
        _FrozenContract,
        _canonical_json,
        _reject_secrets,
    )

    # _canonical_json rejects values that are not JSON-compatible.
    try:
        _canonical_json(object())
        raise AssertionError("expected JSON-compatibility rejection")
    except ProviderExecutionContractError:
        pass
    # _reject_secrets walks lists and condemns embedded secrets.
    try:
        _reject_secrets([{"api_key": "sk"}])
        raise AssertionError("expected secret-free rejection inside list")
    except ProviderExecutionContractError:
        pass
    # A list with no secrets traverses cleanly and returns.
    _reject_secrets([{"safe": 1}, {"also_safe": {"nested": 2}}])
    # Non-string dict keys are rejected.
    try:
        _reject_secrets({1: "x"})
        raise AssertionError("expected non-string-key rejection")
    except ProviderExecutionContractError:
        pass
    # A frozen contract whose payload decodes to a non-dict is corrupt.
    try:
        _FrozenContract("[1, 2]").value
        raise AssertionError("expected corrupt-contract rejection")
    except ProviderExecutionContractError:
        pass


def test_decode_family_rejects_invalid_shape_and_values() -> None:
    from provider_execution_contract import FAMILY_CONTRACT_SCHEMA, _decode_family

    # Wrong key set → invalid.
    try:
        _decode_family({"schema": FAMILY_CONTRACT_SCHEMA})
        raise AssertionError("expected invalid-shape rejection")
    except ProviderExecutionContractError:
        pass
    # Right key set, unsupported schema value → unsupported.
    bad = {
        "schema": FAMILY_CONTRACT_SCHEMA + 99,
        "provider_id": "p-1",
        "provider_kind": "claude",
        "provider_generation": "g",
        "provider_revision": 0,
        "provider_execution_revision": 0,
        "payload": {},
    }
    try:
        _decode_family(bad)
        raise AssertionError("expected unsupported-values rejection")
    except ProviderExecutionContractError:
        pass


def test_decode_codex_rejects_non_object() -> None:
    from provider_execution_contract import _decode_codex

    # Non-object input is rejected at the codec boundary.
    try:
        _decode_codex("not-a-dict")
        raise AssertionError("expected codex non-object rejection")
    except ProviderExecutionContractError:
        pass


def test_freeze_rejects_malformed_and_unsupported_envelopes() -> None:
    from provider_execution_contract import freeze_provider_contract

    # A None value short-circuits: nothing to freeze.
    assert freeze_provider_contract(PROVIDER, None) is None
    # A value that is not a {type, contract} object is invalid.
    try:
        freeze_provider_contract(PROVIDER, {"nope": 1})
        raise AssertionError("expected invalid-envelope rejection")
    except ProviderExecutionContractError:
        pass
    # An unknown contract type is unsupported.
    try:
        freeze_provider_contract(PROVIDER, {"type": "alien", "contract": {}})
        raise AssertionError("expected unsupported-type rejection")
    except ProviderExecutionContractError:
        pass


def test_freeze_rejects_envelope_whose_family_kind_disagrees_with_type() -> None:
    from provider_execution_contract import (
        FAMILY_CONTRACT_SCHEMA,
        freeze_provider_contract,
    )

    # Authority matches, but the envelope's family kind disagrees with the
    # declared contract type → the tamper guard rejects.
    envelope = {
        "type": "claude",
        "contract": {
            "schema": FAMILY_CONTRACT_SCHEMA,
            "provider_id": PROVIDER["id"],
            "provider_kind": "openai",
            "provider_generation": PROVIDER["generation"],
            "provider_revision": 0,
            "provider_execution_revision": 0,
            "payload": {},
        },
    }
    try:
        freeze_provider_contract({**PROVIDER, "kind": "openai"}, envelope)
        raise AssertionError("expected family-kind mismatch rejection")
    except ProviderExecutionContractError:
        pass


def test_family_contract_rejects_unknown_kind_and_non_dict_payload() -> None:
    # Unknown provider kind is rejected before any freezing.
    try:
        provider_family_contract({**PROVIDER, "kind": "alien"}, payload={})
        raise AssertionError("expected unknown-kind rejection")
    except ProviderExecutionContractError:
        pass
    # Non-dict payload is rejected.
    try:
        provider_family_contract(PROVIDER, payload=[])  # type: ignore[arg-type]
        raise AssertionError("expected non-dict-payload rejection")
    except ProviderExecutionContractError:
        pass


def test_family_contract_freezes_payload_without_runtime_capabilities() -> None:
    # A payload with no runtime_capabilities manifest skips the manifest
    # validator and still freezes under the heuristic secret scan.
    envelope = provider_family_contract(
        PROVIDER, payload={"launch_attestation": {"fingerprint": "aa" * 32}}
    )
    assert envelope["type"] == PROVIDER["kind"]
    assert "runtime_capabilities" not in envelope["contract"]["payload"]


TESTS = (
    test_identifier_keyed_manifest_passes_secret_scan,
    test_secret_key_outside_manifest_still_fails_closed,
    test_malformed_manifest_fails_closed,
    test_ba_runner_delegation_admits_runtime_family,
    test_family_execution_kind_reads_frozen_runner_input,
    test_family_execution_kind_rejects_unrelated_family,
    test_freeze_binds_family_contract_to_delegating_record,
    test_native_record_must_match_family,
    test_matching_kind_passes_unchanged,
    test_helpers_reject_invalid_input,
    test_decode_family_rejects_invalid_shape_and_values,
    test_decode_codex_rejects_non_object,
    test_freeze_rejects_malformed_and_unsupported_envelopes,
    test_freeze_rejects_envelope_whose_family_kind_disagrees_with_type,
    test_family_contract_rejects_unknown_kind_and_non_dict_payload,
    test_family_contract_freezes_payload_without_runtime_capabilities,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print("all provider execution contract family tests passed")

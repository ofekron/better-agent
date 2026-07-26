#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "sdk"))

import _test_home

_test_home.isolate("ba-marketplace-protocol-")

import capability_api  # noqa: E402
import global_events  # noqa: E402
import marketplace_bridge_api  # noqa: E402
import runtime_operations  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from marketplace_bridge import MarketplaceBridge, MarketplaceBridgeError  # noqa: E402
from marketplace_bridge_store import (  # noqa: E402
    MarketplaceStateError,
    MarketplaceStateStore,
    empty_state,
)
from marketplace_device_identity import (  # noqa: E402
    DeviceIdentity,
    MarketplaceDeviceIdentity,
)
from marketplace_protocol import PROTOCOL, PROTOCOL_HASH, canonical_hash  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def test_protocol_artifact_is_canonical() -> None:
    sdk_path = (
        ROOT.parent
        / "sdk"
        / "better_agent_sdk"
        / "marketplace_protocol"
        / "v1"
        / "protocol.json"
    )
    raw = sdk_path.read_bytes()
    payload = json.loads(raw)
    check(payload == PROTOCOL, "backend and SDK load one protocol artifact")
    check(len(PROTOCOL_HASH) == 64, "protocol compatibility hash is sha256")
    check(
        set(PROTOCOL["actions"]) == {
            "install",
            "enable",
            "disable",
            "update",
            "uninstall",
        },
        "protocol exposes only typed Marketplace mutations",
    )
    forbidden = set(PROTOCOL["forbidden_action_fields"])
    check(
        {"artifact_url", "artifact_sha256", "command", "arguments"} <= forbidden,
        "protocol forbids coordinates and generic commands",
    )


def test_device_signer_enforces_exact_operation_shape() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_raw = private_key.public_key().public_bytes_raw()
    identity = DeviceIdentity(
        device_id=f"badvc_{'1' * 32}",
        public_key=base64.urlsafe_b64encode(public_raw).decode().rstrip("="),
        label="test",
        private_key=private_key,
    )
    signer = MarketplaceDeviceIdentity()
    body = {"protocol_hash": PROTOCOL_HASH, "wait_seconds": 30}
    path, signed = signer.sign_device_request(
        identity,
        "lease",
        {"device_id": identity.device_id},
        body,
        f"bachal_{'A' * 43}",
        "https://marketplace.example",
    )
    check(
        path == f"/protocol/v1/devices/{identity.device_id}/actions/lease",
        "signer derives the protocol-owned path",
    )
    check(set(signed) == {*body, "challenge", "signature"}, "signer adds only auth fields")
    try:
        signer.sign_device_request(
            identity,
            "lease",
            {"device_id": identity.device_id},
            {**body, "command": "install"},
            f"bachal_{'B' * 43}",
            "https://marketplace.example",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("signer accepted an untyped request field")


def test_store_is_durable_private_and_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        store = MarketplaceStateStore(path)
        state = store.write(empty_state())
        check(store.read() == state, "state round-trips through durable store")
        if os.name != "nt":
            check(path.stat().st_mode & 0o777 == 0o600, "state file is mode 0600")
        invalid = store.read()
        invalid["unexpected"] = True
        try:
            store.write(invalid)
        except MarketplaceStateError:
            pass
        else:
            raise AssertionError("state store accepted an unknown field")
        invalid = store.read()
        invalid["schema_version"] = 2
        try:
            store.write(invalid)
        except MarketplaceStateError:
            pass
        else:
            raise AssertionError("state store accepted an unknown schema")


def _install_action() -> dict:
    action_id = f"baact_{'2' * 32}"
    return {
        "action_id": action_id,
        "action_type": "install",
        "extension_id": "ofek.test",
        "expected_version": "1.2.3",
        "snapshot_id": f"default-v1:7:{'3' * 64}",
        "publisher_fingerprint": "4" * 64,
        "permission_hash": "5" * 64,
        "lease_capability": "lease-secret",
        "extension_name": "Test",
        "publisher_name": "Ofek",
        "permission_delta": ["network"],
    }


def test_action_envelope_excludes_coordinates_and_snapshot_excludes_secrets() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = MarketplaceStateStore(Path(directory) / "state.json")
        bridge = MarketplaceBridge(store=store)
        action = _install_action()
        intent = bridge._validate_action(action)
        check(intent is not None, "typed install action is accepted")
        check(
            intent["envelope_digest"]
            == canonical_hash(
                {
                    "action_id": action["action_id"],
                    "action_type": "install",
                    "extension_id": "ofek.test",
                    "expected_version": "1.2.3",
                    "snapshot_id": action["snapshot_id"],
                    "publisher_fingerprint": "4" * 64,
                    "permission_hash": "5" * 64,
                }
            ),
            "action digest binds only the typed approved envelope",
        )
        try:
            bridge._validate_action(
                {**action, "artifact_url": "https://attacker.invalid/payload"}
            )
        except MarketplaceBridgeError:
            pass
        else:
            raise AssertionError("action accepted an artifact coordinate")
        state = empty_state()
        state["intents"][action["action_id"]] = intent
        store.write(state)
        snapshot = bridge.snapshot()
        serialized = json.dumps(snapshot)
        check("lease-secret" not in serialized, "snapshot excludes lease capability")
        check("envelope_digest" not in serialized, "snapshot excludes receipt digest")


def test_rest_ws_contract_and_extension_capability_boundary() -> None:
    paths = {
        (method, route.path)
        for route in marketplace_bridge_api.router.routes
        for method in route.methods
    }
    expected = {
        ("GET", "/api/marketplace-bridge"),
        ("POST", "/api/internal/marketplace-bridge/pair"),
        ("POST", "/api/marketplace-bridge/intents/{intent_id}/approve"),
        ("POST", "/api/marketplace-bridge/intents/{intent_id}/reject"),
        ("DELETE", "/api/marketplace-bridge/devices/{device_id}"),
    }
    check(expected <= paths, "REST snapshot/pair/approve/reject/revoke contract is registered")
    event = global_events.validate_global_event(
        "marketplace_bridge_changed",
        {"revision": 3},
    )
    check(event == {"revision": 3}, "WS invalidation carries authoritative revision")
    mutations = {"install", "enabled.set", "uninstall", "update"}
    registered = {
        action
        for capability, action in capability_api._ACTIONS
        if capability == "marketplace"
    }
    check(not mutations & registered, "Marketplace extension has no mutation capability")
    check(
        callable(runtime_operations._register_marketplace_tools),
        "separately approved core Marketplace operations remain registered",
    )
    manifest = json.loads(
        (
            ROOT.parent
            / "extensions"
            / "marketplace"
            / "better-agent-extension.json"
        ).read_text()
    )
    grants = set(manifest["entrypoints"]["provider_capabilities"])
    check(
        not {f"marketplace.{action}" for action in mutations} & grants,
        "Marketplace manifest grants no mutation capability",
    )


if __name__ == "__main__":
    test_protocol_artifact_is_canonical()
    test_device_signer_enforces_exact_operation_shape()
    test_store_is_durable_private_and_fail_closed()
    test_action_envelope_excludes_coordinates_and_snapshot_excludes_secrets()
    test_rest_ws_contract_and_extension_capability_boundary()

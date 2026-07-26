#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "sdk"))

import _test_home

_test_home.isolate("ba-marketplace-protocol-")

import capability_api  # noqa: E402
import global_events  # noqa: E402
import marketplace_auth  # noqa: E402
import marketplace_bridge_api  # noqa: E402
import marketplace_service  # noqa: E402
import runtime_operations  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from fastapi import HTTPException  # noqa: E402
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
        set(PROTOCOL["actions"])
        == {
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
    check(
        set(signed) == {*body, "challenge", "signature"}, "signer adds only auth fields"
    )
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


def test_fence_commit_is_recoverable_after_response_loss() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = MarketplaceStateStore(Path(directory) / "state.json")

        class Identity:
            def store_terminal_capability(self, action_id: str, capability: str) -> str:
                check(capability == "terminal-secret", "terminal secret stays in core")
                return f"terminal-{action_id}"

        bridge = MarketplaceBridge(store=store, identity=Identity())
        bridge._extension_state = lambda _extension_id: {"exists": False}
        action = _install_action()
        intent = bridge._validate_action(action)
        state = empty_state()
        state["device"] = {
            "device_id": f"badvc_{'1' * 32}",
            "public_key": "A" * 43,
            "label": "Test device",
            "paired": True,
            "revoked": False,
            "revocation_pending": False,
            "epoch": 1,
            "server_origin": "https://ofek-dev.com/api/marketplace",
        }
        state["connection_state"] = "connected"
        state["intents"][action["action_id"]] = intent
        store.write(state)

        async def resolve_catalog(_state: dict, _intent: dict) -> tuple[dict, str, int]:
            return {}, "default-v1", 7

        async def sign(
            _state: dict,
            _operation: str,
            _params: dict,
            body: dict,
        ) -> tuple[str, dict]:
            return "/fence", body

        revisions: list[int] = []
        original_fence = marketplace_service.protocol_fence

        async def fence(_device_id: str, _action_id: str, signed: dict) -> dict:
            revisions.append(signed["receipt_revision"])
            if len(revisions) == 1:
                raise RuntimeError("response lost after server commit")
            return {
                "terminal_capability": "terminal-secret",
                "reconcile_deadline": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            }

        bridge._resolve_catalog = resolve_catalog
        bridge._signed_operation = sign
        marketplace_service.protocol_fence = fence
        try:
            try:
                asyncio.run(bridge._approve_action(action["action_id"]))
            except RuntimeError:
                pass
            else:
                raise AssertionError("simulated fence response loss did not surface")
            pending = store.read()["receipts"][action["action_id"]]
            check(
                pending["phase"] == "fence_pending",
                "pre-fence receipt survives response loss",
            )
            asyncio.run(bridge._complete_pending_fence(action["action_id"]))
            recovered = store.read()["receipts"][action["action_id"]]
            check(
                recovered["phase"] == "fenced",
                "reconciliation completes the durable pending fence",
            )
            check(
                revisions == [pending["receipt_revision"]] * 2,
                "fence retry preserves its idempotency revision",
            )
        finally:
            marketplace_service.protocol_fence = original_fence


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
    check(
        expected <= paths,
        "REST snapshot/pair/approve/reject/revoke contract is registered",
    )
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
    check(
        not mutations & registered, "Marketplace extension has no mutation capability"
    )
    check(
        callable(runtime_operations._register_marketplace_tools),
        "separately approved core Marketplace operations remain registered",
    )
    manifest = json.loads(
        (
            ROOT.parent / "extensions" / "marketplace" / "better-agent-extension.json"
        ).read_text()
    )
    grants = set(manifest["entrypoints"]["provider_capabilities"])
    check(
        not {f"marketplace.{action}" for action in mutations} & grants,
        "Marketplace manifest grants no mutation capability",
    )
    permissions = manifest["permissions"]
    check("storage" not in permissions, "Marketplace auth secrets stay in core custody")
    extension_source = (
        ROOT.parent / "extensions" / "marketplace" / "backend" / "routes.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "/protocol/v1/pair/context",
        "/protocol/v1/pair/redeem",
        "/protocol/v1/devices/{device_id}/actions/lease",
        "/protocol/v1/actions/{action_id}/terminal-ack",
        "X-BA-Device-Signature",
    ):
        check(
            forbidden not in extension_source,
            f"Marketplace extension cannot observe core mutation secret {forbidden}",
        )


def test_protocol_mutation_transport_bypasses_extension_backend() -> None:
    extension_calls: list[tuple[str, str, dict | None]] = []
    core_calls: list[tuple[str, str, str, dict, bool]] = []
    original_invoke = marketplace_service._invoke
    original_get_secret = marketplace_service.password_manager.get_service_password
    original_request = marketplace_service.marketplace_protocol_transport.request

    async def fake_invoke(
        path: str,
        *,
        method: str = "GET",
        body: dict | None = None,
    ) -> dict:
        extension_calls.append((path, method, body))
        return {"authenticated": True}

    def fake_get_secret(service: str, account: str) -> str:
        check(
            service == marketplace_auth.service_name(),
            "core reads home-scoped auth service",
        )
        check(
            account == marketplace_auth.AUTH_ACCOUNT,
            "core reads canonical auth account",
        )
        return json.dumps({"access_token": "core-only-access"})

    def fake_request(
        method: str,
        path: str,
        *,
        access_token: str,
        body: dict,
        signed: bool = False,
    ) -> dict:
        core_calls.append((method, path, access_token, body, signed))
        return {"terminal_capability": "core-only-terminal"}

    marketplace_service._invoke = fake_invoke
    marketplace_service.password_manager.get_service_password = fake_get_secret
    marketplace_service.marketplace_protocol_transport.request = fake_request
    try:
        response = asyncio.run(
            marketplace_service.protocol_fence(
                f"badvc_{'1' * 32}",
                f"baact_{'2' * 32}",
                {
                    "protocol_hash": PROTOCOL_HASH,
                    "lease_capability": "core-only-lease",
                    "envelope_digest": "3" * 64,
                    "receipt_revision": 1,
                    "challenge": f"bachal_{'A' * 43}",
                    "signature": "B" * 86,
                },
            )
        )
    finally:
        marketplace_service._invoke = original_invoke
        marketplace_service.password_manager.get_service_password = original_get_secret
        marketplace_service.marketplace_protocol_transport.request = original_request
    check(
        extension_calls == [("auth/protocol-ready", "POST", None)],
        "extension receives only the auth readiness handshake",
    )
    check(
        core_calls[0][2] == "core-only-access"
        and core_calls[0][3]["lease_capability"] == "core-only-lease"
        and core_calls[0][4] is True,
        "core alone receives mutation credentials and signed body",
    )
    check(
        response == {"terminal_capability": "core-only-terminal"},
        "terminal capability returns directly to core",
    )


def test_protocol_transport_binds_origin_and_strips_signature_fields() -> None:
    transport = marketplace_service.marketplace_protocol_transport
    original_origin = os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL")
    original_build_opener = transport.urllib.request.build_opener
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://marketplace.test/api/marketplace/protocol/v1/test"

        def read(self, _limit):
            return b'{"ok":true}'

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    try:
        os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = (
            "https://marketplace.test/api/marketplace"
        )
        transport.urllib.request.build_opener = lambda *_handlers: Opener()
        response = transport.request(
            "POST",
            "/protocol/v1/test",
            access_token="core-token",
            body={
                "protocol_hash": PROTOCOL_HASH,
                "challenge": f"bachal_{'A' * 43}",
                "signature": "B" * 86,
            },
            signed=True,
        )
        request_value = captured["request"]
        headers = {
            key.casefold(): value for key, value in request_value.headers.items()
        }
        encoded = json.loads(request_value.data)
        check(
            response == {"ok": True}, "core protocol transport accepts object response"
        )
        check(
            set(encoded) == {"protocol_hash"},
            "device signature fields leave the JSON body",
        )
        check(
            headers["x-ba-device-challenge"] == f"bachal_{'A' * 43}"
            and headers["x-ba-device-signature"] == "B" * 86,
            "core alone maps device proof into authentication headers",
        )
        for invalid in (
            "http://marketplace.test/api/marketplace",
            "https://user@marketplace.test/api/marketplace",
            "https://marketplace.test/api/../marketplace",
            "https://marketplace.test/api/marketplace?target=other",
        ):
            os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = invalid
            try:
                transport.origin()
            except HTTPException:
                continue
            raise AssertionError(f"accepted invalid Marketplace origin: {invalid}")
    finally:
        transport.urllib.request.build_opener = original_build_opener
        if original_origin is None:
            os.environ.pop("BETTER_AGENT_MARKETPLACE_BASE_URL", None)
        else:
            os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = original_origin


def test_marketplace_auth_service_is_home_scoped() -> None:
    original_home = os.environ.get("BETTER_AGENT_HOME")
    try:
        shared_tail = "/same-marketplace-home-tail-" + "x" * 80
        os.environ["BETTER_AGENT_HOME"] = "/tmp/first-root" + shared_tail
        first = marketplace_auth.service_name()
        os.environ["BETTER_AGENT_HOME"] = "/tmp/second-root" + shared_tail
        second = marketplace_auth.service_name()
    finally:
        if original_home is None:
            os.environ.pop("BETTER_AGENT_HOME", None)
        else:
            os.environ["BETTER_AGENT_HOME"] = original_home
    check(first != second, "Marketplace OAuth credentials are isolated by home")


if __name__ == "__main__":
    test_protocol_artifact_is_canonical()
    test_device_signer_enforces_exact_operation_shape()
    test_store_is_durable_private_and_fail_closed()
    test_action_envelope_excludes_coordinates_and_snapshot_excludes_secrets()
    test_fence_commit_is_recoverable_after_response_loss()
    test_rest_ws_contract_and_extension_capability_boundary()
    test_protocol_mutation_transport_bypasses_extension_backend()
    test_protocol_transport_binds_origin_and_strips_signature_fields()
    test_marketplace_auth_service_is_home_scoped()

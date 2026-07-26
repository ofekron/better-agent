#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import base64
import hashlib
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
import extension_store  # noqa: E402
import global_events  # noqa: E402
import marketplace_auth  # noqa: E402
import marketplace_bridge_api  # noqa: E402
import marketplace_service  # noqa: E402
import runtime_operations  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
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
    check(
        hashlib.sha256(raw).hexdigest() == PROTOCOL_HASH,
        "protocol compatibility hash is the exact artifact sha256",
    )
    check(
        "ack" in PROTOCOL["http"]
        and "reject" not in PROTOCOL["http"]
        and "terminal_ack" not in PROTOCOL["http"],
        "one signed acknowledgement operation owns rejection and terminal results",
    )
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
    try:
        signer.sign_device_request(
            identity,
            "pair_reject",
            {},
            {"pair_token": "secret", "protocol_hash": PROTOCOL_HASH},
            f"bachal_{'B' * 43}",
            "https://marketplace.example",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("signer accepted a non-device operation")


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


def test_store_rejects_secret_account_substitution() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = MarketplaceStateStore(Path(directory) / "state.json")
        action = _install_action()
        bridge = MarketplaceBridge(store=store)
        bridge._extension_state = lambda _extension_id: {"exists": False}
        intent = bridge._validate_action(action)
        state = empty_state()
        state["intents"][action["action_id"]] = intent
        state["receipts"][action["action_id"]] = {
            "action_id": action["action_id"],
            "intent_id": action["action_id"],
            "device_id": f"badvc_{'1' * 32}",
            "device_epoch": 1,
            "envelope_digest": intent["envelope_digest"],
            "receipt_revision": 1,
            "action": "install",
            "extension_id": "ofek.test",
            "expected_version": "1.2.3",
            "approved_target": intent["approved_target"],
            "verified_catalog": {"catalog": {}, "signature": ""},
            "precondition": {"exists": False},
            "desired": intent["desired"],
            "phase": "fenced",
            "terminal_capability_account": (
                f"marketplace-terminal-capability:{action['action_id']}"
            ),
            "terminal_result": None,
            "ack_status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reconcile_deadline": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
        }
        store.write(state)
        substituted = store.read()
        substituted["receipts"][action["action_id"]][
            "terminal_capability_account"
        ] = "unrelated-keychain-account"
        try:
            store.write(substituted)
        except MarketplaceStateError:
            pass
        else:
            raise AssertionError("state store accepted a substituted secret account")


def test_verified_catalog_proof_survives_expiry() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes_raw()
    ).decode()
    extension_id = "ofek.test"
    publisher_fingerprint = "4" * 64
    permission_hash = "5" * 64
    catalog = {
        "key_id": "default-v1",
        "sequence": 7,
        "issued_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        "extensions": {
            extension_id: {
                "extension_id": extension_id,
                "version": "1.2.3",
                "publisher_fingerprint": publisher_fingerprint,
                "permission_hash": permission_hash,
            }
        },
    }
    canonical = json.dumps(
        catalog,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    snapshot_id = f"default-v1:7:{hashlib.sha256(canonical).hexdigest()}"
    signature = base64.b64encode(private_key.sign(canonical)).decode()
    original_key = os.environ.get("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY")
    os.environ["BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"] = public_key
    try:
        verified = extension_store.verify_marketplace_catalog_snapshot(
            catalog=catalog,
            signature=signature,
            snapshot_id=snapshot_id,
            extension_id=extension_id,
            expected_version="1.2.3",
            publisher_fingerprint=publisher_fingerprint,
            permission_hash=permission_hash,
            minimum_sequence=0,
            allow_expired=True,
        )
    finally:
        if original_key is None:
            os.environ.pop("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY", None)
        else:
            os.environ["BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"] = original_key
    check(
        verified["metadata"]["extension_id"] == extension_id,
        "persisted signed catalog remains verifiable after approval-time expiry",
    )


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
                return f"marketplace-terminal-capability:{action_id}"

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

        async def resolve_catalog(
            _state: dict,
            _intent: dict,
        ) -> tuple[dict, str, int, dict]:
            return {}, "default-v1", 7, {"catalog": {}, "signature": ""}

        async def sign(
            _state: dict,
            _operation: str,
            _params: dict,
            body: dict,
            **_options,
        ) -> tuple[str, dict]:
            return "/fence", body

        fence_bodies: list[dict] = []
        original_fence = marketplace_service.protocol_fence

        async def fence(_device_id: str, _action_id: str, signed: dict) -> dict:
            fence_bodies.append(signed)
            if len(fence_bodies) == 1:
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
            revoked = store.read()
            revoked["device"]["paired"] = False
            revoked["device"]["revoked"] = True
            revoked["device"]["revocation_pending"] = True
            revoked["device"]["epoch"] += 1
            store.write(revoked)
            asyncio.run(bridge._reconcile_revocation())
            check(
                store.read()["device"]["revocation_pending"] is True,
                "revocation waits until the pending fence yields a terminal capability",
            )
            asyncio.run(bridge._complete_pending_fence(action["action_id"]))
            recovered = store.read()["receipts"][action["action_id"]]
            check(
                recovered["phase"] == "fenced",
                "reconciliation completes the durable pending fence",
            )
            check(
                fence_bodies
                == [
                    {
                        "protocol_hash": PROTOCOL_HASH,
                        "lease_capability": "lease-secret",
                    }
                ]
                * 2,
                "fence retry exposes no local receipt internals",
            )
        finally:
            marketplace_service.protocol_fence = original_fence


def test_signed_ack_owns_terminal_and_rejection_recovery() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = MarketplaceStateStore(Path(directory) / "state.json")
        action = _install_action()
        bridge = MarketplaceBridge(store=store)
        bridge._extension_state = lambda _extension_id: {"exists": False}
        intent = bridge._validate_action(action)
        device_id = f"badvc_{'1' * 32}"
        state = empty_state()
        state["device"] = {
            "device_id": device_id,
            "public_key": "A" * 43,
            "label": "Test device",
            "paired": True,
            "revoked": False,
            "revocation_pending": False,
            "epoch": 1,
            "server_origin": "https://ofek-dev.com/api/marketplace",
        }
        state["connection_state"] = "connected"
        intent["status"] = "rejected"
        intent["rejection_pending"] = True
        state["intents"][action["action_id"]] = intent
        store.write(state)

        signed_operations: list[tuple[str, dict, bool]] = []

        async def sign(
            _state: dict,
            operation: str,
            _params: dict,
            body: dict,
            *,
            allow_revoked: bool = False,
        ) -> tuple[str, dict]:
            signed_operations.append((operation, body, allow_revoked))
            return "/ack", body

        acknowledgements: list[dict] = []
        terminal_attempts = 0
        original_ack = marketplace_service.protocol_ack

        async def ack(_device_id: str, _action_id: str, body: dict) -> dict:
            nonlocal terminal_attempts
            acknowledgements.append(body)
            if body["outcome"] == "succeeded":
                terminal_attempts += 1
                if terminal_attempts == 1:
                    raise RuntimeError("terminal acknowledgement response lost")
            return {
                "outcome": body["outcome"],
                "result_code": body["result_code"],
            }

        bridge._signed_operation = sign
        marketplace_service.protocol_ack = ack
        try:
            revoked = store.read()
            revoked["device"]["paired"] = False
            revoked["device"]["revoked"] = True
            revoked["device"]["revocation_pending"] = True
            revoked["device"]["epoch"] += 1
            store.write(revoked)
            asyncio.run(bridge._reconcile_revocation())
            check(
                store.read()["device"]["revocation_pending"] is True,
                "revocation waits for a durable rejection acknowledgement",
            )
            asyncio.run(bridge._flush_rejection(action["action_id"]))

            class Identity:
                def get_secret(self, _account: str) -> str:
                    return "terminal-secret"

                def delete_secret(self, _account: str) -> None:
                    return None

            bridge._identity = Identity()
            pending = store.read()
            pending_intent = pending["intents"][action["action_id"]]
            pending_intent["status"] = "committing"
            pending["receipts"][action["action_id"]] = {
                "action_id": action["action_id"],
                "intent_id": action["action_id"],
                "device_id": device_id,
                "device_epoch": 1,
                "envelope_digest": pending_intent["envelope_digest"],
                "receipt_revision": 1,
                "action": "install",
                "extension_id": "ofek.test",
                "expected_version": "1.2.3",
                "approved_target": pending_intent["approved_target"],
                "verified_catalog": {"catalog": {}, "signature": ""},
                "precondition": {"exists": False},
                "desired": pending_intent["desired"],
                "phase": "fenced",
                "terminal_capability_account": (
                    f"marketplace-terminal-capability:{action['action_id']}"
                ),
                "terminal_result": None,
                "ack_status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "reconcile_deadline": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            }
            store.write(pending)
            try:
                asyncio.run(
                    bridge._finalize_receipt(
                        action["action_id"],
                        "succeeded",
                        "installed",
                    )
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError("lost terminal acknowledgement did not surface")
            check(
                store.read()["receipts"][action["action_id"]]["phase"] == "terminal",
                "terminal result survives a lost acknowledgement response",
            )
            asyncio.run(
                bridge._finalize_receipt(
                    action["action_id"],
                    "succeeded",
                    "installed",
                )
            )
        finally:
            marketplace_service.protocol_ack = original_ack
        check(
            acknowledgements[0]
            == {
                "protocol_hash": PROTOCOL_HASH,
                "capability": "lease-secret",
                "outcome": "rejected",
                "result_code": "user_rejected",
            },
            "rejection uses the unified signed acknowledgement body",
        )
        check(
            signed_operations
            == [
                ("ack", acknowledgements[0], True),
                ("ack", acknowledgements[1], True),
                ("ack", acknowledgements[2], True),
            ],
            "rejection and terminal retries share the signed acknowledgement path",
        )
        check(
            store.read()["intents"][action["action_id"]]["rejection_pending"] is False,
            "rejection acknowledgement clears only after server confirmation",
        )
        check(
            action["action_id"] not in store.read()["receipts"],
            "terminal receipt clears only after a confirmed acknowledgement",
        )


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
    check(
        ("POST", "/api/marketplace-bridge/pair") not in paths,
        "public browser pairing mutation is not registered",
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
        "/protocol/v1/devices/{device_id}/actions/{action_id}/ack",
        "X-BA-Device-Signature",
    ):
        check(
            forbidden not in extension_source,
            f"Marketplace extension cannot observe core mutation secret {forbidden}",
        )


def test_pair_activation_is_internal_and_exact() -> None:
    class FakeBridge:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def activate_pair(self, intent: str, version: int) -> dict:
            self.calls.append((intent, version))
            return {
                "revision": 1,
                "connection_state": "connecting",
                "paired_devices": [],
                "intents": [],
            }

    app = FastAPI()
    app.include_router(marketplace_bridge_api.router)
    client = TestClient(app)
    pair_intent = "A" * 43
    body = {"intent": pair_intent, "version": 1}
    fake_bridge = FakeBridge()
    original_bridge = marketplace_bridge_api.marketplace_bridge.bridge
    original_internal_token = marketplace_bridge_api._internal_token
    try:
        marketplace_bridge_api.marketplace_bridge.bridge = fake_bridge
        marketplace_bridge_api._internal_token = lambda: "desktop-token"
        check(
            client.post("/api/marketplace-bridge/pair", json=body).status_code
            == 404,
            "public browser pairing endpoint is denied",
        )
        check(
            client.post(
                "/api/internal/marketplace-bridge/pair",
                json=body,
            ).status_code
            == 422,
            "internal pairing requires the desktop token header",
        )
        check(
            client.post(
                "/api/internal/marketplace-bridge/pair",
                json=body,
                headers={"X-Internal-Token": "wrong"},
            ).status_code
            == 403,
            "internal pairing rejects the wrong desktop token",
        )
        check(
            client.post(
                "/api/internal/marketplace-bridge/pair",
                json={**body, "unexpected": True},
                headers={"X-Internal-Token": "desktop-token"},
            ).status_code
            == 422,
            "internal pairing rejects extra request fields",
        )
        response = client.post(
            "/api/internal/marketplace-bridge/pair",
            json=body,
            headers={"X-Internal-Token": "desktop-token"},
        )
        check(
            response.status_code == 200
            and fake_bridge.calls == [(pair_intent, 1)],
            "valid desktop pairing reaches the canonical bridge once",
        )
        check(
            pair_intent not in response.text and "desktop-token" not in response.text,
            "pairing response exposes neither the intent secret nor internal token",
        )
    finally:
        client.close()
        marketplace_bridge_api.marketplace_bridge.bridge = original_bridge
        marketplace_bridge_api._internal_token = original_internal_token


def test_protocol_mutation_transport_bypasses_extension_backend() -> None:
    extension_calls: list[tuple[str, str, dict | None]] = []
    core_calls: list[tuple[str, dict, str, dict]] = []
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
        operation: str,
        path_values: dict,
        *,
        access_token: str,
        body: dict,
    ) -> dict:
        core_calls.append((operation, path_values, access_token, body))
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
        and core_calls[0][0] == "fence",
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
            return (
                "https://marketplace.test/api/marketplace/protocol/v1/devices/"
                f"badvc_{'1' * 32}/actions/lease"
            )

        def read(self, _limit):
            return b'{"action":null}'

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
            "lease",
            {"device_id": f"badvc_{'1' * 32}"},
            access_token="core-token",
            body={
                "protocol_hash": PROTOCOL_HASH,
                "wait_seconds": 30,
                "challenge": f"bachal_{'A' * 43}",
                "signature": "B" * 86,
            },
        )
        request_value = captured["request"]
        headers = {
            key.casefold(): value for key, value in request_value.headers.items()
        }
        encoded = json.loads(request_value.data)
        check(
            response == {"action": None},
            "core protocol transport accepts the exact typed response",
        )
        check(
            set(encoded) == {"protocol_hash", "wait_seconds"},
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


def test_protocol_transport_rejects_invalid_response_value_types() -> None:
    transport = marketplace_service.marketplace_protocol_transport
    original_request_sync = transport._request_sync
    try:
        for invalid_revision in ("not-an-integer", 2):
            transport._request_sync = (
                lambda *_args, **_kwargs: {"revision": invalid_revision}
            )
            try:
                transport.request(
                    "projection",
                    {"device_id": f"badvc_{'1' * 32}"},
                    access_token="core-token",
                    body={
                        "protocol_hash": PROTOCOL_HASH,
                        "revision": 1,
                        "extensions": [],
                        "challenge": f"bachal_{'A' * 43}",
                        "signature": "B" * 86,
                    },
                )
            except HTTPException as exc:
                check(
                    exc.status_code == 502,
                    "core protocol transport rejects invalid projection acknowledgements",
                )
            else:
                raise AssertionError(
                    "core protocol transport accepted an invalid projection revision"
                )
    finally:
        transport._request_sync = original_request_sync


def test_pending_pair_recovery_schedules_retry() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = MarketplaceStateStore(Path(directory) / "state.json")
        pair_id = f"pair_{'1' * 32}"
        state = empty_state()
        state["pending_pairs"][pair_id] = {
            "token_account": f"marketplace-pair-token:{pair_id}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        state["intents"][pair_id] = {
            "intent_id": pair_id,
            "action": "pair",
            "status": "pending",
            "device_label": "Test device",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        store.write(state)
        bridge = MarketplaceBridge(store=store)
        check(
            asyncio.run(bridge._needs_reconciliation()),
            "pending pair recovery schedules another retry",
        )


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
    check(
        first != second and len(first.rsplit("-", 1)[-1]) == 64,
        "Marketplace OAuth credentials use a full-digest home namespace",
    )


if __name__ == "__main__":
    test_protocol_artifact_is_canonical()
    test_device_signer_enforces_exact_operation_shape()
    test_store_is_durable_private_and_fail_closed()
    test_store_rejects_secret_account_substitution()
    test_verified_catalog_proof_survives_expiry()
    test_action_envelope_excludes_coordinates_and_snapshot_excludes_secrets()
    test_fence_commit_is_recoverable_after_response_loss()
    test_signed_ack_owns_terminal_and_rejection_recovery()
    test_rest_ws_contract_and_extension_capability_boundary()
    test_pair_activation_is_internal_and_exact()
    test_protocol_mutation_transport_bypasses_extension_backend()
    test_protocol_transport_binds_origin_and_strips_signature_fields()
    test_protocol_transport_rejects_invalid_response_value_types()
    test_pending_pair_recovery_schedules_retry()
    test_marketplace_auth_service_is_home_scoped()

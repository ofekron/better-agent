from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


async def run() -> None:
    root = Path(tempfile.mkdtemp(prefix="better-agent-marketplace-bridge-"))
    os.environ["BETTER_AGENT_HOME"] = str(root)
    os.environ["BETTER_AGENT_TEST_MODE"] = "1"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        import marketplace_bridge
        import marketplace_device_identity

        credentials: dict[tuple[str, str], str] = {}
        marketplace_device_identity.oskeychain.get = lambda service, account: credentials.get(
            (service, account)
        )
        marketplace_device_identity.oskeychain.store = (
            lambda service, account, value: credentials.__setitem__(
                (service, account),
                value,
            )
        )
        marketplace_device_identity.oskeychain.delete = (
            lambda service, account: credentials.pop((service, account), None)
        )

        pair_token = _b64url(bytes(range(32)))
        origin = "https://ofek-dev.com/api/marketplace"
        device: dict[str, str] = {}
        acknowledgements: list[dict] = []

        async def pair_context(token: str) -> dict:
            assert token == pair_token
            return {
                "site_label": "Singular Marketplace",
                "account_label": "marketplace@example.test",
                "server_origin": origin,
                "protocol_hash": marketplace_bridge.PROTOCOL_HASH,
            }

        async def pair(body: dict) -> dict:
            public = Ed25519PublicKey.from_public_bytes(_decode(body["public_key"]))
            transcript = "\n".join(
                [
                    "better-agent-marketplace",
                    "protocol-v1",
                    marketplace_bridge.PROTOCOL_HASH,
                    "pair",
                    body["pair_token"],
                    body["device_id"],
                    body["public_key"],
                ]
            ).encode()
            public.verify(_decode(body["pop_signature"]), transcript)
            device.update(
                device_id=body["device_id"],
                public_key=body["public_key"],
            )
            return {
                "protocol_hash": marketplace_bridge.PROTOCOL_HASH,
                "server_origin": origin,
                "device_id": body["device_id"],
            }

        async def challenges(device_id: str) -> dict:
            assert device_id == device["device_id"]
            return {
                "challenges": ["bachal_" + "B" * 43],
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=1)
                ).isoformat(),
            }

        leased = False

        def verify_device(
            method: str,
            path: str,
            signed: dict,
            business: dict,
        ) -> None:
            public = Ed25519PublicKey.from_public_bytes(_decode(device["public_key"]))
            encoded = json.dumps(
                business,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            transcript = "\n".join(
                [
                    "better-agent-marketplace",
                    "protocol-v1",
                    marketplace_bridge.PROTOCOL_HASH,
                    origin,
                    method,
                    path,
                    device["device_id"],
                    signed["challenge"],
                    hashlib.sha256(encoded).hexdigest(),
                ]
            ).encode()
            public.verify(_decode(signed["signature"]), transcript)

        async def lease(device_id: str, signed: dict) -> dict:
            nonlocal leased
            path = f"/protocol/v1/devices/{device_id}/actions/lease"
            business = {
                "protocol_hash": marketplace_bridge.PROTOCOL_HASH,
                "wait_seconds": 30,
            }
            verify_device("POST", path, signed, business)
            if leased:
                return {"action": None}
            leased = True
            return {
                "action": {
                    "action_id": "baact_" + "b" * 32,
                    "action_type": "install",
                    "extension_id": "ofek-dev.adv",
                    "expected_version": "1.2.3",
                    "snapshot_id": "default-v1:1:" + "a" * 64,
                    "publisher_fingerprint": "b" * 64,
                    "permission_hash": "c" * 64,
                    "lease_capability": "lease-capability",
                    "extension_name": "Adversarial Review",
                    "publisher_name": "Singular Labs",
                    "permission_delta": ["filesystem"],
                }
            }

        async def reject(device_id: str, action_id: str, signed: dict) -> dict:
            business = {
                key: signed[key]
                for key in (
                    "protocol_hash",
                    "lease_capability",
                    "envelope_digest",
                )
            }
            path = f"/protocol/v1/devices/{device_id}/actions/{action_id}/reject"
            verify_device("POST", path, signed, business)
            acknowledgements.append(business)
            return {"outcome": "rejected", "result_code": "user_rejected"}

        marketplace_bridge.marketplace_service.protocol_pair_context = pair_context
        marketplace_bridge.marketplace_service.protocol_pair = pair
        marketplace_bridge.marketplace_service.protocol_challenges = challenges
        marketplace_bridge.marketplace_service.protocol_lease = lease
        marketplace_bridge.marketplace_service.protocol_reject = reject

        service = marketplace_bridge.MarketplaceBridge()
        await service.activate_pair(pair_token, 1)
        pair_intent = service.snapshot()["intents"][0]
        assert pair_intent["status"] == "awaiting_confirmation"
        assert pair_intent["account_label"] == "marketplace@example.test"
        await service.approve(pair_intent["intent_id"])
        paired = service.snapshot()
        assert paired["connection_state"] == "connected"
        assert len(paired["paired_devices"]) == 1

        await service._lease_next()
        action = service.snapshot()["intents"][-1]
        assert action["intent_id"] == "baact_" + "b" * 32
        assert action["status"] == "awaiting_confirmation"
        await service.reject(action["intent_id"])
        assert service.snapshot()["intents"][-1]["status"] == "rejected"
        assert acknowledgements == [
            {
                "protocol_hash": marketplace_bridge.PROTOCOL_HASH,
                "lease_capability": "lease-capability",
                "envelope_digest": service._store.read()["intents"][
                    "baact_" + "b" * 32
                ]["envelope_digest"],
            }
        ]

        verified_metadata = {
            "extension_id": "ofek-dev.adv",
            "version": "1.2.3",
            "artifact_sha256": "d" * 64,
            "signature": "artifact-signature",
        }

        async def resolve_catalog(state: dict, intent: dict) -> tuple[dict, str, int]:
            return verified_metadata, "default-v1", 1

        async def action_metadata(action_id: str) -> dict:
            assert action_id == "baact_" + "b" * 32
            return {
                **verified_metadata,
                "signature_alg": "ed25519",
                "artifact_url": "https://ofek-dev.com/api/marketplace/action-artifact",
            }

        service._resolve_catalog = resolve_catalog
        marketplace_bridge.marketplace_service.protocol_action_metadata = action_metadata
        state = service._store.read()
        intent = state["intents"]["baact_" + "b" * 32]
        resolved = await service._resolve_action_metadata(state, intent)
        assert resolved["artifact_url"].startswith("https://")

        async def mismatched_metadata(action_id: str) -> dict:
            return {**await action_metadata(action_id), "version": "9.9.9"}

        marketplace_bridge.marketplace_service.protocol_action_metadata = (
            mismatched_metadata
        )
        try:
            await service._resolve_action_metadata(state, intent)
        except marketplace_bridge.MarketplaceBridgeError:
            pass
        else:
            raise AssertionError("mismatched action metadata was accepted")

        persisted = (
            root / "marketplace" / "intent-receipts-v1.json"
        ).read_text(encoding="utf-8")
        assert pair_token not in persisted
        assert "private_key" not in persisted
        assert "marketplace-device-ed25519-v1" not in persisted
        print("marketplace bridge integration checks passed")
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    asyncio.run(run())

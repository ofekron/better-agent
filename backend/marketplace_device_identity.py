from __future__ import annotations

import base64
import platform
import re
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import oskeychain
from keychain_names import home_suffix
from marketplace_protocol import PROTOCOL, PROTOCOL_HASH, canonical_hash, require_identifier

_PRIVATE_KEY_ACCOUNT = "marketplace-device-ed25519-v1"
_PAIR_TOKEN_PREFIX = "marketplace-pair-token:"
_LEASE_CAPABILITY_PREFIX = "marketplace-lease-capability:"
_TERMINAL_CAPABILITY_PREFIX = "marketplace-terminal-capability:"
_SIGNED_OPERATIONS = frozenset({"lease", "fence", "ack", "projection", "revoke"})
_PATH_PARAMETER_PATTERN = re.compile(r"\{([a-z_]+)\}")
_PATH_IDENTIFIER_KINDS = {
    "action_id": "action",
    "device_id": "device",
}


class MarketplaceIdentityError(ValueError):
    pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if "=" in value:
        raise MarketplaceIdentityError("invalid base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise MarketplaceIdentityError("invalid base64url") from exc
    if _b64url(decoded) != value:
        raise MarketplaceIdentityError("invalid base64url")
    return decoded


def _service_name() -> str:
    suffix = home_suffix()
    return "better-agent-marketplace-v1" + (f"-{suffix}" if suffix else "")


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    public_key: str
    label: str
    private_key: Ed25519PrivateKey


class MarketplaceDeviceIdentity:
    def create_or_load(self, device: dict | None) -> tuple[dict, DeviceIdentity]:
        stored = oskeychain.get(_service_name(), _PRIVATE_KEY_ACCOUNT)
        if device is None and stored is None:
            private_key = Ed25519PrivateKey.generate()
            private_raw = private_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            public_key = _b64url(
                private_key.public_key().public_bytes(
                    serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw,
                )
            )
            label = platform.node().strip()
            if not label:
                raise MarketplaceIdentityError("Marketplace device label is unavailable")
            device = {
                "device_id": f"badvc_{secrets.token_hex(16)}",
                "public_key": public_key,
                "label": label,
                "paired": False,
                "revoked": False,
                "revocation_pending": False,
                "epoch": 0,
                "server_origin": "",
            }
            oskeychain.store(
                _service_name(),
                _PRIVATE_KEY_ACCOUNT,
                _b64url(private_raw),
            )
            return device, DeviceIdentity(
                device_id=device["device_id"],
                public_key=public_key,
                label=label,
                private_key=private_key,
            )
        if not isinstance(device, dict) or stored is None:
            raise MarketplaceIdentityError("Marketplace device identity is incomplete")
        private_raw = _b64url_decode(stored.strip())
        if len(private_raw) != 32:
            raise MarketplaceIdentityError("Marketplace device private key is invalid")
        private_key = Ed25519PrivateKey.from_private_bytes(private_raw)
        public_key = _b64url(
            private_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )
        if not secrets.compare_digest(public_key, str(device.get("public_key") or "")):
            raise MarketplaceIdentityError("Marketplace device identity does not match")
        return device, DeviceIdentity(
            device_id=require_identifier("device", device["device_id"]),
            public_key=public_key,
            label=str(device["label"]),
            private_key=private_key,
        )

    def delete_private_key(self) -> None:
        oskeychain.delete(_service_name(), _PRIVATE_KEY_ACCOUNT)

    def store_pair_token(self, intent_id: str, token: str) -> str:
        account = f"{_PAIR_TOKEN_PREFIX}{require_identifier('pair_intent', intent_id)}"
        oskeychain.store(_service_name(), account, token)
        return account

    def get_secret(self, account: str) -> str | None:
        return oskeychain.get(_service_name(), account)

    def delete_secret(self, account: str) -> None:
        oskeychain.delete(_service_name(), account)

    def lease_capability_account(self, action_id: str) -> str:
        return f"{_LEASE_CAPABILITY_PREFIX}{require_identifier('action', action_id)}"

    def store_lease_capability(self, action_id: str, capability: str) -> str:
        account = self.lease_capability_account(action_id)
        oskeychain.store(_service_name(), account, capability)
        return account

    def store_terminal_capability(self, action_id: str, capability: str) -> str:
        account = f"{_TERMINAL_CAPABILITY_PREFIX}{require_identifier('action', action_id)}"
        oskeychain.store(_service_name(), account, capability)
        return account

    def sign_pair(self, identity: DeviceIdentity, pair_token: str) -> str:
        transcript = "\n".join(
            [
                "better-agent-marketplace",
                "protocol-v1",
                PROTOCOL_HASH,
                "pair",
                pair_token,
                identity.device_id,
                identity.public_key,
            ]
        ).encode("utf-8")
        return _b64url(identity.private_key.sign(transcript))

    def sign_device_request(
        self,
        identity: DeviceIdentity,
        operation: str,
        path_values: dict[str, str],
        body: dict[str, Any],
        challenge: str,
        server_origin: str,
    ) -> tuple[str, dict[str, Any]]:
        spec = PROTOCOL["http"].get(operation)
        if not isinstance(spec, dict) or operation not in _SIGNED_OPERATIONS:
            raise MarketplaceIdentityError("Marketplace signing operation is not allowed")
        path = str(spec["path"])
        parameters = set(_PATH_PARAMETER_PATTERN.findall(path))
        if set(path_values) != parameters:
            raise MarketplaceIdentityError("Marketplace signing path is incomplete")
        for name, value in path_values.items():
            kind = _PATH_IDENTIFIER_KINDS.get(name)
            if kind is None:
                # Defensive: every shipped signed-operation path parameter is
                # registered above, so this fires only if the protocol artifact
                # introduces an unregistered path parameter kind.
                raise MarketplaceIdentityError(
                    "Marketplace signing path is unsupported"
                )  # pragma: no cover
            path = path.replace(
                f"{{{name}}}",
                require_identifier(kind, value),
            )
        request_fields = set(spec["request"]) - {"challenge", "signature"}
        if set(body) != request_fields:
            raise MarketplaceIdentityError("Marketplace signed request has an invalid shape")
        if body.get("protocol_hash") != PROTOCOL_HASH:
            raise MarketplaceIdentityError("Marketplace protocol hash is missing")
        require_identifier("challenge", challenge)
        transcript = "\n".join(
            [
                "better-agent-marketplace",
                "protocol-v1",
                PROTOCOL_HASH,
                server_origin,
                str(spec["method"]),
                path,
                identity.device_id,
                challenge,
                canonical_hash(body),
            ]
        ).encode("utf-8")
        return path, {
            **body,
            "challenge": challenge,
            "signature": _b64url(identity.private_key.sign(transcript)),
        }

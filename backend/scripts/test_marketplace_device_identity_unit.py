"""Dedicated unit owner for marketplace_device_identity.

This module owns the Marketplace device credential: an Ed25519 keypair whose
private half lives in the OS keychain, the signed pair/fence/ack/projection/
revoke request builder, and the capability secret accounts. Every branch here
is a real security property — key custody, canonical-encoding rejection, and
the exact transcript the server verifies — so the assertions prove behavior,
not line execution.

The OS keychain is swapped for a dict-backed stub; no real credential store is
touched. The conftest already engages an isolated per-module ba_home().
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import oskeychain  # noqa: E402
import pytest  # noqa: E402

import marketplace_device_identity as mdi  # noqa: E402
from marketplace_device_identity import (  # noqa: E402
    DeviceIdentity,
    MarketplaceDeviceIdentity,
    MarketplaceIdentityError,
)
from marketplace_protocol import PROTOCOL, PROTOCOL_HASH, canonical_hash  # noqa: E402


class _Keychain:
    """Dict-backed stand-in for the OS keychain with call logs."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.stores: list[tuple[str, str, str]] = []
        self.deletes: list[tuple[str, str]] = []

    def install(self) -> None:
        self._real = (
            oskeychain.get,
            oskeychain.store,
            oskeychain.delete,
        )
        oskeychain.get = lambda service, account: self.values.get((service, account))

        def _store(service, account, value):
            self.values[(service, account)] = value
            self.stores.append((service, account, value))

        def _delete(service, account):
            self.values.pop((service, account), None)
            self.deletes.append((service, account))

        oskeychain.store = _store
        oskeychain.delete = _delete

    def restore(self) -> None:
        oskeychain.get, oskeychain.store, oskeychain.delete = self._real


@pytest.fixture(autouse=True)
def _keychain():
    """Fresh dict-backed keychain for every test; restored on teardown."""
    stub = _Keychain()
    stub.install()
    yield stub
    stub.restore()


# --- helpers -------------------------------------------------------------

_VALID_IDENTIFIER = {
    "device": f"badvc_{'1' * 32}",
    "action": f"baact_{'2' * 32}",
    "pair_intent": f"pair_{'1' * 32}",
}
_CHALLENGE = f"bachal_{'A' * 43}"


def _fresh_keypair():
    """Return (private_key, public_key_b64, private_raw_b64) for a new identity."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    private_raw = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return private_key, mdi._b64url(public_raw), mdi._b64url(private_raw)


def _identity():
    private_key, public_key, _ = _fresh_keypair()
    return DeviceIdentity(
        device_id=_VALID_IDENTIFIER["device"],
        public_key=public_key,
        label="test-device",
        private_key=private_key,
    )


def _seed_private_key(keychain, private_b64):
    keychain.values[(mdi._service_name(), mdi._PRIVATE_KEY_ACCOUNT)] = private_b64


def _verify(signature_b64, transcript, public_key_b64):
    """Raise (InvalidSignature) unless the signature covers the transcript."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub_bytes = mdi._b64url_decode(public_key_b64)
    Ed25519PublicKey.from_public_bytes(pub_bytes).verify(
        mdi._b64url_decode(signature_b64), transcript
    )


def _path_values(operation):
    spec = PROTOCOL["http"][operation]
    params = set(mdi._PATH_PARAMETER_PATTERN.findall(spec["path"]))
    return {
        name: _VALID_IDENTIFIER[mdi._PATH_IDENTIFIER_KINDS[name]] for name in params
    }


def _valid_body(operation):
    spec = PROTOCOL["http"][operation]
    fields = set(spec["request"]) - {"challenge", "signature"}
    body: dict = {}
    for field in fields:
        if field == "protocol_hash":
            body[field] = PROTOCOL_HASH
        elif field == "extensions":
            body[field] = []
        elif field == "revision":
            body[field] = 1
        elif field == "wait_seconds":
            body[field] = 30
        else:
            body[field] = "capability-secret"
    return body


# --- _b64url / _b64url_decode -------------------------------------------

def test_b64url_roundtrips_arbitrary_bytes():
    payload = bytes(range(0, 40))
    assert mdi._b64url_decode(mdi._b64url(payload)) == payload


def test_b64url_decode_rejects_explicit_padding():
    with pytest.raises(MarketplaceIdentityError):
        mdi._b64url_decode("AB==")


def test_b64url_decode_rejects_non_canonical_encoding():
    # "AB" decodes to a single zero byte whose canonical form is "AA".
    with pytest.raises(MarketplaceIdentityError):
        mdi._b64url_decode("AB")


def test_b64url_decode_rejects_malformed_encoding():
    # A single char is an impossible base64 length: the padded form "A==="
    # raises during decode (caught as ValueError), before the canonical check.
    with pytest.raises(MarketplaceIdentityError):
        mdi._b64url_decode("A")


# --- create_or_load -----------------------------------------------------

def test_create_or_load_mints_new_identity_when_absent(_keychain):
    import platform

    device, identity = MarketplaceDeviceIdentity().create_or_load(None)

    assert device["device_id"].startswith("badvc_")
    assert device["public_key"] == identity.public_key
    assert device["label"] == platform.node().strip()
    assert device["paired"] is False and device["epoch"] == 0
    # Private key is durably stored under the namespaced keychain account.
    assert _keychain.stores
    assert _keychain.stores[0][0] == mdi._service_name()
    assert _keychain.stores[0][1] == mdi._PRIVATE_KEY_ACCOUNT
    assert _keychain.values[(mdi._service_name(), mdi._PRIVATE_KEY_ACCOUNT)]


def test_create_or_load_is_stable_and_round_trips_the_key(_keychain):
    signer = MarketplaceDeviceIdentity()
    created, _ = signer.create_or_load(None)
    stores_after_create = len(_keychain.stores)

    reloaded_device, reloaded = signer.create_or_load(created)

    assert reloaded_device is created
    assert reloaded.device_id == created["device_id"]
    assert reloaded.public_key == created["public_key"]
    # The reloaded private key still verifies signatures (round-trips the key).
    transcript = b"probe"
    signature = reloaded.private_key.sign(transcript)
    reloaded.private_key.public_key().verify(signature, transcript)
    # The reload path serves the cached key without re-storing.
    assert len(_keychain.stores) == stores_after_create


def test_create_or_load_rejects_empty_device_label(monkeypatch):
    monkeypatch.setattr(mdi.platform, "node", lambda: "   ")
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().create_or_load(None)


def test_create_or_load_rejects_non_dict_device_when_key_absent():
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().create_or_load("not-a-dict")


def test_create_or_load_rejects_dict_device_without_key():
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().create_or_load(
            {"device_id": _VALID_IDENTIFIER["device"], "public_key": "x", "label": "t"}
        )


def test_create_or_load_rejects_short_private_key(_keychain):
    _private_key, public_key, _ = _fresh_keypair()
    _seed_private_key(_keychain, mdi._b64url(b"\x00" * 31))
    device = {
        "device_id": _VALID_IDENTIFIER["device"],
        "public_key": public_key,
        "label": "t",
    }
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().create_or_load(device)


def test_create_or_load_rejects_key_mismatch(_keychain):
    _a, _device_public, device_private_b64 = _fresh_keypair()
    _b, other_public, _ = _fresh_keypair()
    _seed_private_key(_keychain, device_private_b64)
    device = {
        "device_id": _VALID_IDENTIFIER["device"],
        "public_key": other_public,
        "label": "t",
    }
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().create_or_load(device)


def test_create_or_load_rejects_invalid_device_id_on_load(_keychain):
    _private_key, public_key, private_b64 = _fresh_keypair()
    _seed_private_key(_keychain, private_b64)
    device = {
        "device_id": "not-a-valid-device-id",
        "public_key": public_key,
        "label": "t",
    }
    with pytest.raises(ValueError):
        MarketplaceDeviceIdentity().create_or_load(device)


# --- keychain secret accounts -------------------------------------------

def test_delete_private_key_removes_account(_keychain):
    _private_key, _pub, private_b64 = _fresh_keypair()
    _seed_private_key(_keychain, private_b64)

    MarketplaceDeviceIdentity().delete_private_key()

    assert (mdi._service_name(), mdi._PRIVATE_KEY_ACCOUNT) in _keychain.deletes


def test_store_pair_token_persists_and_round_trips():
    account = MarketplaceDeviceIdentity().store_pair_token(
        _VALID_IDENTIFIER["pair_intent"], "pair-secret"
    )
    assert account == f"{mdi._PAIR_TOKEN_PREFIX}{_VALID_IDENTIFIER['pair_intent']}"
    assert MarketplaceDeviceIdentity().get_secret(account) == "pair-secret"


def test_store_pair_token_rejects_invalid_intent():
    with pytest.raises(ValueError):
        MarketplaceDeviceIdentity().store_pair_token("bad-intent", "x")


def test_get_secret_returns_none_when_absent():
    assert MarketplaceDeviceIdentity().get_secret("missing") is None


def test_delete_secret_removes_account():
    account = MarketplaceDeviceIdentity().store_pair_token(
        _VALID_IDENTIFIER["pair_intent"], "pair-secret"
    )
    MarketplaceDeviceIdentity().delete_secret(account)
    assert MarketplaceDeviceIdentity().get_secret(account) is None


def test_lease_capability_account_is_deterministic_and_validated():
    signer = MarketplaceDeviceIdentity()
    assert (
        signer.lease_capability_account(_VALID_IDENTIFIER["action"])
        == f"{mdi._LEASE_CAPABILITY_PREFIX}{_VALID_IDENTIFIER['action']}"
    )
    with pytest.raises(ValueError):
        signer.lease_capability_account("bad-action")


def test_store_lease_capability_round_trips():
    account = MarketplaceDeviceIdentity().store_lease_capability(
        _VALID_IDENTIFIER["action"], "lease-cap"
    )
    assert account == f"{mdi._LEASE_CAPABILITY_PREFIX}{_VALID_IDENTIFIER['action']}"
    assert MarketplaceDeviceIdentity().get_secret(account) == "lease-cap"


def test_store_terminal_capability_round_trips():
    account = MarketplaceDeviceIdentity().store_terminal_capability(
        _VALID_IDENTIFIER["action"], "terminal-cap"
    )
    assert account == f"{mdi._TERMINAL_CAPABILITY_PREFIX}{_VALID_IDENTIFIER['action']}"
    assert MarketplaceDeviceIdentity().get_secret(account) == "terminal-cap"


# --- sign_pair ----------------------------------------------------------

def test_sign_pair_produces_verifiable_signature():
    identity = _identity()
    pair_token = "pair-token-secret"
    signature = MarketplaceDeviceIdentity().sign_pair(identity, pair_token)

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
    _verify(signature, transcript, identity.public_key)


# --- sign_device_request ------------------------------------------------

@pytest.mark.parametrize("operation", sorted(mdi._SIGNED_OPERATIONS))
def test_sign_device_request_builds_verifiable_signed_request(operation):
    identity = _identity()
    body = _valid_body(operation)
    server_origin = "https://marketplace.example"

    path, signed = MarketplaceDeviceIdentity().sign_device_request(
        identity, operation, _path_values(operation), body, _CHALLENGE, server_origin
    )

    spec = PROTOCOL["http"][operation]
    assert "{" not in path  # every path parameter was substituted
    assert set(signed) == {*body, "challenge", "signature"}
    assert signed["challenge"] == _CHALLENGE
    assert signed["signature"]

    transcript = "\n".join(
        [
            "better-agent-marketplace",
            "protocol-v1",
            PROTOCOL_HASH,
            server_origin,
            str(spec["method"]),
            path,
            identity.device_id,
            _CHALLENGE,
            canonical_hash(body),
        ]
    ).encode("utf-8")
    _verify(signed["signature"], transcript, identity.public_key)


def test_sign_device_request_rejects_unknown_operation():
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().sign_device_request(
            _identity(), "bogus", {}, _valid_body("revoke"), _CHALLENGE, "https://x"
        )


def test_sign_device_request_rejects_unsigned_operation():
    # pair_context exists in the protocol but is not a device-signed operation.
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().sign_device_request(
            _identity(),
            "pair_context",
            {},
            {"protocol_hash": PROTOCOL_HASH},
            _CHALLENGE,
            "https://x",
        )


def test_sign_device_request_rejects_incomplete_path():
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().sign_device_request(
            _identity(),
            "fence",
            {"device_id": _VALID_IDENTIFIER["device"]},  # missing action_id
            _valid_body("fence"),
            _CHALLENGE,
            "https://x",
        )


def test_sign_device_request_rejects_extra_path_value():
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().sign_device_request(
            _identity(),
            "lease",
            {
                "device_id": _VALID_IDENTIFIER["device"],
                "action_id": _VALID_IDENTIFIER["action"],
            },
            _valid_body("lease"),
            _CHALLENGE,
            "https://x",
        )


def test_sign_device_request_rejects_invalid_body_shape():
    body = _valid_body("lease")
    body.pop("wait_seconds")
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().sign_device_request(
            _identity(),
            "lease",
            _path_values("lease"),
            body,
            _CHALLENGE,
            "https://x",
        )


def test_sign_device_request_rejects_wrong_protocol_hash():
    body = _valid_body("lease")
    body["protocol_hash"] = "deadbeef"
    with pytest.raises(MarketplaceIdentityError):
        MarketplaceDeviceIdentity().sign_device_request(
            _identity(),
            "lease",
            _path_values("lease"),
            body,
            _CHALLENGE,
            "https://x",
        )


def test_sign_device_request_rejects_invalid_challenge():
    with pytest.raises(ValueError):
        MarketplaceDeviceIdentity().sign_device_request(
            _identity(),
            "lease",
            _path_values("lease"),
            _valid_body("lease"),
            "bad-challenge",
            "https://x",
        )

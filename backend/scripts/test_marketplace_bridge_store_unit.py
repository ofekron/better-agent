"""Dedicated unit coverage for marketplace_bridge_store.py.

The store is a pure validation + persistence layer over a JSON state file:
`validate_state` walks the whole state through nested validators, and
`MarketplaceStateStore` adds durable read/write/mutate on top. Every branch
here is exercised by building a known-valid fixture (via canonical_hash so the
envelope digest is real) and mutating exactly one field to trip one error
branch, asserting the specific MarketplaceStateError message.
"""

from __future__ import annotations

import os

import pytest

import marketplace_bridge_store as store
from marketplace_bridge_store import (
    CONNECTION_STATES,
    INTENT_STATES,
    SCHEMA_VERSION,
    MarketplaceStateError,
    MarketplaceStateStore,
    empty_state,
    validate_state,
)
from marketplace_protocol import PROTOCOL, PROTOCOL_HASH, canonical_hash

SHA = lambda c: c * 64
DEVICE_ID = "badvc_" + "a" * 32
ACTION_ID = "baact_" + "1" * 32
PAIR_ID = "pair_" + "0" * 32
TS = "2025-01-01T00:00:00+00:00"


# --- fixture factories -------------------------------------------------------


def valid_device() -> dict:
    return {
        "device_id": DEVICE_ID,
        "public_key": "X" * 43,
        "label": "Phone",
        "paired": True,
        "revoked": False,
        "revocation_pending": False,
        "epoch": 1,
        "server_origin": "https://example.com",
    }


def valid_extension() -> dict:
    return {
        "id": "ofek.test",
        "name": "Test",
        "version": "1.2.3",
        "publisher": "Ofek",
        "permission_delta": ["network"],
    }


def ext_state(exists: bool) -> dict:
    if not exists:
        return {"exists": False}
    return {
        "exists": True,
        "source_type": "marketplace",
        "version": "1.2.3",
        "enabled": True,
    }


def valid_action_intent(
    intent_id: str = ACTION_ID, action: str = "enable"
) -> dict:
    extension = valid_extension()
    target_version = ""
    catalog_snapshot = SHA("3")
    envelope_digest = canonical_hash(
        {
            "action_id": intent_id,
            "action_type": action,
            "extension_id": extension["id"],
            "target_version": target_version,
            "catalog_snapshot_sha256": catalog_snapshot,
        }
    )
    return {
        "intent_id": intent_id,
        "action": action,
        "status": "succeeded",
        "extension": extension,
        "lease_capability_account": f"marketplace-lease-capability:{intent_id}",
        "lease_capability_ready": True,
        "lease_cleanup_pending": False,
        "lease_expires_at": TS,
        "target_version": target_version,
        "catalog_snapshot_sha256": catalog_snapshot,
        "envelope_digest": envelope_digest,
        "precondition": ext_state(False),
        "desired": ext_state(True),
        "rejection_pending": False,
        "error": "",
        "created_at": TS,
    }


def valid_pair_intent(intent_id: str = PAIR_ID) -> dict:
    return {
        "intent_id": intent_id,
        "action": "pair",
        "status": "redeemed",
        "created_at": TS,
    }


def valid_receipt(action_id: str = ACTION_ID) -> dict:
    return {
        "action_id": action_id,
        "intent_id": action_id,
        "device_id": DEVICE_ID,
        "device_epoch": 1,
        "envelope_digest": SHA("5"),
        "receipt_revision": 1,
        "action": "enable",
        "extension_id": "ofek.test",
        "expected_version": "",
        "catalog_snapshot_sha256": SHA("3"),
        "precondition": ext_state(False),
        "desired": ext_state(True),
        "phase": "terminal",
        "terminal_capability_account": f"marketplace-terminal-capability:{action_id}",
        "terminal_result": {"outcome": "succeeded", "result_code": "ok"},
        "ack_status": "acked",
        "created_at": TS,
        "reconcile_deadline": "2025-01-01T01:00:00+00:00",
    }


def valid_tombstone() -> dict:
    return {
        "envelope_digest": SHA("5"),
        "terminal_result": {"outcome": "failed_unknown", "result_code": "ambiguous"},
        "receipt_revision": 1,
        "created_at": TS,
    }


def valid_pending_pair(intent_id: str = PAIR_ID) -> dict:
    return {
        "token_account": f"marketplace-pair-token:{intent_id}",
        "created_at": TS,
    }


def populated_state() -> dict:
    state = empty_state()
    state["connection_state"] = "connected"
    state["device"] = valid_device()
    state["pending_pairs"] = {PAIR_ID: valid_pending_pair()}
    state["intents"] = {
        ACTION_ID: valid_action_intent(),
        PAIR_ID: valid_pair_intent(),
    }
    state["receipts"] = {ACTION_ID: valid_receipt()}
    state["tombstones"] = {ACTION_ID: valid_tombstone()}
    return state


# --- validate_state: structural top-level ------------------------------------


def test_validate_state_rejects_non_dict():
    with pytest.raises(MarketplaceStateError, match="must be an object"):
        validate_state([])


def test_validate_state_rejects_unknown_keys():
    state = empty_state()
    state["extra"] = 1
    with pytest.raises(MarketplaceStateError, match="unsupported shape"):
        validate_state(state)


def test_validate_state_rejects_wrong_schema_version():
    state = empty_state()
    state["schema_version"] = 999
    with pytest.raises(MarketplaceStateError, match="version is unsupported"):
        validate_state(state)


def test_validate_state_rejects_wrong_protocol_hash():
    state = empty_state()
    state["protocol_hash"] = "x" * 64
    with pytest.raises(MarketplaceStateError, match="protocol version is unsupported"):
        validate_state(state)


def test_validate_state_rejects_bad_connection_state():
    state = empty_state()
    state["connection_state"] = "teleporting"
    with pytest.raises(MarketplaceStateError, match="connection state is invalid"):
        validate_state(state)


@pytest.mark.parametrize("field", ["revision", "projection_revision"])
def test_validate_state_rejects_negative_revision(field):
    state = empty_state()
    state[field] = -1
    with pytest.raises(MarketplaceStateError, match=f"{field} is invalid"):
        validate_state(state)


@pytest.mark.parametrize("field", ["revision", "projection_revision"])
def test_validate_state_rejects_non_int_revision(field):
    state = empty_state()
    state[field] = "0"
    with pytest.raises(MarketplaceStateError, match=f"{field} is invalid"):
        validate_state(state)


@pytest.mark.parametrize(
    "field", ["pending_pairs", "intents", "receipts", "tombstones"]
)
def test_validate_state_rejects_non_dict_collection(field):
    state = empty_state()
    state[field] = []
    with pytest.raises(MarketplaceStateError, match=f"{field} is invalid"):
        validate_state(state)


def test_validate_state_accepts_empty_and_populated():
    # empty_state is the minimal valid state
    assert validate_state(empty_state())["schema_version"] == SCHEMA_VERSION
    # fully populated state validates and is returned by identity
    state = populated_state()
    assert validate_state(state) is state


def test_state_constants_align_with_protocol():
    assert "unpaired" in CONNECTION_STATES
    # INTENT_STATES is awaiting_confirmation + pair + action states
    assert set(PROTOCOL["pair_states"]) <= INTENT_STATES
    assert set(PROTOCOL["action_states"]) <= INTENT_STATES
    assert "awaiting_confirmation" in INTENT_STATES


# --- validate_state: device delegation ---------------------------------------


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda d: (d.pop("label"), d)[1], "unsupported shape"),  # _require_keys
        (lambda d: {**d, "device_id": "x"}, "device id is invalid"),
        (lambda d: {**d, "public_key": "short"}, "public key is invalid"),
        (lambda d: {**d, "label": ""}, "label is invalid"),
        (lambda d: {**d, "paired": "yes"}, "flags are invalid"),
        (lambda d: {**d, "epoch": -1}, "epoch is invalid"),
        (lambda d: {**d, "server_origin": 5}, "origin is invalid"),
    ],
)
def test_validate_device_rejects_bad_fields(mutation, message):
    state = empty_state()
    state["device"] = mutation(valid_device())
    with pytest.raises(MarketplaceStateError, match=message):
        validate_state(state)


def test_validate_device_rejects_non_dict():
    with pytest.raises(MarketplaceStateError, match="device is invalid"):
        store._validate_device("not-a-dict")


def test_validate_device_accepts_none():
    store._validate_device(None)  # no raise


def test_validate_device_rejects_label_too_long():
    dev = valid_device()
    dev["label"] = "x" * 501
    with pytest.raises(MarketplaceStateError, match="label is invalid"):
        store._validate_device(dev)


# --- validate_state: pending pairs -------------------------------------------


def test_pending_pair_rejects_bad_key_pattern():
    state = empty_state()
    state["pending_pairs"] = {"bad-key": valid_pending_pair("bad-key")}
    with pytest.raises(MarketplaceStateError, match="pending pair is invalid"):
        validate_state(state)


def test_pending_pair_rejects_non_dict_value():
    state = empty_state()
    state["pending_pairs"] = {PAIR_ID: "nope"}
    with pytest.raises(MarketplaceStateError, match="pending pair is invalid"):
        validate_state(state)


def test_pending_pair_rejects_wrong_shape():
    state = empty_state()
    state["pending_pairs"] = {PAIR_ID: {"token_account": "x"}}
    with pytest.raises(MarketplaceStateError, match="unsupported shape"):
        validate_state(state)


def test_pending_pair_rejects_wrong_account():
    state = empty_state()
    state["pending_pairs"] = {
        PAIR_ID: {**valid_pending_pair(), "token_account": "marketplace-pair-token:other"}
    }
    with pytest.raises(MarketplaceStateError, match="account is invalid"):
        validate_state(state)


def test_pending_pair_rejects_bad_timestamp():
    state = empty_state()
    state["pending_pairs"] = {
        PAIR_ID: {**valid_pending_pair(), "created_at": "nope"}
    }
    with pytest.raises(MarketplaceStateError, match="pair created_at is invalid"):
        validate_state(state)


# --- validate_state: intents (pair + action dispatch) ------------------------


def test_intent_rejects_non_dict():
    with pytest.raises(MarketplaceStateError, match="intent is invalid"):
        store._validate_intent(ACTION_ID, "nope")


def test_intent_rejects_id_mismatch():
    intent = valid_action_intent()
    intent["intent_id"] = "baact_" + "9" * 32
    with pytest.raises(MarketplaceStateError, match="intent is invalid"):
        store._validate_intent(ACTION_ID, intent)


def test_pair_intent_rejects_bad_id_pattern():
    # action == pair but id is not a pair_intent pattern
    intent = {"intent_id": "wrong", "action": "pair", "status": "redeemed", "created_at": TS}
    with pytest.raises(MarketplaceStateError, match="pair intent id is invalid"):
        store._validate_intent("wrong", intent)


def test_action_intent_rejects_bad_id_pattern():
    intent = valid_action_intent()
    intent["intent_id"] = "wrong"
    with pytest.raises(MarketplaceStateError, match="action id is invalid"):
        store._validate_intent("wrong", intent)


def test_intent_rejects_bad_status():
    intent = valid_action_intent()
    intent["status"] = "teleporting"
    with pytest.raises(MarketplaceStateError, match="intent state is invalid"):
        store._validate_intent(ACTION_ID, intent)


def test_intent_rejects_non_str_created_at():
    intent = valid_action_intent()
    intent["created_at"] = 5
    with pytest.raises(MarketplaceStateError, match="intent timestamp is invalid"):
        store._validate_intent(ACTION_ID, intent)


def test_pair_intent_validates():
    store._validate_intent(PAIR_ID, valid_pair_intent())  # no raise


# --- _validate_action_intent branches ----------------------------------------


def _action(state_override=None) -> dict:
    intent = valid_action_intent()
    if state_override:
        state_override(intent)
    return intent


@pytest.mark.parametrize(
    "override, message",
    [
        # _require_keys (shape)
        (lambda i: i.pop("error"), "unsupported shape"),
        # extension shape
        (lambda i: i.__setitem__("extension", {"id": "ofek.test"}), "extension is invalid"),
        # extension id pattern
        (
            lambda i: i["extension"].__setitem__("id", "BAD ID!"),
            "extension id is invalid",
        ),
        # name/version/publisher type or empty name -> "extension is invalid"
        (lambda i: i["extension"].__setitem__("name", ""), "extension is invalid"),
        (lambda i: i["extension"].__setitem__("version", 5), "extension is invalid"),
    ],
)
def test_action_intent_extension_shape(override, message):
    intent = _action(override)
    with pytest.raises(MarketplaceStateError, match=message):
        store._validate_action_intent(ACTION_ID, intent)


@pytest.mark.parametrize(
    "override, message",
    [
        # control char in name -> permissions block
        (lambda i: i["extension"].__setitem__("name", "bad\nname"), "permissions are invalid"),
        # name too long -> permissions block
        (lambda i: i["extension"].__setitem__("name", "x" * 501), "permissions are invalid"),
        # version too long -> permissions block
        (lambda i: i["extension"].__setitem__("version", "v" * 129), "permissions are invalid"),
        # permission_delta not a list -> permissions block
        (lambda i: i["extension"].__setitem__("permission_delta", "network"), "permissions are invalid"),
        # empty permission string -> permissions block
        (lambda i: i["extension"].__setitem__("permission_delta", [""]), "permissions are invalid"),
        # non-str permission -> permissions block
        (lambda i: i["extension"].__setitem__("permission_delta", [5]), "permissions are invalid"),
        # control char in permission -> permissions block
        (lambda i: i["extension"].__setitem__("permission_delta", ["net\nwork"]), "permissions are invalid"),
    ],
)
def test_action_intent_permissions(override, message):
    intent = _action(override)
    with pytest.raises(MarketplaceStateError, match=message):
        store._validate_action_intent(ACTION_ID, intent)


def test_action_intent_too_many_permissions():
    intent = valid_action_intent()
    intent["extension"]["permission_delta"] = ["p"] * (
        PROTOCOL["bounds"]["permission_count_max"] + 1
    )
    with pytest.raises(MarketplaceStateError, match="permissions are invalid"):
        store._validate_action_intent(ACTION_ID, intent)


def test_action_intent_target_version_invalid_for_non_install():
    # enable must have an empty target_version
    intent = valid_action_intent()
    intent["target_version"] = "9.9.9"
    with pytest.raises(MarketplaceStateError, match="target version is invalid"):
        store._validate_action_intent(ACTION_ID, intent)


def test_action_intent_install_requires_matching_version():
    intent_id = "baact_" + "2" * 32
    extension = valid_extension()
    intent = valid_action_intent(intent_id, "install")
    # install with empty target_version -> invalid
    with pytest.raises(MarketplaceStateError, match="target version is invalid"):
        store._validate_action_intent(intent_id, intent)


def test_action_intent_install_valid_with_matching_version():
    intent_id = "baact_" + "2" * 32
    extension = valid_extension()
    target_version = extension["version"]  # "1.2.3"
    catalog_snapshot = SHA("3")
    intent = {
        **valid_action_intent(intent_id, "install"),
        "target_version": target_version,
        "envelope_digest": canonical_hash(
            {
                "action_id": intent_id,
                "action_type": "install",
                "extension_id": extension["id"],
                "target_version": target_version,
                "catalog_snapshot_sha256": catalog_snapshot,
            }
        ),
    }
    store._validate_action_intent(intent_id, intent)  # no raise


def test_action_intent_unknown_action():
    intent = valid_action_intent()
    intent["action"] = "teleport"
    with pytest.raises(MarketplaceStateError, match="target version is invalid"):
        store._validate_action_intent(ACTION_ID, intent)


@pytest.mark.parametrize(
    "override, message",
    [
        # ready but cleanup_pending -> journal invalid
        (lambda i: i.__setitem__("lease_cleanup_pending", True), "lease capability journal is invalid"),
        # ready True but account empty -> journal invalid
        (lambda i: i.__setitem__("lease_capability_account", ""), "lease capability journal is invalid"),
        # account not in {empty, expected} -> journal invalid
        (lambda i: i.__setitem__("lease_capability_account", "marketplace-lease-capability:other"), "lease capability journal is invalid"),
        # ready not bool
        (lambda i: i.__setitem__("lease_capability_ready", "yes"), "lease capability journal is invalid"),
        # lease_expires_at bad timestamp
        (lambda i: i.__setitem__("lease_expires_at", "nope"), "lease expires_at is invalid"),
        # lease_expires_at naive (no tz)
        (lambda i: i.__setitem__("lease_expires_at", "2025-01-01T00:00:00"), "lease expires_at is invalid"),
        # catalog_snapshot wrong shape
        (lambda i: i.__setitem__("catalog_snapshot_sha256", "x"), "catalog_snapshot_sha256 is invalid"),
        # envelope_digest wrong shape
        (lambda i: i.__setitem__("envelope_digest", "x"), "envelope_digest is invalid"),
        # recovery state
        (lambda i: i.__setitem__("rejection_pending", "x"), "recovery state is invalid"),
        (lambda i: i.__setitem__("error", 5), "recovery state is invalid"),
    ],
)
def test_action_intent_misc_branches(override, message):
    intent = _action(override)
    with pytest.raises(MarketplaceStateError, match=message):
        store._validate_action_intent(ACTION_ID, intent)


def test_action_intent_envelope_digest_mismatch():
    intent = valid_action_intent()
    # valid sha256 but wrong value
    intent["envelope_digest"] = SHA("9")
    with pytest.raises(MarketplaceStateError, match="action digest is invalid"):
        store._validate_action_intent(ACTION_ID, intent)


def test_action_intent_bad_extension_state_precondition():
    intent = valid_action_intent()
    intent["precondition"] = {"exists": "maybe"}
    with pytest.raises(MarketplaceStateError, match="receipt precondition is invalid"):
        store._validate_action_intent(ACTION_ID, intent)


def test_action_intent_bad_extension_state_desired():
    intent = valid_action_intent()
    intent["desired"] = {"exists": True, "source_type": "marketplace"}  # missing keys
    with pytest.raises(MarketplaceStateError, match="receipt desired is invalid"):
        store._validate_action_intent(ACTION_ID, intent)


def test_action_intent_cleanup_pending_with_account_valid():
    # cleanup_pending True with a real account and ready False is valid
    intent = valid_action_intent()
    intent["lease_capability_ready"] = False
    intent["lease_cleanup_pending"] = True
    store._validate_action_intent(ACTION_ID, intent)  # no raise


# --- _validate_receipt branches ----------------------------------------------


@pytest.mark.parametrize(
    "override, message",
    [
        (lambda r: r.pop("ack_status"), "unsupported shape"),
        (lambda r: r.__setitem__("intent_id", "baact_" + "9" * 32), "receipt intent id is invalid"),
        (lambda r: r.__setitem__("device_id", "x"), "receipt device id is invalid"),
        (lambda r: r.__setitem__("device_epoch", -1), "receipt revision is invalid"),
        (lambda r: r.__setitem__("receipt_revision", 0), "receipt revision is invalid"),
        (lambda r: r.__setitem__("device_epoch", "x"), "receipt revision is invalid"),
        (lambda r: r.__setitem__("envelope_digest", "x"), "receipt digest is invalid"),
        (lambda r: r.__setitem__("action", "teleport"), "receipt action is invalid"),
        (lambda r: r.__setitem__("extension_id", "BAD!"), "receipt extension id is invalid"),
        (lambda r: r.__setitem__("expected_version", 5), "receipt version is invalid"),
        (lambda r: r.__setitem__("catalog_snapshot_sha256", "x"), "receipt catalog snapshot is invalid"),
        (lambda r: r.__setitem__("phase", "teleport"), "receipt phase is invalid"),
        (lambda r: r.__setitem__("ack_status", "weird"), "receipt ack state is invalid"),
        (lambda r: r.__setitem__("created_at", "nope"), "receipt created_at is invalid"),
    ],
)
def test_receipt_misc_branches(override, message):
    receipt = valid_receipt()
    override(receipt)
    with pytest.raises(MarketplaceStateError, match=message):
        store._validate_receipt(ACTION_ID, receipt)


def test_receipt_rejects_non_dict():
    with pytest.raises(MarketplaceStateError, match="receipt is invalid"):
        store._validate_receipt(ACTION_ID, "nope")


def test_receipt_rejects_id_mismatch():
    receipt = valid_receipt()
    receipt["action_id"] = "baact_" + "9" * 32
    with pytest.raises(MarketplaceStateError, match="receipt is invalid"):
        store._validate_receipt(ACTION_ID, receipt)


def test_receipt_rejects_bad_action_id_pattern():
    with pytest.raises(MarketplaceStateError, match="receipt id is invalid"):
        store._validate_receipt("wrong-id", valid_receipt("wrong-id"))


def test_receipt_terminal_capability_account_invalid():
    receipt = valid_receipt()
    receipt["terminal_capability_account"] = "wrong"
    with pytest.raises(MarketplaceStateError, match="terminal capability account is invalid"):
        store._validate_receipt(ACTION_ID, receipt)


def test_receipt_fence_pending_requires_empty_terminal_account():
    receipt = valid_receipt()
    receipt["phase"] = "fence_pending"
    receipt["terminal_capability_account"] = ""
    receipt["terminal_result"] = None
    receipt["ack_status"] = "pending"
    # fence_pending allows empty reconcile_deadline
    receipt["reconcile_deadline"] = ""
    store._validate_receipt(ACTION_ID, receipt)  # no raise


def test_receipt_fence_pending_rejects_real_terminal_account():
    receipt = valid_receipt()
    receipt["phase"] = "fence_pending"
    # non-empty account while fence_pending -> invalid
    with pytest.raises(MarketplaceStateError, match="terminal capability account is invalid"):
        store._validate_receipt(ACTION_ID, receipt)


def test_receipt_non_terminal_requires_empty_terminal_account():
    # fenced phase must use expected account, not empty
    receipt = valid_receipt()
    receipt["phase"] = "fenced"
    receipt["terminal_capability_account"] = ""
    receipt["terminal_result"] = None
    receipt["ack_status"] = "pending"
    receipt["reconcile_deadline"] = "2025-01-01T01:00:00+00:00"
    with pytest.raises(MarketplaceStateError, match="terminal capability account is invalid"):
        store._validate_receipt(ACTION_ID, receipt)


def test_receipt_terminal_result_presence_must_match_phase():
    # non-terminal phase with a terminal_result -> invalid
    receipt = valid_receipt()
    receipt["phase"] = "fenced"
    receipt["terminal_result"] = {"outcome": "succeeded", "result_code": "ok"}
    receipt["ack_status"] = "pending"
    receipt["reconcile_deadline"] = "2025-01-01T01:00:00+00:00"
    with pytest.raises(MarketplaceStateError, match="terminal state is invalid"):
        store._validate_receipt(ACTION_ID, receipt)


def test_receipt_terminal_phase_requires_terminal_result():
    receipt = valid_receipt()
    receipt["terminal_result"] = None  # but phase is terminal
    with pytest.raises(MarketplaceStateError, match="terminal state is invalid"):
        store._validate_receipt(ACTION_ID, receipt)


def test_receipt_ack_must_be_terminal_when_not_pending():
    receipt = valid_receipt()
    receipt["ack_status"] = "acked"
    receipt["phase"] = "fenced"  # not terminal
    receipt["terminal_result"] = None  # avoid the terminal-state guard first
    with pytest.raises(MarketplaceStateError, match="receipt ack state is invalid"):
        store._validate_receipt(ACTION_ID, receipt)


def test_receipt_conflict_ack_at_terminal_valid():
    receipt = valid_receipt()
    receipt["ack_status"] = "conflict"
    store._validate_receipt(ACTION_ID, receipt)  # no raise


def test_receipt_reconcile_deadline_required_when_not_fence_pending():
    receipt = valid_receipt()
    receipt["reconcile_deadline"] = "nope"
    with pytest.raises(MarketplaceStateError, match="receipt reconcile_deadline is invalid"):
        store._validate_receipt(ACTION_ID, receipt)


def test_receipt_bad_extension_state():
    receipt = valid_receipt()
    receipt["precondition"] = {"exists": True}  # missing required keys
    with pytest.raises(MarketplaceStateError, match="receipt precondition is invalid"):
        store._validate_receipt(ACTION_ID, receipt)


def test_receipt_bad_terminal_result():
    receipt = valid_receipt()
    receipt["terminal_result"] = {"outcome": "succeeded"}  # missing result_code
    with pytest.raises(MarketplaceStateError, match="terminal result is invalid"):
        store._validate_receipt(ACTION_ID, receipt)


# --- _validate_terminal_result ----------------------------------------------


@pytest.mark.parametrize(
    "value, message",
    [
        ("nope", "terminal result is invalid"),
        ({"outcome": "succeeded"}, "terminal result is invalid"),
        ({"outcome": "weird", "result_code": "ok"}, "terminal outcome is invalid"),
        ({"outcome": "succeeded", "result_code": ""}, "terminal result code is invalid"),
        ({"outcome": "succeeded", "result_code": "x" * 81}, "terminal result code is invalid"),
    ],
)
def test_validate_terminal_result_branches(value, message):
    with pytest.raises(MarketplaceStateError, match=message):
        store._validate_terminal_result(value)


def test_validate_terminal_result_accepts_valid():
    store._validate_terminal_result({"outcome": "failed", "result_code": "boom"})  # no raise


# --- _validate_extension_state ----------------------------------------------


@pytest.mark.parametrize(
    "value, message",
    [
        ("nope", "receipt state is invalid"),
        ({"exists": "x"}, "receipt state is invalid"),
        ({"exists": False, "extra": 1}, "receipt state is invalid"),
        ({"exists": True, "source_type": "x"}, "receipt state is invalid"),
        ({"exists": True, "version": 5, "source_type": "x", "enabled": True}, "receipt state is invalid"),
    ],
)
def test_validate_extension_state_branches(value, message):
    with pytest.raises(MarketplaceStateError, match=message):
        store._validate_extension_state(value, "state")


def test_validate_extension_state_accepts_absent():
    store._validate_extension_state({"exists": False}, "state")  # no raise


def test_validate_extension_state_accepts_present():
    store._validate_extension_state(
        {"exists": True, "source_type": "marketplace", "version": "1.0", "enabled": False},
        "state",
    )  # no raise


# --- _validate_timestamp -----------------------------------------------------


@pytest.mark.parametrize(
    "value, allow_empty, message",
    [
        ("nope", False, "ts is invalid"),
        ("2025-01-01T00:00:00", False, "ts is invalid"),  # naive
        (5, False, "ts is invalid"),  # non-str
        (5, True, "ts is invalid"),  # non-str even when allow_empty
    ],
)
def test_validate_timestamp_branches(value, allow_empty, message):
    with pytest.raises(MarketplaceStateError, match=message):
        store._validate_timestamp(value, "ts", allow_empty=allow_empty)


def test_validate_timestamp_allow_empty_string():
    store._validate_timestamp("", "ts", allow_empty=True)  # no raise


def test_validate_timestamp_accepts_z_suffix():
    store._validate_timestamp("2025-01-01T00:00:00Z", "ts", allow_empty=False)  # no raise


# --- _require_keys + empty_state --------------------------------------------


def test_require_keys_rejects_mismatch():
    with pytest.raises(MarketplaceStateError, match="unsupported shape"):
        store._require_keys({"a": 1}, {"a", "b"}, "thing")


def test_require_keys_accepts_exact():
    store._require_keys({"a": 1}, {"a"}, "thing")  # no raise


def test_empty_state_shape():
    state = empty_state()
    assert state["schema_version"] == SCHEMA_VERSION
    assert state["protocol_hash"] == PROTOCOL_HASH
    assert state["connection_state"] == "unpaired"
    assert state["device"] is None
    for field in ("pending_pairs", "intents", "receipts", "tombstones"):
        assert state[field] == {}


# --- tombstones (validate_state path) ---------------------------------------


def test_tombstone_rejects_bad_key():
    state = empty_state()
    state["tombstones"] = {"bad-key": valid_tombstone()}
    with pytest.raises(MarketplaceStateError, match="tombstone is invalid"):
        validate_state(state)


def test_tombstone_rejects_non_dict():
    state = empty_state()
    state["tombstones"] = {ACTION_ID: "nope"}
    with pytest.raises(MarketplaceStateError, match="tombstone is invalid"):
        validate_state(state)


def test_tombstone_rejects_wrong_shape():
    state = empty_state()
    state["tombstones"] = {ACTION_ID: {"envelope_digest": SHA("5")}}
    with pytest.raises(MarketplaceStateError, match="unsupported shape"):
        validate_state(state)


def test_tombstone_rejects_bad_digest():
    state = empty_state()
    ts = {**valid_tombstone(), "envelope_digest": "x"}
    state["tombstones"] = {ACTION_ID: ts}
    with pytest.raises(MarketplaceStateError, match="tombstone digest is invalid"):
        validate_state(state)


def test_tombstone_rejects_bad_revision():
    state = empty_state()
    ts = {**valid_tombstone(), "receipt_revision": 0}
    state["tombstones"] = {ACTION_ID: ts}
    with pytest.raises(MarketplaceStateError, match="tombstone revision is invalid"):
        validate_state(state)


def test_tombstone_rejects_bad_terminal_result():
    state = empty_state()
    ts = {**valid_tombstone(), "terminal_result": {"outcome": "weird", "result_code": "ok"}}
    state["tombstones"] = {ACTION_ID: ts}
    with pytest.raises(MarketplaceStateError, match="terminal outcome is invalid"):
        validate_state(state)


def test_tombstone_rejects_bad_timestamp():
    state = empty_state()
    ts = {**valid_tombstone(), "created_at": "nope"}
    state["tombstones"] = {ACTION_ID: ts}
    with pytest.raises(MarketplaceStateError, match="tombstone created_at is invalid"):
        validate_state(state)


# --- MarketplaceStateStore ---------------------------------------------------


def test_store_read_missing_path_returns_empty_state(tmp_path):
    store_obj = MarketplaceStateStore(tmp_path / "missing.json")
    assert store_obj.read() == empty_state()


def test_store_path_property_defaults_to_state_path():
    store_obj = MarketplaceStateStore()
    assert store_obj.path.name == "intent-receipts-v2.json"
    assert store_obj.path.parent.name == "marketplace"


def test_store_write_read_roundtrip_increments_revision(tmp_path):
    path = tmp_path / "state.json"
    store_obj = MarketplaceStateStore(path)
    written = store_obj.write(empty_state())
    assert written["revision"] == 1
    # read returns a deep copy with revision preserved
    assert store_obj.read()["revision"] == 1
    second = store_obj.write(store_obj.read())
    assert second["revision"] == 2


def test_store_write_sets_mode_0600_on_posix(tmp_path):
    path = tmp_path / "state.json"
    store_obj = MarketplaceStateStore(path)
    store_obj.write(empty_state())
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_store_write_skips_chmod_on_windows(tmp_path, monkeypatch):
    # Force the `os.name == "nt"` branch: patch the module's `os` reference so
    # chmod is never called. json_store.write_json_durable uses its own os
    # import, so this patch does not interfere with the durable write.
    path = tmp_path / "state.json"
    store_obj = MarketplaceStateStore(path)

    class FakeOs:
        name = "nt"

        @staticmethod
        def chmod(*_a, **_k):
            raise AssertionError("chmod must not run on Windows")

    monkeypatch.setattr(store, "os", FakeOs(), raising=False)
    store_obj.write(empty_state())
    assert path.exists()


def test_store_write_rejects_invalid_state(tmp_path):
    store_obj = MarketplaceStateStore(tmp_path / "state.json")
    bad = empty_state()
    bad["schema_version"] = 999
    with pytest.raises(MarketplaceStateError):
        store_obj.write(bad)


def test_store_read_rejects_corrupt_state_on_disk(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"schema_version": 999}')
    store_obj = MarketplaceStateStore(path)
    with pytest.raises(MarketplaceStateError):
        store_obj.read()


def test_store_mutate_roundtrip(tmp_path):
    store_obj = MarketplaceStateStore(tmp_path / "state.json")
    state, result = store_obj.mutate(lambda s: s.setdefault("device", None))
    assert result is None
    assert state["revision"] == 1


def test_store_does_not_mutate_caller_input(tmp_path):
    store_obj = MarketplaceStateStore(tmp_path / "state.json")
    original = empty_state()
    snapshot = store_obj.write(original)
    assert original["revision"] == 0  # caller's dict untouched
    assert snapshot["revision"] == 1
    snapshot["revision"] = 999
    assert store_obj.read()["revision"] == 1  # internal copy isolated


def test_store_full_populated_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    store_obj = MarketplaceStateStore(path)
    state = store_obj.write(populated_state())
    # round-trips with all entities intact
    reread = store_obj.read()
    assert reread["device"] == valid_device()
    assert ACTION_ID in reread["intents"]
    assert PAIR_ID in reread["intents"]
    assert ACTION_ID in reread["receipts"]
    assert ACTION_ID in reread["tombstones"]
    assert reread["revision"] == state["revision"]

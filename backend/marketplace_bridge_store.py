from __future__ import annotations

import os
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

import json_store
from marketplace_protocol import (
    ALLOWED_ACTIONS,
    PATTERNS,
    PROTOCOL,
    PROTOCOL_HASH,
    canonical_hash,
)
from paths import bc_home

SCHEMA_VERSION = 2
CONNECTION_STATES = frozenset({"unpaired", "connecting", "connected", "offline"})
INTENT_STATES = frozenset(
    {
        "awaiting_confirmation",
        *PROTOCOL["pair_states"],
        *PROTOCOL["action_states"],
    }
)
PAIR_ACTION = "pair"
_T = TypeVar("_T")


class MarketplaceStateError(ValueError):
    pass


def _state_path() -> Path:
    return bc_home() / "marketplace" / "intent-receipts-v2.json"


def empty_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_hash": PROTOCOL_HASH,
        "revision": 0,
        "connection_state": "unpaired",
        "device": None,
        "pending_pairs": {},
        "intents": {},
        "receipts": {},
        "tombstones": {},
        "projection_revision": 0,
    }


def _require_keys(value: dict, expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise MarketplaceStateError(f"{label} has an unsupported shape")


def _validate_device(device: object) -> None:
    if device is None:
        return
    if not isinstance(device, dict):
        raise MarketplaceStateError("Marketplace device is invalid")
    _require_keys(
        device,
        {
            "device_id",
            "public_key",
            "label",
            "paired",
            "revoked",
            "revocation_pending",
            "epoch",
            "server_origin",
        },
        "Marketplace device",
    )
    if not PATTERNS["device"].fullmatch(str(device["device_id"])):
        raise MarketplaceStateError("Marketplace device id is invalid")
    if not isinstance(device["public_key"], str) or len(device["public_key"]) != 43:
        raise MarketplaceStateError("Marketplace device public key is invalid")
    if not isinstance(device["label"], str) or not 1 <= len(device["label"]) <= 500:
        raise MarketplaceStateError("Marketplace device label is invalid")
    if (
        not isinstance(device["paired"], bool)
        or not isinstance(device["revoked"], bool)
        or not isinstance(device["revocation_pending"], bool)
    ):
        raise MarketplaceStateError("Marketplace device flags are invalid")
    if not isinstance(device["epoch"], int) or device["epoch"] < 0:
        raise MarketplaceStateError("Marketplace device epoch is invalid")
    if not isinstance(device["server_origin"], str):
        raise MarketplaceStateError("Marketplace device origin is invalid")


def _validate_intent(intent_id: str, intent: object) -> None:
    if not isinstance(intent, dict) or intent.get("intent_id") != intent_id:
        raise MarketplaceStateError("Marketplace intent is invalid")
    action = intent.get("action")
    if action == PAIR_ACTION:
        if not PATTERNS["pair_intent"].fullmatch(intent_id):
            raise MarketplaceStateError("Marketplace pair intent id is invalid")
    else:
        if not PATTERNS["action"].fullmatch(intent_id):
            raise MarketplaceStateError("Marketplace action id is invalid")
        _validate_action_intent(intent_id, intent)
    if intent.get("status") not in INTENT_STATES:
        raise MarketplaceStateError("Marketplace intent state is invalid")
    if not isinstance(intent.get("created_at"), str):
        raise MarketplaceStateError("Marketplace intent timestamp is invalid")


def _validate_action_intent(intent_id: str, intent: dict) -> None:
    _require_keys(
        intent,
        {
            "intent_id",
            "action",
            "status",
            "extension",
            "lease_capability_account",
            "lease_capability_ready",
            "lease_cleanup_pending",
            "lease_expires_at",
            "target_version",
            "catalog_snapshot_sha256",
            "envelope_digest",
            "precondition",
            "desired",
            "rejection_pending",
            "error",
            "created_at",
        },
        "Marketplace action intent",
    )
    extension = intent["extension"]
    if not isinstance(extension, dict) or set(extension) != {
        "id",
        "name",
        "version",
        "publisher",
        "permission_delta",
    }:
        raise MarketplaceStateError("Marketplace action extension is invalid")
    if not PATTERNS["extension"].fullmatch(str(extension["id"])):
        raise MarketplaceStateError("Marketplace action extension id is invalid")
    if not all(
        isinstance(extension[field], str)
        for field in ("name", "version", "publisher")
    ) or not extension["name"]:
        raise MarketplaceStateError("Marketplace action extension is invalid")
    if (
        any(
            len(extension[field]) > PROTOCOL["bounds"]["display_text_max"]
            or any(ord(character) < 32 for character in extension[field])
            for field in ("name", "publisher")
        )
        or len(extension["version"]) > 128
        or not isinstance(extension["permission_delta"], list)
        or len(extension["permission_delta"])
        > PROTOCOL["bounds"]["permission_count_max"]
        or not all(
            isinstance(permission, str)
            and permission
            and len(permission) <= PROTOCOL["bounds"]["display_text_max"]
            and not any(ord(character) < 32 for character in permission)
            for permission in extension["permission_delta"]
        )
    ):
        raise MarketplaceStateError("Marketplace action permissions are invalid")
    action = intent["action"]
    target_version = intent["target_version"]
    if (
        action not in ALLOWED_ACTIONS
        or not isinstance(target_version, str)
        or len(target_version) > 128
        or action in {"install", "update"}
        and (not target_version or extension["version"] != target_version)
        or action not in {"install", "update"}
        and target_version
    ):
        raise MarketplaceStateError("Marketplace action target version is invalid")
    expected_account = f"marketplace-lease-capability:{intent_id}"
    account = intent["lease_capability_account"]
    ready = intent["lease_capability_ready"]
    cleanup_pending = intent["lease_cleanup_pending"]
    if (
        not isinstance(ready, bool)
        or not isinstance(cleanup_pending, bool)
        or account not in {"", expected_account}
        or ready and (not account or cleanup_pending)
        or cleanup_pending and not account
        or not account and (ready or cleanup_pending)
    ):
        raise MarketplaceStateError("Marketplace lease capability journal is invalid")
    _validate_timestamp(
        intent["lease_expires_at"],
        "lease expires_at",
        allow_empty=False,
    )
    for field in ("catalog_snapshot_sha256", "envelope_digest"):
        if not PATTERNS["sha256"].fullmatch(str(intent[field])):
            raise MarketplaceStateError(f"Marketplace action {field} is invalid")
    expected_digest = canonical_hash(
        {
            "action_id": intent_id,
            "action_type": action,
            "extension_id": extension["id"],
            "target_version": target_version,
            "catalog_snapshot_sha256": intent["catalog_snapshot_sha256"],
        }
    )
    if intent["envelope_digest"] != expected_digest:
        raise MarketplaceStateError("Marketplace action digest is invalid")
    _validate_extension_state(intent["precondition"], "precondition")
    _validate_extension_state(intent["desired"], "desired")
    if not isinstance(intent["rejection_pending"], bool) or not isinstance(
        intent["error"], str
    ):
        raise MarketplaceStateError("Marketplace action recovery state is invalid")


def _validate_receipt(action_id: str, receipt: object) -> None:
    if not isinstance(receipt, dict) or receipt.get("action_id") != action_id:
        raise MarketplaceStateError("Marketplace receipt is invalid")
    if not PATTERNS["action"].fullmatch(action_id):
        raise MarketplaceStateError("Marketplace receipt id is invalid")
    required = {
        "action_id",
        "intent_id",
        "device_id",
        "device_epoch",
        "envelope_digest",
        "receipt_revision",
        "action",
        "extension_id",
        "expected_version",
        "catalog_snapshot_sha256",
        "precondition",
        "desired",
        "phase",
        "terminal_capability_account",
        "terminal_result",
        "ack_status",
        "created_at",
        "reconcile_deadline",
    }
    _require_keys(receipt, required, "Marketplace receipt")
    if receipt["intent_id"] != action_id:
        raise MarketplaceStateError("Marketplace receipt intent id is invalid")
    if not PATTERNS["device"].fullmatch(str(receipt["device_id"])):
        raise MarketplaceStateError("Marketplace receipt device id is invalid")
    if (
        not isinstance(receipt["device_epoch"], int)
        or receipt["device_epoch"] < 0
        or not isinstance(receipt["receipt_revision"], int)
        or receipt["receipt_revision"] < 1
    ):
        raise MarketplaceStateError("Marketplace receipt revision is invalid")
    if not PATTERNS["sha256"].fullmatch(str(receipt["envelope_digest"])):
        raise MarketplaceStateError("Marketplace receipt digest is invalid")
    if receipt["action"] not in PROTOCOL["actions"]:
        raise MarketplaceStateError("Marketplace receipt action is invalid")
    if not PATTERNS["extension"].fullmatch(str(receipt["extension_id"])):
        raise MarketplaceStateError("Marketplace receipt extension id is invalid")
    if not isinstance(receipt["expected_version"], str):
        raise MarketplaceStateError("Marketplace receipt version is invalid")
    if not PATTERNS["sha256"].fullmatch(
        str(receipt["catalog_snapshot_sha256"])
    ):
        raise MarketplaceStateError("Marketplace receipt catalog snapshot is invalid")
    _validate_extension_state(receipt["precondition"], "precondition")
    _validate_extension_state(receipt["desired"], "desired")
    if receipt["phase"] not in {
        "fence_pending",
        "fenced",
        "effect_started",
        "effect_applied",
        "terminal",
    }:
        raise MarketplaceStateError("Marketplace receipt phase is invalid")
    expected_account = f"marketplace-terminal-capability:{action_id}"
    account = receipt["terminal_capability_account"]
    if receipt["phase"] == "fence_pending":
        account_is_valid = account == ""
    else:
        account_is_valid = account == expected_account
    if not account_is_valid:
        raise MarketplaceStateError(
            "Marketplace receipt terminal capability account is invalid"
        )
    terminal_result = receipt["terminal_result"]
    if terminal_result is not None:
        _validate_terminal_result(terminal_result)
    if (receipt["phase"] == "terminal") != (terminal_result is not None):
        raise MarketplaceStateError(
            "Marketplace receipt terminal state is invalid"
        )
    if receipt["ack_status"] not in {"pending", "acked", "conflict"}:
        raise MarketplaceStateError("Marketplace receipt ack state is invalid")
    if receipt["ack_status"] != "pending" and receipt["phase"] != "terminal":
        raise MarketplaceStateError("Marketplace receipt ack state is invalid")
    _validate_timestamp(receipt["created_at"], "receipt created_at", allow_empty=False)
    _validate_timestamp(
        receipt["reconcile_deadline"],
        "receipt reconcile_deadline",
        allow_empty=receipt["phase"] == "fence_pending",
    )


def _validate_timestamp(value: object, field: str, *, allow_empty: bool) -> None:
    if allow_empty and value == "":
        return
    if not isinstance(value, str):
        raise MarketplaceStateError(f"Marketplace {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketplaceStateError(f"Marketplace {field} is invalid") from exc
    if parsed.tzinfo is None:
        raise MarketplaceStateError(f"Marketplace {field} is invalid")


def _validate_extension_state(value: object, field: str) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("exists"), bool):
        raise MarketplaceStateError(f"Marketplace receipt {field} is invalid")
    if value["exists"] is False:
        if set(value) != {"exists"}:
            raise MarketplaceStateError(f"Marketplace receipt {field} is invalid")
        return
    if set(value) != {"exists", "source_type", "version", "enabled"}:
        raise MarketplaceStateError(f"Marketplace receipt {field} is invalid")
    if (
        not isinstance(value["source_type"], str)
        or not isinstance(value["version"], str)
        or not isinstance(value["enabled"], bool)
    ):
        raise MarketplaceStateError(f"Marketplace receipt {field} is invalid")


def _validate_terminal_result(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"outcome", "result_code"}:
        raise MarketplaceStateError("Marketplace terminal result is invalid")
    if value["outcome"] not in {
        "succeeded",
        "rejected",
        "failed",
        "failed_unknown",
    }:
        raise MarketplaceStateError("Marketplace terminal outcome is invalid")
    if not isinstance(value["result_code"], str) or not 1 <= len(
        value["result_code"]
    ) <= 80:
        raise MarketplaceStateError("Marketplace terminal result code is invalid")


def validate_state(state: object) -> dict:
    if not isinstance(state, dict):
        raise MarketplaceStateError("Marketplace state must be an object")
    _require_keys(state, set(empty_state()), "Marketplace state")
    if state["schema_version"] != SCHEMA_VERSION:
        raise MarketplaceStateError("Marketplace state version is unsupported")
    if state["protocol_hash"] != PROTOCOL_HASH:
        raise MarketplaceStateError("Marketplace protocol version is unsupported")
    if state["connection_state"] not in CONNECTION_STATES:
        raise MarketplaceStateError("Marketplace connection state is invalid")
    for field in ("revision", "projection_revision"):
        if not isinstance(state[field], int) or state[field] < 0:
            raise MarketplaceStateError(f"Marketplace {field} is invalid")
    _validate_device(state["device"])
    for field in (
        "pending_pairs",
        "intents",
        "receipts",
        "tombstones",
    ):
        if not isinstance(state[field], dict):
            raise MarketplaceStateError(f"Marketplace {field} is invalid")
    for intent_id, pending in state["pending_pairs"].items():
        if not PATTERNS["pair_intent"].fullmatch(intent_id) or not isinstance(
            pending, dict
        ):
            raise MarketplaceStateError("Marketplace pending pair is invalid")
        _require_keys(
            pending, {"token_account", "created_at"}, "Marketplace pending pair"
        )
        if pending["token_account"] != f"marketplace-pair-token:{intent_id}":
            raise MarketplaceStateError("Marketplace pending pair account is invalid")
        _validate_timestamp(pending["created_at"], "pair created_at", allow_empty=False)
    for intent_id, intent in state["intents"].items():
        _validate_intent(intent_id, intent)
    for action_id, receipt in state["receipts"].items():
        _validate_receipt(action_id, receipt)
    for action_id, tombstone in state["tombstones"].items():
        if not PATTERNS["action"].fullmatch(action_id) or not isinstance(
            tombstone, dict
        ):
            raise MarketplaceStateError("Marketplace tombstone is invalid")
        _require_keys(
            tombstone,
            {"envelope_digest", "terminal_result", "receipt_revision", "created_at"},
            "Marketplace tombstone",
        )
        if not PATTERNS["sha256"].fullmatch(str(tombstone["envelope_digest"])):
            raise MarketplaceStateError("Marketplace tombstone digest is invalid")
        if (
            not isinstance(tombstone["receipt_revision"], int)
            or tombstone["receipt_revision"] < 1
        ):
            raise MarketplaceStateError("Marketplace tombstone revision is invalid")
        _validate_terminal_result(tombstone["terminal_result"])
        _validate_timestamp(
            tombstone["created_at"],
            "tombstone created_at",
            allow_empty=False,
        )
    return state


class MarketplaceStateStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _state_path()
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> dict:
        with self._lock:
            if not self._path.exists():
                return empty_state()
            state = json_store.read_json(self._path, {})
            return deepcopy(validate_state(state))

    def write(self, state: dict) -> dict:
        with self._lock:
            snapshot = deepcopy(validate_state(state))
            snapshot["revision"] += 1
            json_store.write_json_durable(self._path, snapshot)
            if os.name != "nt":
                os.chmod(self._path, 0o600)
            return deepcopy(snapshot)

    def mutate(self, update: Callable[[dict], _T]) -> tuple[dict, _T]:
        with self._lock:
            state = self.read()
            result = update(state)
            return self.write(state), result

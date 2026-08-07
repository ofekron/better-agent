from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import extension_store
import marketplace_service
from marketplace_bridge_store import MarketplaceStateStore
from marketplace_device_identity import MarketplaceDeviceIdentity
from marketplace_protocol import (
    PATTERNS,
    PROTOCOL,
    PROTOCOL_HASH,
    canonical_hash,
    require_hash,
    require_identifier,
    validate_leased_action,
)

_ACTIVE_INTENT_STATES = frozenset({"awaiting_confirmation", "committing"})


class MarketplaceBridgeError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(
    value: object,
    field: str,
    *,
    required: bool = True,
    maximum: int = 500,
) -> str:
    if not isinstance(value, str):
        raise MarketplaceBridgeError(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise MarketplaceBridgeError(f"{field} is required")
    if len(text) > maximum or any(ord(character) < 32 for character in text):
        raise MarketplaceBridgeError(f"{field} is invalid")
    return text


def _server_origin(value: object) -> str:
    origin = _bounded_text(value, "server_origin")
    parsed = urlparse(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
        or "\\" in parsed.path
        or "//" in parsed.path
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
    ):
        raise MarketplaceBridgeError("server_origin is invalid")
    return origin.rstrip("/")


def _future_timestamp(value: object, field: str) -> str:
    text = _bounded_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketplaceBridgeError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed <= datetime.now(timezone.utc):
        raise MarketplaceBridgeError(f"{field} is expired")
    return text


class MarketplaceBridge:
    def __init__(
        self,
        *,
        store: MarketplaceStateStore | None = None,
        identity: MarketplaceDeviceIdentity | None = None,
    ) -> None:
        self._store = store or MarketplaceStateStore()
        self._identity = identity or MarketplaceDeviceIdentity()
        self._mutation_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._on_change: Callable[[int], Awaitable[None]] | None = None
        self._on_extensions_change: Callable[[], Awaitable[None]] | None = None

    async def _read(self) -> dict:
        return await asyncio.to_thread(self._store.read)

    async def _write(self, state: dict) -> dict:
        return await asyncio.to_thread(self._store.write, state)

    async def _notify(self, state: dict) -> None:
        if self._on_change is not None:
            await self._on_change(state["revision"])

    def snapshot(self) -> dict:
        state = self._store.read()
        device = state["device"]
        paired_devices = []
        revocation_pending = False
        if isinstance(device, dict):
            revocation_pending = device["revocation_pending"]
            if device["paired"] and not device["revoked"]:
                paired_devices.append(
                    {
                        "device_id": device["device_id"],
                        "label": device["label"],
                        "public_key": device["public_key"],
                    }
                )
        intents = []
        public_fields = {
            "intent_id",
            "action",
            "status",
            "extension",
            "account_label",
            "site_label",
            "device_label",
            "error",
        }
        ordered_intents = sorted(
            state["intents"].values(),
            key=lambda item: (item["created_at"], item["intent_id"]),
        )
        for intent in ordered_intents:
            if (
                intent["action"] != "pair"
                and intent["status"] == "awaiting_confirmation"
                and not intent["lease_capability_ready"]
            ):
                continue
            intents.append(
                {
                    key: deepcopy(value)
                    for key, value in intent.items()
                    if key in public_fields
                }
            )
        return {
            "revision": state["revision"],
            "connection_state": state["connection_state"],
            "revocation_pending": revocation_pending,
            "paired_devices": paired_devices,
            "intents": intents,
        }

    async def activate_pair(self, pair_token: str, version: int) -> dict:
        if version != 1 or not PATTERNS["pair_token"].fullmatch(pair_token):
            raise MarketplaceBridgeError("invalid Marketplace pair intent")
        digest = hashlib.sha256(pair_token.encode("ascii")).hexdigest()
        intent_id = f"pair_{digest[:32]}"
        async with self._mutation_lock:
            state = await self._read()
            existing_device = state["device"]
            identity_device = (
                None
                if isinstance(existing_device, dict)
                and existing_device["revoked"]
                and not existing_device["revocation_pending"]
                else existing_device
            )
            device, _ = await asyncio.to_thread(
                self._identity.create_or_load,
                identity_device,
            )
            token_account = await asyncio.to_thread(
                self._identity.store_pair_token,
                intent_id,
                pair_token,
            )
            state["device"] = device
            state["pending_pairs"][intent_id] = {
                "token_account": token_account,
                "created_at": _now(),
            }
            state["intents"][intent_id] = {
                "intent_id": intent_id,
                "action": "pair",
                "status": "pending",
                "device_label": device["label"],
                "created_at": _now(),
            }
            state["connection_state"] = "connecting"
            state = await self._write(state)
        await self._notify(state)
        self._wake.set()
        await self._resolve_pair(intent_id)
        return self.snapshot()

    async def _resolve_pair(self, intent_id: str) -> None:
        async with self._mutation_lock:
            state = await self._read()
            pending = state["pending_pairs"].get(intent_id)
            if pending is None:
                return
            token = await asyncio.to_thread(
                self._identity.get_secret,
                pending["token_account"],
            )
        if token is None:
            await self._expire_pair(intent_id)
            return
        context = await marketplace_service.protocol_pair_context(token.strip())
        site_label = _bounded_text(context.get("site_label"), "site_label")
        account_label = _bounded_text(context.get("account_label"), "account_label")
        _future_timestamp(context.get("expires_at"), "pair expiry")
        require_hash(
            context.get("catalog_snapshot_sha256"),
            "catalog_snapshot_sha256",
        )
        origin = _server_origin(marketplace_service.protocol_origin())
        async with self._mutation_lock:
            state = await self._read()
            intent = state["intents"].get(intent_id)
            if intent is None or intent["status"] != "pending":
                return
            intent.update(
                {
                    "status": "awaiting_confirmation",
                    "site_label": site_label,
                    "account_label": account_label,
                }
            )
            state["device"]["server_origin"] = origin
            state = await self._write(state)
        await self._notify(state)

    async def _expire_pair(self, intent_id: str) -> None:
        async with self._mutation_lock:
            state = await self._read()
            pending = state["pending_pairs"].pop(intent_id, None)
            intent = state["intents"].get(intent_id)
            if intent is not None:
                intent["status"] = "expired"
            state["connection_state"] = "unpaired"
            state = await self._write(state)
        if pending is not None:
            await asyncio.to_thread(
                self._identity.delete_secret,
                pending["token_account"],
            )
        await self._notify(state)

    async def approve(self, intent_id: str) -> dict:
        if not (
            PATTERNS["pair_intent"].fullmatch(intent_id)
            or PATTERNS["action"].fullmatch(intent_id)
        ):
            raise MarketplaceBridgeError("Marketplace intent id is invalid")
        state = await self._read()
        intent = state["intents"].get(intent_id)
        if intent is None:
            raise MarketplaceBridgeError("Marketplace intent not found")
        if intent["status"] != "awaiting_confirmation":
            raise MarketplaceBridgeError(
                "Marketplace intent is not awaiting confirmation"
            )
        if intent["action"] == "pair":
            await self._approve_pair(intent_id)
        else:
            await self._approve_action(intent_id)
        self._wake.set()
        return self.snapshot()

    async def _approve_pair(self, intent_id: str) -> None:
        async with self._mutation_lock:
            state = await self._read()
            intent = state["intents"].get(intent_id)
            pending = state["pending_pairs"].get(intent_id)
            if (
                intent is None
                or pending is None
                or intent["status"] != "awaiting_confirmation"
            ):
                raise MarketplaceBridgeError("Marketplace pair intent is unavailable")
            token = await asyncio.to_thread(
                self._identity.get_secret,
                pending["token_account"],
            )
            if token is None:
                raise MarketplaceBridgeError("Marketplace pair intent expired")
            device, identity = await asyncio.to_thread(
                self._identity.create_or_load,
                state["device"],
            )
            response = await marketplace_service.protocol_pair(
                {
                    "pair_token": token.strip(),
                    "protocol_hash": PROTOCOL_HASH,
                    "device_id": identity.device_id,
                    "public_key": identity.public_key,
                    "label": identity.label,
                    "pop_signature": self._identity.sign_pair(identity, token.strip()),
                }
            )
            if (
                response.get("protocol_version") != 1
                or response.get("protocol_hash") != PROTOCOL_HASH
                or response.get("device_id") != identity.device_id
                or response.get("paired") is not True
                or _server_origin(response.get("server_origin"))
                != device["server_origin"]
                or device["server_origin"] != marketplace_service.protocol_origin()
            ):
                raise MarketplaceBridgeError("Marketplace pair response is invalid")
            device["paired"] = True
            device["revoked"] = False
            device["revocation_pending"] = False
            intent["status"] = "redeemed"
            state["connection_state"] = "connected"
            state["pending_pairs"].pop(intent_id, None)
            state = await self._write(state)
        await asyncio.to_thread(
            self._identity.delete_secret,
            pending["token_account"],
        )
        await self._notify(state)

    async def reject(self, intent_id: str) -> dict:
        async with self._mutation_lock:
            state = await self._read()
            intent = state["intents"].get(intent_id)
            if intent is None:
                raise MarketplaceBridgeError("Marketplace intent not found")
            if intent["status"] != "awaiting_confirmation":
                raise MarketplaceBridgeError(
                    "Marketplace intent is not awaiting confirmation"
                )
            if intent["action"] == "pair":
                pending = state["pending_pairs"].pop(intent_id, None)
                token = (
                    await asyncio.to_thread(
                        self._identity.get_secret,
                        pending["token_account"],
                    )
                    if pending is not None
                    else None
                )
                intent["status"] = "rejected"
                state["connection_state"] = "unpaired"
                state = await self._write(state)
            else:
                intent["status"] = "rejected"
                intent["rejection_pending"] = True
                state = await self._write(state)
                pending = None
                token = None
        await self._notify(state)
        if pending is not None:
            try:
                if token is not None:
                    await marketplace_service.protocol_pair_reject(token.strip())
            finally:
                await asyncio.to_thread(
                    self._identity.delete_secret,
                    pending["token_account"],
                )
        elif intent["action"] != "pair":
            await self._flush_rejection(intent_id)
        self._wake.set()
        return self.snapshot()

    async def _challenge(self, device_id: str) -> str:
        response = await marketplace_service.protocol_challenges(device_id)
        challenges = response.get("challenges")
        _future_timestamp(response.get("expires_at"), "challenge expiry")
        if (
            not isinstance(challenges, list)
            or not 1 <= len(challenges) <= PROTOCOL["bounds"]["challenge_batch_max"]
        ):
            raise MarketplaceBridgeError("Marketplace challenge batch is invalid")
        return require_identifier("challenge", challenges[0])

    async def _signed_operation(
        self,
        state: dict,
        operation: str,
        path_values: dict[str, str],
        body: dict[str, Any],
        *,
        allow_revoked: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        device, identity = await asyncio.to_thread(
            self._identity.create_or_load,
            state["device"],
        )
        if device["revoked"] and not allow_revoked:
            raise MarketplaceBridgeError("Marketplace device is revoked")
        challenge = await self._challenge(identity.device_id)
        return self._identity.sign_device_request(
            identity,
            operation,
            path_values,
            body,
            challenge,
            device["server_origin"],
        )

    def _extension_state(self, extension_id: str) -> dict:
        record = extension_store.get_extension(extension_id)
        if record is None:
            return {"exists": False}
        return {
            "exists": True,
            "source_type": str((record.get("source") or {}).get("type") or ""),
            "version": str((record.get("manifest") or {}).get("version") or ""),
            "enabled": record.get("enabled") is True,
        }

    def _validate_action(self, raw: object) -> dict | None:
        if raw is None:
            return None
        try:
            leased = validate_leased_action(raw)
        except ValueError as exc:
            raise MarketplaceBridgeError(str(exc)) from exc
        action_id = leased["action_id"]
        action = leased["action_type"]
        extension = leased["extension"]
        expected_version = leased["target_version"]
        catalog_snapshot_sha256 = leased["catalog_snapshot_sha256"]
        envelope = {
            "action_id": action_id,
            "action_type": action,
            "extension_id": extension["id"],
            "target_version": expected_version,
            "catalog_snapshot_sha256": catalog_snapshot_sha256,
        }
        precondition = self._extension_state(extension["id"])
        if action == "install" and precondition["exists"]:
            raise MarketplaceBridgeError(
                "Marketplace install target is already installed"
            )
        if action != "install" and (
            not precondition["exists"]
            or precondition.get("source_type") != "marketplace"
        ):
            raise MarketplaceBridgeError(
                "Marketplace action target is not an installed Marketplace extension"
            )
        desired = self._desired_state(action, expected_version, precondition)
        return {
            "intent_id": action_id,
            "action": action,
            "status": "awaiting_confirmation",
            "extension": extension,
            "lease_capability": leased["lease_capability"],
            "lease_capability_account": "",
            "lease_capability_ready": False,
            "lease_cleanup_pending": False,
            "lease_expires_at": leased["lease_expires_at"],
            "target_version": expected_version,
            "catalog_snapshot_sha256": catalog_snapshot_sha256,
            "envelope_digest": canonical_hash(envelope),
            "precondition": precondition,
            "desired": desired,
            "rejection_pending": False,
            "error": "",
            "created_at": _now(),
        }

    @staticmethod
    def _desired_state(
        action: str,
        expected_version: str,
        precondition: dict,
    ) -> dict:
        if action == "uninstall":
            return {"exists": False}
        if action == "enable":
            return {**precondition, "enabled": True}
        if action == "disable":
            return {**precondition, "enabled": False}
        if action == "install":
            return {
                "exists": True,
                "source_type": "marketplace",
                "version": expected_version,
                "enabled": False,
            }
        return {
            "exists": True,
            "source_type": "marketplace",
            "version": expected_version,
            "enabled": bool(precondition.get("enabled")),
        }

    async def _resolve_action_metadata(
        self,
        intent: dict,
        receipt: dict,
    ) -> dict:
        terminal_capability = await asyncio.to_thread(
            self._identity.get_secret,
            receipt["terminal_capability_account"],
        )
        if terminal_capability is None:
            raise MarketplaceBridgeError(
                "Marketplace terminal capability is unavailable"
            )
        action_id = intent["intent_id"]
        metadata = await marketplace_service.protocol_action_metadata(
            action_id,
            terminal_capability,
        )
        expected = {
            "extension_id": intent["extension"]["id"],
            "version": receipt["expected_version"],
            "catalog_snapshot_sha256": receipt["catalog_snapshot_sha256"],
        }
        actual = {
            "extension_id": metadata.get("extension_id"),
            "version": metadata.get("version"),
            "catalog_snapshot_sha256": metadata.get("catalog_snapshot_sha256"),
        }
        if actual != expected:
            raise MarketplaceBridgeError(
                "Marketplace action metadata does not match the approved catalog"
            )
        return metadata

    async def _approve_action(self, intent_id: str) -> None:
        async with self._mutation_lock:
            state = await self._read()
            intent = deepcopy(state["intents"].get(intent_id))
            device = deepcopy(state["device"])
            if intent is None or intent["status"] != "awaiting_confirmation":
                raise MarketplaceBridgeError("Marketplace action is unavailable")
            if (
                not isinstance(device, dict)
                or device["revoked"]
                or not device["paired"]
            ):
                raise MarketplaceBridgeError("Marketplace device is unavailable")
            if (
                not intent["lease_capability_ready"]
                or intent["lease_cleanup_pending"]
                or datetime.fromisoformat(
                    intent["lease_expires_at"].replace("Z", "+00:00")
                )
                <= datetime.now(timezone.utc)
            ):
                raise MarketplaceBridgeError("Marketplace action lease is unavailable")
            epoch = device["epoch"]
        async with self._mutation_lock:
            state = await self._read()
            current_device = state["device"]
            current_intent = state["intents"].get(intent_id)
            if (
                not isinstance(current_device, dict)
                or current_device["epoch"] != epoch
                or current_device["revoked"]
                or current_intent is None
                or current_intent["status"] != "awaiting_confirmation"
            ):
                raise MarketplaceBridgeError(
                    "Marketplace device changed while preparing action"
                )
            receipt_revision = state["revision"] + 1
            state["receipts"][intent_id] = {
                "action_id": intent_id,
                "intent_id": intent_id,
                "device_id": device["device_id"],
                "device_epoch": epoch,
                "envelope_digest": intent["envelope_digest"],
                "receipt_revision": receipt_revision,
                "action": intent["action"],
                "extension_id": intent["extension"]["id"],
                "expected_version": intent["target_version"],
                "catalog_snapshot_sha256": intent["catalog_snapshot_sha256"],
                "precondition": deepcopy(intent["precondition"]),
                "desired": deepcopy(intent["desired"]),
                "phase": "fence_pending",
                "terminal_capability_account": "",
                "terminal_result": None,
                "ack_status": "pending",
                "created_at": _now(),
                "reconcile_deadline": "",
            }
            current_intent["status"] = "committing"
            state = await self._write(state)
        await self._notify(state)
        await self._complete_pending_fence(intent_id)
        await self._run_receipt(intent_id)

    async def _complete_pending_fence(self, intent_id: str) -> None:
        async with self._mutation_lock:
            state = await self._read()
            receipt = deepcopy(state["receipts"].get(intent_id))
            intent = deepcopy(state["intents"].get(intent_id))
            device = deepcopy(state["device"])
            if receipt is None or receipt["phase"] != "fence_pending":
                return
            device_matches_receipt = (
                isinstance(device, dict)
                and device["device_id"] == receipt["device_id"]
                and (
                    (
                        not device["revoked"]
                        and device["epoch"] == receipt["device_epoch"]
                    )
                    or (
                        device["revoked"]
                        and device["revocation_pending"]
                        and device["epoch"] == receipt["device_epoch"] + 1
                    )
                )
            )
            if (
                intent is None
                or intent["status"] != "committing"
                or not device_matches_receipt
                or not intent["lease_capability_ready"]
                or intent["lease_cleanup_pending"]
            ):
                raise MarketplaceBridgeError(
                    "Marketplace device changed while fencing action"
                )
            lease_capability_account = intent["lease_capability_account"]
        lease_capability = await asyncio.to_thread(
            self._identity.get_secret,
            lease_capability_account,
        )
        if lease_capability is None:
            raise MarketplaceBridgeError("Marketplace lease capability is unavailable")
        fence_body = {
            "protocol_hash": PROTOCOL_HASH,
            "lease_capability": lease_capability,
        }
        _, signed = await self._signed_operation(
            state,
            "fence",
            {"device_id": device["device_id"], "action_id": intent_id},
            fence_body,
            allow_revoked=device["revoked"],
        )
        fenced = await marketplace_service.protocol_fence(
            device["device_id"],
            intent_id,
            signed,
        )
        terminal_capability = _bounded_text(
            fenced.get("terminal_capability"),
            "terminal_capability",
        )
        reconcile_deadline = _future_timestamp(
            fenced.get("reconcile_deadline"),
            "reconcile_deadline",
        )
        async with self._mutation_lock:
            state = await self._read()
            current_receipt = state["receipts"].get(intent_id)
            if current_receipt is None or current_receipt["phase"] != "fence_pending":
                return
            if (
                current_receipt["receipt_revision"] != receipt["receipt_revision"]
                or current_receipt["device_epoch"] != receipt["device_epoch"]
            ):
                raise MarketplaceBridgeError(
                    "Marketplace receipt changed while fencing"
                )
            capability_account = await asyncio.to_thread(
                self._identity.store_terminal_capability,
                intent_id,
                terminal_capability,
            )
            current_receipt["phase"] = "fenced"
            current_receipt["terminal_capability_account"] = capability_account
            current_receipt["reconcile_deadline"] = reconcile_deadline
            current_intent = state["intents"].get(intent_id)
            if current_intent is not None:
                current_intent["lease_capability_ready"] = False
                current_intent["lease_cleanup_pending"] = True
            state = await self._write(state)
        state = await self._cleanup_lease_capability(intent_id)
        await self._notify(state)

    async def _execute_receipt(
        self,
        state: dict,
        receipt: dict,
        *,
        metadata: dict | None = None,
    ) -> None:
        action = receipt["action"]
        extension_id = receipt["extension_id"]
        if action in {"install", "update"}:
            if metadata is None:
                intent = state["intents"][receipt["intent_id"]]
                metadata = await self._resolve_action_metadata(
                    intent,
                    receipt,
                )
        if datetime.fromisoformat(
            receipt["reconcile_deadline"].replace("Z", "+00:00")
        ) <= datetime.now(timezone.utc):
            raise MarketplaceBridgeError(
                "Marketplace terminal capability is expired"
            )
        if action in {"install", "update"}:
            if action == "install":
                await marketplace_service.install_exact(
                    extension_id,
                    metadata,
                    expected_version=receipt["expected_version"],
                )
                await asyncio.to_thread(
                    extension_store.set_enabled,
                    extension_id,
                    False,
                    required_source_type="marketplace",
                )
                return
            await marketplace_service.update_exact(
                extension_id,
                metadata,
                expected_version=receipt["expected_version"],
            )
            return
        if action in {"enable", "disable"}:
            await asyncio.to_thread(
                extension_store.set_enabled,
                extension_id,
                action == "enable",
                required_source_type="marketplace",
            )
            return
        if action == "uninstall":
            await asyncio.to_thread(
                extension_store.uninstall,
                extension_id,
                required_source_type="marketplace",
            )
            return
        raise MarketplaceBridgeError("Marketplace action is not allowed")

    async def _run_receipt(
        self,
        action_id: str,
        *,
        metadata: dict | None = None,
    ) -> None:
        async with self._mutation_lock:
            state = await self._read()
            receipt = state["receipts"].get(action_id)
            if receipt is None or receipt["phase"] == "terminal":
                return
            if datetime.fromisoformat(
                receipt["reconcile_deadline"].replace("Z", "+00:00")
            ) <= datetime.now(timezone.utc):
                outcome = result_code = "failed_unknown"
                execute = False
            else:
                actual = await asyncio.to_thread(
                    self._extension_state,
                    receipt["extension_id"],
                )
                if actual == receipt["desired"]:
                    outcome, result_code, execute = "succeeded", "ok", False
                elif actual == receipt["precondition"]:
                    receipt["phase"] = "effect_started"
                    state = await self._write(state)
                    outcome = result_code = ""
                    execute = True
                else:
                    outcome = result_code = "failed_unknown"
                    execute = False
        if execute:
            try:
                await self._execute_receipt(state, receipt, metadata=metadata)
            except Exception as exc:
                actual = await asyncio.to_thread(
                    self._extension_state,
                    receipt["extension_id"],
                )
                deadline_expired = datetime.fromisoformat(
                    receipt["reconcile_deadline"].replace("Z", "+00:00")
                ) <= datetime.now(timezone.utc)
                if deadline_expired:
                    outcome = result_code = "failed_unknown"
                elif actual == receipt["desired"]:
                    outcome, result_code = "succeeded", "ok"
                elif actual == receipt["precondition"]:
                    outcome, result_code = "failed", "local_mutation_failed"
                else:
                    outcome = result_code = "failed_unknown"
                async with self._mutation_lock:
                    current = await self._read()
                    intent = current["intents"].get(action_id)
                    if intent is not None:
                        intent["error"] = str(exc)[:500]
                    current = await self._write(current)
                await self._notify(current)
            else:
                deadline_expired = datetime.fromisoformat(
                    receipt["reconcile_deadline"].replace("Z", "+00:00")
                ) <= datetime.now(timezone.utc)
                outcome, result_code = (
                    ("failed_unknown", "failed_unknown")
                    if deadline_expired
                    else ("succeeded", "ok")
                )
                async with self._mutation_lock:
                    current = await self._read()
                    current_receipt = current["receipts"].get(action_id)
                    if current_receipt is not None:
                        current_receipt["phase"] = "effect_applied"
                        current = await self._write(current)
                if self._on_extensions_change is not None:
                    await self._on_extensions_change()
                await self._publish_projection()
        await self._finalize_receipt(action_id, outcome, result_code)

    async def _finalize_receipt(
        self,
        action_id: str,
        outcome: str,
        result_code: str,
    ) -> None:
        async with self._mutation_lock:
            state = await self._read()
            receipt = state["receipts"].get(action_id)
            if receipt is None:
                return
            deadline_expired = datetime.fromisoformat(
                receipt["reconcile_deadline"].replace("Z", "+00:00")
            ) <= datetime.now(timezone.utc)
            if deadline_expired:
                outcome = result_code = "failed_unknown"
            terminal_result = {
                "outcome": outcome,
                "result_code": result_code,
            }
            if (
                receipt["terminal_result"] is not None
                and receipt["terminal_result"] != terminal_result
                and not deadline_expired
            ):
                receipt["ack_status"] = "conflict"
                state = await self._write(state)
                conflict = True
                capability = None
            else:
                conflict = False
                receipt["terminal_result"] = terminal_result
                receipt["phase"] = "terminal"
                state["tombstones"][action_id] = {
                    "envelope_digest": receipt["envelope_digest"],
                    "terminal_result": deepcopy(terminal_result),
                    "receipt_revision": receipt["receipt_revision"],
                    "created_at": _now(),
                }
                intent = state["intents"].get(action_id)
                if intent is not None:
                    intent["status"] = outcome
                state = await self._write(state)
                capability = await asyncio.to_thread(
                    self._identity.get_secret,
                    receipt["terminal_capability_account"],
                )
        if conflict:
            await self._notify(state)
            raise MarketplaceBridgeError("Marketplace terminal ack conflicts")
        if capability is None:
            raise MarketplaceBridgeError(
                "Marketplace terminal capability is unavailable"
            )
        device = state["device"]
        if (
            not isinstance(device, dict)
            or device["device_id"] != receipt["device_id"]
        ):
            raise MarketplaceBridgeError("Marketplace receipt device is unavailable")
        _, signed = await self._signed_operation(
            state,
            "ack",
            {"device_id": device["device_id"], "action_id": action_id},
            {
                "protocol_hash": PROTOCOL_HASH,
                "capability": capability,
                "outcome": outcome,
                "result_code": result_code,
            },
            allow_revoked=device["revoked"] and device["revocation_pending"],
        )
        try:
            response = await marketplace_service.protocol_ack(
                device["device_id"],
                action_id,
                signed,
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) != 409:
                raise
            current_state = await self._read()
            current_receipt = current_state["receipts"].get(action_id)
            if (
                outcome != "failed_unknown"
                and current_receipt is not None
                and datetime.fromisoformat(
                    current_receipt["reconcile_deadline"].replace("Z", "+00:00")
                ) <= datetime.now(timezone.utc)
            ):
                await self._finalize_receipt(
                    action_id,
                    "failed_unknown",
                    "failed_unknown",
                )
                return
            async with self._mutation_lock:
                state = await self._read()
                current = state["receipts"].get(action_id)
                if current is not None:
                    current["ack_status"] = "conflict"
                    state = await self._write(state)
            await self._notify(state)
            raise MarketplaceBridgeError("Marketplace terminal ack conflicts") from exc
        if (
            response.get("outcome") != outcome
            or response.get("result_code") != result_code
        ):
            current_state = await self._read()
            current_receipt = current_state["receipts"].get(action_id)
            if (
                outcome != "failed_unknown"
                and current_receipt is not None
                and datetime.fromisoformat(
                    current_receipt["reconcile_deadline"].replace("Z", "+00:00")
                ) <= datetime.now(timezone.utc)
            ):
                await self._finalize_receipt(
                    action_id,
                    "failed_unknown",
                    "failed_unknown",
                )
                return
            async with self._mutation_lock:
                state = await self._read()
                current = state["receipts"].get(action_id)
                if current is not None:
                    current["ack_status"] = "conflict"
                    state = await self._write(state)
            await self._notify(state)
            raise MarketplaceBridgeError("Marketplace terminal ack conflicts")
        async with self._mutation_lock:
            state = await self._read()
            current = state["receipts"].get(action_id)
            if current is not None:
                current["ack_status"] = "acked"
                account = current["terminal_capability_account"]
                state["receipts"].pop(action_id, None)
                state = await self._write(state)
            else:
                account = ""
        if account:
            await asyncio.to_thread(self._identity.delete_secret, account)
        await self._maybe_delete_revoked_key(state)
        await self._notify(state)
        self._wake.set()

    async def _cleanup_lease_capability(self, action_id: str) -> dict:
        async with self._mutation_lock:
            state = await self._read()
            intent = state["intents"].get(action_id)
            if intent is None or not intent["lease_capability_account"]:
                return state
            account = intent["lease_capability_account"]
            intent["lease_capability_ready"] = False
            intent["lease_cleanup_pending"] = True
            state = await self._write(state)
        await asyncio.to_thread(self._identity.delete_secret, account)
        async with self._mutation_lock:
            state = await self._read()
            intent = state["intents"].get(action_id)
            if (
                intent is not None
                and intent["lease_cleanup_pending"]
                and intent["lease_capability_account"] == account
            ):
                intent["lease_capability_account"] = ""
                intent["lease_cleanup_pending"] = False
                state = await self._write(state)
        return state

    async def _reconcile_lease_capability_journals(self) -> None:
        state = await self._read()
        for action_id, intent in list(state["intents"].items()):
            account = intent.get("lease_capability_account")
            if not account:
                continue
            if intent["lease_cleanup_pending"]:
                state = await self._cleanup_lease_capability(action_id)
                await self._notify(state)
                continue
            receipt = state["receipts"].get(action_id)
            cleanup_ready = intent["lease_capability_ready"] and (
                receipt is not None and receipt["phase"] != "fence_pending"
                or intent["status"] == "rejected"
                and not intent["rejection_pending"]
            )
            if cleanup_ready:
                state = await self._cleanup_lease_capability(action_id)
                await self._notify(state)
                continue
            if intent["lease_capability_ready"]:
                continue
            await asyncio.to_thread(self._identity.delete_secret, account)
            async with self._mutation_lock:
                state = await self._read()
                current = state["intents"].get(action_id)
                if (
                    current is not None
                    and not current["lease_capability_ready"]
                    and not current["lease_cleanup_pending"]
                    and current["lease_capability_account"] == account
                    and action_id not in state["receipts"]
                ):
                    state["intents"].pop(action_id)
                    state = await self._write(state)
            await self._notify(state)

    async def _stage_leased_action(self, intent: dict, device_epoch: int) -> None:
        lease_capability = intent.pop("lease_capability")
        action_id = intent["intent_id"]
        account = self._identity.lease_capability_account(action_id)
        async with self._mutation_lock:
            state = await self._read()
            device = state["device"]
            if (
                not isinstance(device, dict)
                or device["epoch"] != device_epoch
                or device["revoked"]
                or not device["paired"]
            ):
                return
            tombstone = state["tombstones"].get(action_id)
            if tombstone is not None:
                if tombstone["envelope_digest"] != intent["envelope_digest"]:
                    raise MarketplaceBridgeError(
                        "Marketplace action id conflicts with tombstone"
                    )
                raise MarketplaceBridgeError(
                    "Marketplace server re-leased a terminal action"
                )
            existing = state["intents"].get(action_id)
            if (
                existing is not None
                and existing["envelope_digest"] != intent["envelope_digest"]
            ):
                raise MarketplaceBridgeError(
                    "Marketplace action id has a conflicting envelope"
                )
            renew_rejection = bool(
                existing is not None
                and existing["status"] == "rejected"
                and existing["rejection_pending"]
            )
            if existing is not None and not renew_rejection:
                raise MarketplaceBridgeError(
                    "Marketplace server re-leased an active action"
                )
            if renew_rejection and existing["lease_cleanup_pending"]:
                raise MarketplaceBridgeError(
                    "Marketplace lease capability cleanup is pending"
                )
            if not renew_rejection:
                intent["lease_capability_account"] = account
                state["intents"][action_id] = intent
                await self._write(state)
        if renew_rejection:
            stored_account = await asyncio.to_thread(
                self._identity.store_lease_capability,
                action_id,
                lease_capability,
            )
            if stored_account != account:
                raise MarketplaceBridgeError(
                    "Marketplace lease capability account changed"
                )
            async with self._mutation_lock:
                state = await self._read()
                current = state["intents"].get(action_id)
                if (
                    current is None
                    or current["envelope_digest"] != intent["envelope_digest"]
                    or current["status"] != "rejected"
                    or not current["rejection_pending"]
                ):
                    raise MarketplaceBridgeError(
                        "Marketplace rejection changed during lease renewal"
                    )
                current["lease_capability_account"] = account
                current["lease_capability_ready"] = True
                current["lease_cleanup_pending"] = False
                current["lease_expires_at"] = intent["lease_expires_at"]
                current["extension"] = intent["extension"]
                state = await self._write(state)
            await self._notify(state)
            await self._flush_rejection(action_id)
            return
        try:
            stored_account = await asyncio.to_thread(
                self._identity.store_lease_capability,
                action_id,
                lease_capability,
            )
            if stored_account != account:
                raise MarketplaceBridgeError(
                    "Marketplace lease capability account changed"
                )
        except Exception:
            await asyncio.to_thread(self._identity.delete_secret, account)
            async with self._mutation_lock:
                state = await self._read()
                current = state["intents"].get(action_id)
                if (
                    current is not None
                    and current["envelope_digest"] == intent["envelope_digest"]
                    and not current["lease_capability_ready"]
                ):
                    state["intents"].pop(action_id)
                    await self._write(state)
            raise
        async with self._mutation_lock:
            state = await self._read()
            current = state["intents"].get(action_id)
            if (
                current is None
                or current["envelope_digest"] != intent["envelope_digest"]
                or current["lease_capability_account"] != account
                or current["lease_cleanup_pending"]
            ):
                raise MarketplaceBridgeError(
                    "Marketplace action changed during lease persistence"
                )
            current["lease_capability_ready"] = True
            state = await self._write(state)
        await self._notify(state)

    async def _flush_rejection(self, action_id: str) -> None:
        async with self._mutation_lock:
            state = await self._read()
            intent = deepcopy(state["intents"].get(action_id))
            device = deepcopy(state["device"])
            if (
                intent is None
                or not intent.get("rejection_pending")
                or not isinstance(device, dict)
                or (device["revoked"] and not device["revocation_pending"])
            ):
                return
            if (
                not intent["lease_capability_ready"]
                or intent["lease_cleanup_pending"]
                or datetime.fromisoformat(
                    intent["lease_expires_at"].replace("Z", "+00:00")
                )
                <= datetime.now(timezone.utc)
            ):
                return
            epoch = device["epoch"]
            lease_capability_account = intent["lease_capability_account"]
        lease_capability = await asyncio.to_thread(
            self._identity.get_secret,
            lease_capability_account,
        )
        if lease_capability is None:
            raise MarketplaceBridgeError("Marketplace lease capability is unavailable")
        _, signed = await self._signed_operation(
            state,
            "ack",
            {"device_id": device["device_id"], "action_id": action_id},
            {
                "protocol_hash": PROTOCOL_HASH,
                "capability": lease_capability,
                "outcome": "rejected",
                "result_code": "user_rejected",
            },
            allow_revoked=device["revoked"] and device["revocation_pending"],
        )
        response = await marketplace_service.protocol_ack(
            device["device_id"],
            action_id,
            signed,
        )
        if (
            response.get("outcome") != "rejected"
            or response.get("result_code") != "user_rejected"
        ):
            raise MarketplaceBridgeError("Marketplace rejection ack conflicts")
        async with self._mutation_lock:
            state = await self._read()
            current_device = state["device"]
            current = state["intents"].get(action_id)
            if (
                isinstance(current_device, dict)
                and current_device["epoch"] == epoch
                and current is not None
            ):
                current["rejection_pending"] = False
                current["lease_capability_ready"] = False
                current["lease_cleanup_pending"] = True
                state = await self._write(state)
        state = await self._cleanup_lease_capability(action_id)
        await self._notify(state)

    async def _publish_projection(self) -> None:
        async with self._mutation_lock:
            state = await self._read()
            device = deepcopy(state["device"])
            if (
                not isinstance(device, dict)
                or device["revoked"]
                or not device["paired"]
            ):
                return
            epoch = device["epoch"]
            revision = state["projection_revision"] + 1
        extensions = []
        for record in await asyncio.to_thread(extension_store.list_extensions):
            if (record.get("source") or {}).get("type") != "marketplace":
                continue
            manifest = record.get("manifest") or {}
            extensions.append(
                {
                    "id": manifest.get("id"),
                    "version": manifest.get("version"),
                    "enabled": record.get("enabled") is True,
                }
            )
        extensions.sort(key=lambda item: str(item["id"]))
        _, signed = await self._signed_operation(
            state,
            "projection",
            {"device_id": device["device_id"]},
            {
                "protocol_hash": PROTOCOL_HASH,
                "revision": revision,
                "extensions": extensions,
            },
        )
        await marketplace_service.protocol_projection(device["device_id"], signed)
        async with self._mutation_lock:
            state = await self._read()
            current = state["device"]
            if (
                isinstance(current, dict)
                and current["epoch"] == epoch
                and not current["revoked"]
            ):
                state["projection_revision"] = revision
                state = await self._write(state)
        await self._notify(state)

    async def revoke(self, device_id: str) -> dict:
        require_identifier("device", device_id)
        cancelled_action_ids: list[str] = []
        async with self._mutation_lock:
            state = await self._read()
            device = state["device"]
            if not isinstance(device, dict) or device["device_id"] != device_id:
                raise MarketplaceBridgeError("Marketplace device not found")
            if not device["revoked"]:
                device["epoch"] += 1
                device["paired"] = False
                device["revoked"] = True
                device["revocation_pending"] = True
                state["connection_state"] = "unpaired"
                for intent_id, intent in state["intents"].items():
                    if (
                        intent["action"] != "pair"
                        and intent["status"] == "awaiting_confirmation"
                    ):
                        intent["status"] = "cancelled"
                        cancelled_action_ids.append(intent_id)
                state = await self._write(state)
        await self._notify(state)
        for action_id in cancelled_action_ids:
            state = await self._cleanup_lease_capability(action_id)
            await self._notify(state)
        self._wake.set()
        await self._reconcile_revocation()
        return self.snapshot()

    async def _reconcile_revocation(self) -> None:
        await self._abandon_expired_rejections_for_revocation()
        async with self._mutation_lock:
            state = await self._read()
            device = deepcopy(state["device"])
            if (
                not isinstance(device, dict)
                or not device["revoked"]
                or not device["revocation_pending"]
            ):
                return
            if state["receipts"] or any(
                intent.get("rejection_pending")
                for intent in state["intents"].values()
            ):
                return
            _, identity = await asyncio.to_thread(
                self._identity.create_or_load,
                state["device"],
            )
        challenge = await self._challenge(device["device_id"])
        path, signed = self._identity.sign_device_request(
            identity,
            "revoke",
            {"device_id": device["device_id"]},
            {"protocol_hash": PROTOCOL_HASH},
            challenge,
            device["server_origin"],
        )
        if path != f"/protocol/v1/devices/{device['device_id']}/revoke":
            raise MarketplaceBridgeError("Marketplace revoke path is invalid")
        response = await marketplace_service.protocol_revoke(
            device["device_id"],
            signed,
        )
        if (
            response.get("device_id") != device["device_id"]
            or response.get("revoked") is not True
        ):
            raise MarketplaceBridgeError("Marketplace revoke response is invalid")
        async with self._mutation_lock:
            state = await self._read()
            current = state["device"]
            if (
                isinstance(current, dict)
                and current["device_id"] == device["device_id"]
                and current["revoked"]
            ):
                current["revocation_pending"] = False
                state = await self._write(state)
        await self._maybe_delete_revoked_key(state)
        await self._notify(state)

    async def _abandon_expired_rejections_for_revocation(self) -> None:
        expired_action_ids: list[str] = []
        async with self._mutation_lock:
            state = await self._read()
            device = state["device"]
            if (
                not isinstance(device, dict)
                or not device["revoked"]
                or not device["revocation_pending"]
            ):
                return
            now = datetime.now(timezone.utc)
            for action_id, intent in state["intents"].items():
                if (
                    intent.get("rejection_pending")
                    and datetime.fromisoformat(
                        intent["lease_expires_at"].replace("Z", "+00:00")
                    )
                    <= now
                ):
                    intent["rejection_pending"] = False
                    if intent["lease_capability_account"]:
                        intent["lease_capability_ready"] = False
                        intent["lease_cleanup_pending"] = True
                        expired_action_ids.append(action_id)
            if expired_action_ids:
                state = await self._write(state)
        for action_id in expired_action_ids:
            state = await self._cleanup_lease_capability(action_id)
            await self._notify(state)

    async def _maybe_delete_revoked_key(self, state: dict) -> None:
        device = state["device"]
        if (
            isinstance(device, dict)
            and device["revoked"]
            and not device["revocation_pending"]
            and not state["receipts"]
        ):
            await asyncio.to_thread(self._identity.delete_private_key)

    async def _lease_next(self) -> None:
        async with self._mutation_lock:
            state = await self._read()
            device = deepcopy(state["device"])
            if (
                not isinstance(device, dict)
                or device["revoked"]
                or not device["paired"]
                or any(
                    intent["status"] in _ACTIVE_INTENT_STATES
                    for intent in state["intents"].values()
                )
            ):
                return
            epoch = device["epoch"]
        _, signed = await self._signed_operation(
            state,
            "lease",
            {"device_id": device["device_id"]},
            {
                "protocol_hash": PROTOCOL_HASH,
                "wait_seconds": PROTOCOL["bounds"]["lease_wait_seconds_max"],
            },
        )
        payload = await marketplace_service.protocol_lease(
            device["device_id"],
            signed,
        )
        action = self._validate_action(payload.get("action"))
        async with self._mutation_lock:
            state = await self._read()
            current = state["device"]
            if (
                not isinstance(current, dict)
                or current["epoch"] != epoch
                or current["revoked"]
                or not current["paired"]
            ):
                return
            state["connection_state"] = "connected"
            state = await self._write(state)
        await self._notify(state)
        if action is not None:
            await self._stage_leased_action(action, epoch)

    async def _reconcile(self) -> None:
        await self._reconcile_lease_capability_journals()
        state = await self._read()
        for intent_id, pending in list(state["pending_pairs"].items()):
            if state["intents"].get(intent_id, {}).get("status") == "pending":
                try:
                    await self._resolve_pair(intent_id)
                except Exception:
                    pass
        for action_id, receipt in list(state["receipts"].items()):
            try:
                if receipt["phase"] == "fence_pending":
                    await self._complete_pending_fence(action_id)
                    await self._run_receipt(action_id)
                elif receipt["phase"] == "terminal":
                    result = receipt["terminal_result"]
                    await self._finalize_receipt(
                        action_id,
                        result["outcome"],
                        result["result_code"],
                    )
                else:
                    await self._run_receipt(action_id)
            except Exception:
                pass
        for action_id, intent in list(state["intents"].items()):
            if intent.get("rejection_pending"):
                try:
                    await self._flush_rejection(action_id)
                except Exception:
                    pass
        try:
            await self._reconcile_revocation()
        except Exception:
            pass

    async def _can_lease(self) -> bool:
        state = await self._read()
        device = state["device"]
        return bool(
            isinstance(device, dict)
            and device["paired"]
            and not device["revoked"]
            and not any(
                intent["status"] in _ACTIVE_INTENT_STATES
                for intent in state["intents"].values()
            )
        )

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self._reconcile()
            self._wake.clear()
            if not await self._can_lease():
                retry = 5 if await self._needs_reconciliation() else None
                await self._wait_for_wake(timeout=retry)
                continue
            lease = asyncio.create_task(self._lease_next())
            wake = asyncio.create_task(self._wake.wait())
            done, pending = await asyncio.wait(
                {lease, wake},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if lease in done:
                try:
                    await lease
                except Exception:
                    state = await self._read()
                    if state["connection_state"] != "unpaired":
                        state["connection_state"] = "offline"
                        state = await self._write(state)
                        await self._notify(state)
                    await self._wait_for_wake(timeout=5)

    async def _wait_for_wake(self, timeout: float | None = None) -> None:
        wake = asyncio.create_task(self._wake.wait())
        stop = asyncio.create_task(self._stop.wait())
        tasks = {wake, stop}
        if timeout is not None:
            retry = asyncio.create_task(asyncio.sleep(timeout))
            tasks.add(retry)
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled():
                task.exception()

    async def _needs_reconciliation(self) -> bool:
        state = await self._read()
        device = state["device"]
        return bool(
            state["receipts"]
            or any(
                state["intents"].get(intent_id, {}).get("status") == "pending"
                for intent_id in state["pending_pairs"]
            )
            or any(
                intent.get("rejection_pending")
                for intent in state["intents"].values()
            )
            or any(
                intent.get("lease_capability_account")
                and (
                    not intent.get("lease_capability_ready")
                    or intent.get("lease_cleanup_pending")
                )
                for intent in state["intents"].values()
            )
            or (
                isinstance(device, dict)
                and device["revocation_pending"]
            )
        )

    async def start(
        self,
        on_change: Callable[[int], Awaitable[None]],
        on_extensions_change: Callable[[], Awaitable[None]],
    ) -> None:
        if self._task is not None and not self._task.done():
            return
        self._on_change = on_change
        self._on_extensions_change = on_extensions_change
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(),
            name="marketplace-bridge",
        )

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            await task


bridge = MarketplaceBridge()

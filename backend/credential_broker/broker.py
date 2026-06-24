"""Broker core — the single place that turns a consent_id into an executed
operation. This is the in-process logic; the hardened daemon + IPC socket
wrap it, and the MCP/REST/WS surface calls it.

Flow:
  store_secret(ref, value)            user provides the value once
  request_consent(descriptor)         provider proposes the operation
    → validate → resolve sink → enforce provider pin → persist pending
  approve_consent / deny / revoke     user decides (surface layer)
  execute(consent_id, proof)          broker runs the frozen op
    → acquire (atomic) → use-gate by risk → decrypt secret → run executor
    → output-echo guard → audit → return guarded result (NO secret)
"""

from __future__ import annotations

import secrets as _secrets
from typing import Optional

from credential_broker import (
    audit,
    consent_store,
    descriptor as descriptor_mod,
    manifest,
    presence,
    secret_store,
)
from credential_broker.executors import get_executor
from credential_broker.output_guard import OutputEchoError, guard
from credential_broker.sink_resolver import resolve
import password_manager


class BrokerError(Exception):
    """User-facing broker failure (safe to surface; never contains a secret)."""


def _new_id() -> str:
    return _secrets.token_hex(16)


# ── Secret provisioning ──────────────────────────────────────────────────


def _store_secret(value: str) -> str:
    """Encrypt a user-provided secret value; return its non-secret ref."""
    ref = _new_id()
    secret_store.store_secret(ref, value, presence.get_key_provider())
    return ref


def _store_secret_values(values: dict[str, str]) -> dict[str, str]:
    refs: dict[str, str] = {}
    try:
        for name, value in values.items():
            refs[name] = _store_secret(value)
    except Exception:
        for ref in refs.values():
            secret_store.delete_secret(ref)
        raise
    return refs


# ── Consent lifecycle ────────────────────────────────────────────────────


def request_consent(
    *, app_session_id: str, descriptor_raw: dict, allowed_sinks: list[str]
) -> dict:
    """Validate a provider's proposed operation, pin-check it, and persist a
    pending consent. Returns the public consent view (no secret, no raw
    templates beyond the computed sink). Raises BrokerError on rejection —
    rejected requests never become a pending consent the user must triage.
    """
    try:
        norm = descriptor_mod.validate(descriptor_raw)
    except descriptor_mod.DescriptorError as e:
        raise BrokerError(f"invalid descriptor: {e}") from e

    info = resolve(norm)

    try:
        manifest.enforce(info.computed_host, allowed_sinks)
    except manifest.PinViolation as e:
        audit.record(
            "consent_request_rejected",
            provider_id=norm["provider_id"],
            app_session_id=app_session_id,
            computed_host=info.computed_host,
            outcome="pin_violation",
        )
        raise BrokerError(str(e)) from e
    _validate_stored_secret_sources_exist(norm.get("secret_sources") or {})

    consent_id = _new_id()
    rec = consent_store.create(
        consent_id=consent_id,
        app_session_id=app_session_id,
        provider_id=norm["provider_id"],
        descriptor=norm,
        descriptor_hash=descriptor_mod.descriptor_hash(norm),
        sink_public=info.to_public(),
    )
    audit.record(
        "consent_requested",
        consent_id=consent_id,
        provider_id=norm["provider_id"],
        app_session_id=app_session_id,
        computed_host=info.computed_host,
        computed_target=info.computed_target,
        risk=info.risk,
        outcome="pending",
    )
    return consent_store.public_view(rec)


def approve_consent(
    consent_id: str,
    *,
    secret_value: Optional[str] = None,
    secret_values: Optional[dict[str, str]] = None,
) -> tuple[Optional[dict], str]:
    """Approve a pending consent, binding user-provided secret values to it."""
    pending = consent_store.get(consent_id)
    if not pending:
        return None, "missing"
    descriptor = pending.get("descriptor", {})
    expected = list(descriptor.get("secret_names") or ["secret"])
    stored_values = _read_stored_secret_sources(descriptor.get("secret_sources") or {})
    typed_expected = [name for name in expected if name not in stored_values]
    if secret_values is None:
        secret_values = {"secret": secret_value or ""} if secret_value else {}
    _validate_secret_values(typed_expected, secret_values)
    secret_values = {**secret_values, **stored_values}

    secret_refs = _store_secret_values(secret_values)
    secret_ref = secret_refs.get("secret")
    rec, reason = consent_store.approve(
        consent_id,
        secret_ref=secret_ref,
        secret_refs=secret_refs,
    )
    if reason != "ok":
        # consent wasn't approvable — don't leave an orphan secret around.
        for ref in secret_refs.values():
            secret_store.delete_secret(ref)
    audit.record("consent_approved", consent_id=consent_id, outcome=reason)
    return rec, reason


def _validate_secret_values(expected: list[str], values: dict[str, str]) -> None:
    if not isinstance(values, dict):
        raise BrokerError("secret values must be an object")
    expected_set = set(expected)
    got_set = set(values)
    if got_set != expected_set:
        missing = sorted(expected_set - got_set)
        extra = sorted(got_set - expected_set)
        parts = []
        if missing:
            parts.append(f"missing secrets: {', '.join(missing)}")
        if extra:
            parts.append(f"unexpected secrets: {', '.join(extra)}")
        raise BrokerError("; ".join(parts))
    for name, value in values.items():
        if not isinstance(value, str) or not value:
            raise BrokerError(f"secret {name!r} must be non-empty")


def _validate_stored_secret_sources_exist(secret_sources: dict[str, dict]) -> None:
    for name, source in secret_sources.items():
        if source.get("kind") != "password_manager":
            raise BrokerError(f"secret source {name!r} is unsupported")
        try:
            exists = password_manager.has_service_password(
                source.get("service", ""),
                source.get("account", ""),
            )
        except password_manager.PasswordManagerError as e:
            raise BrokerError(f"stored secret {name!r} is invalid") from e
        if not exists:
            raise BrokerError(f"stored secret {name!r} was not found")


def _read_stored_secret_sources(secret_sources: dict[str, dict]) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, source in secret_sources.items():
        if source.get("kind") != "password_manager":
            raise BrokerError(f"secret source {name!r} is unsupported")
        try:
            values[name] = password_manager.get_service_password(
                source.get("service", ""),
                source.get("account", ""),
            )
        except password_manager.PasswordManagerError as e:
            raise BrokerError(f"stored secret {name!r} was not found") from e
    return values


def deny_consent(consent_id: str) -> tuple[Optional[dict], str]:
    rec, reason = consent_store.deny(consent_id)
    audit.record("consent_denied", consent_id=consent_id, outcome=reason)
    return rec, reason


def revoke_consent(consent_id: str) -> tuple[Optional[dict], str]:
    rec, reason = consent_store.revoke(consent_id)
    if reason == "ok" and rec and rec.get("secret_ref"):
        # Revoked: the bound secret can never be used again — drop it.
        secret_store.delete_secret(rec["secret_ref"])
    if reason == "ok" and rec and rec.get("secret_refs"):
        for ref in rec["secret_refs"].values():
            secret_store.delete_secret(ref)
    audit.record("consent_revoked", consent_id=consent_id, outcome=reason)
    return rec, reason


# ── Execute ──────────────────────────────────────────────────────────────


def execute(consent_id: str, *, proof: Optional[str] = None) -> dict:
    """Run the frozen operation for an approved consent. The caller supplies
    ONLY the consent_id (and an optional presence proof) — never a
    descriptor. Returns a guarded result dict with no secret. Raises
    BrokerError on any refusal (fail-closed)."""
    rec, reason = consent_store.acquire_for_execute(consent_id)
    if reason != "ok":
        audit.record("execute_denied", consent_id=consent_id, outcome=reason)
        raise BrokerError(f"consent not usable: {reason}")

    info = rec.get("sink", {})
    risk = info.get("risk", "high")  # missing → high, fail-closed

    gate = presence.get_use_gate()
    if not gate.authorize(risk=risk, consent_id=consent_id, proof=proof):
        audit.record(
            "execute_denied",
            consent_id=consent_id,
            risk=risk,
            outcome="use_gate_denied",
        )
        raise BrokerError("use gate not satisfied (presence required)")

    norm = rec["descriptor"]
    key_provider = presence.get_key_provider()
    try:
        secrets = _read_secret_values(rec, key_provider)
    except KeyError as e:
        raise BrokerError("secret missing") from e

    try:
        executor = get_executor(norm["sink_kind"])
        result = executor.execute(norm, secrets)
        result = guard(result, secrets)
    except OutputEchoError as e:
        audit.record(
            "execute_denied",
            consent_id=consent_id,
            computed_host=info.get("computed_host"),
            outcome="output_echo",
        )
        raise BrokerError(str(e)) from e
    finally:
        # Drop the plaintext reference promptly.
        secrets = {}

    audit.record(
        "execute_ok" if result.ok else "execute_failed",
        consent_id=consent_id,
        computed_host=info.get("computed_host"),
        computed_target=info.get("computed_target"),
        risk=risk,
        outcome="ok" if result.ok else "op_failed",
        status_code=result.status,
    )
    return {
        "ok": result.ok,
        "status": result.status,
        "body": result.body,
        "error": result.error,
    }


def _read_secret_values(rec: dict, key_provider) -> dict[str, str]:
    refs = rec.get("secret_refs")
    if isinstance(refs, dict):
        return {
            name: secret_store.read_secret(ref, key_provider)
            for name, ref in refs.items()
        }
    return {
        "secret": secret_store.read_secret(rec["secret_ref"], key_provider)
    }

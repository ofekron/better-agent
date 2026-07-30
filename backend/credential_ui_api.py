"""Credential-broker UI surface: the consent records the broker extension
renders (list/approve/deny/revoke) and the OS-keychain password-manager
entries it manages on the user's behalf.

Every route is gated on the credential-broker role holding a valid
internal authority, so secret values never cross this boundary without
one. The coordinator is injected by the composition root (see
`configure`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

import extension_store
import internal_guards
from i18n import t

router = APIRouter()
logger = logging.getLogger(__name__)

_coordinator_ref: Any = None


def configure(*, coordinator: Any) -> None:
    """Bind the collaborators this router needs."""
    global _coordinator_ref
    _coordinator_ref = coordinator


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="credential UI API is not configured")
    return _coordinator_ref


def _require_credential_broker_internal(x_internal_token: str) -> None:
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    internal_guards.require_builtin_runtime_extension(
        extension_store.extension_id_for_role('credential-broker')
    )


@router.post("/api/internal/credential-ui/pending")
async def internal_list_pending_credentials(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    from credential_broker import consent_store as _cs

    app_session_id = (body or {}).get("app_session_id")
    pending = await asyncio.to_thread(_cs.list_pending, app_session_id=app_session_id)
    out = [
        _cs.public_view(rec)
        for rec in pending
    ]
    return {"consents": out}


@router.post("/api/internal/credential-ui/approve")
async def internal_approve_credential_consent(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    from credential_broker import broker as _broker

    body = body or {}
    consent_id = str(body.get("consent_id") or "").strip()
    if not consent_id:
        raise HTTPException(status_code=400, detail="consent_id is required")
    secret_values = body.get("secrets")
    secret_value = body.get("secret")
    if secret_values is not None and not isinstance(secret_values, dict):
        raise HTTPException(status_code=400, detail="secrets must be an object")
    try:
        rec, reason = _broker.approve_consent(
            consent_id,
            secret_value=secret_value,
            secret_values=secret_values,
        )
    except _broker.BrokerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if reason == "missing":
        raise HTTPException(status_code=404, detail="consent not found")
    if reason == "expired":
        raise HTTPException(status_code=410, detail="consent expired")
    app_sid = (rec or {}).get("app_session_id")
    await _coordinator().broadcast_credential_consent_changed(app_sid)
    return {"status": reason}


@router.post("/api/internal/credential-ui/deny")
async def internal_deny_credential_consent(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    from credential_broker import broker as _broker

    consent_id = str((body or {}).get("consent_id") or "").strip()
    if not consent_id:
        raise HTTPException(status_code=400, detail="consent_id is required")
    rec, reason = _broker.deny_consent(consent_id)
    if reason == "missing":
        raise HTTPException(status_code=404, detail="consent not found")
    await _coordinator().broadcast_credential_consent_changed(
        (rec or {}).get("app_session_id")
    )
    return {"status": reason}


@router.post("/api/internal/credential-ui/revoke")
async def internal_revoke_credential_consent(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    from credential_broker import broker as _broker

    consent_id = str((body or {}).get("consent_id") or "").strip()
    if not consent_id:
        raise HTTPException(status_code=400, detail="consent_id is required")
    rec, reason = _broker.revoke_consent(consent_id)
    if reason == "missing":
        raise HTTPException(status_code=404, detail="consent not found")
    await _coordinator().broadcast_credential_consent_changed(
        (rec or {}).get("app_session_id")
    )
    return {"status": reason}


@router.post("/api/internal/credential-ui/password-manager/list")
async def internal_list_password_manager_secrets(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    import password_manager

    try:
        return password_manager.list_service_passwords()
    except password_manager.PasswordManagerError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        logger.exception("password manager keychain list failed")
        raise HTTPException(status_code=500, detail="failed to list passwords")


@router.post("/api/internal/credential-ui/password-manager/store")
async def internal_store_password_manager_secret(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    import password_manager

    try:
        stored = password_manager.store_service_password(body or {})
    except password_manager.PasswordManagerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("password manager keychain store failed")
        raise HTTPException(status_code=500, detail="failed to store password")
    return {"status": "stored", **stored}


@router.post("/api/internal/credential-ui/password-manager/delete")
async def internal_delete_password_manager_secret(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_credential_broker_internal(x_internal_token)
    import password_manager

    try:
        deleted = password_manager.delete_service_password(body or {})
    except password_manager.PasswordManagerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("password manager keychain delete failed")
        raise HTTPException(status_code=500, detail="failed to delete password")
    return {"status": "deleted", **deleted}

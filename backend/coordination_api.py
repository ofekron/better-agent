"""The internal gateway to the cross-session coordination lock manager.

Owner-based operations (reattach, list-owned, release-owned, and an
untokenised renew) are restricted to the trusted core runner identity: an
extension principal must present the holder token it was given.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

import coordination
import extension_store
import internal_guards
from i18n import t

router = APIRouter()


@router.post("/api/internal/coordination/lock-ops")
async def internal_coordination_lock_ops(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(
        extension_store.BUILTIN_COORDINATION_EXTENSION_ID
    )
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    raw_owner = body.get("owner") if isinstance(body.get("owner"), dict) else {}
    principal_extension_id = internal_guards.internal_authority_extension_id() or "core"
    requested_op = str(body.get("op") or "").strip().lower().replace("-", "_")
    release = bool(body.get("release") or False)
    renew = bool(body.get("renew") or False)
    validate = bool(body.get("validate") or False)
    reattach = bool(body.get("reattach") or False)
    owned = bool(body.get("owned") or False)
    holder_token = str(body.get("holder_token") or "")
    if not requested_op:
        if release and owned:
            requested_op = "release_owned"
        elif release:
            requested_op = "release"
        elif renew:
            requested_op = "renew"
        elif validate:
            requested_op = "validate"
        elif reattach:
            requested_op = "reattach"
        elif owned:
            requested_op = "list_owned"
        else:
            requested_op = "acquire"
    owner_auth_required = requested_op in {"reattach", "list_owned", "release_owned"} or (
        requested_op == "renew" and not holder_token
    )
    if owner_auth_required and principal_extension_id != "core":
        raise HTTPException(status_code=403, detail="trusted runner identity required for owner-based lock operation")
    owner = {
        **raw_owner,
        "principal_extension_id": principal_extension_id,
        "source": str(raw_owner.get("source") or "internal_coordination_lock_ops"),
    }
    return await coordination.lock_ops(
        key=str(body.get("key") or ""),
        keys=body.get("keys") if isinstance(body.get("keys"), list) else None,
        op=str(body.get("op") or ""),
        release=release,
        renew=renew,
        validate=validate,
        reattach=reattach,
        owned=owned,
        holder_token=holder_token,
        timeout_seconds=body.get("timeout_seconds"),
        lease_seconds=body.get("lease_seconds"),
        owner=owner,
    )

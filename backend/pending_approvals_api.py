"""Fresh-worker creation approvals.

Depends on the coordinator only through the two named capabilities it
actually needs, bound by the composition root. Fails closed: an
unconfigured module serves nothing.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection
from typing import Any, Optional

from fastapi import APIRouter, Body, Header, HTTPException

import internal_guards
from i18n import t
from stores import pending_approvals

router = APIRouter()

_ROLE = "team-orchestration"

_awaited_delegation_ids: Optional[Callable[[], Collection[str]]] = None
_resolve_approval: Optional[Callable[[str, dict], Any]] = None


def configure(
    awaited_delegation_ids: Callable[[], Collection[str]],
    resolve_approval: Callable[[str, dict], Any],
) -> None:
    """Bind the coordinator capabilities this router needs."""
    global _awaited_delegation_ids, _resolve_approval
    _awaited_delegation_ids = awaited_delegation_ids
    _resolve_approval = resolve_approval


def _require_configured() -> tuple[Callable[[], Collection[str]], Callable[[str, dict], Any]]:
    if _awaited_delegation_ids is None or _resolve_approval is None:
        raise HTTPException(status_code=503, detail="pending approvals API is not configured")
    return _awaited_delegation_ids, _resolve_approval


def _resolved_or_error(rec: dict, reason: str) -> Optional[dict]:
    """Map a store outcome to its HTTP result, or None to continue."""
    if reason == "missing":
        raise HTTPException(status_code=404, detail=t("error.approval_not_found"))
    if reason == "expired":
        raise HTTPException(status_code=410, detail=t("error.approval_expired"))
    if reason == "already_resolved":
        # Idempotent — return the existing record so the second tab sees
        # the same answer the first tab got.
        return {"status": rec.get("status"), "record": rec, "idempotent": True}
    return None


def _required_delegation_id(body: dict | None) -> str:
    delegation_id = str((body or {}).get("delegation_id") or "").strip()
    if not delegation_id:
        raise HTTPException(status_code=400, detail="delegation_id is required")
    return delegation_id


# `x_internal_token` is declared so a request missing the header is
# rejected before the handler runs; the header value itself grants
# nothing — authority comes from the bound request principal.
@router.post("/api/internal/pending-approvals/list")
async def internal_list_pending_approvals(
    body: dict | None = None,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Pending fresh-worker creation approvals, optionally filtered by
    cwd. Used by the frontend on WS reconnect to rehydrate inline
    approval cards.

    Filtered to delegations the backend is ACTIVELY awaiting — disk
    records with no in-memory waiter are orphans (left over from a
    crash; the runner already gave up or never came back). Surfacing
    them as cards would invite the user to approve a delegation no one
    is waiting on, spawning a worker for nothing.
    """
    internal_guards.require_role_internal(_ROLE)
    awaited_delegation_ids, _ = _require_configured()
    cwd = (body or {}).get("cwd")
    active_dids = set(awaited_delegation_ids())
    pending = await asyncio.to_thread(pending_approvals.list_pending, cwd=cwd)
    return {"approvals": [rec for rec in pending if rec.get("delegation_id") in active_dids]}


@router.post("/api/internal/pending-approvals/approve")
async def internal_approve_pending_approval(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Approve a fresh-worker creation request. Body may include
    optional `description` and `orchestration_mode` overrides (the
    user's edits in the inline card)."""
    internal_guards.require_role_internal(_ROLE)
    _, resolve_approval = _require_configured()
    body = body or {}
    delegation_id = _required_delegation_id(body)
    rec, reason = pending_approvals.approve(
        delegation_id,
        description=body.get("description"),
        orchestration_mode=body.get("orchestration_mode"),
    )
    early = _resolved_or_error(rec, reason)
    if early is not None:
        return early
    resolve_approval(delegation_id, rec)
    return {"status": "approved", "record": rec}


@router.post("/api/internal/pending-approvals/deny")
async def internal_deny_pending_approval(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal(_ROLE)
    _, resolve_approval = _require_configured()
    delegation_id = _required_delegation_id(body)
    rec, reason = pending_approvals.deny(delegation_id)
    early = _resolved_or_error(rec, reason)
    if early is not None:
        return early
    resolve_approval(delegation_id, rec)
    return {"status": "denied", "record": rec}

"""Mid-turn tool-approval handshake: the runner's blocking request for
human consent on a CLI tool/command, the frontend's decision, and the
pending-card rehydration read.

The registry is in-memory and live-only, so the pending read is the
recovery path for a missed WS event. The coordinator is injected by the
composition root (see `configure`).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

import internal_guards
import tool_approval
from backend.event_bus import BusEvent, bus
from backend.surface_contract.nodes import (
    ApprovalDecision,
    TOOL_APPROVAL_REF_PREFIX,
    UserInteractionKind,
    UserInteractionState,
    interaction_fact_payload,
)
from i18n import t

router = APIRouter()
logger = logging.getLogger(__name__)

# v2 UserInteraction fact producer (ADR 0006 §5) — additive to the legacy
# `tool_approval_requested`/`tool_approval_resolved` WS broadcasts below,
# which stay as-is; `backend/adapters/chat_adapter.py`'s
# `_on_interaction_fact` is the one consumer.


def _tool_approval_request_dict(rec: "tool_approval.ToolApproval") -> dict:
    return {
        "subject": "tool", "tool_name": rec.tool_name,
        "summary": dict(rec.summary or {}), "risk_scope": rec.provider_kind,
    }


def _publish_interaction_fact(fact_type: str, app_session_id: str, payload: dict) -> None:
    try:
        bus.publish_threadsafe(BusEvent(
            type=fact_type, root_id=app_session_id, sid=app_session_id,
            payload=payload, persist=False,
        ))
    except Exception:
        logger.exception("tool_approvals_api: %s publish failed", fact_type)

_coordinator_ref: Any = None


def configure(*, coordinator: Any) -> None:
    """Bind the collaborators this router needs."""
    global _coordinator_ref
    _coordinator_ref = coordinator


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="tool approvals API is not configured")
    return _coordinator_ref


@router.post("/api/internal/tool-approvals/request")
async def internal_tool_approval_request(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Runner→backend: a CLI tool/command needs human approval mid-turn
    (Claude `can_use_tool` or Codex app-server approval ServerRequest).

    Creates a pending record, broadcasts `tool_approval_requested` to the
    session's WS subscribers, and BLOCKS until the frontend decides or the
    fail-closed timeout fires. Returns {approved}; the runner treats any
    non-approved/failed response as a denial."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    body = body or {}
    app_session_id = str(body.get("app_session_id") or "").strip()
    if not app_session_id:
        raise HTTPException(status_code=400, detail="app_session_id is required")
    rec = tool_approval.registry.create(
        app_session_id=app_session_id,
        run_id=str(body.get("run_id") or ""),
        provider_kind=str(body.get("provider_kind") or ""),
        tool_name=str(body.get("tool_name") or ""),
        summary=body.get("summary") if isinstance(body.get("summary"), dict) else {},
    )
    _publish_interaction_fact(
        "interaction.fire.requested", app_session_id,
        interaction_fact_payload(
            f"{TOOL_APPROVAL_REF_PREFIX}{rec.approval_id}", UserInteractionKind.APPROVAL,
            _tool_approval_request_dict(rec),
        ),
    )
    coordinator = _coordinator()
    await coordinator.broadcast_session(
        app_session_id,
        "tool_approval_requested",
        tool_approval.registry.public_view(rec),
        source="tool_approval",
    )
    approved = await tool_approval.registry.await_decision(rec)
    await coordinator.broadcast_session(
        app_session_id,
        "tool_approval_resolved",
        {"approval_id": rec.approval_id, "approved": approved},
        source="tool_approval",
    )
    return {"approved": approved, "approval_id": rec.approval_id}


@router.post("/api/sessions/{session_id}/tool-approvals/{approval_id}/decide")
async def decide_tool_approval(session_id: str, approval_id: str, body: dict = Body(default={})):
    """Frontend→backend: user clicked Approve/Deny on a tool-approval card.
    Resolves the runner's blocked request. Unknown/already-resolved ids are
    a no-op (the runner already timed out/denied).

    Object-level authz: the approval MUST belong to this session — the
    approval_id alone (even though uuid4) is not accepted across sessions."""
    rec = tool_approval.registry.get(approval_id)
    if rec is None or rec.app_session_id != session_id:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    approved = bool((body or {}).get("approved"))
    ok = tool_approval.registry.decide(approval_id, approved)
    if ok:
        _publish_interaction_fact(
            "interaction.fire.resolved", session_id,
            interaction_fact_payload(
                f"{TOOL_APPROVAL_REF_PREFIX}{approval_id}", UserInteractionKind.APPROVAL,
                _tool_approval_request_dict(rec),
                state=UserInteractionState.RESOLVED,
                response={"decision": (ApprovalDecision.APPROVE if approved else ApprovalDecision.DENY).value},
            ),
        )
    return {"ok": ok}


@router.get("/api/sessions/{session_id}/tool-approvals/pending")
async def list_pending_tool_approvals(session_id: str):
    """Rehydrate pending tool-approval cards on mount/reconnect. Live-only
    source (in-memory registry); a missed WS event no longer silently becomes
    a 5-minute denial — the card reappears on the next poll/reload."""
    return {
        "approvals": [tool_approval.registry.public_view(r) for r in tool_approval.registry.list_for_session(session_id)]
    }

"""Blocking user-input requests (questions, approvals, memory proposals)
and the agent-driven panel opens that surface UI in the active session.

The coordinator is injected by the composition root — see `configure`.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException

import internal_guards
import memory_api
import user_input_store
from event_bus import BusEvent, bus
from i18n import t
from session_helpers import session_lite as _session_lite
from session_manager import manager as session_manager
from backend.event_bus import bus as _v2_bus
from backend.event_bus import BusEvent as _V2BusEvent
from backend.surface_contract.nodes import (
    USER_INPUT_REF_PREFIX,
    UserInteractionKind,
    UserInteractionState,
    interaction_fact_payload,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# v2 UserInteraction fact producer (ADR 0006 §5) — additive to the legacy
# `user_input_requested`/`user_input_resolved` WS broadcasts below, which
# stay as-is; `backend/adapters/chat_adapter.py`'s `_on_interaction_fact`
# is the one consumer. Every legacy sub-kind here (approval/input/memory)
# maps onto the ONE `UserInteractionKind.INPUT` — the remaining kind slot
# not already covered by tool/worker approval (`approval`) or the
# delegation picker (`choice`).


def _publish_interaction_fact(fact_type: str, app_session_id: str, payload: dict) -> None:
    try:
        _v2_bus.publish_threadsafe(_V2BusEvent(
            type=fact_type, root_id=app_session_id, sid=app_session_id,
            payload=payload, persist=False,
        ))
    except Exception:
        logger.exception("user_input_api: %s publish failed", fact_type)

_coordinator_ref: Any = None


def configure(*, coordinator: Any) -> None:
    """Bind the coordinator this router broadcasts through."""
    global _coordinator_ref
    _coordinator_ref = coordinator


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="user input API is not configured")
    return _coordinator_ref


@router.post("/api/internal/open-file-panel")
async def internal_open_file_panel(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Invoked by the active session's `open_file_panel` SDK MCP tool.

    mode="panel": mutate the session's backend-owned `open_file_panels`
    (fires `session_metadata_updated` → every connected tab opens the
    tab). mode="inline": NO state mutation — the persisted tool-call
    event on the assistant message is the source of truth; the frontend
    renders an embedded viewer from it. Either way return success so
    the agent gets a clean tool_result.
    """
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))

    app_session_id = body.get("app_session_id") or ""
    sess = await _session_lite(app_session_id)
    if sess is None:
        return {"success": False, "error": t("error.session_not_found_retry")}

    mode = body.get("mode")
    if mode not in ("panel", "inline"):
        return {"success": False, "error": "mode must be 'panel' or 'inline'"}

    raw_path = str(body.get("path") or "").strip()
    if not raw_path:
        return {"success": False, "error": t("error.file_panel_path_required")}
    # Resolve relative paths against the session cwd so the persisted
    # panel path is absolute + consistent with how the frontend's
    # manual-open path resolves (App.tsx handleFileClick).
    if not raw_path.startswith("/"):
        cwd = (sess.get("cwd") or "").rstrip("/")
        raw_path = f"{cwd}/{raw_path}" if cwd else raw_path

    def _range(s, e) -> Optional[dict]:
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
            return None
        return {"startLine": int(s), "endLine": int(e)}

    panel = {
        "id": uuid.uuid4().hex[:12],
        "path": raw_path,
        "focus": _range(body.get("start_line"), body.get("end_line")),
        "selection": _range(body.get("selected_start"), body.get("selected_end")),
    }

    if mode == "panel":
        await asyncio.to_thread(
            session_manager.add_open_file_panel,
            app_session_id,
            panel,
        )

    return {"success": True, "mode": mode, "panel": panel}


def _validate_user_input_questions(raw_questions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 3:
        raise HTTPException(status_code=400, detail="questions must contain 1-3 items")
    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_questions:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="question entries must be objects")
        qid = str(raw.get("id") or "").strip()
        header = str(raw.get("header") or "").strip()
        question = str(raw.get("question") or "").strip()
        if not qid or qid in seen_ids:
            raise HTTPException(status_code=400, detail="question ids must be unique and non-empty")
        if not header or not question:
            raise HTTPException(status_code=400, detail="question header and question are required")
        options_raw = raw.get("options") or []
        if not isinstance(options_raw, list) or len(options_raw) > 3:
            raise HTTPException(status_code=400, detail="question options must contain at most 3 items")
        options: list[dict[str, str]] = []
        for option_raw in options_raw:
            if not isinstance(option_raw, dict):
                raise HTTPException(status_code=400, detail="question options must be objects")
            label = str(option_raw.get("label") or "").strip()
            description = str(option_raw.get("description") or "").strip()
            if not label:
                raise HTTPException(status_code=400, detail="option label is required")
            options.append({"label": label[:120], "description": description[:500]})
        seen_ids.add(qid)
        questions.append({
            "id": qid[:80],
            "header": header[:120],
            "question": question[:1000],
            "options": options,
        })
    return questions


def _validate_user_approval_prompt(raw_prompt: Any) -> str:
    if not isinstance(raw_prompt, str):
        raise HTTPException(status_code=400, detail="prompt must be a string")
    prompt = raw_prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    return prompt[:2000]


async def _broadcast_user_input(event_type: str, payload: dict[str, Any]) -> None:
    app_session_id = str(payload.get("app_session_id") or "").strip()
    if not app_session_id:
        return
    await _coordinator().dispatch_raw(app_session_id, {"type": event_type, "data": payload})


async def _broadcast_user_input_state(app_session_id: str) -> None:
    sid = str(app_session_id or "").strip()
    if not sid:
        return
    pending_count = await asyncio.to_thread(
        user_input_store.pending_count_for_session,
        sid,
    )
    await bus.publish(BusEvent(
        type="session.user_input_changed",
        root_id=session_manager.root_id_for(sid) or sid,
        sid=sid,
        payload={
            "kind": "user_input_changed",
            "pending_user_input_count": pending_count,
        },
        persist=False,
    ))
    await _coordinator().broadcast_global("session_user_input_changed", {
        "session_id": sid,
        "app_session_id": sid,
        "pending_user_input_count": pending_count,
    })


@router.get("/api/user-input/pending")
async def get_pending_user_inputs(app_session_id: str | None = None):
    sid = str(app_session_id or "").strip()
    if not sid:
        return {
            "requests": await asyncio.to_thread(
                user_input_store.pending_requests,
            )
        }
    if await _session_lite(sid) is None:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    return {
        "requests": await asyncio.to_thread(
            user_input_store.pending_for_session,
            sid,
        )
    }


def build_resolve_response(req: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Kind-specific response-shape validation/construction — the single
    source both `resolve_user_input` (REST) and `backend/surface_commands.
    py`'s `resolve_interaction` v2 routing call, so the two transports
    validate identically and neither re-derives the rule. Raises
    `HTTPException` on a malformed shape; the REST route lets it propagate
    as-is, the v2 path catches it and converts it to a typed
    `invalid_response` rejection."""
    kind = req.get("kind")
    if kind == "approval":
        approved = body.get("approved")
        if not isinstance(approved, bool):
            raise HTTPException(status_code=400, detail="approved must be a boolean")
        alternative = str(body.get("alternative") or "").strip()
        if approved and alternative:
            raise HTTPException(status_code=400, detail="approved response cannot include alternative text")
        if not approved and not alternative:
            raise HTTPException(status_code=400, detail="alternative is required when approval is not granted")
        response: dict[str, Any] = {"approved": approved}
        if alternative:
            response["alternative"] = alternative[:4000]
        return response
    if kind == "memory":
        approved = body.get("approved")
        if not isinstance(approved, bool):
            raise HTTPException(status_code=400, detail="approved must be a boolean")
        response = {"approved": approved}
        if approved:
            response["memory_proposal"] = memory_api.validate_memory_proposal(body.get("edited"))
        return response
    if not isinstance(body.get("answers"), dict):
        raise HTTPException(status_code=400, detail="answers object is required")
    expected = {q["id"] for q in req.get("questions") or []}
    response = {}
    for qid in expected:
        value = str(body["answers"].get(qid) or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail=f"answer is required for {qid}")
        response[qid] = value[:2000]
    return response


@router.post("/api/user-input/{request_id}/resolve")
async def resolve_user_input(request_id: str, body: dict):
    req = await asyncio.to_thread(user_input_store.get_request, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")
    if str(body.get("app_session_id") or "").strip() != req.get("app_session_id"):
        raise HTTPException(status_code=403, detail="session mismatch")
    if req.get("status") != "pending":
        return {"success": False, "status": req.get("status")}
    response = build_resolve_response(req, body)
    resolved = await asyncio.to_thread(
        user_input_store.resolve_request,
        request_id,
        response,
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="request not found")
    _publish_interaction_fact(
        "interaction.fire.resolved", str(resolved.get("app_session_id") or ""),
        interaction_fact_payload(
            f"{USER_INPUT_REF_PREFIX}{request_id}", UserInteractionKind.INPUT,
            user_input_store.interaction_request_dict(req),
            state=UserInteractionState.RESOLVED, response=response,
        ),
    )
    await _broadcast_user_input("user_input_resolved", {
        "request_id": request_id,
        "app_session_id": resolved.get("app_session_id"),
        "status": resolved.get("status"),
    })
    await _broadcast_user_input_state(str(resolved.get("app_session_id") or ""))
    return {"success": True, "status": resolved.get("status")}


@router.post("/api/user-input/{request_id}/cancel")
async def cancel_user_input(request_id: str, body: dict):
    req = await asyncio.to_thread(user_input_store.get_request, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")
    if str(body.get("app_session_id") or "").strip() != req.get("app_session_id"):
        raise HTTPException(status_code=403, detail="session mismatch")
    resolved = await asyncio.to_thread(user_input_store.cancel_request, request_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="request not found")
    _publish_interaction_fact(
        "interaction.fire.resolved", str(resolved.get("app_session_id") or ""),
        interaction_fact_payload(
            f"{USER_INPUT_REF_PREFIX}{request_id}", UserInteractionKind.INPUT,
            user_input_store.interaction_request_dict(req),
            state=UserInteractionState.CANCELLED, response={},
        ),
    )
    await _broadcast_user_input("user_input_resolved", {
        "request_id": request_id,
        "app_session_id": resolved.get("app_session_id"),
        "status": resolved.get("status"),
    })
    await _broadcast_user_input_state(str(resolved.get("app_session_id") or ""))
    return {"success": True, "status": resolved.get("status")}


@router.post("/api/internal/user-input/request")
async def internal_request_user_input(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    app_session_id = str(body.get("app_session_id") or "").strip()
    if not app_session_id:
        return {"success": False, "error": "app_session_id is required"}
    if await _session_lite(app_session_id) is None:
        return {"success": False, "error": t("error.session_not_found_retry")}
    try:
        kind = str(body.get("kind") or "input").strip()
        memory_proposal: dict[str, Any] | None = None
        if kind == "approval":
            prompt = _validate_user_approval_prompt(body.get("prompt"))
            questions: list[dict[str, Any]] = []
        elif kind == "input":
            questions = _validate_user_input_questions(body.get("questions"))
            prompt = ""
        elif kind == "memory":
            memory_proposal = memory_api.validate_memory_proposal(body.get("memory_proposal"))
            questions = []
            prompt = ""
        else:
            raise HTTPException(status_code=400, detail="kind must be input, approval, or memory")
    except HTTPException as exc:
        return {"success": False, "error": str(exc.detail)}
    raw_timeout = body.get("timeout_seconds")
    timeout_seconds = 86400.0
    if raw_timeout is not None:
        try:
            timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            return {"success": False, "error": "timeout_seconds must be a number"}
        if timeout_seconds <= 0 or timeout_seconds > 86400:
            return {"success": False, "error": "timeout_seconds must be between 1 and 86400"}
    public_req, created = await asyncio.to_thread(
        user_input_store.create_or_get_pending_request,
        app_session_id=app_session_id,
        kind=kind,
        questions=questions,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        memory_proposal=memory_proposal,
    )
    if created:
        _publish_interaction_fact(
            "interaction.fire.requested", app_session_id,
            interaction_fact_payload(
                f"{USER_INPUT_REF_PREFIX}{public_req['request_id']}", UserInteractionKind.INPUT,
                user_input_store.interaction_request_dict(public_req),
            ),
        )
        await _broadcast_user_input("user_input_requested", public_req)
        await _broadcast_user_input_state(app_session_id)
    wait_timeout = timeout_seconds
    if not created:
        expires_at = public_req.get("expires_at")
        wait_timeout = max(float(expires_at) - time.time(), 0.05) if expires_at else None
    completed = await user_input_store.wait_for_completion(
        public_req["request_id"],
        wait_timeout,
    )
    if completed is None:
        return {"success": False, "error": "request not found"}
    if completed.get("status") == "resolved":
        await _broadcast_user_input_state(str(completed.get("app_session_id") or ""))
        result = {
            "success": True,
            "request_id": completed["request_id"],
        }
        if completed.get("kind") in ("approval", "memory"):
            result.update(completed.get("response") or {})
        else:
            result["answers"] = completed.get("response") or {}
        return result
    await _broadcast_user_input("user_input_resolved", {
        "request_id": completed.get("request_id"),
        "app_session_id": completed.get("app_session_id"),
        "status": completed.get("status"),
    })
    await _broadcast_user_input_state(str(completed.get("app_session_id") or ""))
    result = {
        "success": False,
        "request_id": completed.get("request_id"),
        "status": completed.get("status"),
    }
    if completed.get("kind") in ("approval", "memory"):
        result["approved"] = False
    return result


@router.post("/api/internal/open-config-panel")
async def internal_open_config_panel(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Invoked by the active session's `open_config_panel` SDK MCP tool.

    Always INLINE: NO session state mutation. The persisted tool-call
    event on the assistant message is the source of truth — the frontend
    renders an embedded config panel editor from it.
    Popping it into the right side panel is a later user action via the
    inline widget's button (→ /api/sessions/.../config-panels). Returns
    success + the resolved panel so the agent gets a clean tool_result.
    """
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))

    app_session_id = body.get("app_session_id") or ""
    sess = await _session_lite(app_session_id)
    if sess is None:
        return {"success": False, "error": t("error.session_not_found_retry")}

    capability_id = str(body.get("capability_id") or "").strip()
    if not capability_id:
        return {"success": False, "error": "capability_id is required"}

    scope = str(body.get("scope") or "project").strip()
    if scope not in ("global", "project"):
        scope = "project"

    # Resolve project cwd against the session cwd when the agent didn't
    # pass one explicitly, so the persisted panel targets the right project.
    cwd = str(body.get("cwd") or "").strip()
    if scope == "project" and not cwd:
        cwd = (sess.get("cwd") or "").strip()

    panel = {
        "id": uuid.uuid4().hex[:12],
        "capability_id": capability_id,
        "scope": scope,
        "cwd": cwd,
    }
    return {"success": True, "panel": panel}

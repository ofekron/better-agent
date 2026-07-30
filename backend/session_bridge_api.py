"""The session bridge: cross-session search, the inline session picker an
agent proposes, blocking delegation plus its picker resolution, and the
Agent Board's prompt-lane delivery.

The coordinator is injected by the composition root — see `configure`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

import extension_store
import harness_profile_resolver
import internal_extension_api
import internal_guards
import session_bridge
import session_search
from harness_profiles_api import harness_profile_selection as _harness_profile_selection
from i18n import t
from provider_validation import (
    resolve_auto_search_provider_id as _resolve_auto_search_provider_id,
    resolve_provider_id_ref as _resolve_provider_id_ref,
    validate_optional_run_selector as _validate_optional_run_selector,
)
from session_helpers import session_exists as _session_exists

router = APIRouter()
logger = logging.getLogger(__name__)

_coordinator_ref: Any = None


def configure(*, coordinator: Any) -> None:
    """Bind the coordinator this router reads turn state from."""
    global _coordinator_ref
    _coordinator_ref = coordinator


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="session bridge API is not configured")
    return _coordinator_ref


@router.post("/api/internal/ask-propose")
async def internal_ask_propose(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(
        extension_store.extension_id_for_role('team-orchestration')
    )
    """Invoked by the session-bridge `propose_sessions` MCP tool. Stamps the
    inline session picker on the CALLING session's in-flight assistant
    message (validated ids; broadcasts `message_ask_result_changed`). The
    Ask UI's own picker is stamped directly server-side by
    `session_search.search()`, not via this endpoint."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    caller_sid = str(body.get("caller_sid") or "")
    if not caller_sid:
        raise HTTPException(status_code=400, detail="caller_sid is required")
    in_flight = _coordinator().turn_manager.get_in_flight_assistant_msg(caller_sid)
    if not in_flight or not in_flight.get("id"):
        raise HTTPException(
            status_code=409,
            detail="no in-flight assistant message to attach the picker to",
        )
    result = await asyncio.to_thread(
        session_search.propose_sessions,
        body.get("session_ids") or [],
        str(body.get("reasoning") or ""),
        target_sid=caller_sid,
        msg_id=in_flight["id"],
        proposed_project_path=str(body.get("proposed_project_path") or ""),
    )
    return {"success": True, **result}


@router.post("/api/internal/session-bridge/search")
async def internal_session_bridge_search(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(
        extension_store.extension_id_for_role('team-orchestration')
    )
    """Invoked by the session-bridge `search_sessions` MCP tool. Runs the
    same provisioned search-worker ranking engine as the Ask UI and returns
    ranked sessions. No picker; the agent can call `propose_sessions`
    separately. Optional filters (`provider_id` / `model` /
    `reasoning_effort` / `node_id`) narrow the candidate set before the
    worker runs and post-validate its output."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    durable = await internal_extension_api.maybe_run_core_mcp_job("session-bridge-search", body)
    if durable is not None:
        return durable
    return await _handle_internal_session_bridge_search(body)


async def _handle_internal_session_bridge_search(body: dict) -> dict[str, Any]:
    query = str(body.get("query") or "").strip()
    if not query:
        return {"results": [], "error": "empty_query"}
    try:
        limit = int(body.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5

    def _opt_str(key: str) -> str:
        val = body.get(key)
        return val.strip() if isinstance(val, str) else ""

    provider_id = await _resolve_auto_search_provider_id(
        body,
        str(body.get("app_session_id") or ""),
    )
    raw_tags = body.get("tags")
    tags = [t for t in raw_tags if isinstance(t, str) and t.strip()] if isinstance(raw_tags, list) else None
    flow = await session_search.run_search_sessions_session(
        query,
        timeout=session_search._DEFAULT_TIMEOUT_SECONDS,
        max_results=max(1, min(limit, 10)),
        provider_id=provider_id or None,
        model=_opt_str("model") or None,
        reasoning_effort=_opt_str("reasoning_effort") or None,
        node_id=_opt_str("node_id") or None,
        cwd=_opt_str("cwd") or None,
        tag_ids=tags,
        folder_id=_opt_str("folder") or None,
    )
    return await asyncio.to_thread(session_search.canonical_search_response, flow)


@router.post("/api/internal/session-bridge/delegate")
async def internal_session_bridge_delegate(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(
        extension_store.extension_id_for_role('team-orchestration')
    )
    """Invoked by the session-bridge `delegate_to_session` MCP tool. Blocks
    on the picker approval (unless auto+fork) AND the delegated turn, then
    returns the target's final message."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    durable = await internal_extension_api.maybe_run_core_mcp_job("session-bridge-delegate", body)
    if durable is not None:
        return durable
    return await _handle_internal_session_bridge_delegate(body)


async def _handle_internal_session_bridge_delegate(body: dict) -> dict[str, Any]:
    caller_sid = str(body.get("app_session_id") or "")
    # Only new-session mode consumes the profile; an existing target keeps its
    # own. Validated here so a bad selection fails before the user picker.
    harness_profile_id = _harness_profile_selection(body)
    profile_selectors = harness_profile_resolver.merge_selector_defaults(
        {
            "provider_id": str(body.get("provider_id") or "").strip() or None,
            "model": str(body.get("model") or "").strip() or None,
            "reasoning_effort": body.get("reasoning_effort"),
        },
        harness_profile_id,
    )
    requested_provider_id = await _resolve_provider_id_ref(
        str(profile_selectors["provider_id"] or "").strip(),
    )
    requested_model = str(profile_selectors["model"] or "").strip()
    await _validate_optional_run_selector(
        caller_sid,
        requested_provider_id,
        requested_model,
    )
    result = await session_bridge.delegate(
        caller_sid=caller_sid,
        target_sid=str(body.get("session_id") or ""),
        prompt=str(body.get("prompt") or ""),
        run_mode=str(body.get("run_mode") or ""),
        approval=str(body.get("approval") or ""),
        display_prompt=str(body.get("display_prompt") or "") or None,
        source=str(body.get("source") or "") or None,
        client_id=str(body.get("client_id") or "") or None,
        provider_id=requested_provider_id,
        model=requested_model,
        reasoning_effort=str(profile_selectors["reasoning_effort"] or "").strip(),
        runner=str(body.get("runner") or "").strip(),
        harness_profile_id=harness_profile_id,
    )
    return result


_AGENT_BOARD_MAX_PROMPT_LEN = 8000
# Strong refs to in-flight delivery tasks so they aren't GC'd mid-await.
_AGENT_BOARD_DELIVERY_TASKS: set[asyncio.Task] = set()


@router.post("/api/internal/agent-board/run-prompt")
async def internal_agent_board_run_prompt(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Deliver a prompt lane action from the Agent Board extension. Only the
    agent-board builtin (identified by its minted per-extension token) may
    call this; the privileged cross-session turn stays inside core. Runs the
    turn in the background so the drop request returns immediately."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    # Runtime-readiness gate MUST precede the identity check: in a pure-public
    # agent-board role is absent,
    # and principal_extension_id() is also None for a core token — so a bare
    # `None != None` identity check would let any core-token holder through.
    # runtime_not_ready_message(None) returns "not installed" -> 404 first.
    internal_guards.require_builtin_runtime_extension(
        extension_store.extension_id_for_role('agent-board')
    )
    if (
        internal_guards.internal_authority_extension_id()
        != extension_store.extension_id_for_role('agent-board')
    ):
        raise HTTPException(status_code=403, detail="caller is not the agent-board extension")
    session_id = str((body or {}).get("session_id") or "").strip()
    prompt = str((body or {}).get("prompt") or "").strip()
    if not session_id or not prompt:
        raise HTTPException(status_code=400, detail="session_id and prompt are required")
    if len(prompt) > _AGENT_BOARD_MAX_PROMPT_LEN:
        raise HTTPException(status_code=400, detail="prompt too long")
    # Constrain the target to a real, existing session so a bug in the
    # extension subprocess cannot drive arbitrary/virtual session ids.
    if not await _session_exists(session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    # Continue-mode delivery refuses a busy target; surface that synchronously
    # so the drop UI can tell the user instead of silently dropping the prompt.
    if _coordinator().turn_manager.has_active_runs(session_id):
        raise HTTPException(status_code=409, detail="session has an in-flight turn")

    async def _deliver() -> None:
        try:
            result = await session_bridge.run_for_extension(session_id, prompt, source="agent-board")
        except Exception:
            logger.warning(
                "agent-board run-prompt failed for %s", session_id[:8], exc_info=True
            )
            return
        # run_for_extension returns an error dict (e.g. target became busy in
        # the race after the pre-check) rather than raising — don't drop it.
        if isinstance(result, dict) and result.get("error"):
            logger.warning(
                "agent-board run-prompt for %s returned error: %s",
                session_id[:8], result.get("error"),
            )

    # Hold a reference until completion: a bare create_task may be GC'd
    # mid-await, silently cancelling the delivery.
    task = asyncio.create_task(_deliver())
    _AGENT_BOARD_DELIVERY_TASKS.add(task)
    task.add_done_callback(_AGENT_BOARD_DELIVERY_TASKS.discard)
    return {"scheduled": True}


def _resolve_session_bridge_delegation(body: dict, delegation_id: str = "") -> dict:
    internal_guards.require_builtin_runtime_extension(
        extension_store.extension_id_for_role('team-orchestration')
    )
    body = body or {}
    delegation_id = delegation_id or str(body.get("delegation_id") or "").strip()
    if not delegation_id:
        return {"success": False, "status": 400, "error": "delegation_id is required"}
    chosen = body.get("chosen_session_id")
    chosen = str(chosen) if chosen else None
    ok = session_bridge.resolve_delegation(delegation_id, chosen)
    if not ok:
        return {
            "success": False,
            "status": 409,
            "error": "delegation not pending, already resolved, or invalid target",
        }
    return {"success": True}


@router.post("/api/internal/session-bridge/delegate/resolve")
async def internal_session_bridge_delegate_resolve(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    return _resolve_session_bridge_delegation(body)

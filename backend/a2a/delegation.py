"""Projects a delegated A2A remote-task run as a worker panel on the
parent session, funneling every update through the SAME
`OrchestrationStrategy.apply_event` path (`worker_start` /
`worker_event` / `worker_complete`, `panel_kind="a2a"`) that Better
Agent's own worker delegations use — no parallel render-tree mutation
path. See CLAUDE.md's session-event-ingestion invariant.

`start_a2a_delegation` runs synchronously and returns as soon as the
panel exists, so the caller (the REST handler) can respond immediately
with a `delegation_id`; the remote task then streams in the background
via `run_a2a_delegation`, which fires the async stream loop as a
detached task. State is truthful throughout: a paused/failed/canceled
remote task is reported as such, never shown as optimistic success.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import uuid as uuid_mod
from typing import Optional

from a2a.client import A2ARpcError, stream_message
from orchs import ApplyEventCtx, get_strategy
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

# asyncio only holds a weak reference to a task once nothing else does —
# an unreferenced task can be garbage-collected mid-run. This set is the
# strong reference for the detached delegation-stream tasks fired by
# `run_a2a_delegation`, cleared via each task's own done-callback.
_IN_FLIGHT_TASKS: set[asyncio.Task] = set()


def _parts_text(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text"))
        for part in parts
        if isinstance(part, dict) and part.get("text")
    )


def _status_state_and_text(result: dict) -> tuple[str, str]:
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    message = status.get("message") if isinstance(status.get("message"), dict) else {}
    state = status.get("state")
    if not isinstance(state, str):
        state = "completed" if result.get("kind") == "message" else "unknown"
    text = _parts_text(message.get("parts")) or _parts_text(result.get("parts"))
    return state, text


def _status_inner_event(uid: str, state: str, status_text: str) -> dict:
    """Renders as a normal assistant-text entry inside the worker panel —
    reuses the existing message-rendering pipeline rather than adding a
    bespoke event type the frontend would need to special-case."""
    summary = f"[a2a status: {state}]" + (f"\n{status_text}" if status_text else "")
    return {
        "type": "agent_message",
        "data": {
            "uuid": uid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": summary}]},
        },
    }


def _artifact_inner_event(uid: str, artifact: dict, accumulated_text: str) -> dict:
    name = artifact.get("name") or artifact.get("artifactId") or "artifact"
    summary = f"[a2a artifact: {name}]" + (f"\n{accumulated_text}" if accumulated_text else "")
    return {
        "type": "agent_message",
        "data": {
            "uuid": uid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": summary}]},
        },
    }


def _live_msg(app_session_id: str, msg_id: str) -> Optional[dict]:
    session = session_manager._cached(app_session_id, hydrate_events=False)
    if not session:
        return None
    for message in session.get("messages") or []:
        if message.get("id") == msg_id:
            return message
    return None


def start_a2a_delegation(
    *, app_session_id: str, agent_record: dict, instructions: str,
) -> tuple[str, str]:
    """Mint a delegation id, create a host assistant message, and apply
    `worker_start` so the panel is visible before the remote call
    begins. Returns (delegation_id, msg_id). Raises ValueError if the
    session doesn't exist."""
    session = session_manager.get(app_session_id)
    if session is None:
        raise ValueError(f"unknown session: {app_session_id}")
    mode = session.get("orchestration_mode") or "manager"
    strategy = get_strategy(mode)
    scaffold = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(app_session_id, scaffold)
    root_id = session_manager._root_id_for(app_session_id) or app_session_id
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, user_msg=None, root_id=root_id)
    delegation_id = f"del_a2a_{uuid_mod.uuid4().hex[:10]}"
    msg = _live_msg(app_session_id, scaffold["id"]) or scaffold
    strategy.apply_event(
        app_session_id=app_session_id,
        msg=msg,
        event={
            "type": "worker_start",
            "data": {
                "delegation_id": delegation_id,
                "worker_session_id": "",
                "worker_description": agent_record.get("name") or agent_record.get("id"),
                "panel_kind": "a2a",
                "orchestration_mode": "a2a",
                "run_mode": "a2a",
                "instructions_preview": instructions[:2000],
                "a2a_agent_id": agent_record.get("id"),
                "a2a_base_url": agent_record.get("base_url"),
            },
        },
        ctx=ctx,
        source_is_provider_stream=True,
    )
    return delegation_id, scaffold["id"]


async def _run_a2a_delegation_async(
    *, app_session_id: str, msg_id: str, delegation_id: str,
    agent_record: dict, instructions: str,
) -> None:
    def _ctx_and_strategy() -> tuple[object, str]:
        session = session_manager.get(app_session_id) or {}
        mode = session.get("orchestration_mode") or "manager"
        root_id = session_manager._root_id_for(app_session_id) or app_session_id
        return get_strategy(mode), root_id

    strategy, root_id = await asyncio.to_thread(_ctx_and_strategy)
    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, user_msg=None, root_id=root_id)

    def _apply(event: dict) -> None:
        msg = _live_msg(app_session_id, msg_id)
        if msg is None:
            return
        strategy.apply_event(
            app_session_id=app_session_id, msg=msg, event=event,
            ctx=ctx, source_is_provider_stream=True,
        )

    artifacts_text: dict[str, str] = {}
    status_counter = itertools.count()
    success = False
    error: Optional[str] = None
    try:
        async for result in stream_message(
            base_url=agent_record["base_url"],
            auth_header_name=agent_record.get("auth_header_name") or "",
            auth_secret=agent_record.get("auth_secret") or "",
            text=instructions,
        ):
            artifact = result.get("artifact") if isinstance(result.get("artifact"), dict) else None
            if artifact is not None:
                artifact_id = str(
                    artifact.get("artifactId") or artifact.get("name") or uuid_mod.uuid4().hex
                )
                chunk_text = _parts_text(artifact.get("parts"))
                prior = artifacts_text.get(artifact_id, "")
                combined = prior + chunk_text if result.get("append") is True else chunk_text
                artifacts_text[artifact_id] = combined
                inner = _artifact_inner_event(
                    f"a2a-artifact-{delegation_id}-{artifact_id}", artifact, combined,
                )
            else:
                state, status_text = _status_state_and_text(result)
                inner = _status_inner_event(
                    f"a2a-status-{delegation_id}-{next(status_counter)}", state, status_text,
                )
                if state == "completed":
                    success = True
                elif state in ("failed", "rejected"):
                    error = status_text or f"remote task {state}"
                elif state == "canceled":
                    error = "remote task canceled"
                elif state in ("input-required", "auth-required"):
                    error = status_text or f"remote task requires {state.replace('-', ' ')}"
            await asyncio.to_thread(
                _apply,
                {
                    "type": "worker_event",
                    "data": {"delegation_id": delegation_id, "event": inner},
                },
            )
    except A2ARpcError as exc:
        error = str(exc)
    except Exception as exc:
        logger.exception("a2a delegation stream failed for %s", delegation_id)
        error = str(exc)

    def _complete() -> None:
        msg = _live_msg(app_session_id, msg_id)
        if msg is None:
            return
        strategy.apply_event(
            app_session_id=app_session_id,
            msg=msg,
            event={
                "type": "worker_complete",
                "data": {
                    "delegation_id": delegation_id,
                    "worker_session_id": "",
                    "success": success and error is None,
                    "error": error,
                    "run_mode": "a2a",
                },
            },
            ctx=ctx,
            source_is_provider_stream=True,
        )
    await asyncio.to_thread(_complete)


async def run_a2a_delegation(
    *, app_session_id: str, agent_record: dict, instructions: str,
) -> str:
    """Starts the panel synchronously, fires the remote-task stream as
    a detached background task, and returns the delegation_id
    immediately."""
    delegation_id, msg_id = await asyncio.to_thread(
        start_a2a_delegation,
        app_session_id=app_session_id,
        agent_record=agent_record,
        instructions=instructions,
    )
    task = asyncio.create_task(
        _run_a2a_delegation_async(
            app_session_id=app_session_id,
            msg_id=msg_id,
            delegation_id=delegation_id,
            agent_record=agent_record,
            instructions=instructions,
        )
    )
    _IN_FLIGHT_TASKS.add(task)
    task.add_done_callback(_IN_FLIGHT_TASKS.discard)
    return delegation_id

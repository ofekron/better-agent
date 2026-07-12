"""Manager-mode worker delegation.

Entry point invoked from `/api/internal/delegate` (the in-process
`delegate` SDK MCP tool's HTTP loopback). Resolves the worker BC
session (or blocks on user approval to mint a fresh one), serializes
per-(caller, worker) under an asyncio.Lock so a fork's claude jsonl
can't be corrupted by concurrent resumes, then spawns a runner.py
invocation in the worker's orchestration mode and streams its events
back as `worker_event` frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from event_bus import BusEvent, bus
from i18n import t
import llm_call_log
import perf
from orchs.jsonl_helpers import (
    compute_jsonl_path,
    compute_jsonl_read_path,
    count_jsonl_lines,
    jsonl_byte_size,
)
from stores import worker_store
from stores import session_fork_store
from orchs.manager._approval import (
    await_fresh_worker_approval,
    spawn_approved_worker,
)
from orchs.manager._rewind import _safe_delete_forks
from provider import StreamEvent
from event_shape import is_synthetic_event as _is_synthetic_event
from session_manager import manager as session_manager
import delegation_status_store

if TYPE_CHECKING:
    from orchestrator import Coordinator

logger = logging.getLogger(__name__)


def _jsonl_line_has_final_text(raw: bytes, expected: str) -> bool:
    try:
        entry = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return False
    if entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and block.get("text") == expected
        for block in content
    )


def _final_text_line_end(path: Path, start: int, expected: str) -> Optional[int]:
    last_match_end: Optional[int] = None
    offset = start
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                line_end = offset + len(raw)
                if _jsonl_line_has_final_text(raw, expected):
                    last_match_end = line_end
                offset = line_end
    except OSError:
        return None
    return last_match_end


def _delegation_event_is_tool_activity(event: dict) -> bool:
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return False
    message = data.get("message")
    content = data.get("content")
    if isinstance(message, dict):
        content = message.get("content")
    blocks = content if isinstance(content, list) else []
    return any(
        isinstance(block, dict)
        and str(block.get("type") or "") in {"tool_use", "tool_result"}
        for block in blocks
    )


def _delegation_event_has_final_answer(event: dict) -> bool:
    try:
        from event_shape import has_final_answer_event

        return has_final_answer_event([event])
    except Exception:
        data = event.get("data") if isinstance(event, dict) else None
        return isinstance(data, dict) and data.get("final_answer") is True


async def _durable_provider_output_drained(
    run_dir: Path,
    complete_payload: dict,
    start_offset: int = 0,
) -> bool:
    state_path = run_dir / "backend_state.json"
    try:
        state = await asyncio.to_thread(
            lambda: json.loads(state_path.read_text(encoding="utf-8"))
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False

    jsonl_path_value = state.get("jsonl_path")
    if not isinstance(jsonl_path_value, str) or not jsonl_path_value:
        return False
    jsonl_path = Path(jsonl_path_value)

    processed_byte = state.get("processed_byte")
    if processed_byte is not None:
        try:
            size = await asyncio.to_thread(lambda: jsonl_path.stat().st_size)
            expected_final_text = complete_payload.get("final_assistant_text")
            if isinstance(expected_final_text, str) and expected_final_text:
                line_end = await asyncio.to_thread(
                    _final_text_line_end,
                    jsonl_path,
                    start_offset,
                    expected_final_text,
                )
                return (
                    line_end is not None
                    and int(processed_byte) >= max(size, line_end)
                )
            return int(processed_byte) >= size
        except (OSError, TypeError, ValueError):
            return False

    processed_line = state.get("processed_line")
    if processed_line is not None:
        try:
            line_count = await asyncio.to_thread(count_jsonl_lines, jsonl_path)
            return int(processed_line) >= line_count
        except (OSError, TypeError, ValueError):
            return False

    return False


async def _wait_for_provider_complete_event(provider_rs: object) -> StreamEvent:
    run_dir = provider_rs.run_dir
    complete_path = run_dir / "complete.json"
    while True:
        exists = await asyncio.to_thread(complete_path.exists)
        payload = None
        if exists:
            from runs_dir import read_best_complete

            payload = await asyncio.to_thread(read_best_complete, run_dir)
        complete_task = getattr(provider_rs, "complete_task", None)
        complete_task_finished = (
            complete_task is not None
            and complete_task.done()
        )
        if exists and isinstance(payload, dict) and (
            complete_task_finished
            or await _durable_provider_output_drained(
                run_dir,
                payload,
                int(
                    getattr(
                        getattr(provider_rs, "tailer", None),
                        "start_offset",
                        0,
                    ) or 0
                ),
            )
        ):
            return StreamEvent("complete", payload)
        await asyncio.sleep(0.1)


async def _compute_jsonl_read_path_off_loop(
    cwd: str,
    agent_sid: str,
    session: Optional[dict],
) -> Optional[Path]:
    return await asyncio.to_thread(compute_jsonl_read_path, cwd, agent_sid, session)


def delegate_error_payload(
    worker_session_id: Optional[str],
    worker_description: str,
    error: str,
) -> dict:
    """Shape an error response for the delegate tool's tool_result."""
    return {
        "success": False,
        "error": error,
        "worker_session_id": worker_session_id,
        "worker_description": worker_description,
        "fork_agent_sid": None,
        "jsonl_path": None,
        "new_byte_offset": 0,
        "total_bytes_now": 0,
        "token_usage": None,
    }


async def _cleanup_ephemeral_delegate_fork(
    *,
    app_session_id: str,
    worker_agent_session_id: str,
    fork_agent_session_id: Optional[str],
    fork_agent_sid: Optional[str],
    ephemeral: bool,
) -> None:
    if (
        not ephemeral
        or not fork_agent_session_id
        or fork_agent_session_id == worker_agent_session_id
    ):
        return

    def _cleanup() -> None:
        if fork_agent_sid:
            fork = session_manager.get(fork_agent_session_id)
            cursor = int(
                ((fork or {}).get("processed_line_by_sid") or {}).get(fork_agent_sid)
                or 0
            )
            if cursor > 0:
                session_manager.advance_processed_lines(
                    app_session_id,
                    fork_agent_sid,
                    cursor,
                    bump_updated_at=False,
                )
        session_manager.delete(fork_agent_session_id)

    try:
        await asyncio.to_thread(_cleanup)
    except Exception:
        logger.exception("ephemeral delegate fork cleanup failed")


def missing_parent_should_run_direct(run_mode: str, worker_session: dict) -> bool:
    return bool(worker_session.get("bare_config") and run_mode != "fork")


def _append_candidate_cwd(candidates: list[str], cwd: Optional[str]) -> None:
    if not cwd:
        return
    resolved = str(Path(cwd).expanduser().resolve())
    if resolved not in candidates:
        candidates.append(resolved)


def _clear_stale_worker_records(
    session_cwd_candidates: list[str],
    worker_session_id: str,
) -> list[str]:
    for candidate_cwd in session_cwd_candidates:
        worker_store.remove_worker(candidate_cwd, worker_session_id)
    return session_fork_store.clear_forks_for_session_everywhere(worker_session_id)


def _find_worker_record(
    session_cwd_candidates: list[str],
    worker_session_id: str,
) -> Optional[tuple[str, dict]]:
    for candidate_cwd in session_cwd_candidates:
        candidate_record = worker_store.get_worker(candidate_cwd, worker_session_id)
        if candidate_record is not None:
            return (candidate_record.get("cwd") or candidate_cwd, candidate_record)
    return None


def _session_registry_cwd_candidates(
    coordinator: "Coordinator",
    app_session_id: str,
    worker_session_id: Optional[str],
    fallback_cwd: str,
    explicit_cwd: Optional[str] = None,
) -> list[str]:
    candidates: list[str] = []
    if explicit_cwd is not None:
        if not isinstance(explicit_cwd, str) or not explicit_cwd.strip():
            raise ValueError("worker_registry_cwd must be a non-empty string")
        expanded = Path(explicit_cwd).expanduser()
        if not expanded.is_absolute():
            raise ValueError("worker_registry_cwd must be absolute")
        _append_candidate_cwd(candidates, str(expanded))
    if worker_session_id is not None:
        resolver = getattr(coordinator, "known_worker_registry_cwd", None)
        if callable(resolver):
            resolved = resolver(app_session_id, worker_session_id)
            if resolved is not None:
                if not isinstance(resolved, str) or not resolved.strip():
                    raise ValueError("known worker registry cwd must be a non-empty string")
                expanded = Path(resolved).expanduser()
                if not expanded.is_absolute():
                    raise ValueError("known worker registry cwd must be absolute")
                _append_candidate_cwd(candidates, str(expanded))
    _append_candidate_cwd(candidates, fallback_cwd)
    return candidates


def lock_for_pair(
    coordinator: "Coordinator",
    caller_agent_session_id: str,
    worker_agent_session_id: str,
) -> asyncio.Lock:
    """Return the per-(caller, worker) serialization lock.

    Held end-to-end across a single delegation: covers the fork
    invalidation check, the fork mint or resume decision, and the
    worker run itself. Two delegations between the same pair are
    serialized; delegations from different callers to the same
    worker (or the same caller to different workers) run in
    parallel because they hit different keys.
    """
    key = (caller_agent_session_id, worker_agent_session_id)
    return coordinator.pair_locks.setdefault(key, asyncio.Lock())


def lock_for_delegation(
    coordinator: "Coordinator",
    caller_agent_session_id: str,
    worker_agent_session_id: str,
    run_mode: str,
    ephemeral: bool = False,
) -> asyncio.Lock:
    if ephemeral:
        return asyncio.Lock()
    if run_mode == "direct":
        return coordinator.pair_locks.setdefault(
            ("direct-worker", worker_agent_session_id),
            asyncio.Lock(),
        )
    return lock_for_pair(coordinator, caller_agent_session_id, worker_agent_session_id)


@perf.timed_fn("delegate.run")
async def run_delegation(
    coordinator: "Coordinator",
    app_session_id: str,
    instructions: str,
    worker_session_id: Optional[str],
    worker_description: str,
    model: str,
    cwd: str,
    provider_id: str = "",
    reasoning_effort: str = "",
    justification: Optional[str] = None,
    proposed_orchestration_mode: Optional[str] = None,
    client_delegation_id: Optional[str] = None,
    node_id: Optional[str] = None,
    run_mode: str = "fork",
    worker_registry_cwd: Optional[str] = None,
    ephemeral: bool = False,
    machine_completion: bool = False,
    provision_prompt: Optional[str] = None,
    provisioned_tool_profile: str = "",
    include_events: bool = False,
) -> dict:
    """Run a worker for one delegate tool call.

    New design (post worker-redesign):
      - `worker_session_id` is a Better Agent session id (NOT a
        claude jsonl sid). In fork mode, the actual claude session
        that runs is a per-(caller, worker) fork of that Better Agent session's
        claude_sid. In direct mode, the worker's own claude session is
        resumed and accumulates context.
      - When `worker_session_id` is None, the manager is asking for
        a brand-new worker. We block on user approval (REST
        approve/deny → in-memory Future); on approve, a fresh BC
        session is created in the chosen mode and used as the
        worker. Nested calls (this delegation is itself running
        inside another) cannot create fresh workers — they must
        resume an existing one.

    Streams worker_start / worker_event / worker_complete to the WS.
    Returns the JSON payload the MCP server forwards to the manager
    as the delegate tool_result. The manager continues to use its
    Read/Grep tail pattern on the returned `jsonl_path` (which
    points at the FORK's jsonl, not the worker's Better Agent jsonl).
    """
    if run_mode not in ("fork", "direct"):
        return delegate_error_payload(
            worker_session_id, worker_description,
            "run_mode must be 'fork' or 'direct'",
        )
    if ephemeral and run_mode != "fork":
        return delegate_error_payload(
            worker_session_id, worker_description,
            "ephemeral is only valid for run_mode='fork'",
        )
    # Capture the turn's save callback ONCE at delegation start.  The
    # runner's HTTP POST blocks until run_delegation returns, so the
    # manager turn cannot finalize mid-delegation — the callback is
    # stable for the entire lifetime of this ws_callback.  Capturing
    # once prevents split-brain where early events go to msg.events
    # (via turn_save) and late events go only to events.jsonl (via
    # persist_and_dispatch_raw) if the callback were re-read per-event
    # and the turn somehow ended mid-stream.
    _turn_save = coordinator.turn_manager.get_turn_save_callback(app_session_id)

    async def ws_callback(event: dict) -> None:
        if _turn_save is not None:
            await _turn_save(event)
        else:
            await coordinator.persist_and_dispatch_raw(app_session_id, event)

    cancel_event = coordinator.turn_manager.cancel_events.get(app_session_id) or asyncio.Event()
    # Prefer the runner-provided id so a backend restart's URLError
    # retry lands on the same delegation_id and re-binds to the
    # existing pending_approvals record (if any). Fall back to a
    # server-minted id when called without one (legacy callers).
    delegation_id = client_delegation_id or f"del_{uuid.uuid4().hex[:10]}"
    instructions_preview = instructions[:2000]
    await delegation_status_store.write_status_async(
        delegation_id,
        status="resolving",
        app_session_id=app_session_id,
        worker_session_id=worker_session_id,
        worker_description=worker_description,
        cwd=cwd,
    )

    # Snapshot the depth BEFORE incrementing — this is the value the
    # nested guard checks. We do NOT increment yet because the
    # approval wait is passive: a manager waiting hours for the user
    # to approve is not "running" and shouldn't block its own retry
    # path if the connection drops mid-await. The actual increment
    # happens below right before the worker runs, scoped to the run
    # via try/finally.
    depth_before = coordinator.active_delegations.get(app_session_id, 0)

    # ------------------------------------------------------
    # Step 1: resolve the target Better Agent session (or get approval for a new worker).
    # ------------------------------------------------------
    if worker_session_id is None:
        caller_session = await asyncio.to_thread(session_manager.get, app_session_id)
        worker_creation_policy = (
            (caller_session or {}).get("worker_creation_policy") or "ask"
        )
        if worker_creation_policy not in ("ask", "approve", "deny"):
            worker_creation_policy = "ask"
        if worker_creation_policy == "deny":
            return delegate_error_payload(
                None, worker_description,
                "Fresh worker creation is auto-denied for this session.",
            )
        if depth_before > 0:
            return delegate_error_payload(
                None, worker_description,
                t("delegation.nested_no_fresh_workers"),
            )
        if not justification or not justification.strip():
            return delegate_error_payload(
                None, worker_description,
                t("delegation.justification_required"),
            )
        if proposed_orchestration_mode == "manager":
            proposed_orchestration_mode = "team"
        if proposed_orchestration_mode not in ("team", "native"):
            return delegate_error_payload(
                None, worker_description,
                t("delegation.orchestration_mode_required"),
            )
        # Resolve effective node_id: explicit arg > session default > "primary".
        effective_node_id = (
            node_id
            or (caller_session.get("node_id") if caller_session else None)
            or "primary"
        )
        if worker_creation_policy == "approve":
            effective_provider_id = provider_id
            if not effective_provider_id:
                effective_provider = await asyncio.to_thread(
                    coordinator.provider_for_session,
                    app_session_id,
                )
                effective_provider_id = effective_provider.id
            approved = await spawn_approved_worker(
                coordinator,
                cwd=cwd,
                model=model,
                mode=proposed_orchestration_mode,
                description=worker_description,
                ws_callback=ws_callback,
                cancel_event=cancel_event,
                delegation_id=delegation_id,
                app_session_id=app_session_id,
                provider_id=effective_provider_id,
                node_id=effective_node_id,
            )
        else:
            # Fresh-worker approval requires a way to surface the
            # approval card to the user.  Without a turn_save callback
            # (live turn) the approval event is persisted to events.jsonl
            # but never shown — the delegation would hang forever on the
            # approval Future. Fail fast unless this session explicitly
            # opted into unattended auto-approval.
            if _turn_save is None:
                return delegate_error_payload(
                    None, worker_description,
                    t("delegation.no_active_turn"),
                )
            approved = await await_fresh_worker_approval(
                coordinator,
                delegation_id=delegation_id,
                app_session_id=app_session_id,
                cwd=cwd,
                justification=justification,
                proposed_description=worker_description,
                proposed_orchestration_mode=proposed_orchestration_mode,
                instructions_preview=instructions_preview,
                model=model,
                ws_callback=ws_callback,
                cancel_event=cancel_event,
                node_id=effective_node_id,
            )
        if approved is None:
            return delegate_error_payload(
                None, worker_description,
                (
                    "Fresh worker auto-approval failed."
                    if worker_creation_policy == "approve"
                    else t("delegation.user_denied_creation")
                ),
            )
        # Approved: a new Better Agent session was created + registered. Use it.
        worker_session_id = approved["agent_session_id"]
        worker_description = approved["description"]
    try:
        session_cwd_candidates = _session_registry_cwd_candidates(
            coordinator, app_session_id, worker_session_id, cwd, worker_registry_cwd,
        )
    except ValueError as e:
        return delegate_error_payload(
            worker_session_id, worker_description, str(e),
        )

    # ------------------------------------------------------
    # Step 2: load the target Better Agent session + its live parent sid.
    # ------------------------------------------------------
    worker_session = await asyncio.to_thread(session_manager.get, worker_session_id)
    if worker_session is None:
        # Stale registry — clean up any candidate records and report.
        stale_forks = await asyncio.to_thread(
            _clear_stale_worker_records,
            session_cwd_candidates,
            worker_session_id,
        )
        _safe_delete_forks(stale_forks, "delete orphan delegate-fork BC %s failed")
        await coordinator.broadcast_workers_changed(None)
        return delegate_error_payload(
            worker_session_id, worker_description,
            t("delegation.worker_bc_deleted", worker_session_id=worker_session_id),
        )
    session_cwd = str(worker_session.get("cwd") or "")
    _append_candidate_cwd(session_cwd_candidates, session_cwd)
    worker_cwd = (
        str(Path(session_cwd).expanduser().resolve())
        if session_cwd else
        (session_cwd_candidates[0] if session_cwd_candidates else cwd)
    )
    worker_record = None
    worker_record_result = await asyncio.to_thread(
        _find_worker_record,
        session_cwd_candidates,
        worker_session_id,
    )
    if worker_record_result is not None:
        worker_cwd, worker_record = worker_record_result
    worker_record = worker_record or {}
    mode = worker_record.get("orchestration_mode") or worker_session.get(
        "orchestration_mode"
    ) or "native"
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        mode = "native"
    display_description = (
        worker_session.get("name") or worker_description or t("session.untitled_worker")
    )
    if run_mode != "direct" and hasattr(coordinator, "provider_for_session"):
        fork_provider = await asyncio.to_thread(
            coordinator.provider_for_session,
            worker_session_id,
        )
        if fork_provider is not None and not getattr(fork_provider, "supports_fork", True):
            return delegate_error_payload(
                worker_session_id,
                display_description,
                f"{getattr(fork_provider, 'KIND', 'This')} provider does not support fork.",
            )
    live_parent_sid = worker_session.get("agent_session_id")
    if not live_parent_sid:
        if missing_parent_should_run_direct(run_mode, worker_session):
            run_mode = "direct"
        else:
            init_cancel_event = asyncio.Event()
            coordinator.init_cancel_events[worker_session_id] = (
                app_session_id,
                init_cancel_event,
            )
            try:
                with perf.timed("delegate.init_target_session"):
                    live_parent_sid = await coordinator._init_target_agent_session(
                        bc_session=worker_session,
                        model=worker_session.get("model") or model,
                        cwd=worker_cwd,
                        description=worker_description,
                        cancel_event=init_cancel_event,
                        ws_callback=ws_callback,
                        provision_prompt=provision_prompt,
                        provisioned_tool_profile=provisioned_tool_profile,
                    )
            finally:
                coordinator.init_cancel_events.pop(worker_session_id, None)
            if not live_parent_sid:
                return delegate_error_payload(
                    worker_session_id, worker_description,
                    t("delegation.worker_no_claude_session", worker_session_id=worker_session_id, mode=mode),
                )
            await asyncio.to_thread(
                session_manager.set_agent_sid,
                worker_session_id,
                mode,
                live_parent_sid,
            )
            if worker_record:
                await asyncio.to_thread(
                    worker_store.upsert_worker,
                    cwd=worker_cwd,
                    agent_session_id=worker_session_id,
                    orchestration_mode=mode,
                    agent_sid=live_parent_sid,
                    node_id=worker_record.get("node_id") or "primary",
                )
                await coordinator.broadcast_workers_changed(None)
    if worker_record and worker_record.get("agent_sid") != live_parent_sid:
        # Refresh the worker_store record + invalidate any forks
        # whose recorded parent sid doesn't match the live one.
        # Preserve the worker's node_id binding when refreshing.
        await asyncio.to_thread(
            worker_store.upsert_worker,
            cwd=worker_cwd,
            agent_session_id=worker_session_id,
            orchestration_mode=mode,
            agent_sid=live_parent_sid,
            node_id=worker_record.get("node_id") or "primary",
        )
        await coordinator.broadcast_workers_changed(None)

    # Use the target Better Agent session's own model so a registered worker
    # with one model isn't silently coerced to the manager's.
    worker_model = worker_session.get("model") or model
    worker_provider_id = worker_session.get("provider_id") or provider_id
    worker_reasoning_effort = worker_session.get("reasoning_effort") or reasoning_effort
    started_at = datetime.now(timezone.utc).isoformat()
    # Inline position in the manager stream where this delegation occurs:
    # the count of manager events already on the in-flight assistant
    # message. The frontend interleaves the panel here (`tagEvents`) so it
    # renders at the delegation point — not stuck at the bottom. Stamped
    # once here (single source of truth) so live, reload, and restore agree.
    insert_at = coordinator.turn_manager.in_flight_event_count_after_current_event(
        app_session_id
    )

    await ws_callback({"type": "worker_start", "data": {
        "delegation_id": delegation_id,
        "worker_session_id": worker_session_id,
        "worker_description": display_description,
        "panel_kind": "worker",
        "started_at": started_at,
        "insert_at": insert_at,
        "orchestration_mode": mode,
        "provider_id": worker_provider_id,
        "model": worker_model,
        "reasoning_effort": worker_reasoning_effort,
        "run_mode": run_mode,
        "is_new": False,
        "instructions_preview": instructions_preview,
    }})

    # Eagerly attach the panel to the in-flight turn's worker
    # list. `save_ws_callback` snapshots `current_turn_workers`
    # into `assistant_msg.workers` on every event, so the
    # in-progress worker (and the events we mutate onto it in
    # `_run_delegation_locked`) is visible to anyone refreshing
    # mid-delegation. Fields populated in-place below:
    #   events / jsonl_path / new_byte_offset / fork_agent_sid /
    #   token_usage.
    panels = coordinator.turn_manager.current_turn_workers.get(app_session_id)
    panel: dict = {
        "delegation_id": delegation_id,
        "worker_session_id": worker_session_id,
        "worker_description": display_description,
        "panel_kind": "worker",
        "started_at": started_at,
        "insert_at": insert_at,
        "orchestration_mode": mode,
        "provider_id": worker_provider_id,
        "model": worker_model,
        "reasoning_effort": worker_reasoning_effort,
        "is_new": False,
        "instructions_preview": instructions_preview,
        "events": [],
        "jsonl_path": None,
        "new_byte_offset": None,
        "fork_agent_sid": None,
        "run_mode": run_mode,
        "token_usage": None,
    }
    if panels is not None:
        panels.append(panel)

    # ------------------------------------------------------
    # Step 3: per-pair lock → resolve fork → run. Counter is
    # incremented HERE (not at function entry) so that the
    # nested-guard reflects only currently-EXECUTING workers,
    # not ones still awaiting user approval. A stranded await
    # (e.g. runner connection drop while approval is pending)
    # therefore can't permanently block its own retry path.
    # ------------------------------------------------------
    coordinator.active_delegations[app_session_id] = (
        coordinator.active_delegations.get(app_session_id, 0) + 1
    )

    # Register this worker run in the per-session run_state. The
    # target_message_id points at the in-flight assistant_msg (if
    # one has been lazily created by now); the delegation_id maps
    # to the worker panel inside that assistant_msg. UI uses these
    # to render a per-panel "running" badge.
    in_flight_aid = coordinator.turn_manager.current_assistant_msgs.get(app_session_id)
    worker_run_id = f"worker-{delegation_id}"
    await delegation_status_store.write_status_async(
        delegation_id,
        status="queued",
        worker_session_id=worker_session_id,
        worker_description=display_description,
        worker_run_id=worker_run_id,
        run_mode=run_mode,
        cwd=worker_cwd,
    )
    await asyncio.to_thread(
        coordinator.turn_manager.run_state_add,
        app_session_id,
        run_id=worker_run_id,
        kind="worker",
        target_message_id=(in_flight_aid or {}).get("id"),
        delegation_id=delegation_id,
    )
    await coordinator.turn_manager.emit_run_state(app_session_id)
    try:
        lock = lock_for_delegation(
            coordinator, app_session_id, worker_session_id, run_mode, ephemeral,
        )
        wait_started = perf.stamp_enq()
        async with lock:
            perf.record_lag("delegate.lock_wait", wait_started)
            return await run_delegation_locked(
                coordinator,
                app_session_id=app_session_id,
                ws_callback=ws_callback,
                cancel_event=cancel_event,
                delegation_id=delegation_id,
                worker_run_id=worker_run_id,
                instructions=instructions,
                instructions_preview=instructions_preview,
                worker_agent_session_id=worker_session_id,
                worker_session=worker_session,
                worker_description=display_description,
                worker_orchestration_mode=mode,
                worker_parent_claude_sid=live_parent_sid,
                session_is_registered_worker=bool(worker_record),
                target_message_id=(in_flight_aid or {}).get("id"),
                run_mode=run_mode,
                model=worker_model,
                cwd=worker_cwd,
                panel=panel,
                ephemeral=ephemeral,
                machine_completion=machine_completion,
                include_events=include_events,
                provider_id=provider_id,
                reasoning_effort=reasoning_effort,
                provisioned_tool_profile=provisioned_tool_profile,
            )
    finally:
        new_depth = coordinator.active_delegations.get(app_session_id, 1) - 1
        if new_depth <= 0:
            coordinator.active_delegations.pop(app_session_id, None)
        else:
            coordinator.active_delegations[app_session_id] = new_depth
        await asyncio.to_thread(
            coordinator.turn_manager.run_state_remove,
            app_session_id,
            worker_run_id,
        )
        try:
            await coordinator.turn_manager.emit_run_state(app_session_id)
        except Exception:
            pass


@perf.timed_fn("delegate.run_locked")
async def run_delegation_locked(
    coordinator: "Coordinator",
    *,
    app_session_id: str,
    ws_callback: Callable[[dict], Awaitable[None]],
    cancel_event: asyncio.Event,
    delegation_id: str,
    worker_run_id: str,
    instructions: str,
    instructions_preview: str,
    worker_agent_session_id: str,
    worker_session: dict,
    worker_description: str,
    worker_orchestration_mode: str,
    worker_parent_claude_sid: Optional[str],
    session_is_registered_worker: bool,
    target_message_id: Optional[str],
    run_mode: str,
    model: str,
    cwd: str,
    panel: dict,
    ephemeral: bool = False,
    machine_completion: bool = False,
    include_events: bool = False,
    provider_id: str = "",
    reasoning_effort: str = "",
    provisioned_tool_profile: str = "",
) -> dict:
    """Inner worker-run body — runs under the per-(caller, worker) lock.

    Resolves the fork (mint or resume), spawns one runner.py
    invocation in the worker's orchestration mode, streams events,
    eagerly persists the fork sid on session_discovered, computes
    the jsonl path/offset for the manager's Read pattern, and
    bumps usage counters on success.
    """
    # ---- Fork/direct resolution + invalidation ------------------
    # Each (caller, worker) pair owns a delegate-fork Better Agent session: an
    # internal-only side branch off the worker, with its own claude_sid.
    # worker_store tracks the mapping; the Better Agent session record carries
    # the invalidation snapshot (forked_from_agent_sid +
    # parent_line_count_at_fork).
    # Route via the read-path helper so remote-pinned workers resolve
    # to primary's shadow file instead of a non-existent local path.
    with perf.timed("delegate.resolve_fork_inputs"):
        parent_jsonl = await _compute_jsonl_read_path_off_loop(
            cwd, worker_parent_claude_sid, worker_session,
        )
        parent_line_count_now = (
            await asyncio.to_thread(count_jsonl_lines, parent_jsonl)
            if parent_jsonl else 0
        )
        fork_record = (
            None if run_mode == "direct" or ephemeral else
            await asyncio.to_thread(
                session_fork_store.get_fork_record,
                cwd,
                app_session_id,
                worker_agent_session_id,
            )
        )
    needs_fork: bool
    resume_sid: Optional[str]
    fork_bc: Optional[dict] = None
    fork_agent_session_id: Optional[str] = None
    if fork_record is not None:
        fork_agent_session_id = fork_record.get("fork_agent_session_id")
        fork_bc = (
            await asyncio.to_thread(session_manager.get, fork_agent_session_id)
            if fork_agent_session_id else None
        )

    if run_mode == "direct":
        needs_fork = False
        resume_sid = worker_parent_claude_sid
        fork_bc = worker_session
        fork_agent_session_id = worker_agent_session_id
    elif ephemeral:
        needs_fork = True
        resume_sid = worker_parent_claude_sid
    elif not fork_bc:
        # No record, or the record points at a deleted Better Agent session.
        needs_fork = True
        resume_sid = worker_parent_claude_sid
        if fork_record is not None:
            await asyncio.to_thread(
                session_fork_store.clear_fork,
                cwd,
                app_session_id,
                worker_agent_session_id,
            )
    else:
        recorded_parent = fork_bc.get("forked_from_agent_sid")
        recorded_count = int(fork_bc.get("parent_line_count_at_fork") or 0)
        recorded_fork_sid = fork_bc.get("agent_session_id")
        if (
            recorded_parent != worker_parent_claude_sid
            or parent_line_count_now > recorded_count
            or not recorded_fork_sid
        ):
            # Worker BC's underlying claude session has rotated or
            # grown since we forked, OR the fork BC never got past
            # session_discovered (no claude_sid stored). Either way,
            # the recorded fork is stale: delete the Better Agent session, drop
            # the worker_store mapping, and mint a fresh fork off the
            # current head.
            try:
                await asyncio.to_thread(session_manager.delete, fork_agent_session_id)
            except Exception:
                logger.exception("invalidating stale fork Better Agent session failed")
            await asyncio.to_thread(
                session_fork_store.clear_fork,
                cwd,
                app_session_id,
                worker_agent_session_id,
            )
            needs_fork = True
            resume_sid = worker_parent_claude_sid
            fork_bc = None
            fork_agent_session_id = None
        else:
            needs_fork = False
            resume_sid = recorded_fork_sid

    # When minting, create the fork Better Agent session NOW so we have a stable
    # agent_session_id to set the claude_sid on once `session_discovered`
    # arrives. Empty messages — the fork is a thread, not a chat copy.
    if needs_fork:
        with perf.timed("delegate.create_delegate_fork"):
            fork_bc = await asyncio.to_thread(
                session_manager.create_delegate_fork,
                parent_agent_session_id=worker_agent_session_id,
                caller_agent_session_id=app_session_id,
                parent_agent_sid_at_fork=worker_parent_claude_sid,
                parent_line_count_at_fork=parent_line_count_now,
                orchestration_mode=worker_orchestration_mode,
            )
            fork_agent_session_id = fork_bc["id"]

    # Pre-run BYTE size of the FORK's jsonl (for new_byte_offset). The caller
    # samples the delta via `tail -c +<size+1>` (O(1) seek). When minting the
    # fork doesn't exist yet → 0; when resuming, measure what's there now.
    pre_run_fork_bytes = 0
    if not needs_fork:
        with perf.timed("delegate.pre_run_fork_bytes"):
            fork_jsonl_now = await _compute_jsonl_read_path_off_loop(
                cwd, resume_sid, fork_bc
            )
            pre_run_fork_bytes = (
                await asyncio.to_thread(jsonl_byte_size, fork_jsonl_now)
                if fork_jsonl_now else 0
            )

    # ---- Spawn the runner --------------------------------------
    run_id = str(uuid.uuid4())
    # TODO: LaggedQueue caused "cannot unpack non-iterable StreamEvent"
    # TypeError — root cause unknown. Re-enable once diagnosed.
    queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    from env_compat import get_env
    worker_backend_url = get_env("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000")
    worker_internal_token = coordinator.internal_token

    provider = await asyncio.to_thread(
        coordinator.provider_for_run,
        worker_agent_session_id,
        provider_id,
    )
    provider_run_config = worker_session.get("provider_run_config") or None
    capability_contexts = worker_session.get("capability_contexts") or None
    reasoning_effort = reasoning_effort or worker_session.get("reasoning_effort")
    if machine_completion:
        worker_prompt = instructions
    else:
        from orchs.manager import bootstrap as manager_bootstrap
        manager_session = await asyncio.to_thread(session_manager.get, app_session_id) or {}
        worker_prompt = "\n\n".join([
            manager_bootstrap.format_team_context(
                cwd=cwd,
                self_session_id=worker_agent_session_id,
                self_role="worker",
                self_description=worker_description,
                manager_session_id=app_session_id,
                manager_description=str(manager_session.get("name") or "manager"),
            ),
            f"<user_prompt>\n{instructions}\n</user_prompt>",
        ])
    try:
        with perf.timed("delegate.provider_start_run"):
            import startup_recovery_gate
            with perf.timed("delegate.provider_start_run.recovery_gate"):
                await startup_recovery_gate.wait_for_recovery_ready()
            if getattr(provider, "suspended", False):
                raise RuntimeError("provider is suspended")
            # Offload the synchronous spawn body (session-manager reads in
            # _build_input_payload, input.json write, Popen) to a worker
            # thread — parity with turn_manager's top-level spawn path.
            # Without this, get_fields blocks on the per-root lock and
            # freezes the asyncio event loop for tens of seconds, hanging
            # the whole app during worker delegations.
            with perf.timed("delegate.provider_start_run.flush_pending_persists"):
                await asyncio.to_thread(session_manager.flush_pending_persists)
            with perf.timed("delegate.provider_start_run.provider_call"):
                await asyncio.to_thread(
                    provider.start_run,
                run_id=run_id,
                prompt=worker_prompt,
                cwd=cwd,
                loop=loop,
                queue=queue,
                model=model,
                reasoning_effort=reasoning_effort,
                session_id=resume_sid,
                mode=worker_orchestration_mode,
                app_session_id=app_session_id,
                backend_url=worker_backend_url,
                internal_token=worker_internal_token,
                fork=needs_fork,
                worker_agent_session_id=(
                    worker_agent_session_id if run_mode == "direct" else None
                ),
                mssg_sender_session_id=None if machine_completion else worker_agent_session_id,
                is_worker=True,
                provider_run_config=provider_run_config,
                capability_contexts=capability_contexts,
                target_message_id=target_message_id,
                    provisioned_tool_profile=provisioned_tool_profile,
                )
    except Exception:
        # start_run failed — no runner to cancel, no run_id to track.
        raise
    coordinator.turn_manager.active_run_ids.setdefault(app_session_id, []).append(run_id)

    # Stamp the runner's OS PID on the worker's run_state entry so
    # consumers can verify liveness.
    provider_rs = provider._runs.get(run_id)
    if provider_rs and provider_rs.popen.pid:
        await delegation_status_store.write_status_async(
            delegation_id,
            status="running",
            worker_run_id=worker_run_id,
            provider_run_id=run_id,
            provider_run_dir=str(provider_rs.run_dir),
            provider_id=provider.id,
            worker_pid=provider_rs.popen.pid,
            worker_agent_session_id=worker_agent_session_id,
            run_mode=run_mode,
            cwd=cwd,
            pre_run_fork_bytes=pre_run_fork_bytes,
        )
        await asyncio.to_thread(
            coordinator.turn_manager.run_state_set_pid,
            app_session_id,
            worker_run_id,
            provider_rs.popen.pid,
        )
        await coordinator.turn_manager.emit_run_state(app_session_id)
        current_status = await asyncio.to_thread(
            delegation_status_store.read_status,
            delegation_id,
        )
        if isinstance(current_status, dict) and current_status.get("cancel_requested") is True:
            cancel_event.set()

    def _remove_run_id() -> None:
        """Remove `run_id` from the per-session active list; drop the
        per-session entry entirely once the last id leaves. Called both
        on success and from the unexpected-error path."""
        run_ids = coordinator.turn_manager.active_run_ids.get(app_session_id)
        if run_ids and run_id in run_ids:
            run_ids.remove(run_id)
            if not run_ids:
                coordinator.turn_manager.active_run_ids.pop(app_session_id, None)

    collected: list[dict] = []
    fork_agent_sid: Optional[str] = None if needs_fork else resume_sid
    cancelled = False
    run_started = perf.stamp_enq()
    first_event_seen = False
    run_started_at = time.perf_counter()
    first_event_ms: Optional[float] = None
    first_tool_ms: Optional[float] = None
    final_answer_ms: Optional[float] = None
    terminal_event_ms: Optional[float] = None

    durable_complete_task: Optional[asyncio.Task[StreamEvent]] = None
    if provider_rs is not None:
        durable_complete_task = asyncio.create_task(
            _wait_for_provider_complete_event(provider_rs),
            name=f"delegate-complete-{run_id[:8]}",
        )

    try:
        with perf.timed("delegate.event_drain"):
            while True:
                get_task = asyncio.create_task(queue.get())
                cancel_task = asyncio.create_task(cancel_event.wait())
                wait_tasks: list[asyncio.Task] = [get_task, cancel_task]
                if durable_complete_task is not None:
                    wait_tasks.append(durable_complete_task)
                event_wait_started = perf.stamp_enq()
                try:
                    done, _ = await asyncio.wait(
                        wait_tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for t in (get_task, cancel_task):
                        if not t.done():
                            t.cancel()

                if cancel_task in done and get_task not in done:
                    cancelled = True
                    # Soft turn-stop: worker runner interrupts, drains,
                    # sweeps own bg, exits. No backend killpg.
                    provider.cancel_turn(run_id)
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=5)
                        event_dict = {"type": event.type, "data": event.data}
                        if not _is_synthetic_event(event_dict):
                            collected.append(event_dict)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    break

                perf.record_lag("delegate.queue_event_wait", event_wait_started)
                if get_task in done:
                    event = get_task.result()
                elif durable_complete_task is not None and durable_complete_task in done:
                    event = durable_complete_task.result()
                    if not get_task.done():
                        get_task.cancel()
                else:
                    event = get_task.result()
                if not first_event_seen:
                    perf.record_lag("delegate.to_first_event", run_started)
                    first_event_seen = True
                    first_event_ms = round((time.perf_counter() - run_started_at) * 1000, 3)
                event_dict = {"type": event.type, "data": event.data}
                if not _is_synthetic_event(event_dict):
                    collected.append(event_dict)
            # NOTE: panel["events"] is NOT mutated here. The
            # `worker_event` WS frame emitted below at line ~604 funnels
            # through `save_ws_callback` → `apply_event` → the
            # `worker_event` branch in `orchs/base.py` → the sole
            # writer `session_manager.apply_worker_panel_event(...)`,
            # which routes to the matching panel by `delegation_id`
            # and uuid-dedups. Mirrors the rule that the primary
            # `msg.events` only mutates via `apply_event`.

                with perf.timed("delegate.event_process"):
                    if event.type == "session_discovered":
                        perf.record_lag("delegate.to_session_discovered", run_started)
                        disc = event.data.get("session_id")
                        if disc:
                            if run_mode == "direct" and fork_agent_sid is None:
                                fork_agent_sid = disc
                                try:
                                    await asyncio.to_thread(
                                        session_manager.set_agent_sid,
                                        worker_agent_session_id,
                                        mode=worker_orchestration_mode,
                                        agent_sid=disc,
                                    )
                                    if session_is_registered_worker:
                                        await asyncio.to_thread(
                                            worker_store.upsert_worker,
                                            cwd=cwd,
                                            agent_session_id=worker_agent_session_id,
                                            orchestration_mode=worker_orchestration_mode,
                                            agent_sid=disc,
                                            node_id=(worker_session_for_path or {}).get("node_id") or "primary",
                                        )
                                except Exception:
                                    logger.exception("direct worker sid persist failed")
                            if needs_fork and fork_agent_sid is None:
                                fork_agent_sid = disc
                                # ORDER MATTERS — the panel mutation below is what
                                # makes the fork discoverable to native_files_manager
                                # via cold-seed (any WS subscriber attaching at this
                                # point will read the panel and open a tailer at the
                                # CURRENT prep-skip cursor). Therefore the prep-skip
                                # MUST be written BEFORE the panel exposes the fork.
                                # Skip parent-inherited lines (the worker's
                                # one-time prep turn) so the per-pair fork's
                                # tailer doesn't re-emit them — the prep is
                                # surfaced live via worker_prep_event frames
                                # instead, rendered in a collapsible block.
                                if parent_line_count_now > 0:
                                    try:
                                        await asyncio.to_thread(
                                            session_manager.advance_processed_lines,
                                            fork_agent_session_id, disc,
                                            int(parent_line_count_now),
                                            bump_updated_at=False,
                                        )
                                    except Exception:
                                        logger.exception(
                                            "advance_processed_lines on fork BC failed"
                                        )
                                try:
                                    await asyncio.to_thread(
                                        session_manager.set_agent_sid,
                                        fork_agent_session_id,
                                        mode=worker_orchestration_mode,
                                        agent_sid=disc,
                                    )
                                except Exception:
                                    logger.exception("set_agent_sid on fork BC failed")
                                # Now expose the fork on the parent panel.
                                try:
                                    # Use the read-path helper so remote-pinned
                                    # forks resolve to the shadow path on primary.
                                    jp_now = await _compute_jsonl_read_path_off_loop(
                                        cwd, disc, fork_bc
                                    )
                                    await delegation_status_store.write_status_async(
                                        delegation_id,
                                        status="running",
                                        fork_agent_sid=disc,
                                        fork_agent_session_id=fork_agent_session_id,
                                        jsonl_path=str(jp_now) if jp_now else None,
                                        new_byte_offset=pre_run_fork_bytes + 1,
                                    )
                                    panel["fork_agent_sid"] = disc
                                    panel["fork_agent_session_id"] = fork_agent_session_id
                                    panel["jsonl_path"] = (
                                        str(jp_now) if jp_now else None
                                    )
                                    panel["new_byte_offset"] = pre_run_fork_bytes + 1
                                except Exception:
                                    logger.exception("eager panel jsonl meta failed")
                                # Publish the fork as a tail target. Both the cold-
                                # seed path (reads panel) and the live path (reads
                                # this event) see the post-prep-skip cursor on the
                                # fork BC record.
                                if panel.get("jsonl_path"):
                                    try:
                                        root_id = await asyncio.to_thread(
                                            session_manager._root_id_for,
                                            app_session_id,
                                        )
                                        await bus.publish(BusEvent(
                                            type="native_files.fork_target",
                                            root_id=root_id or "",
                                            sid=app_session_id,
                                            payload={
                                                "parent_app_session_id": app_session_id,
                                                "fork_agent_sid": disc,
                                                "fork_agent_session_id": fork_agent_session_id,
                                                "jsonl_path": panel["jsonl_path"],
                                            },
                                            persist=False,
                                        ))
                                    except Exception:
                                        logger.exception("fork_target publish failed")
                                if not ephemeral:
                                    try:
                                        await asyncio.to_thread(
                                            session_fork_store.set_fork,
                                            cwd=cwd,
                                            caller_agent_session_id=app_session_id,
                                            session_agent_session_id=worker_agent_session_id,
                                            fork_agent_session_id=fork_agent_session_id,
                                        )
                                    except Exception:
                                        logger.exception("eager fork persist failed")
                            elif run_mode == "direct" and disc == fork_agent_sid:
                                try:
                                    jp_now = await _compute_jsonl_read_path_off_loop(
                                        cwd, disc, fork_bc
                                    )
                                    await delegation_status_store.write_status_async(
                                        delegation_id,
                                        status="running",
                                        fork_agent_sid=disc,
                                        fork_agent_session_id=fork_agent_session_id,
                                        jsonl_path=str(jp_now) if jp_now else None,
                                        new_byte_offset=pre_run_fork_bytes + 1,
                                    )
                                    panel["fork_agent_sid"] = disc
                                    panel["fork_agent_session_id"] = fork_agent_session_id
                                    panel["jsonl_path"] = (
                                        str(jp_now) if jp_now else None
                                    )
                                    panel["new_byte_offset"] = pre_run_fork_bytes + 1
                                except Exception:
                                    logger.exception("direct panel jsonl meta failed")
                            elif not needs_fork and disc != fork_agent_sid:
                                logger.warning(
                                    "delegation: resume sid mismatch — expected %s, "
                                    "got %s",
                                    fork_agent_sid, disc,
                                )
                with perf.timed("delegate.worker_event_callback"):
                    await ws_callback({"type": "worker_event", "data": {
                        "delegation_id": delegation_id,
                        "event": event_dict,
                    }})

                if first_tool_ms is None and _delegation_event_is_tool_activity(event_dict):
                    first_tool_ms = round((time.perf_counter() - run_started_at) * 1000, 3)
                if final_answer_ms is None and _delegation_event_has_final_answer(event_dict):
                    final_answer_ms = round((time.perf_counter() - run_started_at) * 1000, 3)
                if event.type in ("complete", "error"):
                    perf.record_lag("delegate.to_terminal_event", run_started)
                    terminal_event_ms = round((time.perf_counter() - run_started_at) * 1000, 3)
                    break
    except Exception:
        # Never-kill: a backend-side read error must NOT terminate the
        # runner. Drop our tracking ref and re-raise; the runner's own
        # watcher reaps it when its process exits.
        _remove_run_id()
        raise
    finally:
        if durable_complete_task is not None and not durable_complete_task.done():
            durable_complete_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await durable_complete_task

    # ---- Result assembly ---------------------------------------
    with perf.timed("delegate.result_assembly"):
        complete = next((e for e in collected if e["type"] == "complete"), None)
        complete_data = (complete.get("data") or {}) if complete else {}
        success = bool(complete_data.get("success")) and not cancelled
        token_usage = complete_data.get("token_usage")
        error = next(
            ((e.get("data") or {}).get("error") for e in collected if e["type"] == "error"),
            None,
        )
        if not error and complete_data.get("error"):
            error = str(complete_data.get("error"))
        if cancelled and not error:
            error = t("delegation.cancelled")

    # Fall back to complete-event sid if session_discovered didn't fire.
    if not fork_agent_sid and complete:
        fork_agent_sid = complete_data.get("session_id")

    jsonl_path: Optional[Path] = None
    total_bytes_now = 0
    if fork_agent_sid:
        # The MANAGER reads this path via its own Read/Bash tool — must
        # resolve via the fork BC's node_id so remote workers get the
        # shadow path on primary instead of a missing local file.
        with perf.timed("delegate.final_jsonl_bytes"):
            jsonl_path = await _compute_jsonl_read_path_off_loop(
                cwd, fork_agent_sid, fork_bc
            )
            if jsonl_path:
                total_bytes_now = await asyncio.to_thread(
                    jsonl_byte_size, jsonl_path
                )
    # 1-based start byte so the caller samples via `tail -c +new_byte_offset`.
    new_byte_offset = pre_run_fork_bytes + 1

    # Bump worker + fork usage on success.
    if success and not cancelled:
        try:
            if session_is_registered_worker:
                await asyncio.to_thread(
                    worker_store.touch_worker,
                    cwd,
                    worker_agent_session_id,
                    token_usage=token_usage,
                )
            if run_mode != "direct" and not ephemeral:
                await asyncio.to_thread(
                    session_fork_store.touch_fork,
                    cwd,
                    app_session_id,
                    worker_agent_session_id,
                )
            if session_is_registered_worker:
                # delegation_count + last_active changed; let any open
                # Team Orchestration UI reflects the bump without a manual refresh.
                await coordinator.broadcast_workers_changed(None)
        except Exception:
            logger.exception("touch_worker/touch_fork failed")

    result_payload = {
        "success": success,
        "error": error,
        # Hand the Better Agent session id back so the manager refers to the
        # worker by its persistent identity, not the (ephemeral, per-
        # caller) fork sid. The fork sid is internal bookkeeping.
        "worker_session_id": worker_agent_session_id,
        "worker_description": worker_description,
        "fork_agent_sid": fork_agent_sid,
        "run_mode": run_mode,
        "ephemeral": ephemeral,
        "jsonl_path": str(jsonl_path) if jsonl_path else None,
        "new_byte_offset": new_byte_offset,
        "total_bytes_now": total_bytes_now,
        "token_usage": token_usage,
        "sdk_output": complete_data.get("sdk_output"),
        "timings_ms": {
            "runner_enqueue_to_first_event": first_event_ms,
            "runner_enqueue_to_first_tool": first_tool_ms,
            "runner_enqueue_to_final_answer": final_answer_ms,
            "runner_enqueue_to_terminal_event": terminal_event_ms,
        },
    }
    if include_events:
        result_payload["events"] = collected
    try:
        await asyncio.to_thread(
            llm_call_log.append_call,
            source="worker_delegation",
            reason=worker_description or "delegation",
            provider_id=provider.id,
            provider_kind=provider.KIND,
            provider_name=provider.record.get("name"),
            model=model,
            reasoning_effort=reasoning_effort,
            app_session_id=worker_agent_session_id,
            provider_session_id=fork_agent_sid,
            run_id=run_id,
            prompt=instructions,
            token_usage=token_usage,
            success=success,
            error=error,
            metadata={
                "manager_session_id": app_session_id,
                "delegation_id": delegation_id,
                "run_mode": run_mode,
                "ephemeral": ephemeral,
            },
        )
    except Exception:
        logger.exception("failed to append worker llm call log")
    await delegation_status_store.write_status_async(
        delegation_id,
        status="complete",
        result=result_payload,
    )

    await ws_callback({"type": "worker_complete", "data": {
        "delegation_id": delegation_id,
        **result_payload,
    }})

    # (ii) worker-inner terminal — single bus emit through
    # `TurnManager._publish_terminal_lifecycle` so every
    # `lifecycle.turn_*` subscriber sees the worker turn end.
    # Pre-cutover this fact was invisible on the bus; only the parent
    # manager turn's terminal fired. Worker turns are independent units
    # of work — under the per-session lock this collapses safely
    # (no fan-out explosion).
    await coordinator.turn_manager._publish_terminal_lifecycle(
        "complete" if success else "stopped",
        app_session_id=app_session_id,
        reason="worker_inner",
        provider_kind=provider.KIND,
    )

    # Update the eagerly-attached panel in-place. Its `events` array
    # has been growing throughout the run via the streaming loop
    # above, so we don't reassign it here (would clobber the
    # session_watcher's catchup additions). We just patch the
    # late-known metadata.
    panel["jsonl_path"] = str(jsonl_path) if jsonl_path else panel.get("jsonl_path")
    panel["new_byte_offset"] = new_byte_offset if new_byte_offset is not None else panel.get("new_byte_offset")
    if fork_agent_sid:
        panel["fork_agent_sid"] = fork_agent_sid
    panel["token_usage"] = token_usage

    await _cleanup_ephemeral_delegate_fork(
        app_session_id=app_session_id,
        worker_agent_session_id=worker_agent_session_id,
        fork_agent_session_id=fork_agent_session_id,
        fork_agent_sid=fork_agent_sid,
        ephemeral=ephemeral,
    )

    # Clean up run_id from active list now that the worker completed.
    _remove_run_id()

    return result_payload

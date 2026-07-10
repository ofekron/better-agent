"""Fresh-worker approval handshake (manager mode only).

When the manager asks for a brand-new worker via the `delegate` MCP
tool, we block on user approval (REST approve/deny resolves an
in-memory Future, disk-backed via pending_approvals.py so a backend
restart doesn't strand a detached runner). On approve we spawn a new
Better Agent session and run a tiny native init turn to mint its claude_sid;
that sid is what per-pair forks branch off later.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from i18n import t
from stores import pending_approvals, worker_store
from orchs._subprocess_agent import SubprocessAgent
from provisioning.prompts import render_prompt
from provider import StreamEvent
from session_manager import manager as session_manager

if TYPE_CHECKING:
    from orchestrator import Coordinator

logger = logging.getLogger(__name__)


def resolve_approval(
    coordinator: "Coordinator",
    delegation_id: str,
    payload: dict,
) -> bool:
    """Resolve any in-memory Future waiting on this delegation_id.

    Called by the REST approve/deny handler after the disk record
    has been transitioned. Returns True if a waiter was present.
    Idempotent — second call with the same delegation_id is a no-op
    because the future is already done.
    """
    fut = coordinator.approval_waiters.get(delegation_id)
    if fut is None or fut.done():
        return False
    try:
        fut.set_result(payload)
    except asyncio.InvalidStateError:
        # Lost a race with another resolver — already done by now.
        return False
    return True


async def _emit_creation_failed(
    ws_callback: Callable[[dict], Awaitable[None]],
    delegation_id: str,
    error: str,
) -> None:
    """Tell any frontend tab still showing the approval card that it
    should dismiss — sent on user-denial AND on every post-approve
    failure path so the card never lingers."""
    await ws_callback({"type": "worker_creation_failed", "data": {
        "delegation_id": delegation_id,
        "error": error,
    }})


async def await_fresh_worker_approval(
    coordinator: "Coordinator",
    *,
    delegation_id: str,
    app_session_id: str,
    cwd: str,
    justification: str,
    proposed_description: str,
    proposed_orchestration_mode: str,
    instructions_preview: str,
    model: str,
    ws_callback: Callable[[dict], Awaitable[None]],
    cancel_event: asyncio.Event,
    node_id: str = "primary",
) -> Optional[dict]:
    """Block until the user approves or denies fresh worker creation.

    On approve: spawns a new Better Agent session in the chosen mode + runs a
    tiny init turn (synchronously) to mint its claude_sid + registers
    it in worker_store. Returns `{agent_session_id, description}`.

    On deny or cancel: returns None.
    """
    # Re-entry case (backend restart mid-approval): the disk record
    # already exists for this client_delegation_id. If it's been
    # resolved while we were down, short-circuit with the prior
    # answer. If it's still pending, attach a fresh waiter Future
    # and keep waiting — the user's UI is unaffected by the
    # restart, and the runner's URLError-retry put us back here.
    existing = pending_approvals.get(delegation_id)
    if existing is not None:
        status = existing.get("status")
        if status in ("approved", "denied"):
            payload = existing
            if status == "approved":
                chosen_mode = (
                    payload.get("approved_orchestration_mode")
                    or proposed_orchestration_mode
                )
                chosen_description = (
                    payload.get("approved_description") or proposed_description
                )
                # Resolve through `provider_for_session` so the
                # fallback (active) is captured at the spawn site —
                # otherwise a legacy manager with provider_id=None
                # would let the worker drift to whatever's active
                # at first worker-load instead.
                return await spawn_approved_worker(
                    coordinator,
                    cwd=cwd, model=model, mode=chosen_mode,
                    description=chosen_description, ws_callback=ws_callback,
                    cancel_event=cancel_event, delegation_id=delegation_id,
                    app_session_id=app_session_id,
                    provider_id=coordinator.provider_for_session(app_session_id).id,
                    node_id=existing.get("node_id") or node_id,
                    # Reached from await_fresh_worker_approval — the user
                    # approved this fresh-worker popup, so they are aware.
                    user_initiated=True,
                )
            # status == "denied" — emit a creation_failed so any
            # frontend that still has the card showing dismisses
            # it. (The original deny path emits the same event;
            # this covers re-entry from the runner's retry after a
            # backend restart that lost the in-memory waiter.)
            await _emit_creation_failed(
                ws_callback, delegation_id,
                t("delegation.user_denied_fresh_worker"),
            )
            return None
        # status == "pending" → fall through to attach a fresh
        # waiter and re-emit the request event (bring the UI card
        # back if the frontend is now reconnecting too).
    else:
        try:
            pending_approvals.create(
                delegation_id=delegation_id,
                app_session_id=app_session_id,
                cwd=cwd,
                justification=justification,
                proposed_description=proposed_description,
                proposed_orchestration_mode=proposed_orchestration_mode,
                instructions_preview=instructions_preview,
                model=model,
                node_id=node_id,
            )
        except Exception:
            logger.exception("failed to persist pending approval; continuing in-memory")

    await ws_callback({"type": "worker_creation_requested", "data": {
        "delegation_id": delegation_id,
        "app_session_id": app_session_id,
        "justification": justification,
        "proposed_description": proposed_description,
        "proposed_orchestration_mode": proposed_orchestration_mode,
        "instructions_preview": instructions_preview,
        "node_id": node_id,
    }})

    # Set up the future the REST handler resolves. If a stranded
    # prior coroutine was awaiting this same delegation_id (runner
    # connection dropped, second retry came in here), cancel its
    # Future so it can exit cleanly instead of leaking forever.
    loop = asyncio.get_running_loop()
    prior = coordinator.approval_waiters.get(delegation_id)
    if prior is not None and not prior.done():
        prior.cancel()
    fut: asyncio.Future = loop.create_future()
    coordinator.approval_waiters[delegation_id] = fut

    # Re-read disk to short-circuit if the user already answered.
    # Guards: (1) REST approve arriving between prior.cancel() and the
    # new fut assignment, and (2) CLI auto-approve that resolves inside
    # the `await ws_callback(...)` above — before this Future exists.
    latest = pending_approvals.get(delegation_id)
    if (
        latest is not None
        and latest.get("status") in ("approved", "denied")
        and not fut.done()
    ):
        fut.set_result(latest)

    try:
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                [fut, cancel_task], return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
        if cancel_task in done and fut not in done:
            fut.cancel()
            pending_approvals.delete(delegation_id)
            return None
        try:
            payload = fut.result()
        except asyncio.CancelledError:
            # Our Future was cancelled (a newer caller superseded
            # us). Bail without disk side-effects — the new caller
            # owns this approval now.
            return None
    finally:
        # Only clear the registry entry if it still points at OUR
        # Future. A newer caller may have overwritten it.
        if coordinator.approval_waiters.get(delegation_id) is fut:
            coordinator.approval_waiters.pop(delegation_id, None)

    if payload.get("status") != "approved":
        # Tell the UI the card can be dismissed (other tabs that
        # didn't optimistically remove it locally need this signal).
        await _emit_creation_failed(
            ws_callback, delegation_id,
            t("delegation.user_denied_fresh_worker"),
        )
        return None

    chosen_mode = payload.get(
        "approved_orchestration_mode"
    ) or proposed_orchestration_mode
    chosen_description = (
        payload.get("approved_description") or proposed_description
    )
    return await spawn_approved_worker(
        coordinator,
        cwd=cwd, model=model, mode=chosen_mode,
        description=chosen_description, ws_callback=ws_callback,
        cancel_event=cancel_event, delegation_id=delegation_id,
        app_session_id=app_session_id,
        provider_id=coordinator.provider_for_session(app_session_id).id,
        node_id=node_id,
        # The user just approved this fresh-worker popup ("ask" policy).
        user_initiated=True,
    )


async def spawn_approved_worker(
    coordinator: "Coordinator",
    *,
    cwd: str,
    model: str,
    mode: str,
    description: str,
    ws_callback: Callable[[dict], Awaitable[None]],
    cancel_event: asyncio.Event,
    delegation_id: str,
    app_session_id: str,
    provider_id: Optional[str] = None,
    node_id: str = "primary",
    user_initiated: bool = False,
) -> Optional[dict]:
    """Spawn the new Better Agent session + init turn for an approved fresh
    worker request. Extracted so the re-entry path (backend
    restarted, approval already on disk) can reuse it.

    `provider_id` should be the manager session's provider — the
    worker's claude jsonl branches off the manager's, so it must
    live under the same `CLAUDE_CONFIG_DIR`. Defaults to the
    currently-active provider when omitted (legacy callers / tests).

    `user_initiated` is True only when the user explicitly approved a
    fresh-worker popup (the "ask" policy). Under "approve" the worker is
    spawned automatically with no popup, so the user is not aware of it.
    """
    new_bc = session_manager.create(
        name=description, model=model, cwd=cwd, orchestration_mode=mode,
        provider_id=provider_id, node_id=node_id,
        user_initiated=user_initiated,
    )
    # Register a cancel event keyed on the new BC id so DELETE
    # /api/workers/{id} during init can short-circuit the spawn —
    # symmetric with the POST /api/workers init path.
    init_cancel = asyncio.Event()
    coordinator.init_cancel_events[new_bc["id"]] = (app_session_id, init_cancel)

    # Compose with the outer cancel_event so EITHER firing wakes
    # the init.
    async def _wait_either():
        await cancel_event.wait()
        init_cancel.set()
    outer_watcher = asyncio.create_task(_wait_either())

    try:
        try:
            init_claude_sid = await init_target_agent_session(
                coordinator,
                bc_session=new_bc, model=model, cwd=cwd,
                description=description, cancel_event=init_cancel,
                ws_callback=ws_callback,
            )
        except Exception as e:
            logger.exception("failed to initialize new worker Better Agent session")
            session_manager.delete(new_bc["id"])
            await _emit_creation_failed(
                ws_callback, delegation_id,
                t("approval.init_failed", e=str(e)),
            )
            return None
        if not init_claude_sid:
            session_manager.delete(new_bc["id"])
            await _emit_creation_failed(
                ws_callback, delegation_id,
                t("approval.init_no_session_id"),
            )
            return None
    finally:
        coordinator.init_cancel_events.pop(new_bc["id"], None)
        outer_watcher.cancel()
        try:
            await outer_watcher
        except (asyncio.CancelledError, Exception):
            pass
    worker_store.upsert_worker(
        cwd=cwd, agent_session_id=new_bc["id"],
        orchestration_mode=mode, agent_sid=init_claude_sid,
        node_id=node_id,
    )
    await coordinator.broadcast_workers_changed(None)
    await ws_callback({"type": "worker_creation_approved", "data": {
        "delegation_id": delegation_id,
        "agent_session_id": new_bc["id"],
        "description": description,
        "orchestration_mode": mode,
    }})
    return {
        "agent_session_id": new_bc["id"],
        "description": description,
        "orchestration_mode": mode,
    }


async def init_target_agent_session(
    coordinator: "Coordinator",
    *,
    bc_session: dict,
    model: str,
    cwd: str,
    description: str,
    cancel_event: asyncio.Event,
    ws_callback: Optional[Callable[[dict], Awaitable[None]]] = None,
    provision_prompt: Optional[str] = None,
    provisioned_tool_profile: str = "",
) -> Optional[str]:
    """Run a one-time preparation turn to mint a Better Agent session's provider sid
    using the shared SubprocessAgent.init().

    Always native mode for the prep turn — even if the target BC
    session itself is team-mode, the initial sid we need to fork
    off is just whatever claude assigns.
    """
    agent = SubprocessAgent(agent_session_id=bc_session["id"], cwd=cwd)
    mode = bc_session.get("orchestration_mode", "native")
    return await agent.init(
        coordinator,
        model=model,
        prep_prompt=provision_prompt if provision_prompt is not None else render_prompt(
            "worker_prep.md", {"description": description},
        ),
        cancel_event=cancel_event,
        ws_callback=ws_callback,
        mode="native",
        ws_event_prefix="worker",
        create_provisioning_messages=True,
        provisioned_tool_profile=provisioned_tool_profile,
    )

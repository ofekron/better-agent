"""Supervisor — orthogonal verdict loop, NOT an orchestration mode.

When a session has ``supervisor_enabled=True``, every primary turn
(manager or native) is followed by a verdict loop:

  1. Primary runs the user's prompt (manager turn or native turn).
  2. After the primary turn returns, ask the supervisor for a verdict
     (DONE / AWAIT_USER / CONTINUE / FIX). The supervisor is a separate
     Claude session identified by ``supervisor_agent_session_id`` on the
     same session record — lazy-spawned on the first verdict, resumed
     thereafter for context continuity across enable/disable cycles.
  3. If verdict is CONTINUE or FIX, feed the supervisor's instructions
     back as another PRIMARY turn (manager or native, matching the
     session's orchestration_mode). The primary keeps running.
  4. Re-check ``supervisor_enabled`` BEFORE each iteration so a mid-loop
     disable terminates cleanly.
  5. Cap at ``MAX_VERDICTS_PER_TURN`` iterations per user prompt.

DONE and AWAIT_USER end the loop. AWAIT_USER additionally broadcasts a
``supervisor_event`` so the UI can surface the supervisor's reason.

Pending verdict persistence:
  When a user interrupt cancels the primary turn that was acting on a
  CONTINUE/FIX verdict, the verdict is persisted on the session record
  as ``pending_supervisor_verdict``. On the next user-prompted turn,
  ``replay_pending_verdict`` feeds it to the primary BEFORE the normal
  verdict loop resumes — so interrupted supervisor feedback is never
  lost.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from i18n import t
import extension_store
from session_manager import manager as session_manager
from orchs.supervisor._primary import run_primary_turn
from orchs.supervisor._verdict import (
    MAX_VERDICTS_PER_TURN,
    request_review,
    request_verdict,
)

if TYPE_CHECKING:
    from orchestrator import Coordinator

logger = logging.getLogger(__name__)


async def maybe_supervise(
    coordinator: "Coordinator",
    *,
    app_session_id: str,
    ws_callback: Callable[[dict], Awaitable[None]],
) -> None:
    """Post-primary-turn supervisor tail shared by native/manager
    handle_turn: replay any interrupted CONTINUE/FIX verdict, then run
    the verdict loop. Both are no-ops unless ``supervisor_enabled`` is
    set on the session."""
    if not extension_store.is_extension_runtime_ready(
        extension_store.BUILTIN_SUPERVISOR_EXTENSION_ID
    ):
        return
    await replay_pending_verdict(
        coordinator,
        app_session_id=app_session_id,
        ws_callback=ws_callback,
    )
    await maybe_run_verdict_loop(
        coordinator,
        app_session_id=app_session_id,
        ws_callback=ws_callback,
    )


async def replay_pending_verdict(
    coordinator: "Coordinator",
    *,
    app_session_id: str,
    ws_callback: Callable[[dict], Awaitable[None]],
) -> None:
    """If a previous verdict loop was interrupted mid-FIX/CONTINUE,
    replay the pending verdict as a primary turn before the normal
    verdict loop starts.

    Called by orchs/native and orchs/manager handle_turn BEFORE
    ``maybe_run_verdict_loop`` so the pending feedback reaches the
    primary before the supervisor evaluates the new turn.
    """
    session = session_manager.get(app_session_id)
    pending = (session or {}).get("pending_supervisor_verdict") if session else None
    if not pending:
        return
    if not extension_store.is_extension_runtime_ready(
        extension_store.BUILTIN_SUPERVISOR_EXTENSION_ID
    ):
        session_manager.clear_pending_supervisor_verdict(app_session_id)
        return

    verdict = pending.get("verdict", "")
    instructions = pending.get("instructions", "")
    if verdict not in ("CONTINUE", "FIX") or not instructions:
        # Stale or corrupt — clear and skip.
        session_manager.clear_pending_supervisor_verdict(app_session_id)
        return

    if not session.get("supervisor_enabled"):
        # Supervisor was disabled since the verdict was saved — discard.
        session_manager.clear_pending_supervisor_verdict(app_session_id)
        return

    if coordinator.is_session_cancelled(app_session_id):
        return

    logger.info(
        "replaying pending supervisor verdict (%s) for session %s",
        verdict, app_session_id,
    )

    # Clear FIRST so a crash mid-replay doesn't loop forever.
    session_manager.clear_pending_supervisor_verdict(app_session_id)

    await run_primary_turn(
        coordinator,
        app_session_id=app_session_id,
        prompt=(
            f"[Replay of interrupted supervisor {verdict}]\n\n"
            f"{instructions}"
        ),
        ws_callback=ws_callback,
        source="supervisor",
    )


async def maybe_run_verdict_loop(
    coordinator: "Coordinator",
    *,
    app_session_id: str,
    ws_callback: Callable[[dict], Awaitable[None]],
) -> None:
    """Run the verdict loop iff ``supervisor_enabled`` is set on the
    session record. Called by orchs/native and orchs/manager
    handle_turn after the FIRST primary turn returns.

    The loop:
      - re-reads the session before each iteration so a mid-loop toggle
        off terminates the loop cleanly.
      - requests one verdict.
      - DONE / AWAIT_USER: terminate (AWAIT_USER also broadcasts).
      - CONTINUE / FIX: persist as pending, run another primary turn,
        clear pending on success. If interrupted, the pending verdict
        survives for ``replay_pending_verdict`` on the next turn.
      - Caps at MAX_VERDICTS_PER_TURN; broadcasts ``verdict_capped`` on
        hitting the cap.
    """
    for verdict_num in range(MAX_VERDICTS_PER_TURN):
        if not extension_store.is_extension_runtime_ready(
            extension_store.BUILTIN_SUPERVISOR_EXTENSION_ID
        ):
            return
        if coordinator.is_session_cancelled(app_session_id):
            logger.info(
                "supervisor verdict loop: session %s cancelled — bailing",
                app_session_id,
            )
            return

        fresh = session_manager.get(app_session_id)
        if not fresh or not fresh.get("supervisor_enabled"):
            return

        verdict, instructions = await request_verdict(
            coordinator,
            primary_session=fresh,
            ws_callback=ws_callback,
        )

        if verdict == "DONE":
            return

        if verdict == "AWAIT_USER":
            await coordinator.broadcast_session(
                app_session_id,
                "supervisor_event",
                {
                    "session_id": app_session_id,
                    "kind": "await_user",
                    "reason": instructions or "",
                },
                source="supervisor.await_user",
            )
            return

        logger.info(
            "supervisor verdict %d/%d: %s — feeding instructions back to primary",
            verdict_num + 1, MAX_VERDICTS_PER_TURN, verdict,
        )

        # Re-check before spending another primary turn — the user may
        # have flipped the toggle off while the verdict was in-flight.
        check = session_manager.get(app_session_id)
        if not check or not check.get("supervisor_enabled"):
            return

        # Persist BEFORE run_primary_turn so an interrupt preserves it.
        session_manager.set_pending_supervisor_verdict(
            app_session_id, verdict, instructions,
        )

        await run_primary_turn(
            coordinator,
            app_session_id=app_session_id,
            prompt=instructions,
            ws_callback=ws_callback,
            source="supervisor",
        )

        # Primary turn completed (or was interrupted). If NOT cancelled,
        # clear the pending verdict — it was successfully acted on.
        if not coordinator.is_session_cancelled(app_session_id):
            session_manager.clear_pending_supervisor_verdict(app_session_id)
        # If cancelled, the pending verdict stays saved and will be
        # replayed on the next user-prompted turn.

    # Cap reached — broadcast warning.
    logger.warning(
        "supervisor verdict cap (%d) reached for session %s",
        MAX_VERDICTS_PER_TURN, app_session_id,
    )
    await coordinator.broadcast_session(
        app_session_id,
        "supervisor_event",
        {
            "session_id": app_session_id,
            "kind": "verdict_capped",
            "max_per_turn": MAX_VERDICTS_PER_TURN,
            "message": t(
                "supervisor.verdict_capped_message",
                max=MAX_VERDICTS_PER_TURN,
            ),
        },
        source="supervisor.verdict_capped",
    )


__all__ = [
    "maybe_supervise",
    "maybe_run_verdict_loop",
    "replay_pending_verdict",
    "run_primary_turn",
    "request_verdict",
    "request_review",
    "MAX_VERDICTS_PER_TURN",
]

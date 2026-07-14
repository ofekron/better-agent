"""UserPromptManager — authority for the user-prompt lifecycle.

Owns the 5-state state machine of a single in-flight user prompt:

  queued    main.py WS handler accepts the prompt
  sent      TurnManager._drive_cli_run spawns the runner
  received  orchs/base.py strategy sees the first agent token
  done      Coordinator.handle_prompt success / cancel terminal
  failed    Coordinator.handle_prompt exception terminal

State owned:
  - `in_flight_lifecycle_msg_id: dict[sid, msg_id]`
    The active lifecycle correlation id for the in-flight prompt on
    each session. Set when the prompt processor picks the prompt off
    the queue; cleared when handle_prompt returns. Read by:
      - TurnManager.run_turn (header + `user_message_persisted` emit)
      - TurnManager._drive_cli_run (emit_sent)
      - orchs/base.py._fire_user_msg_received_if_pending (emit_received)
      - main.py WS handler (cross-ref new interrupts)

NOT owned (stays on TurnManager):
  - `_interrupted_by_msg_id`. This is a TURN-side handoff:
    `TurnManager.cancel_turn` writes it; `TurnManager._run_turn`
    pops it once on the cancel branch and once in the finally; it
    is consumed by `UPM.emit_user_msg_done` via an explicit
    `interrupted_by_msg_id=` parameter. Keeping it on TurnManager
    eliminates one cross-manager hop per cancel and matches the
    write-side authority.

Single bus.publish funnel:
  `_publish_user_lifecycle` is the SOLE emitter of `user_message_*`
  BusEvents in this process. `user_msg_lifecycle.py` exposes the 5
  payload-shape factories (`emit_queued/_sent/_received/_done/_failed`);
  they all route through `_publish_user_lifecycle` via
  `get_active_coordinator().user_prompt_manager._publish_user_lifecycle`.
  A textual + AST + runtime-spy lock in
  `scripts/test_turn_manager_lifecycle_emit.py` keeps this invariant.

Coupling with Coordinator:
  `self._c` reaches Coordinator only for the
  `_emit_user_msg_lifecycle_*` helpers' historical state lookups —
  currently none, but the back-reference is kept symmetric with
  TurnManager so future Coordinator collaborators can be added
  without rewriting the constructor wire-up.
"""

import logging
from typing import Awaitable, Callable, Optional

from event_bus import BusEvent, bus
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)


class UserPromptManager:
    """Per-coordinator user-prompt lifecycle authority."""

    def __init__(self, coordinator) -> None:
        self._c = coordinator
        # State: in-flight lifecycle correlation id per session.
        self.in_flight_lifecycle_msg_id: dict[str, str] = {}
        # Lifecycle ids that reached `sent` (runner spawned → prompt
        # delivered to the CLI). Lets the cancel/shutdown terminal
        # distinguish "cancelled after delivery" (done) from "aborted
        # before ever reaching the CLI" (failed). Cleared on terminal.
        self._sent_lifecycle_ids: set[str] = set()
        self._done_payloads: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # In-flight lifecycle id API (replaces direct dict mutation).
    # ------------------------------------------------------------------
    def get_in_flight_lifecycle_msg_id(self, app_session_id: str) -> Optional[str]:
        return self.in_flight_lifecycle_msg_id.get(app_session_id)

    def set_in_flight_lifecycle_msg_id(
        self, app_session_id: str, lifecycle_msg_id: str,
    ) -> None:
        self.in_flight_lifecycle_msg_id[app_session_id] = lifecycle_msg_id

    def clear_in_flight_lifecycle_msg_id(self, app_session_id: str) -> None:
        self.in_flight_lifecycle_msg_id.pop(app_session_id, None)

    # ------------------------------------------------------------------
    # Delivery tracking ("sent" reached → prompt is on the CLI).
    # ------------------------------------------------------------------
    def mark_sent(self, lifecycle_msg_id: str) -> None:
        """Record that this prompt's runner spawned (lifecycle `sent`)."""
        self._sent_lifecycle_ids.add(lifecycle_msg_id)

    def was_sent(self, lifecycle_msg_id: str) -> bool:
        """True iff the prompt reached the CLI (runner spawned)."""
        return lifecycle_msg_id in self._sent_lifecycle_ids

    def _clear_sent(self, lifecycle_msg_id: str) -> None:
        self._sent_lifecycle_ids.discard(lifecycle_msg_id)

    def pop_done_payload(self, lifecycle_msg_id: str) -> Optional[dict]:
        return self._done_payloads.pop(lifecycle_msg_id, None)

    async def emit_user_msg_cancel_terminal(
        self,
        app_session_id: str,
        lifecycle_msg_id: str,
        mode: str,
        *,
        interrupted_by_msg_id: Optional[str] = None,
    ) -> None:
        """Terminal emit for a cancel/shutdown abort.

        If the prompt already reached the CLI (`sent` fired, runner
        spawned and possibly recoverable), this is a normal cancelled
        completion → `done(cancelled=True)`. If it was aborted BEFORE
        ever reaching the CLI (e.g. backend shutdown between persist and
        runner spawn), the prompt was never delivered — its terminal
        state must be `failed`, not a success-shaped `done`, so the user
        sees it didn't go through instead of a silent empty bubble.
        """
        if self.was_sent(lifecycle_msg_id):
            await self.emit_user_msg_done(
                app_session_id, lifecycle_msg_id, mode,
                cancelled=True, interrupted_by_msg_id=interrupted_by_msg_id,
            )
            return
        await self.emit_user_msg_failed(
            app_session_id, lifecycle_msg_id,
            reason="aborted_before_send",
        )

    # ------------------------------------------------------------------
    # Sole bus.publish funnel for user_message_* events.
    # ------------------------------------------------------------------
    async def _publish_user_lifecycle(
        self,
        event_type: str,
        *,
        app_session_id: str,
        lifecycle_msg_id: str,
        payload: dict,
        run_id: Optional[str] = None,
    ) -> bool:
        """Sole emitter of `user_message_*` BusEvents.

        Returns True iff the event was published (root_id resolved).
        Returns False if the session has no root (rare race —
        publish-from-deleted-session window); caller may log.
        """
        try:
            root_id = session_manager._root_id_for(app_session_id)
            if root_id is None:
                return False
            await bus.publish(BusEvent(
                type=event_type,
                root_id=root_id,
                sid=app_session_id,
                msg_id=lifecycle_msg_id,
                run_id=run_id,
                payload=payload,
            ))
            return True
        except Exception:
            logger.exception(
                "user_message lifecycle publish failed type=%s sid=%s",
                event_type, app_session_id,
            )
            return False

    # ------------------------------------------------------------------
    # WS frame helper: persisted-ack.
    # ------------------------------------------------------------------
    async def notify_user_msg_persisted(
        self,
        ws_callback: Callable[[dict], Awaitable[None]],
        persist_to: str,
        user_msg: dict,
    ) -> None:
        """Emit the `user_message_persisted` WS frame.

        Relocated from inline `await ws_callback(...)` in
        `TurnManager.run_turn` so the user-prompt-state surface lives
        in one place. The WS frame is the immediate ack that the
        user's prompt is on disk; the frontend uses the `client_id`
        echo on `user_msg` to match the canonical message back to
        its in-flight pending entry and remove it.
        """
        await ws_callback({"type": "user_message_persisted", "data": {
            "session_id": persist_to,
            "user_message": user_msg,
        }})

    # ------------------------------------------------------------------
    # Terminal emits.
    # ------------------------------------------------------------------
    async def emit_user_msg_done(
        self,
        app_session_id: str,
        lifecycle_msg_id: str,
        mode: str,
        *,
        cancelled: bool = False,
        interrupted_by_msg_id: Optional[str] = None,
    ) -> None:
        """Assemble the done payload from the strategy's accumulator and
        publish to the bus. Idempotent on per-strategy accumulator
        (`make_done_payload` pops the entry).

        `interrupted_by_msg_id` is passed in by the caller because
        TurnManager owns the `_interrupted_by_msg_id` state — UPM
        does not reach across to read it.
        """
        try:
            from orchs import get_strategy
            from user_msg_lifecycle import emit_done
            payload = get_strategy(mode).make_done_payload(
                lifecycle_msg_id,
                cancelled=cancelled,
                interrupted_by_msg_id=interrupted_by_msg_id,
            )
            self._done_payloads[lifecycle_msg_id] = dict(payload)
            await emit_done(
                app_session_id=app_session_id,
                lifecycle_msg_id=lifecycle_msg_id,
                **payload,
            )
        except Exception:
            logger.exception("lifecycle: emit_done failed")
        finally:
            self._clear_sent(lifecycle_msg_id)

    async def emit_user_msg_done_from_payload(
        self,
        app_session_id: str,
        lifecycle_msg_id: str,
        payload: dict,
    ) -> None:
        try:
            from user_msg_lifecycle import emit_done
            await emit_done(
                app_session_id=app_session_id,
                lifecycle_msg_id=lifecycle_msg_id,
                success=bool(payload.get("success")),
                cancelled=bool(payload.get("cancelled")),
                error=payload.get("error"),
                duration_ms=payload.get("duration_ms"),
                token_usage_total=payload.get("token_usage_total"),
                sub_turns=payload.get("sub_turns") or [],
                interrupted_by_msg_id=payload.get("interrupted_by_msg_id"),
            )
        except Exception:
            logger.exception("lifecycle: emit cloned done failed")
        finally:
            self._clear_sent(lifecycle_msg_id)

    async def emit_user_msg_failed(
        self,
        app_session_id: str,
        lifecycle_msg_id: str,
        *,
        reason: str,
        error: Optional[str] = None,
    ) -> None:
        try:
            from user_msg_lifecycle import emit_failed
            await emit_failed(
                app_session_id=app_session_id,
                lifecycle_msg_id=lifecycle_msg_id,
                reason=reason,
                error=error,
            )
        except Exception:
            logger.exception("lifecycle: emit_failed failed")
        finally:
            self._clear_sent(lifecycle_msg_id)

"""Base class for subprocess-backed agents (workers, browser-harness, etc.).

Owns a Better Agent session record and its underlying agent-CLI session id
(`agent_sid` — the provider-specific session: claude_sid for Claude,
the Gemini session id for Gemini, etc.). Provides shared lifecycle:
create Better Agent session → init turn (prep prompt) → run turns via detached runner.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from event_shape import (
    extract_output_text as _extract_output_text,
    is_synthetic_event as _is_synthetic_event,
)
import perf
from provider import StreamEvent
from session_manager import manager as session_manager

if TYPE_CHECKING:
    from orchestrator import Coordinator

logger = logging.getLogger(__name__)


class SubprocessAgent:
    """Base for Better Agent sessions that run as detached runners.

    Subclasses set a `prep_prompt` and optionally override how results
    are assembled. The base handles:
      - Better Agent session creation
      - One-time init turn to load context + discover agent_sid
      - Per-turn runner spawn + event streaming
    """

    def __init__(
        self,
        *,
        agent_session_id: str,
        cwd: str,
        extra_env: Optional[dict[str, str]] = None,
    ) -> None:
        self.agent_session_id = agent_session_id
        self.cwd = cwd
        # Provider-specific agent session id (claude_sid for Claude, etc.).
        self.agent_sid: Optional[str] = None
        self.initialized: bool = False
        self.extra_env: Optional[dict[str, str]] = extra_env

    async def _ingest_agent_event(
        self,
        event: StreamEvent,
        *,
        message_id: Optional[str] = None,
    ) -> None:
        """Persist a streamed agent event into the Better Agent session's events.jsonl.
        Control events (session_discovered/complete/error) are skipped —
        those are run-control signals, not session content. UUID dedup
        in event_ingester makes this safe to call alongside any
        background tailer that may later attach to the same file.

        Note: the StreamEvent `type` literal is still "agent_message"
        (set by every provider as the agent-stream envelope on the
        run queue) — that's the on-wire event type carried into
        events.jsonl. Renaming the type is a wider, frontend-impacting
        follow-up; this method's name reflects what it does, not the
        on-wire string."""
        if event.type != "agent_message":
            return
        root_id = session_manager._root_id_for(self.agent_session_id)
        if root_id is None:
            logger.error(
                "subprocess agent ingest skipped — no root for bc_session=%s "
                "(would create orphan events.jsonl); event dropped",
                self.agent_session_id,
            )
            return
        try:
            from event_journal import publish_event
            await publish_event(
                session_id=root_id,
                context_id=self.agent_session_id,
                event_type="agent_message",
                data=event.data,
                source="subprocess_agent",
                message_id=message_id,
            )
        except Exception:
            logger.exception(
                "subprocess agent ingest failed bc_session=%s", self.agent_session_id
            )

    async def init(
        self,
        coordinator: "Coordinator",
        *,
        model: str,
        prep_prompt: str,
        cancel_event: asyncio.Event,
        ws_callback: Optional[Callable[[dict], Awaitable[None]]] = None,
        mode: str = "native",
        ws_event_prefix: str = "agent",
        create_provisioning_messages: bool = False,
    ) -> Optional[str]:
        """Run a one-time preparation turn to load context and discover agent_sid.

        Returns the discovered agent_sid, or None on failure/cancel.
        """
        with perf.timed("subprocess_agent.init"):
            init_started = perf.stamp_enq()
            first_event_seen = False
            run_id = str(uuid.uuid4())
            queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
            loop = asyncio.get_running_loop()
            provider = coordinator.provider_for_session(self.agent_session_id)
            if getattr(provider, "suspended", False):
                raise RuntimeError("provider is suspended")
            reasoning_effort = (
                session_manager.get(self.agent_session_id) or {}
            ).get("reasoning_effort")
            provider_run_config = (
                session_manager.get(self.agent_session_id) or {}
            ).get("provider_run_config") or None
            capability_contexts = (
                session_manager.get(self.agent_session_id) or {}
            ).get("capability_contexts") or None
            target_message_id = (
                coordinator.turn_manager.current_assistant_msgs.get(self.agent_session_id)
                or {}
            ).get("id")
            if create_provisioning_messages:
                assistant_msg = self._create_provisioning_messages(
                    mode=mode,
                    prep_prompt=prep_prompt,
                )
                target_message_id = assistant_msg["id"]
            with perf.timed("subprocess_agent.init.start_run"):
                import startup_recovery_gate
                from env_compat import get_env
                await startup_recovery_gate.wait_for_recovery_ready()
                provider.start_run(
                    run_id=run_id,
                    prompt=prep_prompt,
                    cwd=self.cwd,
                    loop=loop,
                    queue=queue,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    session_id=None,
                    mode=mode,
                    app_session_id=self.agent_session_id,
                    backend_url=get_env("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000"),
                    internal_token=getattr(coordinator, "internal_token", None),
                    extra_env=self.extra_env,
                    provider_run_config=provider_run_config,
                    capability_contexts=capability_contexts,
                    target_message_id=target_message_id,
                )
            discovered: Optional[str] = None
            await coordinator.persist_and_dispatch_raw(
                self.agent_session_id,
                {"type": f"{ws_event_prefix}_prep_start", "data": {
                    "agent_session_id": self.agent_session_id,
                }},
            )
            try:
                while True:
                    get_task = asyncio.create_task(queue.get())
                    cancel_task = asyncio.create_task(cancel_event.wait())
                    event_wait_started = perf.stamp_enq()
                    try:
                        done, _ = await asyncio.wait(
                            [get_task, cancel_task], return_when=asyncio.FIRST_COMPLETED
                        )
                    finally:
                        for t in (get_task, cancel_task):
                            if not t.done():
                                t.cancel()
                    if cancel_task in done and get_task not in done:
                        # Soft turn-stop: runner interrupts, drains, sweeps own
                        # bg, exits. No backend killpg.
                        provider.cancel_turn(run_id)
                        if create_provisioning_messages and target_message_id:
                            session_manager.set_streaming(
                                self.agent_session_id,
                                target_message_id,
                                False,
                            )
                        await coordinator.persist_and_dispatch_raw(
                            self.agent_session_id,
                            {"type": f"{ws_event_prefix}_prep_cancelled", "data": {
                                "agent_session_id": self.agent_session_id,
                            }},
                        )
                        return None
                    perf.record_lag("subprocess_agent.init.queue_event_wait", event_wait_started)
                    event = get_task.result()
                    if not first_event_seen:
                        perf.record_lag("subprocess_agent.init.to_first_event", init_started)
                        first_event_seen = True
                    event_dict = {"type": event.type, "data": event.data}
                    is_synth = _is_synthetic_event(event_dict)
                    if not is_synth:
                        await self._ingest_agent_event(
                            event,
                            message_id=(
                                target_message_id
                                if create_provisioning_messages
                                else None
                            ),
                        )
                    if event.type not in ("session_discovered", "complete", "error"):
                        if not is_synth:
                            try:
                                await coordinator.persist_and_dispatch_raw(
                                    self.agent_session_id,
                                    {"type": f"{ws_event_prefix}_prep_event", "data": {
                                        "agent_session_id": self.agent_session_id,
                                        "event": event_dict,
                                    }},
                                )
                            except Exception:
                                logger.debug("prep event broadcast failed", exc_info=True)
                    if event.type == "session_discovered":
                        perf.record_lag("subprocess_agent.init.to_session_discovered", init_started)
                        discovered = event.data.get("session_id") or discovered
                    if event.type in ("complete", "error"):
                        perf.record_lag("subprocess_agent.init.to_terminal_event", init_started)
                        if event.type == "complete":
                            discovered = event.data.get("session_id") or discovered
                        break
            finally:
                from turn_manager import _release_abandoned_queue
                _release_abandoned_queue(
                    provider, run_id, queue,
                    persist_to=self.agent_session_id,
                )

            if discovered:
                # session_manager's API still uses the legacy `claude_sid`
                # name — leave that call site alone; a wider rename is a
                # separate follow-up.
                session_manager.set_agent_sid(self.agent_session_id, mode, discovered)
                self.agent_sid = discovered
            self.initialized = discovered is not None
            if create_provisioning_messages and target_message_id:
                session_manager.set_streaming(
                    self.agent_session_id,
                    target_message_id,
                    False,
                )
            await coordinator.persist_and_dispatch_raw(
                self.agent_session_id,
                {"type": f"{ws_event_prefix}_prep_complete", "data": {
                    "agent_session_id": self.agent_session_id,
                    "agent_sid": discovered,
                }},
            )
            return discovered

    def _create_provisioning_messages(
        self,
        *,
        mode: str,
        prep_prompt: str,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        user_msg = {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": prep_prompt,
            "timestamp": now,
            "source": "provisioning",
        }
        from orchs import get_strategy
        assistant_msg = get_strategy(mode).build_assistant_scaffold()
        assistant_msg["timestamp"] = now
        assistant_msg["source"] = "provisioning"
        session_manager.append_user_msg(self.agent_session_id, user_msg)
        session_manager.append_assistant_msg(self.agent_session_id, assistant_msg)
        session_manager.set_streaming(
            self.agent_session_id,
            assistant_msg["id"],
            True,
        )
        return assistant_msg

    async def run_turn(
        self,
        coordinator: "Coordinator",
        *,
        prompt: str,
        model: str,
        ws_callback: Callable[[dict], Awaitable[None]],
        cancel_event: asyncio.Event,
        mode: str = "native",
        session_id: Optional[str] = None,
        fork: bool = False,
        backend_url: Optional[str] = None,
        internal_token: Optional[str] = None,
    ) -> dict:
        """Spawn a runner for one turn and stream events until completion.

        Forever-retries on upstream 429 / rate_limit with a fixed 5s
        cadence (cancel-aware). Each attempt re-spawns a fresh runner
        with the most recently discovered agent_sid as the --resume
        target, so the rejected turn stays in the CLI's jsonl and the
        next runner picks up right after it.

        Returns {success, session_id, events, error, token_usage}.
        """
        from turn_helpers import _is_rate_limit_attempt

        loop = asyncio.get_running_loop()
        provider = coordinator.provider_for_session(self.agent_session_id)
        active_run_ids: list[str] = []

        def _drop_run_id(rid: str) -> None:
            """Drop `rid` from the per-bc-session active list; pop the
            per-session entry once the last id leaves. Called per
            attempt and on the exception path."""
            run_ids = coordinator.turn_manager.active_run_ids.get(self.agent_session_id)
            if run_ids and rid in run_ids:
                run_ids.remove(rid)
                if not run_ids:
                    coordinator.turn_manager.active_run_ids.pop(self.agent_session_id, None)
            if rid in active_run_ids:
                active_run_ids.remove(rid)

        collected: list[dict] = []
        discovered: Optional[str] = session_id or self.agent_sid
        current_session_id = session_id or self.agent_sid
        cancelled = False
        success = False
        error: Optional[str] = None
        token_usage: Optional[dict] = None
        complete_data: dict = {}

        while True:
            run_id = str(uuid.uuid4())
            queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
            target_message_id = (
                coordinator.turn_manager.current_assistant_msgs.get(self.agent_session_id)
                or {}
            ).get("id")

            import startup_recovery_gate
            await startup_recovery_gate.wait_for_recovery_ready()
            if getattr(provider, "suspended", False):
                raise RuntimeError("provider is suspended")
            provider.start_run(
                run_id=run_id,
                prompt=prompt,
                cwd=self.cwd,
                loop=loop,
                queue=queue,
                model=model,
                reasoning_effort=(
                    session_manager.get(self.agent_session_id) or {}
                ).get("reasoning_effort"),
                session_id=current_session_id,
                mode=mode,
                app_session_id=self.agent_session_id,
                backend_url=backend_url,
                internal_token=internal_token,
                fork=fork,
                extra_env=self.extra_env,
                provider_run_config=(
                    session_manager.get(self.agent_session_id) or {}
                ).get("provider_run_config") or None,
                capability_contexts=(
                    session_manager.get(self.agent_session_id) or {}
                ).get("capability_contexts") or None,
                target_message_id=target_message_id,
            )
            coordinator.turn_manager.active_run_ids.setdefault(self.agent_session_id, []).append(run_id)
            active_run_ids.append(run_id)

            attempt_events: list[dict] = []
            attempt_cancelled = False

            try:
                while True:
                    get_task = asyncio.create_task(queue.get())
                    cancel_task = asyncio.create_task(cancel_event.wait())
                    try:
                        done, _ = await asyncio.wait(
                            [get_task, cancel_task], return_when=asyncio.FIRST_COMPLETED
                        )
                    finally:
                        for _p in (get_task, cancel_task):
                            if not _p.done():
                                _p.cancel()

                    if cancel_task in done and get_task not in done:
                        attempt_cancelled = True
                        # Soft turn-stop: runner interrupts, drains,
                        # sweeps own bg, exits. No backend killpg.
                        provider.cancel_turn(run_id)
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=5)
                            event_dict = {"type": event.type, "data": event.data}
                            if not _is_synthetic_event(event_dict):
                                attempt_events.append(event_dict)
                                await self._ingest_agent_event(event)
                        except (asyncio.TimeoutError, Exception):
                            pass
                        break

                    event: StreamEvent = get_task.result()
                    event_dict = {"type": event.type, "data": event.data}

                    # Synthetic continuation markers from the SDK carry
                    # "No response requested." — skip them entirely.
                    if _is_synthetic_event(event_dict):
                        pass
                    else:
                        attempt_events.append(event_dict)
                        await self._ingest_agent_event(event)

                        if event.type == "session_discovered":
                            disc = event.data.get("session_id")
                            if disc:
                                discovered = disc

                        await ws_callback({"type": "agent_event", "data": {
                            "agent_session_id": self.agent_session_id,
                            "event": event_dict,
                        }})

                    if event.type in ("complete", "error"):
                        break
            except Exception:
                # Never-kill: a backend-side read error must NOT terminate
                # the runner. Drop our tracking ref and re-raise; the
                # runner's own watcher reaps it when its process exits.
                _drop_run_id(run_id)
                raise
            finally:
                from turn_manager import _release_abandoned_queue
                _release_abandoned_queue(
                    provider, run_id, queue,
                    persist_to=self.agent_session_id,
                )

            collected.extend(attempt_events)
            _drop_run_id(run_id)

            if attempt_cancelled:
                cancelled = True
                break

            complete = next((e for e in attempt_events if e["type"] == "complete"), None)
            complete_data = (complete.get("data") or {}) if complete else {}
            success = bool(complete_data.get("success"))
            token_usage = complete_data.get("token_usage")
            error = next(
                ((e.get("data") or {}).get("error") for e in attempt_events if e["type"] == "error"),
                None,
            ) or complete_data.get("error")

            if not discovered and complete:
                discovered = complete_data.get("session_id")

            if not success and _is_rate_limit_attempt(error, attempt_events):
                # Forever-retry: sleep 5s, cancel-aware, then respawn.
                # Workers don't carry an assistant_msg in session_manager,
                # so the retry is silent — the worker's status will flip
                # back to running on the next attempt's agent_event.
                try:
                    await asyncio.wait_for(cancel_event.wait(), timeout=5)
                    cancelled = True
                    break
                except asyncio.TimeoutError:
                    pass
                current_session_id = discovered or current_session_id
                continue

            break

        if cancelled and not error:
            error = "cancelled"

        if success and discovered and not self.agent_sid:
            self.agent_sid = discovered

        return {
            "success": success and not cancelled,
            "session_id": discovered,
            "events": collected,
            "error": error,
            "token_usage": token_usage,
        }

"""Orchestration strategy — the single render/event strategy for all modes.

Every UI session has one persistent CLI session per turn, resumed by
`agent_session_id`. Events render on `msg.events`; worker panels (from
delegation) on `msg.workers`. The only per-mode behavior is
`wrap_cli_prompt`, which injects the manager BOOTSTRAP + <known_workers>
block when `orchestration_mode == "manager"`; the delegate MCP tool that
makes a session delegation-capable is gated separately in runner.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable, Optional
import uuid

from orchs.base import ApplyEventCtx, OrchestrationStrategy
from session_manager import manager as session_manager

if TYPE_CHECKING:
    from orchestrator import Coordinator


class NativeStrategy(OrchestrationStrategy):

    @property
    def mode(self) -> str:
        return "native"

    @property
    def session_id_field(self) -> str:
        return "agent_session_id"

    @property
    def trace_step_name(self) -> str:
        return "native_turn"

    def wrap_cli_prompt(self, *, cwd: str, prompt: str, session: dict) -> str:
        if (session.get("orchestration_mode") or "team") not in ("team", "manager"):
            return prompt
        # Bare (TestApe-isolated) sessions get an EMPTY system prompt — the
        # caller's prompt is the complete contract, no manager bootstrap.
        if session.get("bare_config"):
            return prompt
        from orchs.manager import bootstrap as manager_bootstrap
        is_first_turn = session.get("agent_session_id") is None
        return manager_bootstrap.build_wrapped_prompt(
            cwd,
            prompt,
            is_first_turn,
            self_session_id=str(session.get("id") or ""),
            self_role="manager",
            self_description=str(session.get("name") or "manager"),
            manager_session_id=str(session.get("id") or ""),
            manager_description=str(session.get("name") or "manager"),
        )

    def build_assistant_scaffold(self) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": "",
            "events": [],
            "timestamp": datetime.now().isoformat(),
            "isStreaming": True,
            "workers": [],
            "agent_session_id": None,
        }

    def _events_list(self, msg: dict) -> list:
        return msg.setdefault("events", [])

    def _events_owner(self, msg: dict) -> dict:
        return msg

    def _append_event(
        self, app_session_id: str, msg_id: str, event: dict,
    ) -> None:
        session_manager.append_native_event(app_session_id, msg_id, event)

    def _replace_event(
        self, app_session_id: str, msg_id: str, event: dict, uuid: str,
    ) -> None:
        session_manager.replace_native_event(app_session_id, msg_id, event, uuid)

    def _after_event(
        self,
        *,
        app_session_id: str,
        msg: dict,
        event: dict,
        ctx: ApplyEventCtx,
        source_is_provider_stream: bool,
    ) -> None:
        msg_id = msg.get("id")
        if not msg_id or ctx.manager_sid_holder is None:
            return
        session_manager.set_agent_sid_on_msg(
            app_session_id, msg_id, ctx.manager_sid_holder.get("id"),
        )

    def finalize_turn(
        self,
        *,
        app_session_id: str,
        assistant_msg: dict,
        primary_result: dict,
    ) -> None:
        msg_id = assistant_msg.get("id")
        if not msg_id:
            return

        primary_sid = primary_result.get("session_id")
        if primary_sid:
            session_manager.set_agent_sid_on_msg(
                app_session_id, msg_id, primary_sid,
            )

        live = session_manager.get_ref(app_session_id) or {}
        for m in live.get("messages") or []:
            if m.get("id") == msg_id:
                continue
            if m.get("role") != "assistant":
                continue
            if not m.get("recovered"):
                continue
            if m.get("agent_session_id") != primary_sid:
                continue
            session_manager.clear_recovered_flag(
                app_session_id, m["id"],
            )
            session_manager.set_streaming(
                app_session_id, m["id"], False,
            )


# ─── backward-compat handle_turn ──────────────────────────────────────

async def handle_turn(
    coordinator: "Coordinator",
    *,
    session: dict,
    prompt: str,
    app_session_id: str,
    model: str,
    cwd: str,
    ws_callback: Callable[[dict], Awaitable[None]],
    provider_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    images: Optional[list],
    files: Optional[list] = None,
    client_id: Optional[str] = None,
    cli_prompt: Optional[str] = None,
    source: Optional[str] = None,
    user_initiated: bool = True,
    disallowed_tools: Optional[list[str]] = None,
    disabled_builtin_extensions: Optional[list[str]] = None,
    queue_item_id: Optional[str] = None,
    team_message: Optional[dict] = None,
    capability_contexts: Optional[list[dict]] = None,
    file_discussion_id: Optional[str] = None,
) -> None:
    from orchs import get_strategy
    run_mode = session.get("orchestration_mode") or "native"
    if run_mode == "manager":
        run_mode = "team"
    if run_mode not in ("team", "native"):
        run_mode = "native"
    # `cli_prompt` lets a caller send the model a DIFFERENT text than the
    # one persisted/displayed as the user message (e.g. the Ask singleton
    # persists the raw query but feeds the model the index+contract
    # wrapper). `cli_prompt or None` preserves the legacy `or prompt`
    # fallback: an empty-string override falls back to `wrap_cli_prompt`
    # (identity for native), not to a blank prompt.
    await get_strategy("native").run_primary(
        coordinator,
        session=session,
        prompt=prompt,
        cli_prompt=cli_prompt or None,
        app_session_id=app_session_id,
        model=model,
        cwd=cwd,
        ws_callback=ws_callback,
        provider_id=provider_id,
        reasoning_effort=reasoning_effort,
        images=images,
        files=files,
        client_id=client_id,
        source=source,
        # user_initiated gates the user-facing nudges (open_file_panel
        # MCP tool, open-todo reminder). True for genuine user prompts;
        # the scheduler passes False. Verdict-loop / review-handoff
        # re-entries via run_primary_turn do NOT pass this.
        user_initiated=user_initiated,
        disallowed_tools=disallowed_tools,
        disabled_builtin_extensions=disabled_builtin_extensions,
        queue_item_id=queue_item_id,
        team_message=team_message,
        capability_contexts=capability_contexts,
        file_discussion_id=file_discussion_id,
        run_mode=run_mode,
    )

    from orchs.supervisor import maybe_supervise
    await maybe_supervise(
        coordinator,
        app_session_id=app_session_id,
        ws_callback=ws_callback,
    )


__all__ = ["handle_turn", "NativeStrategy"]

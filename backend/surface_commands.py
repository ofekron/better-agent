"""Composition-side implementation of `backend.adapters.command_port.
ChatCommandPort` (ADR 0006 command plane).

Lives OUTSIDE `backend/adapters/` on purpose: it reaches
transport-independent collaborators — the orchestrator `Coordinator` and
`session_manager` — that `backend/adapters/*.py` is not allowed to import
directly (see `backend/scripts/test_adapter_boundaries.py`, which grants
this module a permitted-importer exemption alongside `main.py`/
`adapter_api.py`). Imports bare (`import session_manager`), matching
`backend/ws_chat.py`'s style, since both live in the same non-`backend.*`
module namespace.

Ownership: this module owns the transport-independent bodies of the chat
command handlers MOVED out of `backend/ws_chat.py` — the
coordinator/session_manager calls, in that order, for `stop`,
`edit_queued`, `delete_queued`, and (as of the Phase D pass) `send_prompt`.
`backend/ws_chat.py` keeps only frame parsing, connection/session
bookkeeping, and legacy reply-frame formatting; it calls this module's port
instance directly to preserve exact legacy behavior (it does not go
through `ChatSurfaceAdapter.submit()`, whose accept/reject-only contract
does not carry the legacy per-frame outcome).

`send_prompt`'s callback seam: every point where the legacy
`backend/ws_chat.py` `send_message` handler used to send a WS reply frame
is now a `notify(frame_type, payload)` call, in the same order with the
same payloads. `backend/ws_chat.py`'s thin handler supplies a `notify`
that formats/sends exactly as before via its existing `ws_callback` send
helper; `backend/adapters/chat_adapter.py`'s v2 `ChatSurfaceAdapter.submit()`
supplies a `notify` that drops frames (v2 acks are projection-fact based).

Pieces that stayed genuinely bound to the live WebSocket connection (not
moved, passed in as narrow callables instead):
  - `ws_callback` itself (raw, `Callable[[dict], Awaitable[None]]`) — not
    just for reply frames (those go through `notify`), but because the
    legacy handler also *hands the reference itself* to collaborators that
    use it long after this call returns: `params["ws_callback"]` forwarded
    to the coordinator for the turn's own streaming events for the rest of
    its lifetime, `virtual_session_prompt_handlers.handle(dispatch_ws=...)`,
    and the Ask-singleton's deferred `on_user_message` callback. `notify`
    can't stand in for these — they need the raw per-connection sender,
    not a (frame_type, payload) tuple call.
  - `register` (`_register` in ws_chat.py) — registers this WS connection
    as a subscriber for `app_session_id` so `/api/internal/ask-fork` can
    fan worker events out to it. Inherently per-connection bookkeeping.

`set_selectors` and `rewind` are intentionally NOT migrated in this pass —
they return an `unsupported` result so callers treat "not yet supported"
uniformly with any other rejection. `resolve_interaction` (ADR 0006 §5) IS
migrated: it routes to whichever legacy store owns the ref (by namespace
prefix — see `backend/surface_contract/nodes.py`'s `*_REF_PREFIX`
constants), calling the SAME functions the legacy REST decide/approve/
deny/resolve routes call — one mutation path, both transports."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

import config_store
import extension_store
import session_bridge
import session_readiness
import session_search
import tool_approval
import user_prefs
import virtual_session_prompt_handlers
from backend.adapters.command_port import ChatCommandPort, CommandResult, NotifyFn, _default_notify
from backend.event_bus import BusEvent, bus
from backend.surface_contract.intents import (
    ApprovalResponse,
    ChoiceResponse,
    InputResponse,
    InteractionResponse,
    SendTarget,
    SendTargetKind,
)
from backend.surface_contract.nodes import (
    ApprovalDecision,
    Attachment,
    DELEGATE_CHOICE_REF_PREFIX,
    SendMode,
    TOOL_APPROVAL_REF_PREFIX,
    USER_INPUT_REF_PREFIX,
    UserInteractionKind,
    UserInteractionState,
    WORKER_APPROVAL_REF_PREFIX,
    interaction_fact_payload,
)
from capability_contexts import normalize_capability_contexts
from stores import pending_approvals
from env_compat import dual_env
from file_attachment_prompt import MAX_FILE_SIZE_BYTES, validate_file_attachment
from i18n import t
from offline_actions_api import _start_prompt_handoff
from orchestrator import build_semantic_alter_prompt
from session_detail_api import (
    _fallback_ws_send_mode_after_failed_steer,
    _node_offline_error,
    _normalize_ws_send_mode_for_turn_state,
    _parse_ws_disabled_builtin_extensions,
    _parse_ws_disallowed_tools,
    _rewind_latest_user_for_alter,
)
from session_manager import manager as session_manager
import user_input_api
import user_input_store
from user_msg_lifecycle import (
    emit_failed,
    emit_queued,
    emit_requested,
    new_lifecycle_msg_id,
    queued_payload,
)
from virtual_session_prompt_handlers import DispatchWS

logger = logging.getLogger(__name__)

_UNSUPPORTED = "not yet migrated off backend/ws_chat.py"


def _ws_queued_prompt_is_user_visible(kind: str) -> bool:
    return kind != "send"


def _publish_resolved_interaction_fact(
    session_id: str, interaction_ref: str, kind: UserInteractionKind, request: dict,
    *, response: dict, state: UserInteractionState = UserInteractionState.RESOLVED,
) -> None:
    """Same `interaction.fire.resolved` fact `backend/tool_approvals_api.py`'s
    `decide_tool_approval` / `backend/pending_approvals_api.py`'s
    approve/deny routes / `backend/user_input_api.py`'s resolve route
    publish — this v2 resolve path bypasses those REST handlers (calls the
    underlying store mutation directly), so it must publish the SAME fact
    itself rather than silently going live-dark. One generic helper for
    every kind (approval/choice/input) — `kind`/`response`/`state` vary per
    caller, the publish mechanics never do."""
    try:
        bus.publish_threadsafe(BusEvent(
            type="interaction.fire.resolved", root_id=session_id, sid=session_id,
            payload=interaction_fact_payload(
                interaction_ref, kind, request, state=state, response=response,
            ),
            persist=False,
        ))
    except Exception:
        logger.exception("surface_commands: interaction.fire.resolved publish failed")


@dataclass
class _ChatCommandPortImpl:
    coordinator: Any

    async def stop(self, session_id: str) -> CommandResult:
        cancelled = await self.coordinator.turn_manager.cancel_turn(session_id)
        if not cancelled:
            return CommandResult(accepted=False, code="no_active_turn")
        return CommandResult(accepted=True)

    async def edit_queued(self, session_id: str, node_id: str, text: str) -> CommandResult:
        updated = await self.coordinator.update_queued(session_id, node_id, text)
        await asyncio.to_thread(
            session_manager.update_queued_prompt,
            session_id,
            node_id,
            {"content": text},
        )
        self.coordinator.finish_queued_edit(session_id, node_id)
        if not updated:
            return CommandResult(accepted=False, code="not_queued")
        return CommandResult(accepted=True)

    async def delete_queued(self, session_id: str, node_id: str | None = None) -> CommandResult:
        cancelled = self.coordinator.cancel_queued(session_id, node_id)
        await asyncio.to_thread(
            session_manager.remove_queued_prompt,
            session_id,
            node_id,
        )
        if not cancelled:
            return CommandResult(accepted=False, code="not_queued")
        return CommandResult(accepted=True)

    async def send_prompt(
        self,
        session_id: str | None,
        text: str,
        attachments: tuple[Attachment, ...],
        send_mode: SendMode | str,
        target: SendTarget,
        intent_id: str,
        *,
        notify: NotifyFn = _default_notify,
        ws_callback: DispatchWS | None = None,
        register: Any = None,
        images: Any = (),
        files: Any = (),
        model: str | None = None,
        cwd: str | None = None,
        orchestration_mode: str | None = None,
        cli_prompt: str | None = None,
        raw_disallowed_tools: Any = None,
        raw_disabled_builtin_extensions: Any = None,
        backend_url: str | None = None,
        raw_known_worker_registry_cwds: Any = None,
        raw_capability_contexts: Any = None,
        harness_profile_id: str = "",
        orchestrator_send_target: Any = None,
        client_id: str | None = None,
    ) -> CommandResult:
        """Transport-neutral body of the legacy `ws_chat.py` `send_message`
        handler. `intent_id` unifies with the legacy WS transport's
        `client_id`: the v2 contract always supplies a real `IntentId`; the
        legacy transport passes `""`/`None` when the browser omitted
        `client_id`. Every dedup/claim check below is gated on this value
        being truthy, matching the legacy `if client_id:` guards exactly.
        `client_id` is accepted as a separate legacy alias so `ws_chat.py`
        can keep passing the raw WS field name; when unset it falls back to
        `intent_id`.
        """
        coordinator = self.coordinator
        cid = client_id if client_id is not None else (intent_id or None)

        if ws_callback is None:
            async def ws_callback(_frame: dict[str, Any]) -> None:
                return None
        if register is None:
            def register(_sid: str) -> None:
                return None

        if target.kind != SendTargetKind.CURRENT:
            return CommandResult(
                accepted=False,
                code="unsupported_target",
                message="fork/new_session send targets are not yet implemented",
            )
        if attachments:
            return CommandResult(
                accepted=False,
                code="unsupported_attachments",
                message="typed attachments are not yet implemented; legacy images/files only",
            )

        prompt = (text or "").strip()
        images = images or []
        files = files or []

        async def error(message: str) -> CommandResult:
            await notify(
                "error",
                {
                    "error": message,
                    "app_session_id": session_id,
                    "session_id": session_id,
                    "client_id": cid,
                },
            )
            return CommandResult(accepted=False, code="rejected", message=message)

        if not prompt and not images and not files:
            return await error(t("error.ws_empty_prompt"))

        # Validate file attachments — reject oversized or malformed entries.
        for index, f in enumerate(files):
            try:
                validate_file_attachment(f, index, max_size=MAX_FILE_SIZE_BYTES)
            except ValueError as exc:
                return await error(str(exc))

        send_mode = send_mode or await asyncio.to_thread(user_prefs.get_send_mode)
        logger.info(
            "Received message: prompt=%s, images=%d, files=%d, send_mode=%s",
            prompt[:50], len(images), len(files), send_mode,
        )

        if not session_id:
            return await error(t("error.ws_no_session_selected"))
        app_session_id = session_id

        _offline_session = await asyncio.to_thread(session_manager.get_lite, app_session_id)
        _offline_err = await _node_offline_error(_offline_session)
        if _offline_err:
            return await error(_offline_err)
        _provider_id_for_send = (_offline_session or {}).get("provider_id")
        if _provider_id_for_send and config_store.provider_suspended(_provider_id_for_send):
            return await error(t("error.provider_suspended", action="run turns"))
        if _offline_session:
            model = _offline_session.get("model") or model
            cwd = _offline_session.get("cwd") or cwd
            orchestration_mode = _offline_session.get("orchestration_mode") or orchestration_mode
        if orchestration_mode == "manager":
            orchestration_mode = "team"
        if orchestration_mode == "team":
            team_not_ready = extension_store.runtime_not_ready_message(
                extension_store.extension_id_for_role('team-orchestration')
            )
            if team_not_ready is not None:
                return await error(team_not_ready)

        # Ask-singleton entry point: when the user sends a prompt into the
        # singleton session via WS, wrap the prompt with the per-call
        # session index + JSON contract instructions so the LLM emits
        # structured `{session_ids, reasoning}` the frontend AskPicker can
        # render. v1 trade-off: the wrapped prompt persists as
        # user_msg.content on the singleton (the Ask UI hides scrollback so
        # this is not user-visible). Documented in session_search.py.
        try:
            disallowed_tools = _parse_ws_disallowed_tools(raw_disallowed_tools)
            disabled_builtin_extensions = _parse_ws_disabled_builtin_extensions(
                raw_disabled_builtin_extensions
            )
        except ValueError as e:
            return await error(str(e))
        if backend_url is not None:
            if not isinstance(backend_url, str) or not (
                backend_url.startswith("http://127.0.0.1:")
                or backend_url.startswith("http://localhost:")
            ):
                return await error("backend_url must be a loopback HTTP URL")
            backend_url = backend_url.rstrip("/")
            os.environ.update(dual_env("BETTER_CLAUDE_BACKEND_URL", backend_url))
            await asyncio.to_thread(session_manager.set_backend_url, app_session_id, backend_url)
        # NOTE: the Ask session runs NO claude turn of its own. A prompt
        # sent into it is an Ask search, orchestrated after `register`
        # below via `session_search.search()` (appends user+assistant
        # turns, runs an ephemeral search worker, stamps the picker).
        known_worker_registry_cwds = None
        if raw_known_worker_registry_cwds is not None:
            if not isinstance(raw_known_worker_registry_cwds, dict):
                return await error("known_worker_registry_cwds must be an object")
            parsed_worker_registry_cwds: dict[str, str] = {}
            for key, value in raw_known_worker_registry_cwds.items():
                if not isinstance(key, str) or not key:
                    return await error("known_worker_registry_cwds keys must be non-empty strings")
                if not isinstance(value, str) or not value.strip():
                    return await error("known_worker_registry_cwds values must be non-empty strings")
                expanded = Path(value).expanduser()
                if not expanded.is_absolute():
                    return await error("known_worker_registry_cwds values must be absolute paths")
                parsed_worker_registry_cwds[key] = str(expanded.resolve())
            known_worker_registry_cwds = parsed_worker_registry_cwds or None
        try:
            capability_contexts = normalize_capability_contexts(raw_capability_contexts)
        except ValueError as e:
            return await error(str(e))

        # Register this connection so /api/internal/ask-fork can fan out
        # worker events.
        register(app_session_id)

        # Ask search: orchestrate entirely via session_search.search()
        # (appends a user turn + an assistant turn with the picker on the
        # stable Ask session, driven by an ephemeral search worker). No
        # claude turn runs on the Ask session, so skip the normal
        # submit/queue path — the broadcasts from search() drive the UI
        # over this registered connection.
        if app_session_id == session_search.ASK_SINGLETON_ID:
            not_ready_msg = extension_store.runtime_not_ready_message(
                extension_store.BUILTIN_ASK_EXTENSION_ID
            )
            if not_ready_msg is not None:
                return await error(not_ready_msg)
            ask_client_id = cid
            already_done = await asyncio.to_thread(
                session_search.find_user_message_by_client_id, ask_client_id,
            )
            if already_done:
                await notify(
                    "user_message_persisted",
                    {"session_id": app_session_id, "user_message": already_done},
                )
                return CommandResult(accepted=True, code="already_persisted")

            async def _ack_ask_user_message(user_message: dict) -> None:
                await notify(
                    "user_message_persisted",
                    {"session_id": app_session_id, "user_message": user_message},
                )

            asyncio.create_task(
                session_search.search(
                    prompt,
                    client_id=ask_client_id,
                    lifecycle_msg_id=new_lifecycle_msg_id(),
                    on_user_message=_ack_ask_user_message,
                )
            )
            return CommandResult(accepted=True, code="ask_dispatched")

        virtual_prompt_handled = await virtual_session_prompt_handlers.handle(
            app_session_id,
            prompt=prompt,
            cwd=cwd,
            client_id=cid,
            lifecycle_msg_id=new_lifecycle_msg_id(),
            dispatch_ws=ws_callback,
        )
        if virtual_prompt_handled:
            return CommandResult(accepted=True, code="virtual_session_handled")

        # Dedup: if a client_id/intent_id is present, check whether this
        # prompt was already processed or is already queued. Prevents
        # duplicate turns when the frontend re-flushes its offline backlog
        # after a WS reconnect or page reload.
        if cid:
            import session_queue_projection
            _sess = await asyncio.to_thread(
                session_queue_projection.get,
                app_session_id,
                storage_identity=session_manager._root_repository.storage_identity(),
            )
            if _sess:
                _already_done = None
                for _m in _sess.get("user_messages", []):
                    if _m.get("client_id") == cid:
                        _already_done = _m
                        break
                if _already_done:
                    await notify(
                        "user_message_persisted",
                        {"session_id": app_session_id, "user_message": _already_done},
                    )
                    return CommandResult(accepted=True, code="already_persisted")

                _already_queued = None
                for _qp in _sess.get("queued_prompts", []):
                    if _qp.get("client_id") == cid:
                        _already_queued = _qp
                        break
                if _already_queued:
                    _already_lifecycle_msg_id = _already_queued.get("lifecycle_msg_id")
                    _already_kind = _already_queued.get("kind") or "queued_behind"
                    if _already_lifecycle_msg_id:
                        await notify(
                            "user_message_queued",
                            {
                                "app_session_id": app_session_id,
                                **queued_payload(
                                    lifecycle_msg_id=_already_lifecycle_msg_id,
                                    content=_already_queued.get("content", ""),
                                    kind=_already_kind,
                                    queue_position=coordinator.get_queued_count(app_session_id),
                                    client_id=cid,
                                    images_count=int(_already_queued.get("images_count") or 0),
                                    orchestration_mode=_already_queued.get("orchestration_mode"),
                                ),
                            },
                        )
                    if _ws_queued_prompt_is_user_visible(_already_kind):
                        await notify(
                            "prompt_queued",
                            {
                                "app_session_id": app_session_id,
                                "queued_id": _already_queued.get("id"),
                                "prompt_preview": _already_queued.get("content", ""),
                                "send_mode": send_mode,
                                "queue_position": coordinator.get_queued_count(app_session_id),
                                "client_id": cid,
                            },
                        )
                    return CommandResult(accepted=True, code="already_queued")

            _already_active = coordinator.active_prompt_for_client_id(app_session_id, cid)
            if _already_active:
                _already_lifecycle_msg_id = _already_active.get("lifecycle_msg_id")
                if _already_lifecycle_msg_id:
                    await notify(
                        "user_message_queued",
                        {
                            "app_session_id": app_session_id,
                            **queued_payload(
                                lifecycle_msg_id=_already_lifecycle_msg_id,
                                content=prompt,
                                kind="send",
                                queue_position=0,
                                client_id=cid,
                                images_count=len(images),
                                orchestration_mode=orchestration_mode,
                            ),
                        },
                    )
                return CommandResult(accepted=True, code="already_active")

        # has_active_runs (not has_active_turn): also covers the
        # dequeue→cancel_events gap and recovered live runs, so an
        # interrupt can't silently degrade to a plain queue. A new prompt
        # is the user retrying, so give a session whose provisioning
        # failed another attempt rather than rejecting every prompt on it
        # forever.
        if session_readiness.fork_provisioning_failed(app_session_id):
            import file_editor
            await file_editor.rearm_fork_provisioning_if_failed(app_session_id)

        is_queued = (
            coordinator.turn_manager.has_active_turn(app_session_id)
            or coordinator.turn_manager.has_active_runs(app_session_id)
            or session_readiness.is_awaiting_fork_provisioning(app_session_id)
        )

        lifecycle_msg_id = new_lifecycle_msg_id()
        alter_rewind_latest = False
        if send_mode == "alter":
            _alter_session = await asyncio.to_thread(session_manager.get_lite, app_session_id)
            queued_prompts = (_alter_session or {}).get("queued_prompts") or []
            latest_queued = queued_prompts[-1] if queued_prompts else None
            if latest_queued:
                lifecycle_msg_id = latest_queued.get("lifecycle_msg_id") or lifecycle_msg_id
                queued_id = await coordinator.update_latest_queued(
                    app_session_id, prompt, cli_prompt, cid, lifecycle_msg_id, capability_contexts,
                )
                if not queued_id:
                    return await error(t("error.ws_no_queued_prompt"))
                await asyncio.to_thread(
                    session_manager.update_queued_prompt,
                    app_session_id,
                    queued_id,
                    {
                        "content": prompt,
                        "cli_prompt": cli_prompt,
                        "client_id": cid,
                        "lifecycle_msg_id": lifecycle_msg_id,
                        "capability_contexts": capability_contexts,
                    },
                )
                await notify(
                    "steer_prompt_persisted",
                    {
                        "app_session_id": app_session_id,
                        "client_id": cid,
                        "lifecycle_msg_id": lifecycle_msg_id,
                    },
                )
                return CommandResult(accepted=True, code="steer_prompt_persisted")

            if is_queued:
                alter_rewind_latest = True
                send_mode = "queue"
            else:
                try:
                    rewind_data = await _rewind_latest_user_for_alter(app_session_id)
                except HTTPException as e:
                    return await error(str(e.detail))
                model = rewind_data.get("retry_model") or model
                cwd = rewind_data.get("retry_cwd") or cwd
                orchestration_mode = (
                    rewind_data.get("retry_orchestration_mode") or orchestration_mode
                )
                previous_prompt = rewind_data.get("semantic_alter_previous_prompt")
                if previous_prompt is not None:
                    cli_prompt = build_semantic_alter_prompt(
                        str(previous_prompt), cli_prompt or prompt,
                    )
                send_mode = "queue"

        send_mode = _normalize_ws_send_mode_for_turn_state(send_mode, is_queued)
        if send_mode == "steer":
            # Steer injects into the live turn and never reaches the claim
            # below, so dedup it here: a concurrent same-client_id steer
            # (offline re-dispatch after a reconnect) must not be injected
            # twice. One-shot — release right after.
            _steer_item = str(uuid.uuid4())
            if cid and coordinator.try_claim_prompt_client_id(app_session_id, _steer_item, cid):
                return CommandResult(accepted=True, code="duplicate_steer_claim")
            try:
                _steered = await coordinator.steer_active_turn(
                    app_session_id=app_session_id,
                    prompt=cli_prompt or prompt,
                    display_prompt=prompt,
                    images=images if images else None,
                    files=files if files else None,
                    client_id=cid,
                    lifecycle_msg_id=lifecycle_msg_id,
                )
            finally:
                if cid:
                    coordinator._forget_active_prompt_item(_steer_item)
            if _steered:
                return CommandResult(accepted=True, code="steered")
            root_id = (
                await asyncio.to_thread(session_manager._root_id_for, app_session_id)
                or app_session_id
            )
            await coordinator.turn_manager.lifecycle.publish(
                "lifecycle.steer_fallback_queued",
                root_id=root_id,
                session_id=app_session_id,
                message_id=lifecycle_msg_id,
                payload={"reason": "steer_rejected"},
            )
            send_mode = _fallback_ws_send_mode_after_failed_steer(send_mode)

        if alter_rewind_latest:
            lifecycle_kind = "interrupt"
        elif send_mode == "interrupt" and is_queued:
            lifecycle_kind = "interrupt"
        elif is_queued:
            lifecycle_kind = "queued_behind"
        else:
            lifecycle_kind = "send"
        queue_position = coordinator.get_queued_count(app_session_id)
        interrupts_msg_id = (
            coordinator.user_prompt_manager.get_in_flight_lifecycle_msg_id(app_session_id)
            if lifecycle_kind == "interrupt" else None
        )
        lifecycle_queued_payload = queued_payload(
            lifecycle_msg_id=lifecycle_msg_id,
            content=prompt,
            kind=lifecycle_kind,
            queue_position=queue_position,
            client_id=cid,
            interrupts_msg_id=interrupts_msg_id,
            images_count=len(images),
            orchestration_mode=orchestration_mode,
        )

        item_id = str(uuid.uuid4())
        # Atomic admission gate: claim the client_id BEFORE
        # persisting/emitting anything. A concurrent same-client_id send
        # (offline re-dispatch after a reconnect) that arrives while this
        # turn is in flight would otherwise slip past the read-only dedup
        # checks above and broadcast a phantom queued bubble before
        # submit_prompt deduped it. Claiming here makes the dedup
        # authoritative under concurrency.
        _claim_cid = cid
        if _claim_cid:
            _dup_of = coordinator.try_claim_prompt_client_id(app_session_id, item_id, _claim_cid)
            if _dup_of:
                _dup_lifecycle = (
                    coordinator.user_prompt_manager.get_in_flight_lifecycle_msg_id(app_session_id)
                )
                if _dup_lifecycle:
                    await notify(
                        "user_message_queued",
                        {
                            "app_session_id": app_session_id,
                            **queued_payload(
                                lifecycle_msg_id=_dup_lifecycle,
                                content=prompt,
                                kind="send",
                                queue_position=0,
                                client_id=_claim_cid,
                                images_count=len(images),
                                orchestration_mode=orchestration_mode,
                            ),
                        },
                    )
                return CommandResult(accepted=True, code="already_active")
        await emit_requested(
            app_session_id=app_session_id,
            lifecycle_msg_id=lifecycle_msg_id,
            content=prompt,
            kind=lifecycle_kind,
            queue_position=queue_position,
            client_id=cid,
            interrupts_msg_id=interrupts_msg_id,
            images_count=len(images),
            orchestration_mode=orchestration_mode,
        )
        # Sample the active provider after the acceptance fact so
        # environment refresh cannot delay the running projection. This
        # remains before durable admission and CLI spawn.
        await asyncio.to_thread(config_store.apply_env_vars)
        params = {
            "prompt": prompt,
            "app_session_id": app_session_id,
            "model": model,
            "cwd": cwd,
            "ws_callback": ws_callback,
            "images": images if images else None,
            "files": files if files else None,
            "orchestration_mode": orchestration_mode,
            "send_target": orchestrator_send_target,
            "client_id": cid,
            "lifecycle_msg_id": lifecycle_msg_id,
            "cli_prompt": cli_prompt,
            "disallowed_tools": disallowed_tools,
            "disabled_builtin_extensions": disabled_builtin_extensions,
            "known_worker_registry_cwds": known_worker_registry_cwds,
            "capability_contexts": capability_contexts,
            "harness_profile_id": harness_profile_id,
            "_queued_id": item_id,
            "_client_id_claimed": bool(_claim_cid),
        }

        # From the claim above until submit_prompt hands the claim's
        # release to turn-end, any failure must release the claim —
        # otherwise the offline backlog's same-client_id re-dispatch would
        # be deduped and the prompt silently lost. Fail closed toward NOT
        # losing user intent.
        def _release_claim_on_failure() -> None:
            if _claim_cid:
                coordinator._forget_active_prompt_item(item_id)

        if send_mode == "interrupt" and is_queued:
            params["_interrupt"] = True
            # Cancel the current turn so the processor picks this up
            # sooner. Pass the incoming lifecycle id so the displaced
            # turn's done event carries the cross-ref.
            try:
                await coordinator.turn_manager.cancel_turn(
                    app_session_id, interrupted_by_msg_id=lifecycle_msg_id,
                )
            except Exception as exc:
                _release_claim_on_failure()
                await emit_failed(
                    app_session_id=app_session_id,
                    lifecycle_msg_id=lifecycle_msg_id,
                    reason="interrupt_failed",
                    error=str(exc),
                )
                raise
        elif alter_rewind_latest:
            params["_alter_rewind_latest"] = True
            try:
                await coordinator.turn_manager.cancel_turn(
                    app_session_id, interrupted_by_msg_id=lifecycle_msg_id,
                )
            except Exception as exc:
                _release_claim_on_failure()
                await emit_failed(
                    app_session_id=app_session_id,
                    lifecycle_msg_id=lifecycle_msg_id,
                    reason="alter_interrupt_failed",
                    error=str(exc),
                )
                raise

        # Hand off to the coordinator-owned per-session processor. It
        # survives this call's lifetime — a disconnect deregisters the
        # ws_callback but the processor keeps running and the detached
        # runner keeps writing events into the persisted session JSON, so a
        # reconnect+refetch shows the same content as live.
        queued_prompt = {
            "id": item_id,
            "lifecycle_msg_id": lifecycle_msg_id,
            "content": prompt,
            "kind": lifecycle_kind,
            "queue_position": queue_position,
            "images_count": len(images),
            "files_count": len(files),
            "images": images if images else None,
            "files": files if files else None,
            "orchestration_mode": orchestration_mode,
            "send_target": orchestrator_send_target,
            "cli_prompt": cli_prompt,
            "disallowed_tools": disallowed_tools,
            "disabled_builtin_extensions": disabled_builtin_extensions,
            "client_id": cid,
            "alter_rewind_latest": alter_rewind_latest,
            "capability_contexts": capability_contexts,
            "harness_profile_id": harness_profile_id,
            "created_at": datetime.now().isoformat(),
        }
        try:
            admission = await _start_prompt_handoff(app_session_id, queued_prompt, params)
        except Exception as exc:
            _release_claim_on_failure()
            await emit_failed(
                app_session_id=app_session_id,
                lifecycle_msg_id=lifecycle_msg_id,
                reason="durable_admission_failed",
                error=str(exc),
            )
            raise
        if not admission.get("session"):
            _release_claim_on_failure()
            await emit_failed(
                app_session_id=app_session_id,
                lifecycle_msg_id=lifecycle_msg_id,
                reason="session_not_found",
            )
            return await error(t("error.session_not_found_retry"))
        existing_user_message = admission.get("existing_user_message")
        if existing_user_message:
            _release_claim_on_failure()
            await emit_failed(
                app_session_id=app_session_id,
                lifecycle_msg_id=lifecycle_msg_id,
                reason="duplicate_user_message",
            )
            await notify(
                "user_message_persisted",
                {"session_id": app_session_id, "user_message": existing_user_message},
            )
            return CommandResult(accepted=True, code="already_persisted")
        existing_queued_prompt = admission.get("existing_queued_prompt")
        if existing_queued_prompt:
            _release_claim_on_failure()
            await emit_failed(
                app_session_id=app_session_id,
                lifecycle_msg_id=lifecycle_msg_id,
                reason="duplicate_queued_prompt",
            )
            existing_lifecycle_msg_id = existing_queued_prompt.get("lifecycle_msg_id")
            existing_kind = existing_queued_prompt.get("kind") or "queued_behind"
            if existing_lifecycle_msg_id:
                await notify(
                    "user_message_queued",
                    {
                        "app_session_id": app_session_id,
                        **queued_payload(
                            lifecycle_msg_id=existing_lifecycle_msg_id,
                            content=existing_queued_prompt.get("content", ""),
                            kind=existing_kind,
                            queue_position=coordinator.get_queued_count(app_session_id),
                            client_id=cid,
                            images_count=int(existing_queued_prompt.get("images_count") or 0),
                            orchestration_mode=existing_queued_prompt.get("orchestration_mode"),
                        ),
                    },
                )
            if _ws_queued_prompt_is_user_visible(existing_kind):
                await notify(
                    "prompt_queued",
                    {
                        "app_session_id": app_session_id,
                        "queued_id": existing_queued_prompt.get("id"),
                        "prompt_preview": existing_queued_prompt.get("content", ""),
                        "send_mode": send_mode,
                        "queue_position": coordinator.get_queued_count(app_session_id),
                        "client_id": cid,
                    },
                )
            return CommandResult(accepted=True, code="already_queued")
        if not is_queued:
            try:
                await emit_queued(
                    app_session_id=app_session_id,
                    lifecycle_msg_id=lifecycle_msg_id,
                    content=prompt,
                    kind=lifecycle_kind,
                    queue_position=queue_position,
                    client_id=cid,
                    interrupts_msg_id=interrupts_msg_id,
                    images_count=len(images),
                    orchestration_mode=orchestration_mode,
                )
            except Exception:
                logger.exception("lifecycle: emit_queued failed")
        await notify(
            "user_message_queued",
            {"app_session_id": app_session_id, **lifecycle_queued_payload},
        )
        if is_queued:
            try:
                await emit_queued(
                    app_session_id=app_session_id,
                    lifecycle_msg_id=lifecycle_msg_id,
                    content=prompt,
                    kind=lifecycle_kind,
                    queue_position=queue_position,
                    client_id=cid,
                    interrupts_msg_id=interrupts_msg_id,
                    images_count=len(images),
                    orchestration_mode=orchestration_mode,
                )
            except Exception:
                logger.exception("lifecycle: emit_queued failed")

        # Notify the frontend about queue state
        if is_queued:
            await notify(
                "prompt_queued",
                {
                    "app_session_id": app_session_id,
                    "queued_id": item_id,
                    "prompt_preview": prompt,
                    "send_mode": send_mode,
                    "queue_position": queue_position,
                    "client_id": cid,
                },
            )
        return CommandResult(accepted=True)

    async def set_selectors(self, session_id, **selectors) -> CommandResult:
        return CommandResult(accepted=False, code="unsupported", message=_UNSUPPORTED)

    async def rewind(self, session_id, node_id) -> CommandResult:
        return CommandResult(accepted=False, code="unsupported", message=_UNSUPPORTED)

    async def resolve_interaction(
        self, session_id: str, interaction_ref: str, response: InteractionResponse,
    ) -> CommandResult:
        """Dispatches by `interaction_ref`'s namespace prefix — the 4
        legacy mechanisms share no id space of their own, so the prefix
        (stamped by the SAME producers, see `backend/adapters/store_access.
        py`'s `list_pending_interactions` and `backend/adapters/chat_adapter.
        py`'s `_on_ask_result_set`) is the single routing key."""
        if interaction_ref.startswith(TOOL_APPROVAL_REF_PREFIX):
            approval_id = interaction_ref[len(TOOL_APPROVAL_REF_PREFIX):]
            return await self._resolve_tool_approval(session_id, approval_id, response)
        if interaction_ref.startswith(WORKER_APPROVAL_REF_PREFIX):
            delegation_id = interaction_ref[len(WORKER_APPROVAL_REF_PREFIX):]
            return await self._resolve_worker_approval(session_id, delegation_id, response)
        if interaction_ref.startswith(DELEGATE_CHOICE_REF_PREFIX):
            delegation_id = interaction_ref[len(DELEGATE_CHOICE_REF_PREFIX):]
            return await self._resolve_delegate_choice(session_id, delegation_id, response)
        if interaction_ref.startswith(USER_INPUT_REF_PREFIX):
            request_id = interaction_ref[len(USER_INPUT_REF_PREFIX):]
            return await self._resolve_user_input(session_id, request_id, response)
        return CommandResult(
            accepted=False, code="unknown_interaction",
            message=f"no owning store for interaction_ref {interaction_ref!r}",
        )

    async def _resolve_tool_approval(
        self, session_id: str, approval_id: str, response: InteractionResponse,
    ) -> CommandResult:
        """Same mutation `POST /api/sessions/{id}/tool-approvals/{id}/decide`
        performs (`backend/tool_approvals_api.py`'s `decide_tool_approval`) —
        object-level authz identical too (approval must belong to
        `session_id`)."""
        if not isinstance(response, ApprovalResponse):
            return CommandResult(
                accepted=False, code="invalid_response",
                message="a tool approval requires an approval response",
            )
        rec = tool_approval.registry.get(approval_id)
        if rec is None or rec.app_session_id != session_id:
            return CommandResult(accepted=False, code="not_found")
        approved = response.decision == ApprovalDecision.APPROVE
        ok = tool_approval.registry.decide(approval_id, approved)
        if not ok:
            return CommandResult(accepted=False, code="already_resolved")
        _publish_resolved_interaction_fact(
            session_id, f"{TOOL_APPROVAL_REF_PREFIX}{approval_id}", UserInteractionKind.APPROVAL,
            {
                "subject": "tool", "tool_name": rec.tool_name,
                "summary": dict(rec.summary or {}), "risk_scope": rec.provider_kind,
            },
            response={"decision": response.decision.value},
        )
        return CommandResult(accepted=True)

    async def _resolve_worker_approval(
        self, session_id: str, delegation_id: str, response: InteractionResponse,
    ) -> CommandResult:
        """Same mutation `POST /api/internal/pending-approvals/{approve,deny}`
        performs (`backend/pending_approvals_api.py`): the store transition
        AND the coordinator's own worker-spawn callback
        (`orchestrator.Coordinator._resolve_approval`, the exact function
        object that route's `resolve_approval` capability is bound to —
        see `backend/app_composition.py`'s `pending_approvals_api.configure`
        call) — never a second, parallel spawn path."""
        if not isinstance(response, ApprovalResponse):
            return CommandResult(
                accepted=False, code="invalid_response",
                message="a worker-creation approval requires an approval response",
            )
        existing = pending_approvals.get(delegation_id)
        if existing is None:
            return CommandResult(accepted=False, code="not_found")
        if existing.get("app_session_id") != session_id:
            return CommandResult(accepted=False, code="forbidden")
        if response.decision == ApprovalDecision.APPROVE:
            rec, reason = pending_approvals.approve(delegation_id)
        else:
            rec, reason = pending_approvals.deny(delegation_id)
        if reason == "missing":
            return CommandResult(accepted=False, code="not_found")
        if reason == "expired":
            return CommandResult(accepted=False, code="expired")
        if reason == "already_resolved":
            return CommandResult(accepted=True, code="already_resolved")
        _publish_resolved_interaction_fact(
            session_id, f"{WORKER_APPROVAL_REF_PREFIX}{delegation_id}", UserInteractionKind.APPROVAL,
            {
                "subject": "delegation",
                "cwd": rec.get("cwd", ""),
                "justification": rec.get("justification", ""),
                "proposed_description": rec.get("proposed_description", ""),
                "risk_scope": rec.get("proposed_orchestration_mode", ""),
            },
            response={"decision": response.decision.value},
        )
        self.coordinator._resolve_approval(delegation_id, rec)
        return CommandResult(accepted=True)

    async def _resolve_delegate_choice(
        self, session_id: str, delegation_id: str, response: InteractionResponse,
    ) -> CommandResult:
        """Same mutation `POST /api/internal/session-bridge/delegate/resolve`
        performs (`session_bridge.resolve_delegation`) — that function
        itself re-fires `session.fire.msg_ask_result_set` on resolution
        (`session_bridge._mark_picker_resolved` -> `session_manager.
        set_msg_ask_result`), which is what `chat_adapter._on_ask_result_set`
        broadcasts as the resolved `UserInteractionUpsert`; no separate fact
        emission needed here."""
        if not isinstance(response, ChoiceResponse):
            return CommandResult(
                accepted=False, code="invalid_response",
                message="a delegation choice requires a choice response",
            )
        pending = session_bridge.get_pending(delegation_id)
        if pending is None:
            return CommandResult(accepted=False, code="not_found")
        if pending.get("caller_sid") != session_id:
            return CommandResult(accepted=False, code="forbidden")
        ok = session_bridge.resolve_delegation(delegation_id, response.picked_ref)
        if not ok:
            return CommandResult(accepted=False, code="already_resolved")
        return CommandResult(accepted=True)

    async def _resolve_user_input(
        self, session_id: str, request_id: str, response: InteractionResponse,
    ) -> CommandResult:
        """Same mutation `POST /api/user-input/{id}/resolve` performs
        (`backend/user_input_api.py`'s `resolve_user_input`) — same
        kind-specific response validation (`user_input_api.
        build_resolve_response`, shared not re-derived), same store
        mutation (`user_input_store.resolve_request`). No cancel
        counterpart: the `resolve` intent (ADR 0006 §5) has no distinct
        cancel variant for any kind — cancel stays legacy-REST-only,
        still fact-producing (see `user_input_api.cancel_user_input`) so
        a live v2 subscriber sees it, just not v2-intent-triggerable."""
        if not isinstance(response, InputResponse):
            return CommandResult(
                accepted=False, code="invalid_response",
                message="a user-input interaction requires an input response",
            )
        req = await asyncio.to_thread(user_input_store.get_request, request_id)
        if req is None:
            return CommandResult(accepted=False, code="not_found")
        if req.get("app_session_id") != session_id:
            return CommandResult(accepted=False, code="forbidden")
        if req.get("status") != "pending":
            return CommandResult(accepted=False, code="already_resolved")
        try:
            resolved_response = user_input_api.build_resolve_response(req, response.response)
        except HTTPException as exc:
            return CommandResult(accepted=False, code="invalid_response", message=str(exc.detail))
        resolved = await asyncio.to_thread(
            user_input_store.resolve_request, request_id, resolved_response,
        )
        if resolved is None:
            return CommandResult(accepted=False, code="not_found")
        _publish_resolved_interaction_fact(
            session_id, f"{USER_INPUT_REF_PREFIX}{request_id}", UserInteractionKind.INPUT,
            user_input_store.interaction_request_dict(req),
            response=resolved_response,
        )
        return CommandResult(accepted=True)


def build_chat_command_port(*, coordinator: Any) -> ChatCommandPort:
    """Factory: the returned port holds no state of its own beyond the
    injected `coordinator` singleton (and the module-level
    `session_manager` singleton it calls into), so building more than one
    instance from the same `coordinator` — one for
    `backend/main.py:_wire_surface_adapter`, one for
    `backend/ws_chat.py:configure` — is safe; every instance delegates to
    the same underlying collaborators."""
    return _ChatCommandPortImpl(coordinator=coordinator)

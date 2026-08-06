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
`edit_queued`, and `delete_queued`. `backend/ws_chat.py` keeps only frame
parsing, connection/session bookkeeping, and legacy reply-frame
formatting; it calls this module's port instance directly to preserve
exact legacy behavior (it does not go through
`ChatSurfaceAdapter.submit()`, whose accept/reject-only contract does not
carry the legacy per-frame outcome).

`send_prompt`, `set_selectors`, `rewind`, and `approve` are intentionally
NOT migrated in this pass — `backend/ws_chat.py`'s `send_message` handler
is deeply entangled with WS-specific reply-frame branching (Ask-singleton
routing, virtual-session routing, multi-stage alter/steer/interrupt
dedup, durable admission via `offline_actions_api._start_prompt_handoff`,
and lifecycle emits whose ORDER is part of the legacy contract) and
extracting it without a dedicated behavior-preserving pass would risk a
silent reply-frame regression. They return an `unsupported` result so
callers treat "not yet supported" uniformly with any other rejection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from backend.adapters.command_port import ChatCommandPort, CommandResult

from session_manager import manager as session_manager  # noqa: E402  (bare — matches ws_chat.py's import)

_UNSUPPORTED = "not yet migrated off backend/ws_chat.py"


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

    async def send_prompt(self, session_id, text, attachments, send_mode, target, intent_id) -> CommandResult:
        return CommandResult(accepted=False, code="unsupported", message=_UNSUPPORTED)

    async def set_selectors(self, session_id, **selectors) -> CommandResult:
        return CommandResult(accepted=False, code="unsupported", message=_UNSUPPORTED)

    async def rewind(self, session_id, node_id) -> CommandResult:
        return CommandResult(accepted=False, code="unsupported", message=_UNSUPPORTED)

    async def approve(self, session_id, approval_ref, decision, scope) -> CommandResult:
        return CommandResult(accepted=False, code="unsupported", message=_UNSUPPORTED)


def build_chat_command_port(*, coordinator: Any) -> ChatCommandPort:
    """Factory: the returned port holds no state of its own beyond the
    injected `coordinator` singleton (and the module-level
    `session_manager` singleton it calls into), so building more than one
    instance from the same `coordinator` — one for
    `backend/main.py:_wire_surface_adapter`, one for
    `backend/ws_chat.py:configure` — is safe; every instance delegates to
    the same underlying collaborators."""
    return _ChatCommandPortImpl(coordinator=coordinator)

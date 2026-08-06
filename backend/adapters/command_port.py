"""Transport-neutral chat command execution contract (ADR 0006 command
plane). `backend/adapters/chat_adapter.py` depends ONLY on this Protocol
— the concrete implementation is composed by the composition root
(backend/main.py) from `backend/surface_commands.py`, which sits OUTSIDE
the adapters import boundary (see
`backend/scripts/test_adapter_boundaries.py`) because it reaches
transport-independent collaborators (the orchestrator `Coordinator`,
`session_manager`) that `backend/adapters/*.py` is not allowed to import
directly. This is the port half of that port/adapter split: adapters see
only the shape, never the implementation module.

Each method mirrors one member of the `ChatIntent` union
(`backend/surface_contract/intents.py`) with a transport-neutral
signature — no WS frame construction, no HTTP/WS-specific bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from backend.surface_contract.identity import ApprovalRef, NodeId, SessionId
from backend.surface_contract.intents import SendTarget
from backend.surface_contract.nodes import Attachment, SendMode

# One reply-frame callback shape for every ChatCommandPort method that can
# emit ordered transport frames during its own execution: `notify(frame_type,
# payload)`. The legacy WS transport implements it as a thin wrapper around
# its existing per-connection send helper (`ws_callback`); the v2 Chat
# Surface Contract transport implements it as a no-op (acks are
# projection-fact based — see backend/adapters/chat_adapter.py's
# `ChatSurfaceAdapter.submit()`).
NotifyFn = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _default_notify(frame_type: str, payload: dict[str, Any]) -> None:
    """Default `notify` for callers that never emit transport frames (e.g.
    non-WS callers exercising the port directly in tests)."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Neutral outcome of a single command-port call.

    `accepted=False` with an empty `code` means "no-op, nothing to do"
    (e.g. stop with no active turn) rather than a hard failure — callers
    branch on `accepted`/`code`, never on an exception, for expected
    rejection paths."""

    accepted: bool
    code: str = ""
    message: str = ""


class ChatCommandPort(Protocol):
    """One method per `ChatIntent` variant. A concrete implementation may
    leave a method only partially migrated off its legacy transport
    handler — such a method returns `CommandResult(accepted=False,
    code="unsupported", ...)` rather than raising, so callers can treat
    "not yet supported" uniformly with any other rejection."""

    async def send_prompt(
        self,
        session_id: SessionId | None,
        text: str,
        attachments: tuple[Attachment, ...],
        send_mode: SendMode,
        target: SendTarget,
        intent_id: str,
        *,
        notify: NotifyFn = _default_notify,
    ) -> CommandResult:
        """`intent_id` doubles as the client-supplied dedup key (unified
        with the legacy WS transport's `client_id`): the v2 contract always
        supplies a real `IntentId`; the legacy transport passes `""` when
        the browser omitted `client_id`, and `""` is treated as "no dedup
        key" everywhere this port checks it — matching the legacy `if
        client_id:` gate exactly. A concrete implementation may accept
        additional legacy-transport-only keyword arguments beyond this
        signature (see `backend/surface_commands.py`); they all have
        defaults so a Protocol-typed caller (this port's only contractual
        surface) never needs to supply them."""
        ...

    async def stop(self, session_id: SessionId) -> CommandResult: ...

    async def edit_queued(
        self, session_id: SessionId, node_id: NodeId, text: str,
    ) -> CommandResult: ...

    async def delete_queued(
        self, session_id: SessionId, node_id: NodeId | None = None,
    ) -> CommandResult: ...

    async def set_selectors(
        self,
        session_id: SessionId,
        *,
        runtime_profile_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        permission: str | None = None,
        harness_profile_id: str | None = None,
        orchestration_mode: str | None = None,
    ) -> CommandResult: ...

    async def rewind(self, session_id: SessionId, node_id: NodeId) -> CommandResult: ...

    async def approve(
        self,
        session_id: SessionId,
        approval_ref: ApprovalRef,
        decision: str,
        scope: str,
    ) -> CommandResult: ...

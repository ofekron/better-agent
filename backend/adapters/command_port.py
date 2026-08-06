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
from typing import Protocol

from backend.surface_contract.identity import ApprovalRef, NodeId, SessionId
from backend.surface_contract.intents import SendTarget
from backend.surface_contract.nodes import Attachment, SendMode


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
        session_id: SessionId,
        text: str,
        attachments: tuple[Attachment, ...],
        send_mode: SendMode,
        target: SendTarget,
        intent_id: str,
    ) -> CommandResult: ...

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

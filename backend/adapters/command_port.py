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

from backend.surface_contract.identity import (
    ApprovalRef as InteractionRef,
    FolderRef,
    NodeId,
    ProjectRef,
    SessionId,
    TagRef,
)
from backend.surface_contract.intents import InteractionResponse, SendTarget
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
    rejection paths.

    `ref` is an optional identifier for the primary resource this command
    produced or affected when that resource has a server-generated id the
    caller couldn't already know (e.g. the new session id `assign_project`'s
    continuation-style move creates, or a newly created folder/tag id).
    NOT a synchronous RPC result — acks stay accept/reject-only (ADR 0006
    §5). The adapter uses `ref` only to know WHICH resource to re-emit an
    intent_id-stamped projection frame for; the caller (frontend) learns
    the ref by observing that frame arrive on the live plane with a
    matching `intent_id`, never from this field directly (it never crosses
    the wire)."""

    accepted: bool
    code: str = ""
    message: str = ""
    ref: str | None = None


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

    async def resolve_interaction(
        self,
        session_id: SessionId,
        interaction_ref: InteractionRef,
        response: InteractionResponse,
    ) -> CommandResult:
        """Routes to whichever legacy store owns `interaction_ref` (see
        `backend/surface_commands.py`'s dispatch by the ref's namespace
        prefix — `nodes.py`'s `TOOL_APPROVAL_REF_PREFIX`/
        `WORKER_APPROVAL_REF_PREFIX`/`DELEGATE_CHOICE_REF_PREFIX`) — the
        SAME mutation legacy REST decide/approve/deny/resolve routes
        perform, not a second implementation."""
        ...


class SessionCommandPort(Protocol):
    """One method per `SessionIntent` variant (ADR 0008 command plane).
    Same port/adapter split as `ChatCommandPort` above: `SessionSurfaceAdapter`
    (`backend/adapters/session_adapter.py`) depends only on this Protocol;
    the concrete implementation (`backend/session_commands.py`) sits outside
    the adapters import boundary because it reaches `session_manager`,
    `session_detail_api`, and `projects_api` — collaborators
    `backend/adapters/*.py` is not allowed to import directly."""

    async def archive_session(self, session_id: SessionId, archived: bool) -> CommandResult: ...

    async def rename_session(self, session_id: SessionId, title: str) -> CommandResult: ...

    async def assign_project(
        self, session_id: SessionId, project_ref: ProjectRef | None,
    ) -> CommandResult: ...

    async def create_project(self, name: str, path: str) -> CommandResult: ...

    async def rename_project(self, project_ref: ProjectRef, name: str) -> CommandResult: ...

    async def delete_project(self, project_ref: ProjectRef) -> CommandResult: ...

    async def mark_opened(self, session_id: SessionId) -> CommandResult: ...

    # ---- ADR 0008 folders/tags/session-organization ---------------------

    async def create_folder(
        self, project_ref: ProjectRef, name: str, parent_folder_ref: FolderRef | None,
    ) -> CommandResult: ...

    async def rename_folder(self, folder_ref: FolderRef, name: str) -> CommandResult: ...

    async def move_folder(
        self, folder_ref: FolderRef, parent_folder_ref: FolderRef | None,
    ) -> CommandResult: ...

    async def delete_folder(self, folder_ref: FolderRef, mode: str) -> CommandResult: ...

    async def create_tag(
        self, name: str, project_ref: ProjectRef | None, color: str | None,
    ) -> CommandResult: ...

    async def rename_tag(self, tag_ref: TagRef, name: str) -> CommandResult: ...

    async def recolor_tag(self, tag_ref: TagRef, color: str) -> CommandResult: ...

    async def delete_tag(self, tag_ref: TagRef) -> CommandResult: ...

    async def assign_folder(
        self, session_id: SessionId, folder_ref: FolderRef | None,
    ) -> CommandResult: ...

    async def assign_tags(
        self,
        session_id: SessionId,
        source: str,
        add_tag_refs: tuple[TagRef, ...] | None,
        remove_tag_refs: tuple[TagRef, ...] | None,
        sync_tag_refs: tuple[TagRef, ...] | None,
    ) -> CommandResult: ...


class SystemCommandPort(Protocol):
    """One method per `SystemIntent` variant (ADR 0011 System & Host
    Surface command plane). Same port/adapter split as `ChatCommandPort`/
    `SessionCommandPort` above: `SystemSurfaceAdapter`
    (`backend/adapters/system_adapter.py`) depends only on this Protocol;
    the concrete implementation (`backend/system_commands.py`) sits
    outside the adapters import boundary because it reaches
    `extension_store`, `harness_profile_store`, `marketplace_bridge`,
    `stores.schedule_store`, `installation_profile`, `node_link`,
    `machine_nodes_api`-owning modules — collaborators
    `backend/adapters/*.py` is not allowed to import directly. Every
    method is the SAME mutation function the legacy REST route already
    calls — never a second implementation."""

    async def update_extension_config(
        self, extension_id: str, section: str, patch: dict[str, object],
    ) -> CommandResult: ...

    async def save_harness_profile(
        self,
        harness_profile_id: str | None,
        config: dict[str, object],
        revision: str | None,
        writes: tuple[dict[str, object], ...],
    ) -> CommandResult: ...

    async def delete_harness_profile(
        self, harness_profile_id: str, revision: str | None,
    ) -> CommandResult: ...

    async def set_default_harness_profile(self, harness_profile_id: str) -> CommandResult: ...

    async def install_extension(self, extension_id: str, source: dict[str, object]) -> CommandResult: ...

    async def update_extension(self, extension_id: str) -> CommandResult: ...

    async def uninstall_extension(self, extension_id: str) -> CommandResult: ...

    async def enable_extension(self, extension_id: str) -> CommandResult: ...

    async def disable_extension(self, extension_id: str) -> CommandResult: ...

    async def decide_marketplace_intent(
        self, marketplace_intent_id: str, decision: str,
    ) -> CommandResult: ...

    async def revoke_marketplace_device(self, device_ref: str) -> CommandResult: ...

    async def create_schedule(
        self, target_session_id: str, prompt: str, cadence: dict[str, object],
    ) -> CommandResult: ...

    async def delete_schedule(self, schedule_id: str) -> CommandResult: ...

    async def set_installation_capability(
        self, capability_id: str, enabled: bool, confirm: bool,
    ) -> CommandResult: ...

    async def remove_node(self, node_id: str) -> CommandResult: ...

    async def sync_node_providers(
        self, node_id: str, include_secrets: bool, provider_ids: tuple[str, ...],
    ) -> CommandResult: ...

    async def resolve_node_registration(self, node_id: str, decision: str) -> CommandResult: ...

"""Chat surface ABC (ADR 0006): hydration/paging pull, live delta push,
command intents, sidecar/run/approval reads. Implementations hide every
provider and execution specific behind the node vocabulary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from backend.surface_contract.identity import (
    Emit,
    Focus,
    NodeId,
    PageCursor,
    ProjectionResult,
    SessionId,
    SidecarRef,
    Subscription,
    SurfaceCursor,
    SurfaceId,
)
from backend.surface_contract.intents import ChatIntent, TransportAck
from backend.surface_contract.nodes import ChildManifest, Node, Run, Sidecar


# The `...` control exists iff manifest.renderable_child_count > 0 —
# derived, not a second field. runtime_change rides along without counting
# against the N-turn window (at most one per turn).
@dataclass(frozen=True, slots=True)
class CompactTurn:
    turn: Node
    prompt: Node
    results: tuple[Node, ...]
    manifest: ChildManifest
    runtime_change: Node | None = None


@dataclass(frozen=True, slots=True)
class CompactSessionSnapshot:
    session_id: SessionId
    surface_id: SurfaceId
    instruction_widget: Node | None
    turns: tuple[CompactTurn, ...]
    live_turn_nodes: tuple[Node, ...]
    runs: tuple[Run, ...]
    older_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class OlderPage:
    turns: tuple[CompactTurn, ...]
    runs: tuple[Run, ...]
    older_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class SearchMatch:
    turn_id: str
    node_id: NodeId
    path: tuple[NodeId, ...]


class ChatSurface(ABC):
    @abstractmethod
    def open_session(
        self, session_id: SessionId
    ) -> ProjectionResult[CompactSessionSnapshot]: ...

    @abstractmethod
    def children(
        self, surface_id: SurfaceId, node_id: NodeId, at_render_rev: int
    ) -> ProjectionResult[tuple[Node, ...]]: ...

    @abstractmethod
    def older(self, cursor: PageCursor) -> ProjectionResult[OlderPage]: ...

    @abstractmethod
    def search(
        self, session_id: SessionId, query: str
    ) -> ProjectionResult[tuple[SearchMatch, ...]]: ...

    @abstractmethod
    def fetch_sidecar(
        self, session_id: SessionId, sidecar_ref: SidecarRef
    ) -> ProjectionResult[Sidecar]: ...

    @abstractmethod
    def subscribe(
        self, cursors: tuple[SurfaceCursor, ...], focus: Focus, emit: Emit
    ) -> Subscription:
        """emit receives ChatFrame values; resync is signaled with
        ResyncRequired, never with silently-degraded replay."""

    @abstractmethod
    def subscribe_control(self, emit: Emit) -> Subscription:
        """emit receives ControlFrame values (e.g. SessionCreated)."""

    @abstractmethod
    def submit(self, intent: ChatIntent) -> TransportAck: ...

"""Session & project surface ABC (ADR 0008): metadata/organization only —
session content is the chat surface's domain; creation stays on the chat
command plane (single authoritative path)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from backend.surface_contract.identity import (
    Emit,
    IntentId,
    PageCursor,
    ProjectRef,
    ProjectionResult,
    SessionId,
    SessionSelectors,
    Subscription,
    SurfaceId,
)
from backend.surface_contract.intents import SessionIntent, TransportAck


class SessionRollupState(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    DETACHED = "detached"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: SessionId
    title: str
    project_ref: ProjectRef | None
    selectors: SessionSelectors
    state: SessionRollupState
    attention_markers: tuple[str, ...]
    opened_at: float | None
    last_activity_at: float
    archived: bool


@dataclass(frozen=True, slots=True)
class SessionPage:
    sessions: tuple[SessionSummary, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class SessionTreeNode:
    surface_id: SurfaceId
    parent_surface_id: SurfaceId | None
    kind: str  # root | fork
    title: str | None
    state: SessionRollupState


@dataclass(frozen=True, slots=True)
class Project:
    project_ref: ProjectRef
    name: str
    order: int
    session_count: int


@dataclass(frozen=True, slots=True)
class SessionSummaryUpsert:
    cv: int
    summary: SessionSummary
    intent_id: IntentId | None = None


@dataclass(frozen=True, slots=True)
class SessionTreeChanged:
    cv: int
    session_id: SessionId


@dataclass(frozen=True, slots=True)
class ProjectUpsert:
    cv: int
    project: Project
    intent_id: IntentId | None = None


@dataclass(frozen=True, slots=True)
class RearrangerChanged:
    cv: int
    intent_id: IntentId | None = None


# Tombstone: a deleted session must be announced, never silently dropped
# from a live subscriber's cache.
@dataclass(frozen=True, slots=True)
class SessionRemoved:
    cv: int
    session_id: SessionId


SessionFrame = (
    SessionSummaryUpsert | SessionTreeChanged | ProjectUpsert | RearrangerChanged | SessionRemoved
)


class SessionSurface(ABC):
    @abstractmethod
    def list_sessions(
        self, cursor: PageCursor | None, query: str | None
    ) -> ProjectionResult[SessionPage]: ...

    @abstractmethod
    def session_tree(
        self, session_id: SessionId
    ) -> ProjectionResult[tuple[SessionTreeNode, ...]]: ...

    @abstractmethod
    def projects(self) -> ProjectionResult[tuple[Project, ...]]: ...

    @abstractmethod
    def rearranger_state(self) -> ProjectionResult[dict[str, object]]: ...

    @abstractmethod
    def subscribe(self, emit: Emit) -> Subscription:
        """emit receives SessionFrame values, change-only."""

    @abstractmethod
    def submit(self, intent: SessionIntent) -> TransportAck: ...

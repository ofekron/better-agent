"""Session & project surface ABC (ADR 0008): metadata/organization only —
session content is the chat surface's domain; creation stays on the chat
command plane (single authoritative path)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from backend.surface_contract.identity import (
    Emit,
    FolderRef,
    IntentId,
    PageCursor,
    ProjectRef,
    ProjectionResult,
    SessionId,
    SessionSelectors,
    Subscription,
    SurfaceId,
    TagRef,
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
class AttentionMarkerDetail:
    """One extension's attention marker on a session, full shape (color/
    tooltip/sound are cosmetic per-extension display hints; `tag` is the
    stable identifier — see `SessionSummary.attention_markers`, the
    tag-only companion field this extends without replacing, same
    "richer field added alongside, not instead of" idiom `TagAssignment`
    already establishes for `tag_refs`)."""

    extension_id: str
    tag: str
    color: str | None
    tooltip: str | None
    sound: bool
    sound_setting: str | None


@dataclass(frozen=True, slots=True)
class TagAssignment:
    """One `tag_refs` entry (ADR 0008): `source` distinguishes manual
    assignment from an automated producer (e.g. `auto_tagging`,
    `requirement_analysis`) — an open string, not a closed enum, matching
    ADR 0006 §6's `provider_id` idiom."""

    tag_ref: TagRef
    source: str


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
    # Organization membership rides the same summary projection (ADR 0008):
    # one session, one summary, one source of truth for its state *and*
    # its organization. `folder_ref` is scalar (at most one folder, matching
    # the legacy model's single `folder_id` per session).
    folder_ref: FolderRef | None
    tag_refs: tuple[TagAssignment, ...]
    # Live signals with no rollup-`state` equivalent (legacy tracks these as
    # independent dimensions, never folded into one enum) — see
    # `backend/adapters/session_adapter.py`'s `_RollupFlags` docstring for
    # why each is a bare bus-fact pass-through, honestly absent until this
    # process has observed the relevant fact for a given session.
    unread_count: int
    has_error: bool
    monitoring_state: str
    attention_marker_details: tuple[AttentionMarkerDetail, ...]
    # Count of open `UserInteraction`s (plane a, ADR 0006 §5) on this
    # session — folded from `store_access.list_pending_interactions`, the
    # SAME aggregation `ChatSurfaceAdapter`'s cold hydration already uses,
    # so this can never drift from the chat surface's own approval count.
    pending_interactions: int


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
class SessionFolder:
    """Hierarchical, arbitrary depth via `parent_folder_ref`; `order` is
    sibling order only, scoped to `(project_ref, parent_folder_ref)`."""

    folder_ref: FolderRef
    project_ref: ProjectRef
    name: str
    parent_folder_ref: FolderRef | None
    order: int


@dataclass(frozen=True, slots=True)
class SessionTag:
    """Free-form vocabulary (user-named, not a fixed set); `project_ref`
    optional (global tags allowed)."""

    tag_ref: TagRef
    project_ref: ProjectRef | None
    name: str
    color: str


@dataclass(frozen=True, slots=True)
class SessionSearchFilters:
    """Session-search filters (ADR 0008): one query path, not a parallel
    filtered-list route. `tag_match` selects `all` (every listed tag_ref
    must be present) or `any` (at least one)."""

    folder_ref: FolderRef | None = None
    tag_refs: tuple[TagRef, ...] = ()
    tag_match: str = "all"  # "all" | "any"


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


# Tombstone: a deleted session must be announced, never silently dropped
# from a live subscriber's cache.
@dataclass(frozen=True, slots=True)
class SessionRemoved:
    cv: int
    session_id: SessionId


# Tombstone: a deleted project must be announced too, same rationale as
# SessionRemoved above.
@dataclass(frozen=True, slots=True)
class ProjectRemoved:
    cv: int
    project_ref: ProjectRef


@dataclass(frozen=True, slots=True)
class FolderUpsert:
    cv: int
    folder: SessionFolder
    intent_id: IntentId | None = None


# Tombstone: replaces the legacy `session_organization_changed` refresh-
# everything frame for folder deletion — a subscriber learns exactly which
# folder_ref vanished, never "go refetch a snapshot."
@dataclass(frozen=True, slots=True)
class FolderRemoved:
    cv: int
    folder_ref: FolderRef


@dataclass(frozen=True, slots=True)
class TagUpsert:
    cv: int
    tag: SessionTag
    intent_id: IntentId | None = None


# Tombstone, same rationale as FolderRemoved above.
@dataclass(frozen=True, slots=True)
class TagRemoved:
    cv: int
    tag_ref: TagRef


SessionFrame = (
    SessionSummaryUpsert
    | SessionTreeChanged
    | ProjectUpsert
    | SessionRemoved
    | ProjectRemoved
    | FolderUpsert
    | FolderRemoved
    | TagUpsert
    | TagRemoved
)


class SessionSurface(ABC):
    @abstractmethod
    def list_sessions(
        self,
        cursor: PageCursor | None,
        query: str | None,
        filters: SessionSearchFilters | None = None,
    ) -> ProjectionResult[SessionPage]: ...

    @abstractmethod
    def session_tree(
        self, session_id: SessionId
    ) -> ProjectionResult[tuple[SessionTreeNode, ...]]: ...

    @abstractmethod
    def projects(self) -> ProjectionResult[tuple[Project, ...]]: ...

    @abstractmethod
    def list_folders(
        self, project_ref: ProjectRef | None
    ) -> ProjectionResult[tuple[SessionFolder, ...]]:
        """Bounded read, not paged — folder catalogs are small per-project
        sets (ADR 0008)."""

    @abstractmethod
    def list_tags(
        self, project_ref: ProjectRef | None
    ) -> ProjectionResult[tuple[SessionTag, ...]]:
        """Bounded read, not paged — tag catalogs are small per-project
        sets (ADR 0008)."""

    @abstractmethod
    def subscribe(self, emit: Emit) -> Subscription:
        """emit receives SessionFrame values, change-only."""

    @abstractmethod
    def submit(self, intent: SessionIntent) -> TransportAck: ...

"""Runs & diagnostics surface ABC (ADR 0009): read/observe-only; run
control lives on the chat command plane. Display resolves via descriptors —
no provider display strings on this surface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from backend.surface_contract.identity import (
    Emit,
    PageCursor,
    ProjectionResult,
    ProviderId,
    SessionId,
    Subscription,
    TurnId,
)


class RunPhase(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    STALLED = "stalled"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN_AFTER_RECOVERY = "unknown_after_recovery"


class DetailValueKind(StrEnum):
    TEXT = "text"
    DURATION = "duration"
    COUNT = "count"
    REF = "ref"
    # `value` is a JSON-safe `list[dict]` — one row per pid in the run's
    # descendant process tree (see `run_detail()`'s on-demand
    # `process_inspect.inspect_process_tree` enrichment). Not a
    # provider-declared token — no i18n resolution beyond the row shape
    # itself, which the frontend renders as a table.
    PROCESS_TREE = "process_tree"


class RunKind(StrEnum):
    """Mirrors `turn_manager.py`'s `Literal["manager", "native", "worker"]`
    run-state entry `kind` — backend-internal categorization, not a
    provider display string."""
    MANAGER = "manager"
    NATIVE = "native"
    WORKER = "worker"


# phase is an opaque provider-declared token; display resolves via the
# provider descriptor's copy keys, never by branching on the value.
@dataclass(frozen=True, slots=True)
class StartupProgress:
    phase: str
    progress: float | None


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    session_id: SessionId
    turn_id: TurnId | None
    provider_id: ProviderId
    runner: str
    phase: RunPhase
    started_at: float
    last_heartbeat_at: float | None
    startup: StartupProgress | None
    # Everything below is sourced from turn_manager.py's `run.state.*`
    # facts (event-driven, see `RunsSurfaceAdapter._on_run_state_fact`) —
    # live-only, in-memory: present while this backend incarnation has
    # live knowledge of the run, honestly absent (None / False) otherwise,
    # INCLUDING after a cold restart (never persisted, never stale).
    kind: RunKind | None
    target_message_id: str | None
    delegation_id: str | None
    pid: int | None
    stalled_at: float | None
    recovered_at_startup: bool


@dataclass(frozen=True, slots=True)
class RunDetailEntry:
    key: str  # i18n key
    value_kind: DetailValueKind
    value: object


@dataclass(frozen=True, slots=True)
class RunDetail:
    summary: RunSummary
    entries: tuple[RunDetailEntry, ...]


class UsageMeasureState(StrEnum):
    REPORTED = "reported"
    UNREPORTED = "unreported"


@dataclass(frozen=True, slots=True)
class UsageRow:
    provider_id: ProviderId
    model: str
    period: str
    state: UsageMeasureState
    tokens: int | None
    turns: int | None
    cost: float | None


@dataclass(frozen=True, slots=True)
class UsagePage:
    rows: tuple[UsageRow, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class RunSummaryUpsert:
    cv: int
    summary: RunSummary


RunsFrame = RunSummaryUpsert


@dataclass(frozen=True, slots=True)
class RunPage:
    runs: tuple[RunSummary, ...]
    next_cursor: PageCursor | None


class RunsSurface(ABC):
    @abstractmethod
    def list_runs(
        self, session_id: SessionId | None, cursor: PageCursor | None
    ) -> ProjectionResult[RunPage]: ...

    @abstractmethod
    def run_detail(self, run_id: str) -> ProjectionResult[RunDetail]: ...

    @abstractmethod
    def usage_analytics(
        self, cursor: PageCursor | None
    ) -> ProjectionResult[UsagePage]: ...

    @abstractmethod
    def subscribe(self, emit: Emit) -> Subscription:
        """emit receives RunsFrame values (run_summary_upsert), change-only,
        including stalled transitions (backend-computed, push-truthful)."""

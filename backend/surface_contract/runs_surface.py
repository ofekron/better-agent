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


class RunsSurface(ABC):
    @abstractmethod
    def list_runs(
        self, session_id: SessionId | None, cursor: PageCursor | None
    ) -> ProjectionResult[tuple[RunSummary, ...]]: ...

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

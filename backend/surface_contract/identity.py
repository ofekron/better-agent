"""Shared identity, revision, and read-result types for the Chat Surface
Contract family (ADR 0006 §0/§2; reused verbatim by ADRs 0007-0009)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Generic, Protocol, TypeVar

CONTRACT_VERSION = 1

SessionId = str
SurfaceId = str
NodeId = str
TurnId = str
RunRef = str
SidecarRef = str
ApprovalRef = str
ProviderId = str
ProjectRef = str
IntentId = str
FolderRef = str
TagRef = str


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    incarnation: str
    render_rev: int
    hist_rev: int


@dataclass(frozen=True, slots=True)
class SurfaceCursor:
    surface_id: SurfaceId
    incarnation: str
    render_rev: int


@dataclass(frozen=True, slots=True)
class PageCursor:
    surface_id: SurfaceId
    snapshot: SnapshotIdentity
    token: str


class Focus(StrEnum):
    OPENED = "opened"
    WARM = "warm"


@dataclass(frozen=True, slots=True)
class SessionSelectors:
    provider_id: ProviderId | None
    runtime_profile_id: str | None
    model: str | None
    reasoning_effort: str | None
    orchestration_mode: str | None
    cwd: str | None


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    value: T
    snapshot: SnapshotIdentity


@dataclass(frozen=True, slots=True)
class Rebuilding:
    retry_after_ms: int | None = None


@dataclass(frozen=True, slots=True)
class StaleCursor:
    pass


# Typed projection states are first-class results, never errors (ADR 0006 §2).
ProjectionResult = Ok[T] | Rebuilding | StaleCursor


class Subscription(Protocol):
    def close(self) -> None: ...


Emit = Callable[[object], None]

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


JsonObject = Mapping[str, Any]


class ChatProjectionStoreError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class SourceWatermark:
    stream_id: str
    generation: int
    sequence: int


@dataclass(frozen=True)
class TurnManifest:
    turn_id: str
    event_count: int
    direct_child_count: int


@dataclass(frozen=True)
class ProjectionCommit:
    root_id: str
    root_generation: int
    event_id: str
    content_hash: str
    canonical_fact: JsonObject
    render_node: JsonObject
    turn_id: str
    message_id: str | None
    parent_event_id: str | None
    owner_scope: str
    manifest: TurnManifest
    visible_delta: JsonObject
    historical_revision: JsonObject
    watermark: SourceWatermark


@dataclass(frozen=True)
class CommitResult:
    duplicate: bool
    fact_sequence: int
    revision: int
    projection_cursor: int


@dataclass(frozen=True)
class StoredFact:
    fact_sequence: int
    event_id: str
    content_hash: str
    canonical_fact: JsonObject


@dataclass(frozen=True)
class StoredRevision:
    revision: int
    fact_sequence: int
    visible_delta: JsonObject
    historical_revision: JsonObject


@dataclass(frozen=True)
class StoredProjection:
    event_id: str
    render_node: JsonObject
    turn_id: str
    message_id: str | None
    parent_event_id: str | None
    owner_scope: str
    manifest: TurnManifest


class ChatProjectionStore(Protocol):
    def select_generation(self, root_id: str, root_generation: int) -> None: ...
    def commit(self, request: ProjectionCommit) -> CommitResult: ...
    def read_facts(self, root_id: str, root_generation: int, *, after: int = 0, limit: int = 1000) -> Sequence[StoredFact]: ...
    def read_revisions(self, root_id: str, root_generation: int, *, after: int = 0, limit: int = 1000) -> Sequence[StoredRevision]: ...
    def projection_cursor(self, root_id: str, root_generation: int) -> int: ...
    def read_projection(self, root_id: str, root_generation: int, event_id: str) -> StoredProjection | None: ...
    def source_watermark(self, root_id: str, root_generation: int, stream_id: str) -> SourceWatermark | None: ...
    def close(self) -> None: ...

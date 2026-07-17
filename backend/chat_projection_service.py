from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Mapping

from chat_projection_authority import (
    ProjectionAuthority, ProjectionAuthorityError, ProjectionAuthorityRegistry,
)
from chat_projection_store import (
    ChatProjectionStore, ChatProjectionStoreError, CommitResult, ProjectionCommit,
    SourceWatermark, StoredFact, StoredProjection, StoredRevision,
)
from chat_projection_store_jsonl import JsonlChatProjectionStore
from chat_projection_store_sqlite import SQLiteChatProjectionStore


class ProjectionServiceError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ProjectionChange:
    authority_id: str
    root_id: str
    root_generation: int
    revision: int
    projection_cursor: int
    event_id: str | None
    kind: str


StoreFactory = Callable[[ProjectionAuthority], ChatProjectionStore]
Subscriber = Callable[[ProjectionChange], None]
MAX_PENDING_PER_STREAM = 1024
MAX_STREAM_ADMISSIONS = 1024
SEQUENCE_WAIT_SECONDS = 30.0


@dataclass
class _PendingCommit:
    request: ProjectionCommit
    future: Future


@dataclass
class _StreamAdmission:
    condition: threading.Condition
    next_sequence: int
    pending: dict[int, _PendingCommit]
    draining: bool = False


class CanonicalChatProjectionService:
    def __init__(
        self,
        registry: ProjectionAuthorityRegistry | None = None,
        *,
        _store_factories: Mapping[str, StoreFactory] | None = None,
    ) -> None:
        self._registry = registry or ProjectionAuthorityRegistry()
        self._store_factories = dict(_store_factories or {
            "jsonl": lambda authority: JsonlChatProjectionStore(authority.store_path),
            "sqlite": lambda authority: SQLiteChatProjectionStore(authority.store_path),
        })
        if set(self._store_factories) != {"jsonl", "sqlite"}:
            raise ProjectionServiceError("invalid_service_config", "both store factories are required")
        self._lock = threading.RLock()
        self._stores: dict[str, ChatProjectionStore] = {}
        self._subscribers: dict[str, dict[str, Subscriber]] = {}
        self._admissions: dict[tuple[str, str, int], _StreamAdmission] = {}
        self._closed = False

    def register(
        self, *, provider: str, session_id: str, root_id: str,
        root_generation: int, store_kind: str,
    ) -> ProjectionAuthority:
        try:
            authority = self._registry.register(
                provider=provider, session_id=session_id, root_id=root_id,
                root_generation=root_generation, store_kind=store_kind,
            )
            store = self._store(authority)
            store.select_generation(authority.root_id, authority.root_generation)
            return authority
        except (ProjectionAuthorityError, ChatProjectionStoreError) as exc:
            self._raise(exc)

    def append_apply(
        self, authority: ProjectionAuthority, request: ProjectionCommit,
    ) -> CommitResult:
        current = self._require(authority)
        if (request.root_id, request.root_generation) != (current.root_id, current.root_generation):
            raise ProjectionServiceError("authority_mismatch", "commit targets another root authority")
        result = self._append_sequenced(current, request)
        self._publish(current, ProjectionChange(
            current.authority_id, current.root_id, current.root_generation,
            result.revision, result.projection_cursor, request.event_id,
            "duplicate" if result.duplicate else "committed",
        ))
        return result

    def _append_sequenced(
        self, authority: ProjectionAuthority, request: ProjectionCommit,
    ) -> CommitResult:
        watermark = request.watermark
        key = (authority.authority_id, watermark.stream_id, watermark.generation)
        admission = self._admission(authority, key, watermark.stream_id, watermark.generation)
        signature = (request.event_id, request.content_hash)
        with admission.condition:
            if watermark.sequence < admission.next_sequence:
                return self._commit_regression_candidate(authority, admission, request)
            existing = admission.pending.get(watermark.sequence)
            if existing is not None:
                if (existing.request.event_id, existing.request.content_hash) != signature:
                    raise ProjectionServiceError(
                        "sequence_conflict", "source sequence carries different content",
                    )
                future = existing.future
            else:
                if len(admission.pending) >= MAX_PENDING_PER_STREAM:
                    raise ProjectionServiceError("sequence_buffer_full", "stream admission buffer is full")
                future = Future()
                admission.pending[watermark.sequence] = _PendingCommit(request, future)
                admission.condition.notify_all()
            self._drain_locked(authority, admission)
            deadline = time.monotonic() + SEQUENCE_WAIT_SECONDS
            while not future.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    pending = admission.pending.get(watermark.sequence)
                    if pending is not None and pending.future is future:
                        del admission.pending[watermark.sequence]
                    raise ProjectionServiceError("sequence_gap", "source sequence gap did not close")
                admission.condition.wait(remaining)
            return future.result()

    def _admission(
        self, authority: ProjectionAuthority, key: tuple[str, str, int],
        stream_id: str, generation: int,
    ) -> _StreamAdmission:
        with self._lock:
            self._ensure_open()
            existing = self._admissions.get(key)
            if existing is not None:
                return existing
            if len(self._admissions) >= MAX_STREAM_ADMISSIONS:
                idle = next((
                    candidate for candidate, admission in self._admissions.items()
                    if not admission.pending and not admission.draining
                ), None)
                if idle is None:
                    raise ProjectionServiceError(
                        "sequence_admission_full", "stream admission capacity is exhausted",
                    )
                del self._admissions[idle]
            try:
                watermark = self._store(authority).source_watermark(
                    authority.root_id, authority.root_generation, stream_id,
                )
            except ChatProjectionStoreError as exc:
                self._raise(exc)
            next_sequence = 1
            if watermark is not None:
                if watermark.generation > generation:
                    raise ProjectionServiceError(
                        "watermark_regression", "source generation cannot regress",
                    )
                if watermark.generation == generation:
                    next_sequence = watermark.sequence + 1
            admission = _StreamAdmission(threading.Condition(), next_sequence, {})
            self._admissions[key] = admission
            return admission

    def _commit_regression_candidate(
        self, authority: ProjectionAuthority, admission: _StreamAdmission,
        request: ProjectionCommit,
    ) -> CommitResult:
        if request.watermark.sequence != admission.next_sequence - 1:
            raise ProjectionServiceError("watermark_regression", "source sequence cannot regress")
        try:
            durable = self._store(authority).source_admission(
                authority.root_id, authority.root_generation, request.watermark.stream_id,
                request.watermark.generation, request.watermark.sequence,
            )
        except ChatProjectionStoreError as exc:
            self._raise(exc)
        if durable is None:
            raise ProjectionServiceError(
                "watermark_regression", "source sequence has no durable admission",
            )
        if (durable.event_id, durable.content_hash) != (request.event_id, request.content_hash):
            raise ProjectionServiceError(
                "sequence_conflict", "committed source sequence carries different content",
            )
        return CommitResult(
            True, durable.fact_sequence, durable.revision, durable.projection_cursor,
        )

    def _drain_locked(
        self, authority: ProjectionAuthority, admission: _StreamAdmission,
    ) -> None:
        if admission.draining:
            return
        admission.draining = True
        try:
            while admission.next_sequence in admission.pending:
                pending = admission.pending.pop(admission.next_sequence)
                admission.condition.release()
                try:
                    result = self._store(authority).commit(pending.request)
                except ChatProjectionStoreError as exc:
                    error = ProjectionServiceError(exc.code, exc.detail)
                    pending.future.set_exception(error)
                except BaseException as exc:
                    pending.future.set_exception(exc)
                else:
                    admission.next_sequence += 1
                    pending.future.set_result(result)
                finally:
                    admission.condition.acquire()
                admission.condition.notify_all()
                if pending.future.exception() is not None:
                    for blocked in admission.pending.values():
                        blocked.future.set_exception(ProjectionServiceError(
                            "sequence_blocked", "prior source sequence failed",
                        ))
                    admission.pending.clear()
                    admission.condition.notify_all()
                    break
        finally:
            admission.draining = False

    def read_facts(
        self, authority: ProjectionAuthority, *, after: int = 0, limit: int = 1000,
    ) -> list[StoredFact]:
        current = self._require(authority)
        return list(self._call(current, "read_facts", after=after, limit=limit))

    def read_revisions(
        self, authority: ProjectionAuthority, *, after: int = 0, limit: int = 1000,
    ) -> list[StoredRevision]:
        current = self._require(authority)
        return list(self._call(current, "read_revisions", after=after, limit=limit))

    def read_projection(
        self, authority: ProjectionAuthority, event_id: str,
    ) -> StoredProjection | None:
        current = self._require(authority)
        return self._call(current, "read_projection", event_id=event_id)

    def source_watermark(
        self, authority: ProjectionAuthority, stream_id: str,
    ) -> SourceWatermark | None:
        current = self._require(authority)
        return self._call(current, "source_watermark", stream_id=stream_id)

    def projection_cursor(self, authority: ProjectionAuthority) -> int:
        current = self._require(authority)
        return self._call(current, "projection_cursor")

    def delete_root(self, authority: ProjectionAuthority) -> None:
        current = self._require(authority)
        try:
            self._store(current).delete_root(current.root_id)
        except ChatProjectionStoreError as exc:
            self._raise(exc)

    def rebuild(self, authority: ProjectionAuthority) -> int:
        current = self._require(authority)
        if current.store_kind != "jsonl":
            raise ProjectionServiceError("rebuild_unsupported", "selected store has no durable journal")
        with self._lock:
            self._ensure_open()
            store = self._stores.pop(current.authority_id, None)
            if store is not None:
                store.close()
            reopened = None
            try:
                reopened = JsonlChatProjectionStore(current.store_path, _force_rebuild=True)
                reopened.select_generation(current.root_id, current.root_generation)
            except ChatProjectionStoreError as exc:
                if reopened is not None:
                    reopened.close()
                self._raise(exc)
            self._stores[current.authority_id] = reopened
            cursor = reopened.projection_cursor(current.root_id, current.root_generation)
        self._publish(current, ProjectionChange(
            current.authority_id, current.root_id, current.root_generation,
            cursor, cursor, None, "rebuilt",
        ))
        return cursor

    def subscribe(self, authority: ProjectionAuthority, callback: Subscriber) -> str:
        current = self._require(authority)
        if not callable(callback):
            raise ProjectionServiceError("invalid_subscriber", "subscriber must be callable")
        token = uuid.uuid4().hex
        with self._lock:
            self._ensure_open()
            self._subscribers.setdefault(current.authority_id, {})[token] = callback
        return token

    def unsubscribe(self, authority: ProjectionAuthority, token: str) -> None:
        current = self._require(authority)
        with self._lock:
            subscribers = self._subscribers.get(current.authority_id)
            if subscribers is None or token not in subscribers:
                raise ProjectionServiceError("subscription_missing", "subscription does not exist")
            del subscribers[token]
            if not subscribers:
                self._subscribers.pop(current.authority_id, None)

    def _call(self, authority: ProjectionAuthority, operation: str, **arguments):
        try:
            return getattr(self._store(authority), operation)(
                authority.root_id, authority.root_generation, **arguments,
            )
        except ChatProjectionStoreError as exc:
            self._raise(exc)

    def _require(self, authority: ProjectionAuthority) -> ProjectionAuthority:
        with self._lock:
            self._ensure_open()
        try:
            return self._registry.require(authority)
        except ProjectionAuthorityError as exc:
            self._raise(exc)

    def _store(self, authority: ProjectionAuthority) -> ChatProjectionStore:
        with self._lock:
            self._ensure_open()
            existing = self._stores.get(authority.authority_id)
            if existing is not None:
                return existing
            store = self._open_store(authority)
            self._stores[authority.authority_id] = store
            return store

    def _open_store(self, authority: ProjectionAuthority) -> ChatProjectionStore:
        store = self._store_factories[authority.store_kind](authority)
        try:
            store.select_generation(authority.root_id, authority.root_generation)
        except BaseException:
            store.close()
            raise
        return store

    def _publish(self, authority: ProjectionAuthority, change: ProjectionChange) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.get(authority.authority_id, {}).values())
        for subscriber in subscribers:
            try:
                subscriber(change)
            except BaseException:
                continue

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProjectionServiceError("service_closed", "projection service is closed")

    @staticmethod
    def _raise(error: ProjectionAuthorityError | ChatProjectionStoreError) -> None:
        raise ProjectionServiceError(error.code, error.detail) from error

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stores = tuple(self._stores.values())
            self._stores.clear()
            self._subscribers.clear()
            admissions = tuple(self._admissions.values())
            self._admissions.clear()
        for admission in admissions:
            with admission.condition:
                for pending in admission.pending.values():
                    pending.future.set_exception(
                        ProjectionServiceError("service_closed", "projection service is closed"),
                    )
                admission.pending.clear()
                admission.condition.notify_all()
        errors = []
        for store in stores:
            try:
                store.close()
            except BaseException as exc:
                errors.append(exc)
        try:
            self._registry.close()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise ProjectionServiceError("service_close_failed", "projection service close failed") from errors[0]

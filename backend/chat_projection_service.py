from __future__ import annotations

import threading
import uuid
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
            self._store(authority)
            return authority
        except (ProjectionAuthorityError, ChatProjectionStoreError) as exc:
            self._raise(exc)

    def append_apply(
        self, authority: ProjectionAuthority, request: ProjectionCommit,
    ) -> CommitResult:
        current = self._require(authority)
        if (request.root_id, request.root_generation) != (current.root_id, current.root_generation):
            raise ProjectionServiceError("authority_mismatch", "commit targets another root authority")
        try:
            result = self._store(current).commit(request)
        except ChatProjectionStoreError as exc:
            self._raise(exc)
        self._publish(current, ProjectionChange(
            current.authority_id, current.root_id, current.root_generation,
            result.revision, result.projection_cursor, request.event_id,
            "duplicate" if result.duplicate else "committed",
        ))
        return result

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

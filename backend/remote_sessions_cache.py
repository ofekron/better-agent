from __future__ import annotations

import asyncio
import logging
import threading
import time

import perf

logger = logging.getLogger(__name__)


def copy_sessions(sessions: list[dict], *, limit: int | None = None) -> list[dict]:
    out: list[dict] = []
    for session in sessions:
        if isinstance(session, dict):
            out.append(dict(session))
            if limit is not None and len(out) >= limit:
                break
    return out


class RemoteSessionsCache:
    """TTL cache over each remote node's session list.

    Owns the entries, the lock guarding them, the set of in-flight
    background refreshes, and the version counter that downstream
    response caches key off. A stale entry is served immediately while
    a single background refresh repopulates it; only a genuinely
    changed list bumps the version.
    """

    def __init__(
        self,
        ttl_seconds: float = 2.0,
        fetch_timeout_seconds: float = 0.75,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._fetch_timeout_seconds = fetch_timeout_seconds
        self._entries: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()
        self._refreshing: set[str] = set()
        self._version = 0

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    @property
    def fetch_timeout_seconds(self) -> float:
        return self._fetch_timeout_seconds

    def version(self) -> int:
        with self._lock:
            return self._version

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._version = 0

    def get(
        self,
        node_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[list[dict] | None, bool, int]:
        with self._lock:
            cached = self._entries.get(node_id)
        if cached is None:
            return None, False, 0
        age = time.monotonic() - cached[0]
        sessions = cached[1]
        return (
            copy_sessions(sessions, limit=limit),
            age <= self._ttl_seconds,
            len(sessions),
        )

    def put(self, node_id: str, sessions: list[dict]) -> None:
        clean = copy_sessions(sessions)
        with self._lock:
            existing = self._entries.get(node_id)
            if existing is not None and existing[1] == clean:
                self._entries[node_id] = (time.monotonic(), clean)
                return
            self._entries[node_id] = (time.monotonic(), clean)
            self._version += 1

    async def fetch_live(self, node_id: str) -> list[dict]:
        import node_link as _nl

        resp = await _nl.rpc_call(
            node_id,
            "list_sessions",
            {},
            timeout=self._fetch_timeout_seconds,
        )
        sessions = (resp or {}).get("sessions", [])
        return copy_sessions(sessions if isinstance(sessions, list) else [])

    def schedule_refresh(self, node_id: str) -> None:
        with self._lock:
            if node_id in self._refreshing:
                return
            self._refreshing.add(node_id)

        async def _refresh() -> None:
            try:
                sessions = await self.fetch_live(node_id)
                self.put(node_id, sessions)
            except Exception:
                logger.debug(
                    "get_sessions: cached remote refresh from %s failed",
                    node_id,
                    exc_info=True,
                )
            finally:
                with self._lock:
                    self._refreshing.discard(node_id)

        asyncio.create_task(_refresh())

    async def for_sidebar(self, node_id: str) -> list[dict]:
        cached, fresh, _total = self.get(node_id)
        if cached is not None:
            if fresh:
                perf.record("sessions.list.remote_cache.hit", 1.0)
            else:
                perf.record("sessions.list.remote_cache.stale", 1.0)
                self.schedule_refresh(node_id)
            return cached
        perf.record("sessions.list.remote_cache.miss", 1.0)
        sessions = await self.fetch_live(node_id)
        self.put(node_id, sessions)
        return sessions

    def for_sidebar_cached(
        self,
        node_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[list[dict], int] | None:
        cached, fresh, total = self.get(node_id, limit=limit)
        if cached is None:
            perf.record("sessions.list.remote_cache.deferred_miss", 1.0)
            self.schedule_refresh(node_id)
            return None
        if fresh:
            perf.record("sessions.list.remote_cache.deferred_hit", 1.0)
        else:
            perf.record("sessions.list.remote_cache.deferred_stale", 1.0)
            self.schedule_refresh(node_id)
        return cached, total


cache = RemoteSessionsCache()

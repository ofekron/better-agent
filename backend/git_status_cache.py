from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from node_op import node_op

logger = logging.getLogger(__name__)

_Key = tuple[str, str]


class GitStatusCache:
    """TTL cache over per-(node, cwd) git status.

    Owns every piece of state the cache needs: the entries, the
    single-flight map of in-flight fetches, and the lock guarding both.
    A hit inside the TTL is served from memory; a stale hit is served
    immediately while a background refresh repopulates the entry.
    """

    def __init__(
        self,
        ttl_seconds: float = 60.0,
        startup_warm_limit: int = 8,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._startup_warm_limit = startup_warm_limit
        self._entries: dict[_Key, tuple[float, dict[str, Any]]] = {}
        self._inflight: dict[_Key, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def invalidate(self, node_id: str | None = None, cwd: str | None = None) -> None:
        if node_id is None and cwd is None:
            self._entries.clear()
            return
        for key in list(self._entries):
            key_node, key_cwd = key
            if node_id is not None and key_node != node_id:
                continue
            if cwd is not None and key_cwd != cwd:
                continue
            self._entries.pop(key, None)

    async def get(self, node_id: str, cwd: str) -> dict:
        key = (node_id, cwd)
        now = time.monotonic()
        async with self._lock:
            cached = self._entries.get(key)
            if cached and now - cached[0] <= self._ttl_seconds:
                return dict(cached[1])
            task = self._inflight.get(key)
            if cached:
                if task is None:
                    task = self._start_fetch(key)
                    task.add_done_callback(
                        lambda done, key=key: asyncio.create_task(
                            self._store_refresh(key, done),
                        ),
                    )
                return dict(cached[1])
            if task is None:
                task = self._start_fetch(key)

        try:
            result = await task
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

        if isinstance(result, dict):
            async with self._lock:
                self._entries[key] = (time.monotonic(), dict(result))
            return dict(result)
        return result

    async def warm_recent(self) -> None:
        """Prime the cache for the most recent projects on the local node."""
        import project_store

        try:
            projects = await asyncio.to_thread(project_store.list_projects)
        except Exception:
            logger.debug("git-status startup warm: project list failed", exc_info=True)
            return
        warmed = 0
        seen: set[_Key] = set()
        for project in projects:
            node_id = str(project.get("node_id") or "primary")
            cwd = str(project.get("path") or "")
            if node_id != "primary" or not cwd:
                continue
            key = (node_id, cwd)
            if key in seen:
                continue
            seen.add(key)
            try:
                await self.get(node_id, cwd)
            except Exception:
                logger.debug("git-status startup warm failed cwd=%s", cwd, exc_info=True)
            warmed += 1
            if warmed >= self._startup_warm_limit:
                break

    def _start_fetch(self, key: _Key) -> asyncio.Task:
        """Register and return the single in-flight fetch for `key`.
        Caller must hold `self._lock`."""
        node_id, cwd = key
        task = asyncio.create_task(node_op(node_id, "get_git_status", {"cwd": cwd}))
        self._inflight[key] = task
        return task

    async def _store_refresh(self, key: _Key, task: asyncio.Task) -> None:
        try:
            result = task.result()
        except Exception:
            result = None
        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
            if isinstance(result, dict):
                self._entries[key] = (time.monotonic(), dict(result))


cache = GitStatusCache()

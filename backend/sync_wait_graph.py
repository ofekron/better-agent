from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class CircularSyncWaitError(ValueError):
    def __init__(self, cycle: list[str]) -> None:
        self.cycle = tuple(cycle)
        super().__init__(
            f"circular synchronous wait rejected: {' -> '.join(cycle)}"
        )


class SyncWaitGraph:
    def __init__(self) -> None:
        self._edges: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()

    @contextmanager
    def waiting(self, caller_session_id: str, target_session_id: str) -> Iterator[None]:
        caller = str(caller_session_id or "").strip()
        target = str(target_session_id or "").strip()
        if not caller or not target:
            raise ValueError("synchronous wait requires caller and target session ids")

        with self._lock:
            path = [caller] if caller == target else self._path(target, caller)
            if path is not None:
                raise CircularSyncWaitError([caller, *path])
            targets = self._edges.setdefault(caller, {})
            targets[target] = targets.get(target, 0) + 1

        try:
            yield
        finally:
            with self._lock:
                targets = self._edges.get(caller)
                if targets:
                    remaining = targets.get(target, 0) - 1
                    if remaining > 0:
                        targets[target] = remaining
                    else:
                        targets.pop(target, None)
                    if not targets:
                        self._edges.pop(caller, None)

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                caller: dict(targets)
                for caller, targets in self._edges.items()
            }

    def _path(self, start: str, destination: str) -> list[str] | None:
        pending: list[tuple[str, list[str]]] = [(start, [start])]
        visited: set[str] = set()
        while pending:
            current, path = pending.pop()
            if current == destination:
                return path
            if current in visited:
                continue
            visited.add(current)
            pending.extend(
                (target, [*path, target])
                for target in reversed(self._edges.get(current, {}))
                if target not in visited
            )
        return None

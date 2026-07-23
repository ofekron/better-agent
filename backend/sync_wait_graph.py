from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Optional


class SyncWaitRejected(ValueError):
    """Common base for every structural sync-wait rejection (cycle or
    depth). Boundaries that must convert a rejection into an agent-visible
    error catch THIS type."""
    pass


class CircularSyncWaitError(SyncWaitRejected):
    def __init__(self, cycle: list[str]) -> None:
        self.cycle = tuple(cycle)
        super().__init__(
            f"circular synchronous wait rejected: {' -> '.join(cycle)}"
        )


class SyncWaitDepthError(SyncWaitRejected):
    def __init__(self, chain: list[str], cap: int) -> None:
        self.chain = tuple(chain)
        self.cap = cap
        super().__init__(
            "synchronous wait chain too deep "
            f"({len(chain) - 1} waits, cap {cap}): {' -> '.join(chain)}"
        )


class SyncWaitGraph:
    def __init__(self, depth_cap: Optional[Callable[[], int]] = None) -> None:
        self._edges: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()
        # Callable so the cap tracks live settings without a restart;
        # None or a non-positive value disables the depth check.
        self._depth_cap = depth_cap

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
            cap = self._depth_cap() if self._depth_cap is not None else 0
            if cap > 0:
                chain = [
                    *self._longest_chain(caller, upstream=True)[:-1],
                    caller,
                    target,
                    *self._longest_chain(target, upstream=False)[1:],
                ]
                if len(chain) - 1 > cap:
                    raise SyncWaitDepthError(chain, cap)
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

    def _longest_chain(self, node: str, *, upstream: bool) -> list[str]:
        """Longest simple wait chain ending at `node` (upstream=True: waiters
        above it) or starting at `node` (upstream=False: sessions it awaits),
        inclusive of `node`. The graph is cycle-free by construction, so a
        depth-first walk terminates; it stays tiny, so no memoization."""
        if upstream:
            reverse: dict[str, list[str]] = {}
            for caller, targets in self._edges.items():
                for target in targets:
                    reverse.setdefault(target, []).append(caller)
            neighbors = reverse
        else:
            neighbors = {
                caller: list(targets) for caller, targets in self._edges.items()
            }
        best = [node]
        pending: list[list[str]] = [[node]]
        while pending:
            chain = pending.pop()
            if len(chain) > len(best):
                best = chain
            pending.extend(
                [*chain, nxt]
                for nxt in neighbors.get(chain[-1], [])
                if nxt not in chain
            )
        return list(reversed(best)) if upstream else best

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

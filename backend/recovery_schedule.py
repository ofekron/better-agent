"""Ordering authority for crash-recovery session buckets.

Recovery integrates one bucket per app session. Order matters: the user
is waiting on exactly one session — the one they opened — and every
bucket integrated ahead of it is dead wait. The schedule is a max-heap
on last activity, so the most recently active sessions recover first,
and `boost()` promotes a session the user opens mid-recovery to the
next pop.

Promotion is a lazy decrease-key: `boost()` pushes a second entry at
the promoted rank and `pop()` drops entries for buckets already taken.
Boosts are rare, so the extra entries are bounded by the number of
sessions actually opened during recovery.

A boost that arrives before the bucket exists is remembered in
`_boosted`, which `push()` consults — recovery scanning the run root
races against the first client subscribing.
"""

from __future__ import annotations

import heapq
import itertools
import threading
from typing import Any, Optional

_PROMOTED_RANK = 0
_DEFAULT_RANK = 1


class _Descending:
    """Order-key wrapper that makes heapq pop the greatest key first."""

    __slots__ = ("key",)

    def __init__(self, key: Any) -> None:
        self.key = key

    def __lt__(self, other: "_Descending") -> bool:
        return self.key > other.key


class RecoverySchedule:
    def __init__(self) -> None:
        self._heap: list[tuple[int, _Descending, int, str]] = []
        self._order_keys: dict[str, Any] = {}
        self._items: dict[str, Any] = {}
        self._pending: set[str] = set()
        self._seq = itertools.count()
        self._lock = threading.Lock()

    def push(self, session_key: str, item: Any, order_key: Any) -> None:
        with self._lock:
            self._items[session_key] = item
            self._order_keys[session_key] = order_key
            self._pending.add(session_key)
            self._push_entry(session_key, order_key)

    def boost(self, session_key: str) -> bool:
        """Promote a queued bucket. False when it is already taken."""
        with self._lock:
            if session_key not in self._pending:
                return False
            self._push_entry(
                session_key,
                self._order_keys[session_key],
                rank=_PROMOTED_RANK,
            )
            return True

    def pop(self) -> Optional[tuple[str, Any]]:
        with self._lock:
            while self._heap:
                _rank, _order, _seq, session_key = heapq.heappop(self._heap)
                if session_key not in self._pending:
                    continue
                self._pending.discard(session_key)
                self._order_keys.pop(session_key, None)
                return session_key, self._items.pop(session_key)
            return None

    def _push_entry(
        self, session_key: str, order_key: Any, rank: Optional[int] = None,
    ) -> None:
        if rank is None:
            rank = _PROMOTED_RANK if session_key in _boosted else _DEFAULT_RANK
        heapq.heappush(
            self._heap,
            (rank, _Descending(order_key), next(self._seq), session_key),
        )


_boosted: set[str] = set()
_active: Optional[RecoverySchedule] = None
_active_lock = threading.Lock()


def set_active(schedule: Optional[RecoverySchedule]) -> None:
    global _active
    with _active_lock:
        _active = schedule


def boost(session_key: str) -> None:
    """Record that the user wants `session_key` recovered next."""
    if not session_key:
        return
    _boosted.add(session_key)
    with _active_lock:
        schedule = _active
    if schedule is not None:
        schedule.boost(session_key)


def is_boosted(session_key: str | None) -> bool:
    return bool(session_key) and session_key in _boosted


def priority_rank(session_key: str | None) -> int:
    """Sort key for callers that order buckets themselves rather than
    popping this heap — the cold/background batches in main. Lower
    sorts first, matching the heap's own ranking."""
    return _PROMOTED_RANK if is_boosted(session_key) else _DEFAULT_RANK


def reset_for_tests() -> None:
    global _active
    _boosted.clear()
    with _active_lock:
        _active = None

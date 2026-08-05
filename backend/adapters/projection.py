"""Shared in-memory projection state for concrete surface adapters.

No persistence lives here — concrete adapters own their own durable state
and call into this base only for revision bookkeeping and subscriber
fan-out. Kept generic and surface-agnostic on purpose (see
backend/surface_contract/ for the contracts this feeds)."""

from __future__ import annotations

import logging
import os
import threading

from backend.event_bus import Handler, bus
from backend.surface_contract.identity import Emit, SnapshotIdentity

logger = logging.getLogger(__name__)


class SurfaceProjection:
    """Per-surface revision counters + subscriber broadcast, in memory.

    `incarnation` is a fresh token generated once per construction — it
    has no meaning across a process restart, which is exactly the signal
    a resuming client needs to detect a stale cursor (ADR 0006 §2:
    SnapshotIdentity)."""

    def __init__(self) -> None:
        self._incarnation = os.urandom(8).hex()
        self._lock = threading.Lock()
        self._render_rev = 0
        self._hist_rev = 0
        self._emits: list[Emit] = []

    def snapshot(self) -> SnapshotIdentity:
        with self._lock:
            return SnapshotIdentity(
                incarnation=self._incarnation,
                render_rev=self._render_rev,
                hist_rev=self._hist_rev,
            )

    def bump_render(self) -> int:
        with self._lock:
            self._render_rev += 1
            return self._render_rev

    def bump_hist(self) -> int:
        with self._lock:
            self._hist_rev += 1
            return self._hist_rev

    def register(self, emit: Emit) -> None:
        with self._lock:
            self._emits.append(emit)

    def unregister(self, emit: Emit) -> None:
        with self._lock:
            try:
                self._emits.remove(emit)
            except ValueError:
                pass

    def broadcast(self, frame: object) -> None:
        """Fan `frame` out to every registered subscriber. One subscriber
        raising never blocks or drops delivery to the others."""
        with self._lock:
            emits = list(self._emits)
        for emit in emits:
            try:
                emit(frame)
            except Exception:
                logger.exception(
                    "surface projection: subscriber raised on broadcast",
                )


class BusBoundProjection(SurfaceProjection):
    """Adds idempotent event-bus binding on top of `SurfaceProjection`.

    Mirrors the native_files_manager.bind() idiom (backend/native_files_manager.py):
    unsubscribe-then-subscribe by a deterministic per-(class, pattern) name
    so calling `bind()` again (e.g. after a reconnect) never duplicates
    subscriptions."""

    def bind(self, subscriptions: list[tuple[str, Handler]]) -> None:
        for pattern, _handler in subscriptions:
            bus.unsubscribe(self._sub_name(pattern))
        for pattern, handler in subscriptions:
            bus.subscribe(pattern, handler, name=self._sub_name(pattern))

    def _sub_name(self, pattern: str) -> str:
        return f"{type(self).__name__}:{pattern}"

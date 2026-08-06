"""Shared in-memory projection state for concrete surface adapters.

No persistence lives here — concrete adapters own their own durable state
and call into this base only for revision bookkeeping and subscriber
fan-out. Kept generic and surface-agnostic on purpose (see
backend/surface_contract/ for the contracts this feeds)."""

from __future__ import annotations

import logging
import os
import threading

from backend import paths, scheme_migrations
from backend.event_bus import Handler, bus
from backend.surface_contract.identity import Emit, SnapshotIdentity

logger = logging.getLogger(__name__)

# Schema version for a `SurfaceProjection`'s persisted incarnation-token
# state (see `backend/scheme_migrations.py`). Bumping this constant
# requires a contiguous registered migration chain from v1 for every
# component that opts into persistence — enforced by
# `backend/scripts/test_scheme_home.py`.
CURRENT_SCHEME_VERSION = 1

_INCARNATION_FILE = "incarnation"


class SurfaceProjection:
    """Per-surface revision counters + subscriber broadcast, in memory.

    `incarnation` is a fresh token generated once per construction — it
    has no meaning across a process restart, which is exactly the signal
    a resuming client needs to detect a stale cursor (ADR 0006 §2:
    SnapshotIdentity) — UNLESS constructed with `component`, in which
    case the incarnation token is persisted under
    `scheme_migrations.ensure(component, CURRENT_SCHEME_VERSION)` and
    reused across constructions in the same BA home, so a process
    restart no longer looks like a fresh incarnation to resuming clients.
    """

    def __init__(self, component: str | None = None) -> None:
        self._incarnation = self._resolve_incarnation(component)
        self._lock = threading.Lock()
        self._render_rev = 0
        self._hist_rev = 0
        self._emits: list[Emit] = []

    @staticmethod
    def _resolve_incarnation(component: str | None) -> str:
        if component is None:
            return os.urandom(8).hex()
        state_dir = scheme_migrations.ensure(component, CURRENT_SCHEME_VERSION)
        incarnation_path = state_dir / _INCARNATION_FILE
        try:
            existing = incarnation_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            existing = ""
        if existing:
            return existing
        token = os.urandom(8).hex()
        incarnation_path.write_text(token, encoding="utf-8")
        paths.make_private_file(incarnation_path)
        return token

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

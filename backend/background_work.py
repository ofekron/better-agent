"""Core-owned registry of user-visible background work.

Every producer of background work — core subsystem or granted extension —
reports through this one funnel, and the frontend renders the result in a
single manager surface. Authoritative state for the work ITSELF stays with
each producer; this registry holds only the disposable projection the UI
needs, so it lives in memory and is never migrated.

Items are global-scoped. `global_ws_callbacks` carries no connection
identity, so `broadcast_global` cannot filter per connection, and the
per-session path persists every frame into `events.jsonl` — which would
journal ephemeral UI state and replay it as ghosts on reconnect. `session_id`
is therefore attribution for grouping and labelling only, never an
authorization boundary.

Clients guard against snapshot/delta interleaving with `(epoch, seq)`: `seq`
is monotonic within a registry instance, and `epoch` changes whenever the
registry is rebuilt, telling a reconnecting client to discard what it has and
take the fresh snapshot.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

EVENT_TYPE = "background_work_changed"

OWNER_CORE = "core"
OWNER_EXTENSION = "extension"
_OWNER_KINDS = frozenset((OWNER_CORE, OWNER_EXTENSION))

KIND_WORK = "work"
_KINDS = frozenset((KIND_WORK,))

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"
_STATUSES = frozenset((
    STATUS_PENDING, STATUS_RUNNING, STATUS_SUCCEEDED,
    STATUS_FAILED, STATUS_CANCELLED, STATUS_UNKNOWN,
))
_TERMINAL_STATUSES = frozenset((
    STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED,
))

# `unknown` is deliberately non-terminal: it means "this durable record
# outlived a restart and nothing has re-registered it", so it stays visible
# until an owner resolves it rather than being evicted as finished.

_ACTION_KINDS = frozenset(("cancel", "retry", "open_session", "open_url"))

# Caps live here, in the mutation funnel, because `validate_global_event`
# only ever sees events that go out globally and so cannot be the primary
# chokepoint.
_MAX_ITEMS_PER_OWNER = 32
_TERMINAL_RETENTION_CAP = 50
_MAX_LABEL = 120
_MAX_DETAIL = 300
_MAX_ERROR = 300
_MAX_PHASE = 80
_MAX_ACTIONS = 3
_MAX_ACTION_TARGET = 512


class BackgroundWorkRejected(Exception):
    """Raised when a report is refused — an owner over its item cap, or a
    field that cannot be coerced into the contract. Callers fail closed:
    the registry is left untouched and nothing is broadcast."""


def _clamp(value: Optional[str], limit: int) -> Optional[str]:
    """Owner-supplied display text is clamped rather than rejected: a long
    label is a formatting problem, and dropping the whole item would hide
    real work from the user."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit]


def _normalize_progress(progress: Optional[dict]) -> Optional[dict]:
    """`None` means indeterminate. A `total` of None keeps the item
    indeterminate but carries a count, so the UI never renders a
    percentage it cannot compute."""
    if progress is None:
        return None
    if not isinstance(progress, dict):
        raise BackgroundWorkRejected("progress must be an object or null")
    try:
        completed = int(progress.get("completed", 0))
    except (TypeError, ValueError):
        raise BackgroundWorkRejected("progress.completed must be an integer")
    raw_total = progress.get("total")
    total: Optional[int]
    if raw_total is None:
        total = None
    else:
        try:
            total = int(raw_total)
        except (TypeError, ValueError):
            raise BackgroundWorkRejected("progress.total must be an integer or null")
        if total <= 0:
            raise BackgroundWorkRejected("progress.total must be positive")
    completed = max(0, completed)
    if total is not None:
        completed = min(completed, total)
    unit = _clamp(progress.get("unit"), 32) or ""
    return {"completed": completed, "total": total, "unit": unit}


def _normalize_actions(actions: Optional[list]) -> list[dict]:
    if not actions:
        return []
    if not isinstance(actions, list):
        raise BackgroundWorkRejected("actions must be a list")
    if len(actions) > _MAX_ACTIONS:
        raise BackgroundWorkRejected("too many actions")
    out: list[dict] = []
    for action in actions:
        if not isinstance(action, dict):
            raise BackgroundWorkRejected("action must be an object")
        kind = action.get("kind")
        if kind not in _ACTION_KINDS:
            raise BackgroundWorkRejected(f"unsupported action kind: {kind!r}")
        target = _clamp(action.get("target"), _MAX_ACTION_TARGET) or ""
        if kind == "open_url" and not target.startswith("https://"):
            raise BackgroundWorkRejected("open_url target must be https")
        out.append({"kind": kind, "target": target})
    return out


@dataclass
class BackgroundWorkItem:
    id: str
    kind: str
    owner_kind: str
    owner_id: str
    label: str
    status: str
    seq: int
    started_at: str
    updated_at: str
    session_id: Optional[str] = None
    title_key: Optional[str] = None
    title_params: dict = field(default_factory=dict)
    detail: Optional[str] = None
    phase: Optional[str] = None
    error: Optional[str] = None
    progress: Optional[dict] = None
    actions: list = field(default_factory=list)
    finished_at: Optional[str] = None
    dismissible: bool = True
    retention_ms: int = 6000


class BackgroundWorkRegistry:
    """In-memory projection of in-flight background work.

    Mutation is serialized by one lock; the broadcast happens after the lock
    is released so a slow WS fan-out cannot hold up producers. Every mutation
    advances `seq`.
    """

    def __init__(self) -> None:
        self._items: "OrderedDict[str, BackgroundWorkItem]" = OrderedDict()
        self._lock = threading.Lock()
        self._seq = 0
        self._epoch = uuid.uuid4().hex
        self._coordinator: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- Wiring ---------------------------------------------------------

    def bind(self, coordinator: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._coordinator = coordinator
        self._loop = loop

    @property
    def epoch(self) -> str:
        return self._epoch

    # -- Mutations ------------------------------------------------------

    def reset(self) -> None:
        """Drop everything and re-epoch so connected clients discard their
        maps instead of merging pre-reset items into post-reset state."""
        with self._lock:
            self._items.clear()
            self._seq = 0
            self._epoch = uuid.uuid4().hex
            epoch = self._epoch
        self._broadcast({"epoch": epoch, "cleared": True})

    def report(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        local_id: str,
        label: str,
        status: str = STATUS_RUNNING,
        kind: str = KIND_WORK,
        session_id: Optional[str] = None,
        title_key: Optional[str] = None,
        title_params: Optional[dict] = None,
        detail: Optional[str] = None,
        phase: Optional[str] = None,
        progress: Optional[dict] = None,
        actions: Optional[list] = None,
        dismissible: bool = True,
        retention_ms: int = 6000,
    ) -> str:
        if owner_kind not in _OWNER_KINDS:
            raise BackgroundWorkRejected(f"unsupported owner_kind: {owner_kind!r}")
        if kind not in _KINDS:
            raise BackgroundWorkRejected(f"unsupported kind: {kind!r}")
        if status not in _STATUSES:
            raise BackgroundWorkRejected(f"unsupported status: {status!r}")
        if not owner_id or not local_id:
            raise BackgroundWorkRejected("owner_id and local_id are required")

        item_id = f"{owner_kind}:{owner_id}:{local_id}"
        now = datetime.now().isoformat()
        normalized_progress = _normalize_progress(progress)
        normalized_actions = _normalize_actions(actions)

        with self._lock:
            existing = self._items.get(item_id)
            if existing is None and self._owner_count_locked(owner_kind, owner_id) >= _MAX_ITEMS_PER_OWNER:
                raise BackgroundWorkRejected(
                    f"owner {owner_kind}:{owner_id} exceeded the background work item cap"
                )
            self._seq += 1
            item = BackgroundWorkItem(
                id=item_id,
                kind=kind,
                owner_kind=owner_kind,
                owner_id=owner_id,
                label=_clamp(label, _MAX_LABEL) or "",
                status=status,
                seq=self._seq,
                started_at=existing.started_at if existing else now,
                updated_at=now,
                session_id=session_id,
                title_key=title_key,
                title_params=dict(title_params or {}),
                detail=_clamp(detail, _MAX_DETAIL),
                phase=_clamp(phase, _MAX_PHASE),
                progress=normalized_progress,
                actions=normalized_actions,
                dismissible=dismissible,
                retention_ms=retention_ms,
            )
            if status in _TERMINAL_STATUSES:
                item.finished_at = now
            self._items[item_id] = item
            self._items.move_to_end(item_id)
            self._evict_terminal_locked()
            snapshot = asdict(item)
            epoch = self._epoch
        self._broadcast({"epoch": epoch, "item": snapshot})
        return item_id

    def update(
        self,
        item_id: str,
        *,
        label: Optional[str] = None,
        phase: Optional[str] = None,
        detail: Optional[str] = None,
        progress: Optional[dict] = None,
        status: Optional[str] = None,
    ) -> bool:
        """Patch a live item. Returns False when the id is unknown or has
        already reached a terminal state — a late update must not resurrect
        finished work."""
        if status is not None and status not in _STATUSES:
            raise BackgroundWorkRejected(f"unsupported status: {status!r}")
        if status in _TERMINAL_STATUSES:
            raise BackgroundWorkRejected("use finish() for terminal transitions")
        normalized_progress = _normalize_progress(progress) if progress is not None else None

        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.status in _TERMINAL_STATUSES:
                return False
            if label is not None:
                item.label = _clamp(label, _MAX_LABEL) or ""
            if phase is not None:
                item.phase = _clamp(phase, _MAX_PHASE)
            if detail is not None:
                item.detail = _clamp(detail, _MAX_DETAIL)
            if normalized_progress is not None:
                item.progress = normalized_progress
            if status is not None:
                item.status = status
            self._seq += 1
            item.seq = self._seq
            item.updated_at = datetime.now().isoformat()
            snapshot = asdict(item)
            epoch = self._epoch
        self._broadcast({"epoch": epoch, "item": snapshot})
        return True

    def finish(
        self,
        item_id: str,
        *,
        status: str = STATUS_SUCCEEDED,
        error: Optional[str] = None,
    ) -> bool:
        """Terminal transition. Idempotent: finishing an already-finished
        item is a no-op and emits nothing, so a producer whose cleanup runs
        twice cannot double-broadcast."""
        if status not in _TERMINAL_STATUSES:
            raise BackgroundWorkRejected(f"{status!r} is not a terminal status")
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.status in _TERMINAL_STATUSES:
                return False
            item.status = status
            item.error = _clamp(error, _MAX_ERROR)
            item.finished_at = datetime.now().isoformat()
            item.updated_at = item.finished_at
            self._seq += 1
            item.seq = self._seq
            snapshot = asdict(item)
            self._evict_terminal_locked()
            epoch = self._epoch
        self._broadcast({"epoch": epoch, "item": snapshot})
        return True

    def dismiss(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if item is None or not item.dismissible:
                return False
            self._items.pop(item_id, None)
            self._seq += 1
            epoch = self._epoch
            seq = self._seq
        self._broadcast({"epoch": epoch, "seq": seq, "removed_id": item_id})
        return True

    def finish_owner(
        self,
        owner_kind: str,
        owner_id: str,
        *,
        status: str = STATUS_CANCELLED,
        error: Optional[str] = None,
    ) -> int:
        """Finalize every open item for one owner. Used when an owner
        becomes observably gone (extension disabled, uninstalled, token
        invalidated) so liveness never depends on a timer."""
        prefix = f"{owner_kind}:{owner_id}:"
        with self._lock:
            open_ids = [
                item_id for item_id, item in self._items.items()
                if item_id.startswith(prefix) and item.status not in _TERMINAL_STATUSES
            ]
        for item_id in open_ids:
            self.finish(item_id, status=status, error=error)
        return len(open_ids)

    # -- Reads ----------------------------------------------------------

    def snapshot(self) -> dict:
        """First-paint payload. Self-sufficient by contract — the manager
        renders from this alone, with no WS dependency, so it still works
        while the backend is mid-boot and the WS channel is closed."""
        with self._lock:
            return {
                "epoch": self._epoch,
                "seq": self._seq,
                "items": [asdict(item) for item in self._items.values()],
            }

    def get(self, item_id: str) -> Optional[dict]:
        with self._lock:
            item = self._items.get(item_id)
            return asdict(item) if item is not None else None

    # -- Internals ------------------------------------------------------

    def _owner_count_locked(self, owner_kind: str, owner_id: str) -> int:
        prefix = f"{owner_kind}:{owner_id}:"
        return sum(1 for item_id in self._items if item_id.startswith(prefix))

    def _evict_terminal_locked(self) -> None:
        """Cap retained finished items FIFO. Open work is never evicted."""
        terminal_ids = [
            item_id for item_id, item in self._items.items()
            if item.status in _TERMINAL_STATUSES
        ]
        excess = len(terminal_ids) - _TERMINAL_RETENTION_CAP
        for item_id in terminal_ids[:excess] if excess > 0 else []:
            self._items.pop(item_id, None)

    async def _broadcast_guarded(self, data: dict) -> None:
        """`schedule_global` raises once the global channel is draining at
        shutdown. The registry mutation has already been applied and is the
        state that matters, so a lost notification is absorbed here rather
        than surfacing as a never-retrieved task exception."""
        try:
            await self._coordinator.broadcast_global(EVENT_TYPE, data)
        except RuntimeError:
            logger.debug("background work broadcast dropped: global channel closed")
        except Exception:
            logger.exception("background work broadcast failed")

    def _broadcast(self, data: dict) -> None:
        """Schedule the delta on whichever loop this caller can reach. The
        two branches are exclusive and each closes the coroutine on failure,
        so a coroutine is never scheduled twice or left un-awaited."""
        if self._coordinator is None:
            return
        coro = self._broadcast_guarded(data)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            try:
                loop.create_task(coro)
                return
            except Exception:
                logger.exception("background work broadcast: create_task failed")
                coro.close()
                return
        if self._loop is not None and not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(coro, self._loop)
                return
            except Exception:
                logger.exception("background work broadcast schedule failed")
        coro.close()


background_work_registry = BackgroundWorkRegistry()

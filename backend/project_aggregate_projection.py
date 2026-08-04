from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from event_bus import BusEvent, bus

logger = logging.getLogger(__name__)

BroadcastGlobal = Callable[[str, dict], Awaitable[None]]
ProjectKey = tuple[str, str]

_COUNTER_FIELDS = (
    ("running", "running_count"),
    ("unread", "unread_session_count"),
    ("waiting_for_user", "waiting_for_user_count"),
    ("errored", "errored_count"),
)


def empty_aggregate() -> dict[str, int]:
    return {counter: 0 for _, counter in _COUNTER_FIELDS}


@dataclass(frozen=True)
class _Contribution:
    project_key: ProjectKey | None
    running: bool
    unread: bool
    waiting_for_user: bool
    errored: bool


@dataclass
class _Aggregate:
    members: int
    counters: dict[str, int]


class ProjectAggregateProjection:
    def __init__(self) -> None:
        self._epoch = uuid.uuid4().hex
        self._revision = 0
        self._contributions: dict[str, _Contribution] = {}
        self._aggregates: dict[ProjectKey, _Aggregate] = {}
        self._pending: dict[str, dict] = {}
        self._drain_task: asyncio.Task[None] | None = None
        self._broadcast_global: BroadcastGlobal | None = None

    def bind(self, broadcast_global: BroadcastGlobal) -> None:
        self._broadcast_global = broadcast_global
        bus.unsubscribe("project_aggregate_projection")
        bus.subscribe(
            "session.status_projected",
            self._on_status_projected,
            priority=60,
            name="project_aggregate_projection",
        )

    async def unbind(self) -> None:
        bus.unsubscribe("project_aggregate_projection")
        await self.flush()
        self._broadcast_global = None

    async def _on_status_projected(self, event: BusEvent) -> None:
        try:
            payload = self._validated_payload(event)
        except ValueError as exc:
            logger.error("invalid canonical session status fact: %s", exc)
            return
        self._pending[event.sid] = payload
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(
                self._drain(),
                name="project-aggregate-projection-drain",
            )

    @staticmethod
    def _validated_payload(event: BusEvent) -> dict:
        payload = event.payload
        if payload.get("session_id") != event.sid:
            raise ValueError("status fact session_id does not match sid")
        if payload.get("deleted") is True:
            return {"session_id": event.sid, "deleted": True}
        project_key = payload.get("project_key")
        if project_key is not None:
            if not isinstance(project_key, dict):
                raise ValueError("status fact project_key must be an object")
            cwd = project_key.get("cwd")
            node_id = project_key.get("node_id")
            if not isinstance(cwd, str) or not cwd:
                raise ValueError("status fact project cwd must be non-empty")
            if not isinstance(node_id, str) or not node_id:
                raise ValueError("status fact project node_id must be non-empty")
        for field, _ in _COUNTER_FIELDS:
            if type(payload.get(field)) is not bool:
                raise ValueError(f"status fact {field} must be boolean")
        return dict(payload)

    async def _drain(self) -> None:
        try:
            while self._pending:
                batch, self._pending = self._pending, {}
                changed_keys: set[ProjectKey] = set()
                for sid, payload in batch.items():
                    changed_keys.update(self._apply(sid, payload))
                if not changed_keys:
                    continue
                self._revision += 1
                upserts: list[dict] = []
                tombstones: list[dict] = []
                for key in sorted(changed_keys):
                    aggregate = self._aggregates.get(key)
                    record = self._record(key, aggregate)
                    if aggregate is None:
                        tombstones.append(record)
                    else:
                        upserts.append(record)
                frame = {
                    "epoch": self._epoch,
                    "revision": self._revision,
                    "upserts": upserts,
                    "tombstones": tombstones,
                }
                broadcast_global = self._broadcast_global
                if broadcast_global is not None:
                    try:
                        await broadcast_global("project_aggregates_changed", frame)
                    except Exception:
                        logger.exception("project aggregate WS broadcast failed")
        finally:
            self._drain_task = None
            if self._pending and self._broadcast_global is not None:
                self._drain_task = asyncio.create_task(
                    self._drain(),
                    name="project-aggregate-projection-drain",
                )

    def _apply(self, sid: str, payload: dict) -> set[ProjectKey]:
        previous = self._contributions.get(sid)
        current = None if payload.get("deleted") is True else self._contribution(payload)
        if previous == current:
            return set()
        changed: set[ProjectKey] = set()
        if previous is not None:
            self._adjust(previous, -1)
            if previous.project_key is not None:
                changed.add(previous.project_key)
        if current is None:
            self._contributions.pop(sid, None)
        else:
            self._contributions[sid] = current
            self._adjust(current, 1)
            if current.project_key is not None:
                changed.add(current.project_key)
        return changed

    @staticmethod
    def _contribution(payload: dict) -> _Contribution:
        raw_key = payload.get("project_key")
        project_key = None
        if raw_key is not None:
            project_key = (raw_key["cwd"], raw_key["node_id"])
        return _Contribution(
            project_key=project_key,
            running=payload["running"],
            unread=payload["unread"],
            waiting_for_user=payload["waiting_for_user"],
            errored=payload["errored"],
        )

    def _adjust(self, contribution: _Contribution, delta: int) -> None:
        key = contribution.project_key
        if key is None:
            return
        aggregate = self._aggregates.get(key)
        if aggregate is None:
            if delta < 0:
                raise RuntimeError("project aggregate contribution underflow")
            aggregate = _Aggregate(members=0, counters=empty_aggregate())
            self._aggregates[key] = aggregate
        aggregate.members += delta
        for field, counter in _COUNTER_FIELDS:
            if getattr(contribution, field):
                aggregate.counters[counter] += delta
                if aggregate.counters[counter] < 0:
                    raise RuntimeError("project aggregate counter underflow")
        if aggregate.members < 0:
            raise RuntimeError("project aggregate membership underflow")
        if aggregate.members == 0:
            self._aggregates.pop(key)

    @staticmethod
    def _record(key: ProjectKey, aggregate: _Aggregate | None) -> dict:
        return {
            "path": key[0],
            "node_id": key[1],
            **(empty_aggregate() if aggregate is None else aggregate.counters),
        }

    async def flush(self) -> None:
        while self._drain_task is not None:
            await self._drain_task

    async def snapshot(self) -> dict:
        await self.flush()
        return {
            "epoch": self._epoch,
            "revision": self._revision,
            "aggregates": {
                key: dict(aggregate.counters)
                for key, aggregate in self._aggregates.items()
            },
        }


projector = ProjectAggregateProjection()


def bind(broadcast_global: BroadcastGlobal) -> None:
    projector.bind(broadcast_global)


async def unbind() -> None:
    await projector.unbind()


async def flush() -> None:
    await projector.flush()


async def snapshot() -> dict:
    return await projector.snapshot()

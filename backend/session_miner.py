"""Two-layer session-mining abstraction.

``SessionMinerBase`` owns the parts every source shares: the per-source mtime
watermark (so unchanged sources are skipped without parsing), the delta-filter
``__iter__``, and the ``mine(consumers)`` driver that fans a single pass out to
N registered consumers. A source is a concrete subclass implementing
``_iter_sources`` — yielding ``(key, visit, mtime)`` for every candidate file
(changed or not); the base applies the delta filter and records watermarks.

Implementations:
- :class:`SessionMiner` — Better Agent session snapshots (``sessions/*.json`` +
  their ``events.jsonl``). The historical source.
- :class:`NativeSessionMiner` (in ``native_session_miner``) — provider-native
  transcripts (e.g. Claude ``projects/<cwd>/<sid>.jsonl``), used when the raw
  native prompt stream is more reliable than the BA render-tree projection.

A consumer subclasses :class:`SessionConsumer` and implements ``begin`` /
``visit`` / ``commit``; it is source-agnostic — it only consumes a
:class:`SessionVisit`. ``mine_registered(state)`` runs every registered
consumer against BA-session sources in one pass.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from paths import bc_home
from render_tree_hydrate import event_rows_by_msg_id_with_orphans


@dataclass
class SessionVisit:
    sid: str
    cwd: str
    data: dict
    messages: list
    events_by_msg_id: dict


class SessionConsumer(ABC):
    """Extension hook: derive one artifact stream from changed sessions.

    The miner calls ``begin`` once before the pass, ``visit`` per changed
    session, and ``commit`` once after. ``visit`` must be order-independent
    enough to tolerate sessions arriving in glob order; consumers sort their own
    output in ``commit``.
    """

    name: str = "consumer"

    @abstractmethod
    def begin(self) -> None: ...

    @abstractmethod
    def visit(self, visit: SessionVisit) -> None: ...

    @abstractmethod
    def commit(self) -> int: ...


_CONSUMERS: list[type[SessionConsumer]] = []


def register_consumer(consumer_cls: type[SessionConsumer]) -> type[SessionConsumer]:
    """Register a consumer class for ``mine_registered`` discovery.

    A class (not an instance) so each mining pass gets a fresh consumer and
    per-pass accumulation state cannot leak across runs.
    """
    _CONSUMERS.append(consumer_cls)
    return consumer_cls


def registered_consumers() -> list[type[SessionConsumer]]:
    return list(_CONSUMERS)


def clear_consumers() -> None:
    """Test helper: reset the registry between tests."""
    _CONSUMERS.clear()


def sessions_dir() -> Path:
    return bc_home() / "sessions"


def _events_path(root: Path, sid: str) -> Path:
    return root / sid / "events.jsonl"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class SessionMinerBase(ABC):
    """Source-agnostic miner driver.

    Subclasses implement :meth:`_iter_sources`, yielding one ``(key, visit,
    mtime)`` triple per candidate source (the base applies the delta filter, so
    yield every candidate — changed or not). ``key`` is the stable watermark
    identifier (e.g. the session-json filename); ``mtime`` is the freshness
    fingerprint (the base stores it and uses it to skip unchanged sources).

    ``state`` maps ``key -> {"mtime": float}``; persisting ``state`` between
    runs makes every pass delta-only. ``scanned_count`` is the total source
    files examined (changed or not).
    """

    def __init__(self, state: dict, *, root: Path | None = None) -> None:
        self._state = state
        self._root = root or sessions_dir()
        self._pending_watermarks: dict[str, float] = {}
        self.scanned_count = 0

    @abstractmethod
    def _iter_sources(self) -> Iterable[tuple[str, SessionVisit, float]]:
        """Yield ``(key, visit, mtime)`` for every candidate source."""

    def __iter__(self) -> Iterable[SessionVisit]:
        for key, visit, current_mtime in self._iter_sources():
            self.scanned_count += 1
            if current_mtime <= self._state.get(key, {}).get("mtime", 0.0):
                continue
            self._pending_watermarks[key] = current_mtime
            yield visit

    def mine(self, consumers: list[SessionConsumer]) -> dict[str, int]:
        """One pass: begin all consumers, visit each changed source, commit all.

        Returns ``{consumer.name: new_count}``. Each source is parsed once
        regardless of how many consumers run.
        """
        for consumer in consumers:
            consumer.begin()
        for visit in self:
            for consumer in consumers:
                consumer.visit(visit)
        counts = {consumer.name: consumer.commit() for consumer in consumers}
        self.apply_watermarks()
        return counts

    def apply_watermarks(self) -> None:
        """Write the per-source mtimes recorded during the last iteration.

        Called automatically by :meth:`mine`; callers that drive ``visit``
        across multiple sources manually (e.g. a dual native + BA pass) call
        this once per source after iterating, then persist ``state``.
        """
        for key, mtime in self._pending_watermarks.items():
            self._state[key] = {"mtime": mtime}


class SessionMiner(SessionMinerBase):
    """Better Agent session-snapshot source.

    Iterates ``sessions/*.json`` (skipping ``.summary.json``) and yields one
    :class:`SessionVisit` per session changed since the stored watermark, with
    messages from the session snapshot and events from the sibling
    ``events.jsonl``. ``state`` maps ``<session.json name> -> {"mtime": float}``.
    """

    def _iter_sources(self) -> Iterable[tuple[str, SessionVisit, float]]:
        if not self._root.exists():
            return
        for session_json in self._root.glob("*.json"):
            if session_json.name.endswith(".summary.json"):
                continue
            sid = session_json.stem
            key = session_json.name
            current_mtime = max(
                _mtime(session_json),
                _mtime(_events_path(self._root, sid)),
            )
            try:
                data = json.loads(session_json.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            visit = SessionVisit(
                sid=sid,
                cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else "",
                data=data,
                messages=data.get("messages", []) if isinstance(data.get("messages"), list) else [],
                events_by_msg_id=event_rows_by_msg_id_with_orphans(data, sid),
            )
            yield key, visit, current_mtime


def mine_registered(state: dict) -> dict[str, int]:
    """Run every registered consumer against changed BA sessions in one pass."""
    classes = registered_consumers()
    if not classes:
        return {}
    return SessionMiner(state).mine([cls() for cls in classes])

"""Generic shared session-mining base.

One pass over ``bc_home()/sessions/`` fans a normalized session stream out to N
extension consumers (requirements, assistant, ...) so each extension stops
reimplementing session iteration, ``events.jsonl`` parsing, and delta
watermarks. Scanning the sessions tree once and routing each session to every
registered consumer keeps mining cost flat in the number of extensions instead
of growing with it.

Extensibility model:
- A consumer subclasses ``SessionConsumer`` and implements ``begin`` (load its
  own state/dedup sets), ``visit`` (accumulate from one ``SessionVisit``), and
  ``commit`` (persist, return how many new records it wrote).
- Consumers register via ``register_consumer`` (usually at module import).
- ``SessionMiner.mine(consumers)`` drives the full lifecycle over a single pass.
- ``mine_registered(state)`` runs every registered consumer in one pass and
  returns ``{consumer.name: new_count}``.

The miner owns the per-file mtime watermark inside a ``state`` dict the caller
persists between runs; unchanged sessions are skipped without parsing. Each
consumer owns its own derived state separately.
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


class SessionMiner:
    """Yield one ``SessionVisit`` per session changed since the stored watermark.

    ``state`` maps ``<session.json name> -> {"mtime": float}``; the miner writes
    the new watermark into it after each visit is consumed, so persisting
    ``state`` between runs makes every pass delta-only. ``scanned_count`` is the
    total session files examined (changed or not).
    """

    def __init__(self, state: dict, *, root: Path | None = None) -> None:
        self._state = state
        self._root = root or sessions_dir()
        self._pending_watermarks: dict[str, float] = {}
        self.scanned_count = 0

    def __iter__(self) -> Iterable[SessionVisit]:
        if not self._root.exists():
            return
        for session_json in self._root.glob("*.json"):
            if session_json.name.endswith(".summary.json"):
                continue
            self.scanned_count += 1
            key = session_json.name
            current_mtime = max(
                _mtime(session_json),
                _mtime(_events_path(self._root, session_json.stem)),
            )
            if current_mtime <= self._state.get(key, {}).get("mtime", 0.0):
                continue
            try:
                data = json.loads(session_json.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            sid = session_json.stem
            visit = SessionVisit(
                sid=sid,
                cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else "",
                data=data,
                messages=data.get("messages", []) if isinstance(data.get("messages"), list) else [],
                events_by_msg_id=event_rows_by_msg_id_with_orphans(data, sid),
            )
            self._pending_watermarks[key] = current_mtime
            yield visit

    def mine(self, consumers: list[SessionConsumer]) -> dict[str, int]:
        """One pass: begin all consumers, visit each changed session, commit all.

        Returns ``{consumer.name: new_count}``. Each session is parsed once
        regardless of how many consumers run.
        """
        for consumer in consumers:
            consumer.begin()
        for visit in self:
            for consumer in consumers:
                consumer.visit(visit)
        counts = {consumer.name: consumer.commit() for consumer in consumers}
        for key, mtime in self._pending_watermarks.items():
            self._state[key] = {"mtime": mtime}
        return counts


def mine_registered(state: dict) -> dict[str, int]:
    """Run every registered consumer against changed sessions in one pass."""
    classes = registered_consumers()
    if not classes:
        return {}
    return SessionMiner(state).mine([cls() for cls in classes])

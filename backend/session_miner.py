"""Generic shared session-mining base.

One pass over ``bc_home()/sessions/`` fans a normalized session stream out to N
extension consumers (requirements, assistant requirement-states, ...) so each
extension no longer reimplements session iteration, ``events.jsonl`` parsing,
and delta watermarks. Scanning the sessions tree once and routing each session
to every registered consumer is what keeps the mining cost flat in the number
of extensions instead of growing with it.

A consumer either iterates a ``SessionMiner`` directly (single consumer,
streaming) or calls ``mine(*consumers)`` to drive one pass that fans each
changed session out to several consumers. The miner owns the per-file mtime
watermark inside a ``state`` dict the caller persists between runs; unchanged
sessions are skipped without parsing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from paths import bc_home
from render_tree_hydrate import event_rows_by_msg_id_with_orphans


@dataclass
class SessionVisit:
    sid: str
    cwd: str
    data: dict
    messages: list
    events_by_msg_id: dict


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
    total session files examined (changed or not), preserving the legacy
    scanner's reporting semantics.
    """

    def __init__(self, state: dict, *, root: Path | None = None) -> None:
        self._state = state
        self._root = root or sessions_dir()
        self.scanned_count = 0

    def __iter__(self) -> Iterator[SessionVisit]:
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
            yield SessionVisit(
                sid=sid,
                cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else "",
                data=data,
                messages=data.get("messages", []) if isinstance(data.get("messages"), list) else [],
                events_by_msg_id=event_rows_by_msg_id_with_orphans(data, sid),
            )
            self._state[key] = {"mtime": current_mtime}

    def mine(self, *consumers: Callable[[SessionVisit], None]) -> int:
        """One pass fanning each changed session out to every consumer.

        Returns ``scanned_count``. Each session is parsed once regardless of how
        many consumers are registered.
        """
        for visit in self:
            for consumer in consumers:
                consumer(visit)
        return self.scanned_count

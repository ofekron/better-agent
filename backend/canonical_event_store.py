from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from canonical_event import CanonicalFact, CommittedFact, SourceOrder, canonical_json


class CanonicalStoreError(RuntimeError):
    pass


class SourceConflictError(CanonicalStoreError):
    pass


@dataclass(frozen=True)
class CommitAck:
    committed: bool
    duplicate: bool
    canonical_seq: int
    acceptance_ticket: int


@dataclass(frozen=True)
class BarrierAck:
    committed_ticket: int
    canonical_through_seq: int


@dataclass
class _Request:
    kind: str
    root_id: str
    ticket: int
    payload: Any
    future: Future


class CanonicalEventStore:
    def __init__(self, path: Path, *, queue_capacity: int = 4096) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._capacity = queue_capacity
        self._condition = threading.Condition()
        self._queues: dict[str, queue.deque[_Request]] = {}
        self._ready: queue.deque[str] = queue.deque()
        self._ready_set: set[str] = set()
        self._tickets: dict[str, int] = {}
        self._pending = 0
        self._accepting = True
        self._thread = threading.Thread(target=self._run, name="canonical-event-writer", daemon=True)
        self._thread.start()

    def submit(self, fact: CanonicalFact, *, timeout: float | None = 30.0) -> CommitAck:
        request = self._accept("write", fact.root_id, fact, timeout)
        return request.future.result(timeout=timeout)

    def barrier(self, root_id: str, *, timeout: float | None = 30.0) -> BarrierAck:
        request = self._accept("barrier", root_id, None, timeout)
        return request.future.result(timeout=timeout)

    def read(self, root_id: str, *, after_seq: int = 0, limit: int = 10_000) -> list[CommittedFact]:
        request = self._accept("read", root_id, (after_seq, limit), 30.0)
        return request.future.result(timeout=30.0)

    def _accept(self, kind: str, root_id: str, payload: Any, timeout: float | None) -> _Request:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._pending >= self._capacity and self._accepting:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("canonical event queue capacity exhausted")
                self._condition.wait(remaining)
            if not self._accepting:
                raise CanonicalStoreError("canonical event store is closed")
            ticket = self._tickets.get(root_id, 0) + 1
            self._tickets[root_id] = ticket
            request = _Request(kind, root_id, ticket, payload, Future())
            root_queue = self._queues.setdefault(root_id, queue.deque())
            root_queue.append(request)
            self._pending += 1
            if root_id not in self._ready_set:
                self._ready.append(root_id)
                self._ready_set.add(root_id)
            self._condition.notify_all()
            return request

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS canonical_facts (
              root_id TEXT NOT NULL,
              canonical_seq INTEGER NOT NULL,
              acceptance_ticket INTEGER NOT NULL,
              schema_version INTEGER NOT NULL,
              fact_id TEXT NOT NULL,
              sid TEXT NOT NULL,
              source TEXT NOT NULL,
              source_stream_id TEXT NOT NULL,
              source_event_id TEXT NOT NULL,
              source_generation INTEGER NOT NULL,
              source_sequence INTEGER NOT NULL,
              payload_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              update_semantics TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              run_id TEXT,
              turn_id TEXT,
              correction_of TEXT,
              PRIMARY KEY (root_id, canonical_seq),
              UNIQUE (root_id, fact_id),
              UNIQUE (root_id, source_stream_id, source_event_id, source_generation, source_sequence)
            );
            CREATE TABLE IF NOT EXISTS root_heads (
              root_id TEXT PRIMARY KEY,
              canonical_seq INTEGER NOT NULL,
              committed_ticket INTEGER NOT NULL
            );
        """)

    def _run(self) -> None:
        connection = sqlite3.connect(self._path)
        self._schema(connection)
        try:
            while True:
                with self._condition:
                    while not self._ready and (self._accepting or self._pending):
                        self._condition.wait()
                    if not self._ready and not self._accepting:
                        return
                    root_id = self._ready.popleft()
                    self._ready_set.remove(root_id)
                    request = self._queues[root_id].popleft()
                    if self._queues[root_id]:
                        self._ready.append(root_id)
                        self._ready_set.add(root_id)
                    else:
                        self._queues.pop(root_id, None)
                try:
                    result = self._execute(connection, request)
                except BaseException as exc:
                    request.future.set_exception(exc)
                else:
                    request.future.set_result(result)
                finally:
                    with self._condition:
                        self._pending -= 1
                        self._condition.notify_all()
        finally:
            connection.close()

    def _execute(self, connection: sqlite3.Connection, request: _Request) -> Any:
        if request.kind == "write":
            return self._write(connection, request.ticket, request.payload)
        if request.kind == "barrier":
            row = connection.execute(
                "SELECT canonical_seq, committed_ticket FROM root_heads WHERE root_id=?",
                (request.root_id,),
            ).fetchone()
            return BarrierAck(
                committed_ticket=max(request.ticket - 1, int(row[1]) if row else 0),
                canonical_through_seq=int(row[0]) if row else 0,
            )
        if request.kind == "read":
            after_seq, limit = request.payload
            rows = connection.execute(
                "SELECT canonical_seq, acceptance_ticket, schema_version, fact_id, sid, source, source_stream_id, source_event_id, source_generation, source_sequence, payload_type, payload_json, update_semantics, content_hash, observed_at, run_id, turn_id, correction_of FROM canonical_facts WHERE root_id=? AND canonical_seq>? ORDER BY canonical_seq LIMIT ?",
                (request.root_id, after_seq, limit),
            ).fetchall()
            return [self._decode(request.root_id, row) for row in rows]
        raise CanonicalStoreError(f"unsupported request kind {request.kind}")

    def _write(self, connection: sqlite3.Connection, ticket: int, fact: CanonicalFact) -> CommitAck:
        existing = connection.execute(
            "SELECT canonical_seq, acceptance_ticket, content_hash FROM canonical_facts WHERE root_id=? AND source_stream_id=? AND source_event_id=? AND source_generation=? AND source_sequence=?",
            (fact.root_id, fact.source_stream_id, fact.source_event_id, fact.source_order.generation, fact.source_order.sequence),
        ).fetchone()
        if existing:
            if existing[2] != fact.content_hash:
                raise SourceConflictError("same source order carried different content")
            return CommitAck(True, True, int(existing[0]), ticket)
        head = connection.execute(
            "SELECT canonical_seq FROM root_heads WHERE root_id=?", (fact.root_id,),
        ).fetchone()
        canonical_seq = (int(head[0]) if head else 0) + 1
        with connection:
            connection.execute(
                "INSERT INTO canonical_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fact.root_id, canonical_seq, ticket, fact.schema_version, fact.fact_id, fact.sid, fact.source, fact.source_stream_id, fact.source_event_id, fact.source_order.generation, fact.source_order.sequence, fact.payload_type, canonical_json(fact.payload), fact.update_semantics, fact.content_hash, fact.observed_at, fact.run_id, fact.turn_id, fact.correction_of),
            )
            connection.execute(
                "INSERT INTO root_heads(root_id, canonical_seq, committed_ticket) VALUES(?,?,?) ON CONFLICT(root_id) DO UPDATE SET canonical_seq=excluded.canonical_seq, committed_ticket=excluded.committed_ticket",
                (fact.root_id, canonical_seq, ticket),
            )
        return CommitAck(True, False, canonical_seq, ticket)

    @staticmethod
    def _decode(root_id: str, row: tuple) -> CommittedFact:
        fact = CanonicalFact(
            schema_version=row[2], fact_id=row[3], root_id=root_id, sid=row[4], source=row[5],
            source_stream_id=row[6], source_event_id=row[7], source_order=SourceOrder(row[9], row[8]),
            payload_type=row[10], payload=json.loads(row[11]), update_semantics=row[12],
            content_hash=row[13], observed_at=row[14], run_id=row[15], turn_id=row[16], correction_of=row[17],
        )
        return CommittedFact(canonical_seq=row[0], acceptance_ticket=row[1], fact=fact)

    def close(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()
        self._thread.join()


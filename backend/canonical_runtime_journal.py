from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from canonical_event import CommittedFact
from canonical_event_adapter import (
    canonical_facts_from_rows,
    canonical_message_facts,
    fact_to_wire,
)
from canonical_event_authority import AuthorityError, RuntimeAuthorityCatalog
from canonical_event_store import CanonicalEventStore
from paths import ba_home


class CanonicalRuntimeJournal:
    def __init__(self, catalog_path: Path | None = None) -> None:
        self._catalog = RuntimeAuthorityCatalog(
            catalog_path or ba_home() / "runtime" / "canonical-authority-v2.sqlite"
        )
        self._store_instance: CanonicalEventStore | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _database_path() -> Path:
        return ba_home() / "runtime" / "canonical-events-v2.sqlite"

    def _store(self) -> CanonicalEventStore:
        with self._lock:
            if self._store_instance is None:
                self._store_instance = CanonicalEventStore(self._database_path())
            return self._store_instance

    @staticmethod
    def _message_head(session: dict[str, Any], default: int) -> int:
        for message in reversed(session.get("messages") or []):
            if not isinstance(message, dict):
                continue
            seq = message.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                return max(default, seq)
        return default

    def is_authoritative(self, root_id: str) -> bool:
        authority = self._catalog.current(root_id)
        return authority is not None and authority.authority == "sqlite"

    def current_authority(self, root_id: str):
        return self._catalog.current(root_id)

    def mirror_event(
        self,
        *,
        root_id: str,
        sid: str,
        seq: int,
        event_type: str,
        data: dict[str, Any],
        source: str,
        msg_id: str | None,
        event_id: str | None,
        turn_id: str | None,
    ) -> None:
        authority = self._catalog.current(root_id)
        if authority is None or authority.authority != "sqlite":
            return
        if seq <= authority.journal_through_seq:
            return
        if seq != authority.journal_through_seq + 1:
            raise AuthorityError("canonical journal coverage gap")
        payload = dict(data)
        if event_id and not payload.get("uuid"):
            payload["uuid"] = event_id
        rows = [{
            "root_id": root_id,
            "root_generation": authority.root_generation,
            "sid": sid,
            "seq": seq,
            "type": event_type,
            "data": payload,
            "source": source,
            "msg_id": msg_id,
            "turn_id": turn_id,
        }]
        facts = canonical_facts_from_rows(rows)
        store = self._store()
        if facts:
            store.submit_many(facts)
        head = store.barrier(root_id, authority.root_generation).canonical_through_seq
        self._catalog.advance_coverage(
            root_id, authority.root_generation,
            canonical_through_seq=head, journal_through_seq=seq,
            message_through_seq=authority.message_through_seq,
        )

    def ensure_cutover(
        self,
        root_id: str,
        *,
        rows: list[dict[str, Any]],
        session: dict[str, Any],
    ) -> int:
        authority = self._catalog.create(root_id)
        if authority.authority == "deleting":
            raise AuthorityError("root deletion is pending")
        generation = authority.root_generation
        gap_rows = self._validated_gap_rows(rows, authority.journal_through_seq)
        normalized_rows = [
            {**row, "root_id": root_id, "root_generation": generation}
            for row in gap_rows
        ]
        message_through_seq = self._message_head(session, authority.message_through_seq)
        if (authority.authority == "sqlite" and not normalized_rows
                and message_through_seq <= authority.message_through_seq):
            return generation
        message_facts = canonical_message_facts(
            root_id,
            {**session, "generation": generation},
            after_seq=authority.message_through_seq,
        )
        facts = [
            *message_facts,
            *canonical_facts_from_rows(normalized_rows),
        ]
        store = self._store()
        if facts:
            store.submit_many(facts)
        barrier = store.barrier(root_id, generation)
        journal_through_seq = max(
            (int(row.get("seq") or 0) for row in rows), default=authority.journal_through_seq,
        )
        database_path = self._database_path()
        if authority.authority == "jsonl":
            persisted_ids: set[str] = set()
            after_seq = 0
            while True:
                page = store.read(root_id, generation, after_seq=after_seq, limit=5_000)
                persisted_ids.update(row.fact.fact_id for row in page)
                if len(page) < 5_000:
                    break
                after_seq = page[-1].canonical_seq
            if not {fact.fact_id for fact in facts}.issubset(persisted_ids):
                raise AuthorityError("canonical import parity check failed")
            self._fsync_database(database_path)
            self._catalog.commit_sqlite_cutover(
                root_id, generation, database_path=database_path,
                canonical_through_seq=barrier.canonical_through_seq,
                journal_through_seq=journal_through_seq,
                message_through_seq=message_through_seq,
            )
        else:
            self._catalog.advance_coverage(
                root_id, generation,
                canonical_through_seq=barrier.canonical_through_seq,
                journal_through_seq=journal_through_seq,
                message_through_seq=message_through_seq,
            )
        return generation

    @staticmethod
    def _validated_gap_rows(
        rows: list[dict[str, Any]],
        journal_through_seq: int,
    ) -> list[dict[str, Any]]:
        tail: list[tuple[int, dict[str, Any]]] = []
        seen: set[int] = set()
        for row in rows:
            seq = row.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                raise AuthorityError("journal reconciliation contains malformed sequence")
            if seq <= journal_through_seq:
                continue
            if seq in seen:
                raise AuthorityError("journal reconciliation contains duplicate sequence")
            seen.add(seq)
            tail.append((seq, row))
        tail.sort(key=lambda item: item[0])
        if not tail:
            return []
        expected = journal_through_seq + 1
        if journal_through_seq == -1 and tail[0][0] in (0, 1):
            expected = tail[0][0]
        for seq, _ in tail:
            if seq != expected:
                raise AuthorityError("journal reconciliation contains a coverage gap")
            expected += 1
        return [row for _, row in tail]

    def read_page(
        self,
        root_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> dict[str, Any]:
        authority = self._catalog.current(root_id)
        if authority is None or authority.authority != "sqlite":
            raise AuthorityError("canonical SQLite authority is not active")
        database_path = self._catalog.require_database(root_id)
        if database_path is None:
            raise AuthorityError("canonical database is unavailable")
        store = self._store()
        rows = store.read(
            root_id, authority.root_generation, after_seq=after_seq, limit=limit,
        )
        head = store.barrier(root_id, authority.root_generation).canonical_through_seq
        next_seq = rows[-1].canonical_seq if rows else after_seq
        return {
            "root_generation": authority.root_generation,
            "facts": [fact_to_wire(row.fact, row.canonical_seq) for row in rows],
            "next_seq": next_seq,
            "has_more": next_seq < head,
            "canonical_through_seq": head,
        }

    def begin_delete_root(self, root_id: str) -> int | None:
        authority = self._catalog.current(root_id)
        if authority is None:
            return None
        self._catalog.begin_delete(root_id, authority.root_generation)
        return authority.root_generation

    def finish_delete_root(self, root_id: str, generation: int | None) -> None:
        if generation is not None:
            self._catalog.finish_delete(root_id, generation)

    def abort_delete_root(self, root_id: str, generation: int | None) -> None:
        if generation is not None:
            self._catalog.abort_delete(root_id, generation)

    def resolve_pending_deletions(self, *, root_id: str | None = None) -> None:
        import session_store
        from root_lifecycle import root_lifecycle_gate

        for authority in self._catalog.deleting():
            if root_id is not None and authority.root_id != root_id:
                continue
            with root_lifecycle_gate(authority.root_id):
                current = self._catalog.current(authority.root_id)
                if current is None or current.authority != "deleting":
                    continue
                if Path(session_store.session_file_path(authority.root_id)).is_file():
                    self._catalog.abort_delete(authority.root_id, authority.root_generation)
                else:
                    self._catalog.finish_delete(authority.root_id, authority.root_generation)

    @staticmethod
    def _fsync_database(path: Path) -> None:
        for candidate in (path, Path(f"{path}-wal")):
            if not candidate.exists():
                continue
            descriptor = os.open(candidate, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def close(self) -> None:
        with self._lock:
            store = self._store_instance
            self._store_instance = None
        if store is not None:
            store.close()
        self._catalog.close()


_instance: CanonicalRuntimeJournal | None = None
_instance_home: Path | None = None
_instance_lock = threading.Lock()


def canonical_runtime_journal() -> CanonicalRuntimeJournal:
    global _instance, _instance_home
    home = ba_home()
    with _instance_lock:
        if _instance is None or _instance_home != home:
            if _instance is not None:
                _instance.close()
            _instance = CanonicalRuntimeJournal()
            _instance_home = home
        return _instance


def close_canonical_runtime_journal() -> None:
    global _instance, _instance_home
    with _instance_lock:
        instance = _instance
        _instance = None
        _instance_home = None
    if instance is not None:
        instance.close()

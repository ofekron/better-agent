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
            catalog_path or ba_home() / "runtime" / "canonical-authority-v1.sqlite"
        )
        self._stores: dict[tuple[str, int], CanonicalEventStore] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _database_path(root_id: str) -> Path:
        import session_store

        root = Path(session_store.session_file_path(root_id)).parent / root_id
        root.mkdir(parents=True, exist_ok=True)
        return root / "canonical-events-v2.sqlite"

    def _store(self, root_id: str, root_generation: int) -> CanonicalEventStore:
        key = root_id, root_generation
        with self._lock:
            store = self._stores.get(key)
            if store is None:
                store = CanonicalEventStore(self._database_path(root_id))
                self._stores[key] = store
            return store

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
        if facts:
            self._store(root_id, authority.root_generation).submit_many(facts)

    def ensure_cutover(
        self,
        root_id: str,
        *,
        rows: list[dict[str, Any]],
        session: dict[str, Any],
    ) -> int:
        authority = self._catalog.create(root_id)
        if authority.authority == "sqlite":
            return authority.root_generation
        generation = authority.root_generation
        normalized_rows = [
            {**row, "root_id": root_id, "root_generation": generation}
            for row in rows
        ]
        facts = [
            *canonical_message_facts(root_id, {**session, "generation": generation}),
            *canonical_facts_from_rows(normalized_rows),
        ]
        store = self._store(root_id, generation)
        if facts:
            store.submit_many(facts)
        barrier = store.barrier(root_id, generation)
        persisted_ids = {
            row.fact.fact_id
            for row in store.read(root_id, generation, limit=max(10_000, len(facts) + 1))
        }
        expected_ids = {fact.fact_id for fact in facts}
        if not expected_ids.issubset(persisted_ids):
            raise AuthorityError("canonical import parity check failed")
        database_path = self._database_path(root_id)
        self._fsync_database(database_path)
        self._catalog.commit_sqlite_cutover(
            root_id,
            generation,
            database_path=database_path,
            canonical_through_seq=barrier.canonical_through_seq,
        )
        return generation

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
        store = self._store(root_id, authority.root_generation)
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
            stores = list(self._stores.values())
            self._stores.clear()
        for store in stores:
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

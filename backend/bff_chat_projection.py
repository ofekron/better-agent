from __future__ import annotations

import asyncio
import weakref
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from bff_projection_registry import ProjectionRegistry
from bff_chat_cache import CachedProjection, ChatProjectionCache
from canonical_event import CanonicalFact, CommittedFact, SourceOrder, canonical_json, content_hash, fact_from_wire
from chat_forest_projection import ChatForestProjector
from paths import ba_home

PROJECTION_SCHEMA_VERSION = 2


class ChatProjectionService:
    def __init__(
        self,
        runtime_service,
        registry: ProjectionRegistry | None = None,
        cache: ChatProjectionCache | None = None,
    ) -> None:
        self._runtime = runtime_service
        self._registry = registry or ProjectionRegistry(
            Path(ba_home()) / "bff" / "chat-projection-v1.sqlite"
        )
        self._cache = cache or ChatProjectionCache()
        self._projector = ChatForestProjector()
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def snapshot(self, session_id: str) -> dict[str, Any]:
        return await self.updates(session_id)

    async def updates(
        self,
        session_id: str,
        *,
        epoch: str | None = None,
        after_revision: int | None = None,
    ) -> dict[str, Any]:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        async with lock:
            cached = self._cache.get(session_id)
            source = await self._read_all(
                session_id, after_seq=cached.source_cursor if cached else 0,
            )
            if not source.get("found"):
                self._cache.discard(session_id)
                return {"found": False}
            generation = int(source.get("root_generation", 0))
            if cached is not None and cached.root_generation != generation:
                cached = None
                source = await self._read_all(session_id, after_seq=0)
            new_facts = [fact_from_wire(value) for value in source.get("facts") or []]
            if cached is not None and not new_facts:
                return self._response_for(cached, epoch, after_revision)
            facts = [*(cached.facts if cached else []), *new_facts]
            facts = self._attach_ownership(facts)
            forest = await asyncio.to_thread(self._projector.project, session_id, facts)
            payload = asdict(forest)
            checksum = content_hash(payload)
            state = await asyncio.to_thread(
                self._registry.publish,
                session_id,
                forest.root_generation,
                canonical_through_seq=forest.canonical_through_seq,
                checksum=checksum,
                schema_version=PROJECTION_SCHEMA_VERSION,
            )
            snapshot = {
                "found": True,
                "kind": "snapshot",
                "schema_version": PROJECTION_SCHEMA_VERSION,
                "root_generation": forest.root_generation,
                "epoch": state.epoch,
                "revision": state.revision,
                "canonical_through_seq": state.canonical_through_seq,
                "checksum": state.checksum,
                "forest": payload,
            }
            delta = self._delta(cached.snapshot, snapshot) if cached else None
            weight = len(canonical_json(snapshot).encode("utf-8")) + sum(
                len(canonical_json(row.fact.payload).encode("utf-8")) + 256 for row in facts
            )
            entry = CachedProjection(
                root_id=session_id,
                root_generation=forest.root_generation,
                source_cursor=int(source.get("next_seq") or 0),
                facts=facts,
                snapshot=snapshot,
                delta=delta,
                weight_bytes=weight,
            )
            self._cache.put(entry)
            return self._response_for(entry, epoch, after_revision)

    async def _read_all(self, session_id: str, *, after_seq: int) -> dict[str, Any]:
        facts: list[dict[str, Any]] = []
        session: dict[str, Any] | None = None
        root_generation: int | None = None
        while True:
            page = await self._runtime.projection_source(session_id, after_seq=after_seq)
            if not page.get("found"):
                return {"found": False}
            session = page.get("session") if session is None else session
            if root_generation is None:
                root_generation = int(page.get("root_generation", 0))
            elif root_generation != int(page.get("root_generation", 0)):
                raise RuntimeError("runtime projection generation changed during catch-up")
            facts.extend(page.get("facts") or [])
            next_seq = int(page.get("next_seq") or after_seq)
            if not page.get("has_more"):
                return {
                    "found": True,
                    "session": session,
                    "root_generation": root_generation,
                    "facts": facts,
                    "next_seq": next_seq,
                }
            if next_seq <= after_seq:
                raise RuntimeError("runtime projection source made no cursor progress")
            after_seq = next_seq

    @staticmethod
    def _delta(previous: dict, current: dict) -> dict:
        previous_trees = {tree["id"]: tree for tree in previous["forest"]["trees"]}
        current_trees = {tree["id"]: tree for tree in current["forest"]["trees"]}
        return {
            "found": True,
            "kind": "delta",
            "schema_version": current["schema_version"],
            "root_generation": current["root_generation"],
            "epoch": current["epoch"],
            "base_revision": previous["revision"],
            "target_revision": current["revision"],
            "canonical_through_seq": current["canonical_through_seq"],
            "checksum": current["checksum"],
            "upsert_trees": [tree for key, tree in current_trees.items() if previous_trees.get(key) != tree],
            "remove_tree_ids": [key for key in previous_trees if key not in current_trees],
        }

    @staticmethod
    def _response_for(
        entry: CachedProjection,
        epoch: str | None,
        after_revision: int | None,
    ) -> dict:
        if after_revision is None:
            return entry.snapshot
        current = entry.snapshot
        if epoch == current["epoch"] and after_revision == current["revision"]:
            return {
                "found": True, "kind": "delta", "schema_version": current["schema_version"],
                "root_generation": current["root_generation"], "epoch": current["epoch"],
                "base_revision": after_revision, "target_revision": after_revision,
                "canonical_through_seq": current["canonical_through_seq"], "checksum": current["checksum"],
                "upsert_trees": [], "remove_tree_ids": [],
            }
        delta = entry.delta
        if delta and epoch == delta["epoch"] and after_revision == delta["base_revision"]:
            return delta
        return current

    def cache_stats(self) -> dict[str, int]:
        return self._cache.stats()

    @staticmethod
    def _attach_ownership(facts: list[CommittedFact]) -> list[CommittedFact]:
        prompt_for_assistant: dict[str, str] = {}
        for row in facts:
            if row.fact.payload_type != "message_ownership_declared":
                continue
            message_id = str(row.fact.payload.get("message_id") or "")
            prompt_id = str(row.fact.payload.get("prompt_message_id") or "")
            if message_id and prompt_id:
                prompt_for_assistant[message_id] = prompt_id
        attached: list[CommittedFact] = []
        for row in facts:
            if row.fact.payload_type == "message_ownership_declared":
                continue
            message_id = str(row.fact.payload.get("message_id") or "")
            prompt_id = prompt_for_assistant.get(message_id)
            if not prompt_id:
                attached.append(row)
                continue
            attached.append(replace(row, fact=replace(
                row.fact, payload={**row.fact.payload, "prompt_message_id": prompt_id},
            )))
        return attached


_service: ChatProjectionService | None = None


def get_chat_projection_service(runtime_service) -> ChatProjectionService:
    global _service
    if _service is None:
        _service = ChatProjectionService(runtime_service)
    return _service

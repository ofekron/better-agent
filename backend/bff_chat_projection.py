from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from bff_projection_registry import ProjectionRegistry
from canonical_event import CanonicalFact, CommittedFact, SourceOrder, canonical_json, content_hash, fact_from_wire
from chat_forest_projection import ChatForestProjector
from paths import ba_home

PROJECTION_SCHEMA_VERSION = 1


class ChatProjectionService:
    def __init__(self, runtime_service, registry: ProjectionRegistry | None = None) -> None:
        self._runtime = runtime_service
        self._registry = registry or ProjectionRegistry(
            Path(ba_home()) / "bff" / "chat-projection-v1.sqlite"
        )
        self._projector = ChatForestProjector()
        self._locks: dict[str, asyncio.Lock] = {}

    async def snapshot(self, session_id: str) -> dict[str, Any]:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            source = await self._read_all(session_id)
            if not source.get("found"):
                return {"found": False}
            facts = [fact_from_wire(value) for value in source.get("facts") or []]
            facts = self._attach_ownership(facts)
            forest = await asyncio.to_thread(self._projector.project, session_id, facts)
            payload = asdict(forest)
            checksum = content_hash(payload)
            state = await asyncio.to_thread(
                self._registry.publish,
                session_id,
                canonical_through_seq=forest.canonical_through_seq,
                checksum=checksum,
                schema_version=PROJECTION_SCHEMA_VERSION,
            )
            return {
                "found": True,
                "schema_version": PROJECTION_SCHEMA_VERSION,
                "epoch": state.epoch,
                "revision": state.revision,
                "canonical_through_seq": state.canonical_through_seq,
                "checksum": state.checksum,
                "forest": payload,
            }

    async def _read_all(self, session_id: str) -> dict[str, Any]:
        after_seq = 0
        facts: list[dict[str, Any]] = []
        session: dict[str, Any] | None = None
        while True:
            page = await self._runtime.projection_source(session_id, after_seq=after_seq)
            if not page.get("found"):
                return {"found": False}
            session = page.get("session") if session is None else session
            facts.extend(page.get("facts") or [])
            next_seq = int(page.get("next_seq") or after_seq)
            if not page.get("has_more"):
                return {"found": True, "session": session, "facts": facts}
            if next_seq <= after_seq:
                raise RuntimeError("runtime projection source made no cursor progress")
            after_seq = next_seq

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

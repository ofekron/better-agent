from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

SCHEMA_VERSION = 1
CHAT_NODE_NAMESPACE = uuid.UUID("462d6b18-a46c-5f11-b19c-8e126cb610d0")
UpdateSemantics = Literal["snapshot", "final", "correction", "incomplete_snapshot", "ambiguous"]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_node_id(kind: str, *identity: str) -> str:
    encoded = b"".join(
        len(part.encode("utf-8")).to_bytes(4, "big") + part.encode("utf-8")
        for part in (f"v{SCHEMA_VERSION}", kind, *identity)
    )
    return str(uuid.uuid5(CHAT_NODE_NAMESPACE, encoded.hex()))


@dataclass(frozen=True)
class SourceOrder:
    sequence: int
    generation: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or self.sequence < 0:
            raise ValueError("source sequence must be a non-negative integer")
        if isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("source generation must be a non-negative integer")

    def key(self) -> tuple[int, int]:
        return self.generation, self.sequence


@dataclass(frozen=True)
class CanonicalFact:
    schema_version: int
    fact_id: str
    root_id: str
    sid: str
    source: str
    source_stream_id: str
    source_event_id: str
    source_order: SourceOrder
    payload_type: str
    payload: dict[str, Any]
    update_semantics: UpdateSemantics
    content_hash: str
    observed_at: str
    run_id: str | None = None
    turn_id: str | None = None
    correction_of: str | None = None

    @classmethod
    def create(
        cls,
        *,
        root_id: str,
        sid: str,
        source: str,
        source_stream_id: str,
        source_event_id: str,
        source_order: SourceOrder,
        payload_type: str,
        payload: dict[str, Any],
        update_semantics: UpdateSemantics,
        observed_at: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        correction_of: str | None = None,
    ) -> "CanonicalFact":
        required = {
            "root_id": root_id,
            "sid": sid,
            "source": source,
            "source_stream_id": source_stream_id,
            "source_event_id": source_event_id,
            "payload_type": payload_type,
        }
        if any(not isinstance(value, str) or not value for value in required.values()):
            raise ValueError("canonical fact identity fields must be non-empty strings")
        if not isinstance(payload, dict):
            raise ValueError("canonical fact payload must be an object")
        if update_semantics not in {
            "snapshot", "final", "correction", "incomplete_snapshot", "ambiguous",
        }:
            raise ValueError("unsupported update semantics")
        if update_semantics == "correction" and not correction_of:
            raise ValueError("correction facts require correction_of")
        hashed = content_hash({
            "payload_type": payload_type,
            "payload": payload,
            "update_semantics": update_semantics,
            "correction_of": correction_of,
        })
        identity = canonical_json([
            root_id, source_stream_id, source_event_id,
            source_order.generation, source_order.sequence, hashed,
        ])
        fact_id = str(uuid.uuid5(CHAT_NODE_NAMESPACE, identity))
        return cls(
            schema_version=SCHEMA_VERSION,
            fact_id=fact_id,
            root_id=root_id,
            sid=sid,
            source=source,
            source_stream_id=source_stream_id,
            source_event_id=source_event_id,
            source_order=source_order,
            payload_type=payload_type,
            payload=payload,
            update_semantics=update_semantics,
            content_hash=hashed,
            observed_at=observed_at or datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            turn_id=turn_id,
            correction_of=correction_of,
        )


@dataclass(frozen=True)
class CommittedFact:
    canonical_seq: int
    acceptance_ticket: int
    fact: CanonicalFact


def fact_from_wire(value: dict[str, Any]) -> CommittedFact:
    order = value.get("source_order")
    if not isinstance(order, dict):
        raise ValueError("canonical fact source_order must be an object")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("canonical fact payload must be an object")
    fact = CanonicalFact(
        schema_version=int(value["schema_version"]),
        fact_id=str(value["fact_id"]),
        root_id=str(value["root_id"]),
        sid=str(value["sid"]),
        source=str(value["source"]),
        source_stream_id=str(value["source_stream_id"]),
        source_event_id=str(value["source_event_id"]),
        source_order=SourceOrder(sequence=int(order["sequence"]), generation=int(order.get("generation", 0))),
        payload_type=str(value["payload_type"]),
        payload=payload,
        update_semantics=value["update_semantics"],
        content_hash=str(value["content_hash"]),
        observed_at=str(value.get("observed_at") or ""),
        run_id=value.get("run_id"),
        turn_id=value.get("turn_id"),
        correction_of=value.get("correction_of"),
    )
    return CommittedFact(
        canonical_seq=int(value["canonical_seq"]),
        acceptance_ticket=int(value.get("acceptance_ticket", value["canonical_seq"])),
        fact=fact,
    )

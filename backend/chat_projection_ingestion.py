"""Admission of canonical feed facts into the chat projection store.

Owned by the BFF process: its chat feed client pulls wire-shaped
canonical facts (`canonical_event_adapter.fact_to_wire`) from the
runtime's `projection-source` endpoint and admits each one here.
Idempotent on (source_event_id, content_hash) via the source catalog,
so re-delivery after reconnect or cursor replay is a no-op.
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from canonical_event_adapter import subagent_scope_source_event_id
from chat_projection_service import CanonicalChatProjectionService
from chat_projection_source_catalog import ChatProjectionSourceCatalog
from chat_projection_store import ProjectionCommit, SourceWatermark, TurnManifest
from chat_projection_store_sqlite import canonical_json
from paths import ba_home


_lock = threading.Lock()
_service: CanonicalChatProjectionService | None = None
_catalog: ChatProjectionSourceCatalog | None = None
_home: Path | None = None

_PROVIDER_KINDS = {"claude", "codex", "gemini"}
_METADATA_TYPES = {"ai-title", "file-history-snapshot"}
_CONTENT_HASH_ALPHABET = set("0123456789abcdef")


def _instances() -> tuple[CanonicalChatProjectionService, ChatProjectionSourceCatalog]:
    global _service, _catalog, _home
    home = ba_home()
    with _lock:
        if _home != home:
            if _service is not None:
                _service.close()
            if _catalog is not None:
                _catalog.close()
            _service = CanonicalChatProjectionService()
            _catalog = ChatProjectionSourceCatalog()
            _home = home
        assert _service is not None and _catalog is not None
        return _service, _catalog


def _required_text(fact: dict[str, Any], key: str) -> str:
    value = fact.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"canonical fact field {key!r} must be a non-empty string")
    return value


def admit_canonical_fact(fact: dict[str, Any], *, provider: str) -> None:
    """Admit one wire-shaped canonical fact into the projection store."""
    if provider not in _PROVIDER_KINDS:
        raise ValueError("canonical provider identity is invalid")
    if not isinstance(fact, dict):
        raise ValueError("canonical fact must be an object")
    root_id = _required_text(fact, "root_id")
    payload_type = _required_text(fact, "payload_type")
    stream_id = _required_text(fact, "source_stream_id")
    event_id = _required_text(fact, "source_event_id")
    content_hash = _required_text(fact, "content_hash")
    if len(content_hash) != 64 or not set(content_hash) <= _CONTENT_HASH_ALPHABET:
        raise ValueError("canonical fact content_hash must be 64 lowercase hex chars")
    payload = fact.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("canonical fact payload must be an object")
    raw_message_id = payload.get("message_id")
    message_id = raw_message_id if isinstance(raw_message_id, str) and raw_message_id else None
    raw_parent_tool_use_id = payload.get("parent_tool_use_id")
    # Derived purely from this fact's own payload via the shared
    # tool_use_id -> source_event_id naming convention (no cross-fact
    # lookup): matches the parent_event_id chat_canonical_adapter resolves
    # for the same sidechain event at render time.
    parent_event_id = (
        subagent_scope_source_event_id(raw_parent_tool_use_id)
        if isinstance(raw_parent_tool_use_id, str) and raw_parent_tool_use_id
        else None
    )

    # The store defines commit identity locally: the persisted fact must
    # carry `event_id`, and the commit's content_hash is the hash of the
    # persisted fact itself (not the upstream fact's own hash field).
    stored_fact = {**fact, "provider": provider, "event_id": event_id}
    commit_hash = hashlib.sha256(
        canonical_json(stored_fact).encode("utf-8")
    ).hexdigest()
    service, catalog = _instances()
    root_generation = catalog.root_generation(root_id)
    identity = catalog.admit(
        root_id=root_id, provider=provider, stream_id=stream_id,
        event_id=event_id, content_hash=commit_hash,
    )
    authority = service.register(
        provider=provider, session_id=root_id, root_id=root_id,
        root_generation=root_generation, store_kind="jsonl",
    )
    # Replay tolerance: the catalog matched this exact (event, content)
    # to an existing stream position; if the store already durably holds
    # that position, the commit happened — skip. (A position admitted but
    # never committed, e.g. crash between admit and append, falls through
    # and commits now.)
    durable = service.source_watermark(authority, identity.stream_id)
    if durable is not None and (
        (durable.generation, durable.sequence)
        >= (identity.generation, identity.sequence)
    ):
        return
    nested_type = payload.get("type")
    is_metadata = payload_type in _METADATA_TYPES or (
        isinstance(nested_type, str) and nested_type in _METADATA_TYPES
    )
    owner_scope = "metadata" if is_metadata else (
        f"message:{message_id}" if message_id else "root"
    )
    raw_turn_id = fact.get("turn_id")
    turn_id = raw_turn_id if isinstance(raw_turn_id, str) and raw_turn_id else None
    resolved_turn = turn_id or message_id or f"root:{root_id}"
    service.append_apply(authority, ProjectionCommit(
        root_id=root_id,
        root_generation=root_generation,
        event_id=event_id,
        content_hash=commit_hash,
        canonical_fact=stored_fact,
        render_node={"type": payload_type, "data": payload},
        turn_id=resolved_turn,
        message_id=message_id,
        parent_event_id=parent_event_id,
        owner_scope=owner_scope,
        manifest=TurnManifest(resolved_turn, 1, 0 if owner_scope == "metadata" else 1),
        visible_delta={"event_id": event_id, "type": payload_type},
        historical_revision={"event_id": event_id, "content_hash": commit_hash},
        watermark=SourceWatermark(identity.stream_id, identity.generation, identity.sequence),
    ))


def close() -> None:
    global _service, _catalog, _home
    with _lock:
        service, catalog = _service, _catalog
        _service = None
        _catalog = None
        _home = None
    if service is not None:
        service.close()
    if catalog is not None:
        catalog.close()

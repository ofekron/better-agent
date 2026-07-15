from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from chat_projection_service import CanonicalChatProjectionService
from chat_projection_source_catalog import ChatProjectionSourceCatalog
from chat_projection_store import ProjectionCommit, SourceWatermark, TurnManifest
from chat_projection_store_sqlite import canonical_json
from paths import ba_home


_lock = threading.Lock()
_service: CanonicalChatProjectionService | None = None
_catalog: ChatProjectionSourceCatalog | None = None
_home: Path | None = None


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


def _provider_event_id(data: dict[str, Any]) -> str | None:
    for candidate in (data, data.get("data"), data.get("event")):
        if not isinstance(candidate, dict):
            continue
        nested = candidate.get("data")
        for owner in (candidate, nested if isinstance(nested, dict) else None):
            if not isinstance(owner, dict):
                continue
            value = owner.get("uuid") or owner.get("id") or owner.get("event_id")
            if isinstance(value, str) and value:
                return value
    return None


def _provider_kind(root_id: str, run_id: str | None) -> str:
    if isinstance(run_id, str) and run_id and "/" not in run_id and "\\" not in run_id:
        try:
            from active_run_catalog import read_relative

            raw = read_relative(ba_home() / "runs", run_id, "backend_state.json")
            state = json.loads(raw.decode("utf-8"))
            kind = state.get("provider_kind") if isinstance(state, dict) else None
            if kind in {"claude", "codex", "gemini"}:
                return kind
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            pass
    raise ValueError("canonical provider identity is unavailable")


def admit_provider_event(
    *, root_id: str, session_id: str, event_type: str, data: dict[str, Any],
    source: str, run_id: str | None, message_id: str | None,
    turn_id: str | None, provider: str | None = None,
) -> None:
    service, catalog = _instances()
    provider = provider or _provider_kind(root_id, run_id)
    if provider not in {"claude", "codex", "gemini"}:
        raise ValueError("canonical provider identity is invalid")
    event_id = _provider_event_id(data)
    if event_id is None:
        event_id = "event-" + hashlib.sha256(canonical_json({
            "type": event_type, "data": data, "source": source,
            "run_id": run_id, "message_id": message_id,
        }).encode("utf-8")).hexdigest()
    stream_id = run_id or f"{provider}:{source}:{session_id}"
    root_generation = catalog.root_generation(root_id)
    canonical_fact = {
        "event_id": event_id,
        "provider": provider,
        "source": source,
        "source_stream": stream_id,
        "type": event_type,
        "data": data,
        "session_id": session_id,
        "message_id": message_id,
        "turn_id": turn_id,
    }
    content_hash = hashlib.sha256(canonical_json(canonical_fact).encode("utf-8")).hexdigest()
    identity = catalog.admit(
        root_id=root_id, provider=provider, stream_id=stream_id,
        event_id=event_id, content_hash=content_hash,
    )
    authority = service.register(
        provider=provider, session_id=root_id, root_id=root_id,
        root_generation=root_generation, store_kind="jsonl",
    )
    metadata_type = data.get("type") if isinstance(data, dict) else None
    owner_scope = "metadata" if metadata_type in {"ai-title", "file-history-snapshot"} else (
        f"message:{message_id}" if message_id else "root"
    )
    resolved_turn = turn_id or message_id or f"root:{root_id}"
    service.append_apply(authority, ProjectionCommit(
        root_id=root_id,
        root_generation=root_generation,
        event_id=event_id,
        content_hash=content_hash,
        canonical_fact=canonical_fact,
        render_node={"type": event_type, "data": data},
        turn_id=resolved_turn,
        message_id=message_id,
        parent_event_id=None,
        owner_scope=owner_scope,
        manifest=TurnManifest(resolved_turn, 1, 0 if owner_scope == "metadata" else 1),
        visible_delta={"event_id": event_id, "type": event_type},
        historical_revision={"event_id": event_id, "content_hash": content_hash},
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

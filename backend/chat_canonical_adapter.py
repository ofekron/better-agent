"""Bridge: stored canonical feed facts → `chat_projector.project_chat` inputs.

The BFF's chat rendering cache stores wire-shaped canonical facts
(`canonical_event_adapter.fact_to_wire`). The projector demands the
closed chat-model vocabulary (`chat_projector._validate_nested_data`)
with a full per-event provider identity. This module adapts one root's
facts plus the runtime session snapshot into `(messages, events)` for
`project_chat`, resolving provider identity by joining assistant
messages' `run_meta` and failing closed when identity is unresolvable.

Facts that cannot satisfy the closed vocabulary (e.g. a model change
missing its target effort) are not silently coerced: they are returned
as typed drops so callers can surface them.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class ChatAdapterError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class AdaptedChatInputs:
    messages: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    dropped: tuple[dict[str, str], ...]


_TEXT_KEYS = ("text", "content", "message")
_METADATA_TYPES = {
    "ai-title": ("ai_title", "title"),
    "file-history-snapshot": ("file_history_snapshot", "snapshot_id"),
}
_TURN_MARKERS = {"turn_start": "turn_started", "turn_complete": "turn_completed"}
_IDENTITY_FIELDS = ("provider", "model", "effort")


def _drop_shadow_provider_stream_facts(
    facts: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    authoritative_event_ids = {
        str(fact.get("source_event_id") or fact.get("fact_id") or "")
        for fact in facts
        if fact.get("source") != "provider_stream"
    }
    return tuple(
        fact for fact in facts
        if (
            fact.get("source") != "provider_stream"
            or str(fact.get("source_event_id") or fact.get("fact_id") or "")
            not in authoritative_event_ids
        )
    )


def _payload_message_id(fact: Mapping[str, Any]) -> str | None:
    payload = fact.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    value = payload.get("message_id")
    return value if isinstance(value, str) and value else None


def _projected_event_ids(
    facts: Sequence[Mapping[str, Any]],
) -> dict[int, str]:
    identities_by_base: dict[str, set[tuple[str | None, str]]] = {}
    for fact in facts:
        base = str(fact.get("source_event_id") or fact.get("fact_id") or "")
        identities_by_base.setdefault(base, set()).add((
            _payload_message_id(fact),
            str(fact.get("payload_type") or ""),
        ))
    projected: dict[int, str] = {}
    for fact in facts:
        base = str(fact.get("source_event_id") or fact.get("fact_id") or "")
        if len(identities_by_base.get(base, ())) <= 1:
            projected[id(fact)] = base
            continue
        projected[id(fact)] = str(fact.get("fact_id") or base)
    return projected


def _json_safe(value: Any) -> Any:
    """Recursively round floats to int (canonical facts allow no float —
    `chat_projector._measure_json` rejects it), matching the codebase-wide
    `duration_ms`-as-int convention. Raw upstream payloads (tool args,
    worker/todo passthrough) are otherwise untyped, so this is the
    boundary that keeps the strict projector contract safe."""
    if isinstance(value, float):
        return round(value) if math.isfinite(value) else value
    if isinstance(value, Mapping):
        return {key: _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return value


def _text_of(payload: Mapping[str, Any]) -> str:
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _tool_result_output(payload: Mapping[str, Any]) -> str:
    for key in ("output", *_TEXT_KEYS):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        if value is not None:
            return json.dumps(value, ensure_ascii=False, default=str)
    return ""


def _normalize_timestamp(value: Any, fallback: str) -> str:
    if not isinstance(value, str) or not value:
        return fallback
    if value.endswith("Z"):
        return value
    if value.endswith("+00:00"):
        return value[: -len("+00:00")] + "Z"
    return fallback


def _run_identity(meta: Mapping[str, Any] | None, session: Mapping[str, Any]) -> dict[str, str] | None:
    provider = None
    model = None
    effort = None
    if isinstance(meta, Mapping):
        provider = meta.get("provider_id")
        model = meta.get("model")
        effort = meta.get("reasoning_effort")
    provider = provider or session.get("provider_id")
    model = model or session.get("model")
    effort = effort or session.get("reasoning_effort")
    if not all(isinstance(value, str) and value for value in (provider, model, effort)):
        return None
    return {"id": provider, "model": model, "effort": effort}


def adapt_chat_inputs(
    facts: Sequence[Mapping[str, Any]],
    session: Mapping[str, Any],
) -> AdaptedChatInputs:
    """Adapt one root's wire facts + session snapshot for project_chat."""
    if not isinstance(session, Mapping):
        raise ChatAdapterError("invalid_session", "session snapshot must be an object")
    root_id = session.get("id")
    if not isinstance(root_id, str) or not root_id:
        raise ChatAdapterError("invalid_session", "session id is required")

    run_meta_by_message: dict[str, Mapping[str, Any]] = {}
    for message in session.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        message_id = message.get("id")
        meta = message.get("run_meta")
        if isinstance(message_id, str) and message_id and isinstance(meta, Mapping):
            run_meta_by_message[message_id] = meta

    ordered = _drop_shadow_provider_stream_facts(
        tuple(sorted(
            (fact for fact in facts if isinstance(fact, Mapping)),
            key=lambda fact: int(fact.get("canonical_seq") or 0),
        ))
    )
    projected_event_ids = _projected_event_ids(ordered)

    messages: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    versions: dict[str, int] = {}
    known_turns: dict[str, str] = {}
    tool_calls: dict[str, dict[str, Any]] = {}

    def identity_for(message_id: str | None, fact_id: str) -> dict[str, str] | None:
        meta = run_meta_by_message.get(message_id or "")
        identity = _run_identity(meta, session)
        if identity is None:
            dropped.append({
                "fact_id": fact_id,
                "code": "provider_identity_unresolvable",
            })
        return identity

    def emit(
        fact: Mapping[str, Any], event_type: str, data: dict[str, Any], *,
        message_id: str | None, provider_final: bool = False,
        metadata_only: bool = False, provider: dict[str, str] | None = None,
        event_id: str | None = None,
    ) -> None:
        fact_id = str(fact.get("fact_id") or fact.get("source_event_id") or "")
        resolved = provider or identity_for(message_id, fact_id)
        if resolved is None:
            return
        seq = int(fact.get("canonical_seq") or 0)
        if seq < 1:
            dropped.append({"fact_id": fact_id, "code": "missing_canonical_seq"})
            return
        eid = event_id or projected_event_ids.get(id(fact), fact_id)
        versions[eid] = versions.get(eid, 0) + 1
        events.append({
            "event_id": eid,
            "timestamp": _normalize_timestamp(
                fact.get("source_timestamp"),
                _normalize_timestamp(fact.get("observed_at"), "1970-01-01T00:00:00Z"),
            ),
            "journal_seq": seq,
            "content_version": versions[eid],
            "context_id": str(fact.get("sid") or root_id),
            "turn_id": None,
            "message_id": message_id,
            "parent_event_id": None,
            "type": event_type,
            "data": data,
            "provider": resolved,
            "provider_final": provider_final,
            "metadata_only": metadata_only,
        })

    for fact in ordered:
        payload = fact.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        payload_type = str(fact.get("payload_type") or "")
        raw_message_id = payload.get("message_id")
        message_id = raw_message_id if isinstance(raw_message_id, str) and raw_message_id else None
        fact_id = str(fact.get("fact_id") or fact.get("source_event_id") or "")
        seq = int(fact.get("canonical_seq") or 0)

        if payload_type == "user_prompt":
            if not message_id or seq < 1:
                dropped.append({"fact_id": fact_id, "code": "invalid_user_prompt"})
                continue
            known_turns[message_id] = message_id
            messages.append({
                "id": message_id, "turn_id": message_id, "seq": seq,
                "role": "user", "content": str(payload.get("text") or ""),
            })
            continue

        if payload_type == "message_ownership_declared":
            prompt_id = payload.get("prompt_message_id")
            if not message_id or not isinstance(prompt_id, str) or not prompt_id or seq < 1:
                dropped.append({"fact_id": fact_id, "code": "invalid_ownership"})
                continue
            turn_id = known_turns.get(prompt_id, prompt_id)
            messages.append({
                "id": message_id, "turn_id": turn_id, "seq": seq,
                "role": "assistant", "content": "",
            })
            continue

        if payload_type == "assistant_output":
            emit(
                fact, "assistant_text", {"text": str(payload.get("text") or "")},
                message_id=message_id,
                provider_final=payload.get("final") is True,
            )
            continue

        if payload_type == "tool_call":
            tool_use_id = payload.get("tool_use_id")
            key = tool_use_id if isinstance(tool_use_id, str) and tool_use_id else fact_id
            tool_name = str(payload.get("tool") or "tool")
            tool_calls[key] = {
                "message_id": message_id,
                "tool_name": tool_name,
            }
            call_data: dict[str, Any] = {
                "tool_name": tool_name, "tool_use_id": key, "status": "running",
            }
            args = payload.get("args")
            if args is not None:
                call_data["args"] = _json_safe(args)
            emit(fact, "tool_interaction", call_data, message_id=message_id)
            continue

        if payload_type == "tool_result":
            tool_use_id = payload.get("tool_use_id")
            key = tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None
            if key is not None and key in tool_calls:
                call = tool_calls[key]
                emit(
                    fact, "tool_interaction",
                    {
                        "tool_name": call["tool_name"],
                        "tool_use_id": key,
                        "status": "complete",
                        "output": _tool_result_output(payload),
                    },
                    message_id=message_id or call["message_id"],
                )
            else:
                dropped.append({"fact_id": fact_id, "code": "unmatched_tool_result"})
            continue

        if payload_type == "steer_prompt":
            emit(fact, "steering_message", {"text": _text_of(payload)}, message_id=message_id)
            continue

        if payload_type == "thinking":
            status = payload.get("status")
            emit(
                fact, "thinking",
                {"text": _text_of(payload),
                 "status": status if isinstance(status, str) and status else "complete"},
                message_id=message_id,
            )
            continue

        if payload_type == "model_switched":
            target = _run_identity({
                "provider_id": payload.get("provider_id"),
                "model": payload.get("model"),
                "reasoning_effort": payload.get("reasoning_effort"),
            }, {})
            if target is None:
                dropped.append({"fact_id": fact_id, "code": "model_change_identity_incomplete"})
                continue
            origin = _run_identity({
                "provider_id": payload.get("previous_provider_id"),
                "model": payload.get("previous_model"),
                "reasoning_effort": payload.get("previous_reasoning_effort"),
            }, {})
            identity = {"provider": target["id"], "model": target["model"], "effort": target["effort"]}
            emit(
                fact, "model_change",
                {"from": {"provider": origin["id"], "model": origin["model"], "effort": origin["effort"]} if origin else None,
                 "to": identity},
                message_id=message_id,
                provider={"id": target["id"], "model": target["model"], "effort": target["effort"]},
            )
            continue

        if payload_type in _METADATA_TYPES:
            canonical_type, field = _METADATA_TYPES[payload_type]
            value = payload.get(field) or _text_of(payload)
            if not isinstance(value, str) or not value:
                dropped.append({"fact_id": fact_id, "code": "invalid_metadata_payload"})
                continue
            emit(fact, canonical_type, {field: value}, message_id=message_id, metadata_only=True)
            continue

        if payload_type in _TURN_MARKERS:
            emit(fact, _TURN_MARKERS[payload_type], {}, message_id=message_id, metadata_only=True)
            continue

        block_type = payload.get("block_type")
        label = (
            f"unsupported block: {block_type}"
            if payload_type == "unsupported_block" and isinstance(block_type, str)
            else payload_type or "unknown"
        )
        # Generic passthrough for payload types with no dedicated canonical
        # shape (worker_start/_event/_complete, todos_snapshot, unsupported
        # blocks, ...): carry the full raw payload so the BFF chat-tree
        # lookup sidecar can serve it, instead of reducing it to kind+label.
        emit(
            fact, "other_typed_work",
            {"kind": payload_type or "unknown", "label": label, "payload": _json_safe(dict(payload))},
            message_id=message_id,
        )

    events.sort(key=lambda event: event["journal_seq"])
    return AdaptedChatInputs(tuple(messages), tuple(events), tuple(dropped))

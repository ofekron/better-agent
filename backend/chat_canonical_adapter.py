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


def _text_of(payload: Mapping[str, Any]) -> str:
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            return value
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

    ordered = sorted(
        (fact for fact in facts if isinstance(fact, Mapping)),
        key=lambda fact: int(fact.get("canonical_seq") or 0),
    )

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
        eid = event_id or str(fact.get("source_event_id") or fact_id)
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
            tool_calls[key] = {
                "fact": fact,
                "message_id": message_id,
                "tool_name": str(payload.get("tool") or "tool"),
                "tool_use_id": key,
                "status": "running",
            }
            continue

        if payload_type == "tool_result":
            tool_use_id = payload.get("tool_use_id")
            key = tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None
            if key is not None and key in tool_calls:
                tool_calls[key]["status"] = "complete"
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
        emit(
            fact, "other_typed_work",
            {"kind": payload_type or "unknown", "label": label},
            message_id=message_id,
        )

    for call in tool_calls.values():
        emit(
            call["fact"], "tool_interaction",
            {"tool_name": call["tool_name"], "tool_use_id": call["tool_use_id"],
             "status": call["status"]},
            message_id=call["message_id"],
        )

    events.sort(key=lambda event: event["journal_seq"])
    return AdaptedChatInputs(tuple(messages), tuple(events), tuple(dropped))

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException


MAX_ACTIONS = 100
MAX_CLIENT_ID_LENGTH = 128

_CREATE_KEYS = {
    "type",
    "dispatchedBy",
    "clientId",
    "session",
    "prompt",
    "images",
    "files",
    "capabilityContexts",
    "harnessProfileId",
}
_CREATE_SESSION_KEYS = {
    "id",
    "name",
    "model",
    "reasoning_effort",
    "permission",
    "cwd",
    "orchestration_mode",
    "runtime_profile_id",
    "node_id",
    "created_at",
    "updated_at",
    "messages",
    "capability_contexts",
    "harness_profile_id",
    "folder_id",
    "draft_input",
    "draft_images",
}
_SEND_KEYS = {
    "type",
    "dispatchedBy",
    "sessionId",
    "clientId",
    "prompt",
    "model",
    "cwd",
    "images",
    "files",
    "orchestrationMode",
    "sendMode",
    "sendTarget",
    "capabilityContexts",
    "harnessProfileId",
    "deferUntilTargetReady",
}


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _reject_unknown_fields(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{field} contains unsupported fields: {', '.join(unknown)}")


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID string")
    return value


def _client_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_CLIENT_ID_LENGTH
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("clientId must be a non-empty string of at most 128 characters")
    return value


def _optional_string(value: Any, field: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")


def _optional_array(value: Any, field: str) -> None:
    if value is not None and not isinstance(value, list):
        raise ValueError(f"{field} must be an array or null")


def _validate_common_payload(action: dict[str, Any]) -> None:
    if not isinstance(action.get("prompt"), str):
        raise ValueError("prompt must be a string")
    _optional_array(action.get("images"), "images")
    _optional_array(action.get("files"), "files")
    _optional_array(action.get("capabilityContexts"), "capabilityContexts")
    _optional_string(action.get("harnessProfileId"), "harnessProfileId")
    _optional_string(action.get("dispatchedBy"), "dispatchedBy")


def _validate_create(action: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_fields(action, _CREATE_KEYS, "create_session action")
    _validate_common_payload(action)
    client_id = _client_id(action.get("clientId"))
    session = _require_object(action.get("session"), "session")
    _reject_unknown_fields(session, _CREATE_SESSION_KEYS, "session")
    session_id = _canonical_uuid(session.get("id"), "session.id")
    return {
        **action,
        "type": "create_session",
        "clientId": client_id,
        "session": {**session, "id": session_id},
    }


def _validate_send(action: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown_fields(action, _SEND_KEYS, "send_message action")
    _validate_common_payload(action)
    if action.get("type") not in (None, "send_message"):
        raise ValueError("type must be send_message")
    if action.get("deferUntilTargetReady") not in (None, False, True):
        raise ValueError("deferUntilTargetReady must be a boolean")
    _optional_string(action.get("model"), "model")
    _optional_string(action.get("cwd"), "cwd")
    _optional_string(action.get("orchestrationMode"), "orchestrationMode")
    _optional_string(action.get("sendMode"), "sendMode")
    _optional_string(action.get("sendTarget"), "sendTarget")
    return {
        **action,
        "type": "send_message",
        "sessionId": _canonical_uuid(action.get("sessionId"), "sessionId"),
        "clientId": _client_id(action.get("clientId")),
    }


def validate_action(value: Any) -> dict[str, Any]:
    action = _require_object(value, "action")
    if action.get("type") == "create_session":
        return _validate_create(action)
    return _validate_send(action)


def validate_envelope(body: Any) -> list[Any]:
    envelope = _require_object(body, "request body")
    _reject_unknown_fields(envelope, {"actions"}, "request body")
    actions = envelope.get("actions")
    if not isinstance(actions, list):
        raise ValueError("actions must be an array")
    if not actions:
        raise ValueError("actions must not be empty")
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"actions must contain at most {MAX_ACTIONS} entries")
    return actions


def _http_error_result(
    *,
    index: int,
    action_type: str | None,
    session_id: str | None,
    client_id: str | None,
    error: HTTPException,
) -> dict[str, Any]:
    return {
        "index": index,
        "type": action_type,
        "session_id": session_id,
        "client_id": client_id,
        "accepted": False,
        "status": error.status_code,
        "error": str(error.detail),
    }


async def process_batch(
    body: Any,
    *,
    create_action: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    send_action: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    try:
        actions = validate_envelope(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    results: list[dict[str, Any]] = []
    for index, raw_action in enumerate(actions):
        action_type = raw_action.get("type") if isinstance(raw_action, dict) else None
        session_id = None
        client_id = raw_action.get("clientId") if isinstance(raw_action, dict) else None
        if isinstance(raw_action, dict):
            if action_type == "create_session" and isinstance(raw_action.get("session"), dict):
                session_id = raw_action["session"].get("id")
            else:
                session_id = raw_action.get("sessionId")
        try:
            action = validate_action(raw_action)
            handler = create_action if action["type"] == "create_session" else send_action
            result = await handler(action)
        except ValueError as exc:
            results.append(_http_error_result(
                index=index,
                action_type=action_type,
                session_id=session_id,
                client_id=client_id,
                error=HTTPException(status_code=400, detail=str(exc)),
            ))
        except HTTPException as exc:
            results.append(_http_error_result(
                index=index,
                action_type=action_type,
                session_id=session_id,
                client_id=client_id,
                error=exc,
            ))
        else:
            results.append({
                "index": index,
                "type": action["type"],
                "session_id": (
                    action["session"]["id"]
                    if action["type"] == "create_session"
                    else action["sessionId"]
                ),
                "client_id": action["clientId"],
                "accepted": True,
                **result,
            })
    return {"results": results}

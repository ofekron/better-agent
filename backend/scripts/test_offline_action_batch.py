from __future__ import annotations

import asyncio
import os
import sys

import pytest
from fastapi import HTTPException

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import offline_action_batch as batch  # noqa: E402


UUID_A = "00000000-0000-0000-0000-000000000001"
UUID_B = "00000000-0000-0000-0000-000000000002"
# Valid but non-canonical (uppercase) — uuid.UUID accepts it, str() lowercases it.
NON_CANONICAL_ID = "ABCDEF12-3456-7890-ABCD-EF1234567890"


def _create_action(prompt: str = "hi", client_id: str = "client-1", **overrides) -> dict:
    action = {
        "type": "create_session",
        "clientId": client_id,
        "session": {"id": UUID_A, "name": "s"},
        "prompt": prompt,
    }
    action.update(overrides)
    return action


def _send_action(prompt: str = "hi", client_id: str = "client-1", **overrides) -> dict:
    action = {
        "type": "send_message",
        "sessionId": UUID_B,
        "clientId": client_id,
        "prompt": prompt,
    }
    action.update(overrides)
    return action


# --- validate_envelope ---------------------------------------------------


def test_envelope_rejects_non_dict_body():
    with pytest.raises(ValueError, match="request body must be an object"):
        batch.validate_envelope("nope")


def test_envelope_rejects_unknown_field():
    with pytest.raises(ValueError, match="unsupported fields: extra"):
        batch.validate_envelope({"actions": [], "extra": 1})


def test_envelope_rejects_non_list_actions():
    with pytest.raises(ValueError, match="actions must be an array"):
        batch.validate_envelope({"actions": "x"})


def test_envelope_rejects_empty_actions():
    with pytest.raises(ValueError, match="actions must not be empty"):
        batch.validate_envelope({"actions": []})


def test_envelope_rejects_too_many_actions():
    with pytest.raises(ValueError, match="at most 100"):
        batch.validate_envelope({"actions": [_send_action() for _ in range(101)]})


def test_envelope_returns_actions_list():
    actions = [_send_action(), _send_action()]
    assert batch.validate_envelope({"actions": actions}) is actions


# --- validate_action dispatch -------------------------------------------


def test_validate_action_rejects_non_dict():
    with pytest.raises(ValueError, match="action must be an object"):
        batch.validate_action(123)


def test_validate_action_defaults_to_send():
    result = batch.validate_action({"sessionId": UUID_A, "clientId": "c", "prompt": "x"})
    assert result["type"] == "send_message"
    assert result["sessionId"] == UUID_A


# --- _validate_create ----------------------------------------------------


def test_create_normalizes_type_and_ids():
    result = batch.validate_action(_create_action())
    assert result["type"] == "create_session"
    assert result["clientId"] == "client-1"
    assert result["session"]["id"] == UUID_A


def test_create_rejects_unknown_top_level_field():
    with pytest.raises(ValueError, match="unsupported fields: bogus"):
        batch.validate_action(_create_action(bogus=1))


def test_create_rejects_non_string_prompt():
    with pytest.raises(ValueError, match="prompt must be a string"):
        batch.validate_action(_create_action(prompt=5))


@pytest.mark.parametrize("field", ["images", "files", "capabilityContexts"])
def test_create_rejects_non_array_optional(field):
    with pytest.raises(ValueError, match=f"{field} must be an array or null"):
        batch.validate_action(_create_action(**{field: 7}))


@pytest.mark.parametrize("field", ["harnessProfileId", "dispatchedBy"])
def test_create_rejects_non_string_optional(field):
    with pytest.raises(ValueError, match=f"{field} must be a string or null"):
        batch.validate_action(_create_action(**{field: 7}))


def test_create_rejects_empty_client_id():
    with pytest.raises(ValueError, match="clientId"):
        batch.validate_action(_create_action(client_id=""))


def test_create_rejects_oversized_client_id():
    with pytest.raises(ValueError, match="clientId"):
        batch.validate_action(_create_action(client_id="x" * 129))


def test_create_rejects_control_char_client_id():
    with pytest.raises(ValueError, match="clientId"):
        batch.validate_action(_create_action(client_id="bad\x00id"))


def test_create_rejects_non_string_client_id():
    with pytest.raises(ValueError, match="clientId"):
        batch.validate_action(_create_action(client_id=5))


def test_create_rejects_non_object_session():
    with pytest.raises(ValueError, match="session must be an object"):
        batch.validate_action(_create_action(session="nope"))


def test_create_rejects_unknown_session_field():
    action = _create_action()
    action["session"]["bogus"] = 1
    with pytest.raises(ValueError, match="unsupported fields: bogus"):
        batch.validate_action(action)


def test_create_rejects_non_string_session_id():
    action = _create_action()
    action["session"]["id"] = 5
    with pytest.raises(ValueError, match="canonical UUID"):
        batch.validate_action(action)


def test_create_rejects_invalid_session_id():
    action = _create_action()
    action["session"]["id"] = "not-a-uuid"
    with pytest.raises(ValueError, match="canonical UUID"):
        batch.validate_action(action)


def test_create_rejects_non_canonical_session_id():
    action = _create_action()
    action["session"]["id"] = NON_CANONICAL_ID
    with pytest.raises(ValueError, match="canonical UUID"):
        batch.validate_action(action)


# --- _validate_send ------------------------------------------------------


def test_send_normalizes_type_and_ids():
    result = batch.validate_action(_send_action())
    assert result["type"] == "send_message"
    assert result["sessionId"] == UUID_B
    assert result["clientId"] == "client-1"


def test_send_rejects_unknown_field():
    with pytest.raises(ValueError, match="unsupported fields: bogus"):
        batch.validate_action(_send_action(bogus=1))


def test_send_rejects_wrong_type():
    with pytest.raises(ValueError, match="type must be send_message"):
        batch.validate_action(_send_action(type="foo"))


def test_send_rejects_non_bool_defer():
    with pytest.raises(ValueError, match="deferUntilTargetReady must be a boolean"):
        batch.validate_action(_send_action(deferUntilTargetReady="yes"))


@pytest.mark.parametrize("field", ["model", "cwd", "orchestrationMode", "sendMode", "sendTarget"])
def test_send_rejects_non_string_optional(field):
    with pytest.raises(ValueError, match=f"{field} must be a string or null"):
        batch.validate_action(_send_action(**{field: 7}))


def test_send_rejects_invalid_session_id():
    with pytest.raises(ValueError, match="canonical UUID"):
        batch.validate_action(_send_action(sessionId="nope"))


def test_send_rejects_non_canonical_session_id():
    with pytest.raises(ValueError, match="canonical UUID"):
        batch.validate_action(_send_action(sessionId=NON_CANONICAL_ID))


# --- process_batch -------------------------------------------------------


async def _ok_create(action):
    return {"created": True}


async def _ok_send(action):
    return {"queued": True}


def _run(body, *, create=_ok_create, send=_ok_send):
    return asyncio.run(batch.process_batch(body, create_action=create, send_action=send))


def test_process_batch_rejects_invalid_envelope_with_http_400():
    with pytest.raises(HTTPException) as exc:
        _run({"actions": []})
    assert exc.value.status_code == 400


def test_process_batch_create_success():
    result = _run({"actions": [_create_action()]})
    [entry] = result["results"]
    assert entry["accepted"] is True
    assert entry["type"] == "create_session"
    assert entry["session_id"] == UUID_A
    assert entry["client_id"] == "client-1"
    assert entry["index"] == 0
    assert entry["created"] is True


def test_process_batch_send_success():
    result = _run({"actions": [_send_action()]})
    [entry] = result["results"]
    assert entry["accepted"] is True
    assert entry["type"] == "send_message"
    assert entry["session_id"] == UUID_B
    assert entry["queued"] is True


def test_process_batch_non_dict_action_element():
    result = _run({"actions": [123]})
    [entry] = result["results"]
    assert entry["accepted"] is False
    assert entry["status"] == 400
    assert entry["index"] == 0
    assert entry["type"] is None
    assert entry["session_id"] is None
    assert entry["client_id"] is None


def test_process_batch_validation_error_recorded():
    action = _send_action()
    action["sessionId"] = "bad"
    result = _run({"actions": [action]})
    [entry] = result["results"]
    assert entry["accepted"] is False
    assert entry["status"] == 400
    assert entry["session_id"] == "bad"
    assert "canonical UUID" in entry["error"]


def test_process_batch_handler_value_error_recorded():
    async def fail(action):
        raise ValueError("nope")

    result = _run({"actions": [_send_action()]}, create=fail, send=fail)
    [entry] = result["results"]
    assert entry["accepted"] is False
    assert entry["status"] == 400
    assert entry["error"] == "nope"


def test_process_batch_handler_http_exception_propagated_status():
    async def fail(action):
        raise HTTPException(status_code=409, detail="conflict")

    result = _run({"actions": [_send_action()]}, create=fail, send=fail)
    [entry] = result["results"]
    assert entry["accepted"] is False
    assert entry["status"] == 409
    assert entry["error"] == "conflict"


def test_process_batch_preserves_order_across_mixed_actions():
    body = {"actions": [_create_action(), _send_action(), _send_action(sessionId="bad")]}
    entries = _run(body)["results"]
    assert [e["index"] for e in entries] == [0, 1, 2]
    assert entries[0]["type"] == "create_session"
    assert entries[1]["type"] == "send_message"
    assert entries[2]["accepted"] is False

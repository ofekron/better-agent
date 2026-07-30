from __future__ import annotations

import json
import multiprocessing.connection as mp_connection
import os
import secrets
import threading
import time
from typing import Literal, TypedDict

CredentialStatus = Literal["unknown", "available", "missing", "blocked"]


class CredentialResponse(TypedDict, total=False):
    status: CredentialStatus
    value: str
    error: str
    applied: bool


_FD_TEXT = os.environ.pop("BETTER_AGENT_CREDENTIAL_SESSION_FD", "")


def _connection_from_handle(
    handle: int,
    *,
    platform_name: str | None = None,
) -> mp_connection.Connection:
    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt":
        return mp_connection.PipeConnection(handle)
    return mp_connection.Connection(handle)


try:
    _FD = int(_FD_TEXT) if _FD_TEXT else -1
    if _FD >= 0 and os.name != "nt":
        os.fstat(_FD)
    _CONNECTION = _connection_from_handle(_FD) if _FD >= 0 else None
    if _FD >= 0:
        if os.name == "nt":
            os.set_handle_inheritable(_FD, False)
        else:
            os.set_inheritable(_FD, False)
except (OSError, ValueError):
    _CONNECTION = None
_FD_TEXT = ""
_LOCK = threading.Lock()
_RESPONSE_TIMEOUT_SECONDS = 10.0


def available() -> bool:
    return _CONNECTION is not None


def request(
    op: str,
    provider_id: str,
    *,
    value: str | None = None,
    expected_value: str | None = None,
    target_provider_id: str | None = None,
) -> CredentialResponse:
    if not available():
        raise RuntimeError("desktop credential session is unavailable")
    request_id = secrets.token_hex(16)
    payload: dict[str, str] = {
        "op": op,
        "provider_id": provider_id,
        "request_id": request_id,
    }
    if value is not None:
        payload["value"] = value
    if expected_value is not None:
        payload["expected_value"] = expected_value
    if target_provider_id is not None:
        payload["target_provider_id"] = target_provider_id
    assert _CONNECTION is not None
    with _LOCK:
        _CONNECTION.send_bytes(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        deadline = time.monotonic() + _RESPONSE_TIMEOUT_SECONDS
        for _ in range(8):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not _CONNECTION.poll(remaining):
                raise RuntimeError("credential session response timed out")
            raw = _CONNECTION.recv_bytes(maxlength=128 * 1024)
            response = json.loads(raw.decode("utf-8"))
            if isinstance(response, dict) and response.get("request_id") == request_id:
                break
        else:
            raise RuntimeError("credential session response correlation failed")
    if not isinstance(response, dict) or response.get("status") not in {
        "unknown", "available", "missing", "blocked",
    }:
        raise RuntimeError("invalid desktop credential response")
    response.pop("request_id", None)
    return response

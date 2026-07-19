from __future__ import annotations

import json
import os
import secrets
import threading
from multiprocessing.connection import Connection
from typing import Literal, TypedDict

CredentialStatus = Literal["unknown", "available", "missing", "blocked"]


class CredentialResponse(TypedDict, total=False):
    status: CredentialStatus
    value: str
    error: str


_FD_TEXT = os.environ.pop("BETTER_AGENT_CREDENTIAL_SESSION_FD", "")
try:
    _FD = int(_FD_TEXT) if _FD_TEXT else -1
    if _FD >= 0:
        os.fstat(_FD)
    _CONNECTION = Connection(_FD) if _FD >= 0 else None
except (OSError, ValueError):
    _CONNECTION = None
_FD_TEXT = ""
_LOCK = threading.Lock()


def available() -> bool:
    return _CONNECTION is not None


def request(op: str, provider_id: str, *, value: str | None = None) -> CredentialResponse:
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
    assert _CONNECTION is not None
    with _LOCK:
        _CONNECTION.send_bytes(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        for _ in range(8):
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

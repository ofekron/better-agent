from __future__ import annotations

import json
import os
from multiprocessing.connection import Client
from typing import Literal, TypedDict

CredentialStatus = Literal["unknown", "available", "missing", "blocked"]


class CredentialResponse(TypedDict, total=False):
    status: CredentialStatus
    value: str
    error: str


_ADDRESS = os.environ.pop("BETTER_AGENT_CREDENTIAL_SESSION_ADDRESS", "")
_FAMILY = os.environ.pop("BETTER_AGENT_CREDENTIAL_SESSION_FAMILY", "")
_AUTH_HEX = os.environ.pop("BETTER_AGENT_CREDENTIAL_SESSION_AUTH", "")
try:
    _AUTHKEY = bytes.fromhex(_AUTH_HEX) if _AUTH_HEX else b""
except ValueError:
    _AUTHKEY = b""
_AUTH_HEX = ""


def available() -> bool:
    return bool(_ADDRESS and _FAMILY in {"AF_UNIX", "AF_PIPE"} and _AUTHKEY)


def request(op: str, provider_id: str, *, value: str | None = None) -> CredentialResponse:
    if not available():
        raise RuntimeError("desktop credential session is unavailable")
    payload: dict[str, str] = {"op": op, "provider_id": provider_id}
    if value is not None:
        payload["value"] = value
    conn = Client(_ADDRESS, family=_FAMILY, authkey=_AUTHKEY)
    try:
        conn.send_bytes(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        raw = conn.recv_bytes(maxlength=128 * 1024)
    finally:
        conn.close()
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict) or response.get("status") not in {
        "unknown", "available", "missing", "blocked",
    }:
        raise RuntimeError("invalid desktop credential response")
    return response

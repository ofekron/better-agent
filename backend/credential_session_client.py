from __future__ import annotations

import json
import multiprocessing.connection as mp_connection
import os
import queue
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
_MAX_FRAME_BYTES = 128 * 1024
_GENERATION_CONNECTION = _CONNECTION


class CredentialSessionRestartRequired(RuntimeError):
    pass


class _SendTask:
    def __init__(self, payload: bytes, request_id: str, deadline: float) -> None:
        self.payload = payload
        self.request_id = request_id
        self.deadline = deadline
        self.completed = threading.Event()
        self.response: CredentialResponse | None = None
        self.error: CredentialSessionRestartRequired | None = None


class _Sender:
    def __init__(self, connection: mp_connection.Connection) -> None:
        self.connection = connection
        self._requests: queue.SimpleQueue[_SendTask | None] = queue.SimpleQueue()
        self._retired = False
        self._thread = threading.Thread(
            target=self._run,
            name="credential-session-sender",
            daemon=True,
        )
        self._thread.start()

    def transact(
        self,
        payload: bytes,
        request_id: str,
        deadline: float,
        timeout: float,
    ) -> CredentialResponse:
        task = _SendTask(payload, request_id, deadline)
        self._requests.put(task)
        if not task.completed.wait(timeout):
            raise CredentialSessionRestartRequired(
                "credential session request timed out"
            )
        if task.error is not None:
            raise task.error
        if task.response is None:
            raise CredentialSessionRestartRequired(
                "invalid desktop credential response"
            )
        return task.response

    def retire(self) -> None:
        if self._retired:
            return
        self._retired = True
        self._requests.put(None)

    def _run(self) -> None:
        while True:
            task = self._requests.get()
            if task is None:
                try:
                    self.connection.close()
                except OSError:
                    pass
                return
            try:
                task.response = self._transact(task)
            except CredentialSessionRestartRequired as exc:
                task.error = exc
            except Exception:
                task.error = CredentialSessionRestartRequired(
                    "credential session transport failed"
                )
            finally:
                task.payload = b""
                task.completed.set()

    def _transact(self, task: _SendTask) -> CredentialResponse:
        try:
            self.connection.send_bytes(task.payload)
        except Exception:
            raise CredentialSessionRestartRequired(
                "credential session transport failed"
            ) from None
        task.payload = b""
        for _ in range(8):
            remaining = _remaining(task.deadline)
            try:
                response_ready = remaining > 0 and self.connection.poll(remaining)
            except (EOFError, OSError, ValueError):
                raise CredentialSessionRestartRequired(
                    "credential session transport failed"
                ) from None
            if not response_ready:
                raise CredentialSessionRestartRequired(
                    "credential session response timed out"
                )
            try:
                raw = self.connection.recv_bytes(maxlength=_MAX_FRAME_BYTES)
                response = json.loads(raw.decode("utf-8"))
            except (EOFError, OSError, ValueError):
                raise CredentialSessionRestartRequired(
                    "invalid desktop credential response"
                ) from None
            if isinstance(response, dict) and response.get("request_id") == task.request_id:
                break
        else:
            raise CredentialSessionRestartRequired(
                "credential session response correlation failed"
            )
        if response.get("status") not in {
            "unknown", "available", "missing", "blocked",
        }:
            raise CredentialSessionRestartRequired(
                "invalid desktop credential response"
            )
        response.pop("request_id", None)
        return response


_SENDER: _Sender | None = None


def available() -> bool:
    return _CONNECTION is not None and _CONNECTION is _GENERATION_CONNECTION


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _sender_for(connection: mp_connection.Connection) -> _Sender:
    global _SENDER
    if connection is not _GENERATION_CONNECTION:
        raise CredentialSessionRestartRequired(
            "credential session generation is stale"
        )
    if _SENDER is None:
        _SENDER = _Sender(connection)
    elif _SENDER.connection is not connection:
        raise CredentialSessionRestartRequired(
            "credential session generation is stale"
        )
    return _SENDER


def _retire_connection(
    connection: mp_connection.Connection,
    sender: _Sender,
) -> None:
    global _CONNECTION
    if _CONNECTION is connection:
        _CONNECTION = None
    sender.retire()


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
    deadline = time.monotonic() + _RESPONSE_TIMEOUT_SECONDS
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
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_FRAME_BYTES:
        raise RuntimeError("credential session request is too large")
    remaining = _remaining(deadline)
    if remaining <= 0 or not _LOCK.acquire(timeout=remaining):
        raise RuntimeError("credential session request timed out")
    try:
        connection = _CONNECTION
        if connection is None:
            raise RuntimeError("desktop credential session is unavailable")
        sender = _sender_for(connection)
        remaining = _remaining(deadline)
        if remaining <= 0:
            _retire_connection(connection, sender)
            raise CredentialSessionRestartRequired(
                "credential session request timed out"
            )
        try:
            response = sender.transact(
                encoded,
                request_id,
                deadline,
                remaining,
            )
        except CredentialSessionRestartRequired:
            _retire_connection(connection, sender)
            raise
        return response
    finally:
        _LOCK.release()

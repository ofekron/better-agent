from __future__ import annotations

import json
import os
import subprocess
import threading
from multiprocessing import Pipe
from typing import Literal

from provider_credentials import ProviderCredentialStore

CredentialStatus = Literal["unknown", "available", "missing", "blocked"]
_MAX_FRAME_BYTES = 128 * 1024


class ProviderCredentialBroker:
    def __init__(self) -> None:
        self._states: dict[str, tuple[CredentialStatus, str]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._credential_store = ProviderCredentialStore()

    def open_session(self) -> "ProviderCredentialSession":
        return ProviderCredentialSession(self)

    def clear(self) -> None:
        self._states.clear()
        self._locks.clear()

    def _provider_lock(self, provider_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(provider_id, threading.Lock())

    def handle(self, request: object) -> dict[str, str]:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        op = request.get("op")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or len(request_id) != 32:
            raise ValueError("invalid request_id")
        provider_id = request.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 128:
            raise ValueError("invalid provider_id")
        if any(character in provider_id for character in "\0\r\n"):
            raise ValueError("invalid provider_id")
        if op == "status":
            status, _ = self._states.get(provider_id, ("unknown", ""))
            return {"status": status}
        with self._provider_lock(provider_id):
            if op == "read":
                return self._read(provider_id, retry=False)
            if op == "retry":
                return self._read(provider_id, retry=True)
            if op == "migrate_flat":
                return self._migrate_flat(provider_id)
            if op == "store":
                value = request.get("value")
                if not isinstance(value, str) or not value or len(value) > 128 * 1024:
                    raise ValueError("invalid credential")
                return self._store(provider_id, value)
            if op == "delete":
                return self._delete(provider_id)
        raise ValueError("unsupported operation")

    def _read(self, provider_id: str, *, retry: bool) -> dict[str, str]:
        if retry:
            self._states.pop(provider_id, None)
        status, value = self._states.get(provider_id, ("unknown", ""))
        if status != "unknown":
            response = {"status": status}
            if status == "available":
                response["value"] = value
            return response
        try:
            value = self._credential_store.read(provider_id)
            if value:
                self._states[provider_id] = ("available", value)
                return {"status": "available", "value": value}
        except RuntimeError:
            self._states[provider_id] = ("blocked", "")
            return {"status": "blocked"}
        self._states[provider_id] = ("missing", "")
        return {"status": "missing"}

    def _store(self, provider_id: str, value: str) -> dict[str, str]:
        try:
            self._credential_store.store(provider_id, value)
        except RuntimeError:
            self._states[provider_id] = ("blocked", "")
            return {"status": "blocked"}
        self._states[provider_id] = ("available", value)
        return {"status": "available"}

    def _migrate_flat(self, provider_id: str) -> dict[str, str]:
        try:
            value = self._credential_store.migrate_flat(provider_id)
        except RuntimeError:
            self._states[provider_id] = ("blocked", "")
            return {"status": "blocked"}
        if not value:
            self._states[provider_id] = ("missing", "")
            return {"status": "missing"}
        self._states[provider_id] = ("available", value)
        return {"status": "available", "value": value}

    def _delete(self, provider_id: str) -> dict[str, str]:
        if self._states.get(provider_id, ("unknown", ""))[0] == "blocked":
            return {"status": "blocked"}
        try:
            self._credential_store.delete(provider_id)
        except RuntimeError:
            self._states[provider_id] = ("blocked", "")
            return {"status": "blocked"}
        self._states[provider_id] = ("missing", "")
        return {"status": "missing"}


class ProviderCredentialSession:
    def __init__(self, broker: ProviderCredentialBroker | None = None) -> None:
        self._broker = broker or ProviderCredentialBroker()
        self._owns_broker = broker is None
        self._server_connection, self._backend_connection = Pipe(duplex=True)
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        if os.name == "nt":
            os.set_handle_inheritable(self._backend_connection.fileno(), True)
        else:
            os.set_inheritable(self._backend_connection.fileno(), True)
        self._thread = threading.Thread(
            target=self._serve,
            name="provider-credential-session",
            daemon=True,
        )
        self._thread.start()

    def backend_env(self) -> dict[str, str]:
        return {
            "BETTER_AGENT_CREDENTIAL_SESSION_FD": str(
                self._backend_connection.fileno()
            ),
        }

    def backend_popen_kwargs(self) -> dict[str, object]:
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.lpAttributeList = {
                "handle_list": [self._backend_connection.fileno()]
            }
            return {"close_fds": True, "startupinfo": startupinfo}
        return {"pass_fds": (self._backend_connection.fileno(),)}

    def revoke_backend_inheritance(self) -> None:
        if os.name == "nt":
            os.set_handle_inheritable(self._backend_connection.fileno(), False)
        else:
            os.set_inheritable(self._backend_connection.fileno(), False)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stopping.set()
        try:
            self._backend_connection.close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            self._server_connection.close()
            self._thread.join(timeout=1)
        self._thread = None
        try:
            self._server_connection.close()
        except OSError:
            pass
        if self._owns_broker:
            self._broker.clear()

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                payload = self._server_connection.recv_bytes(maxlength=_MAX_FRAME_BYTES)
                request = json.loads(payload.decode("utf-8"))
                response = self._broker.handle(request)
                if isinstance(request, dict) and isinstance(request.get("request_id"), str):
                    response["request_id"] = request["request_id"]
                self._server_connection.send_bytes(
                    json.dumps(response, separators=(",", ":")).encode("utf-8")
                )
            except (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                try:
                    self._server_connection.send_bytes(
                        b'{"status":"blocked","error":"invalid request"}'
                    )
                except (EOFError, OSError):
                    return

from __future__ import annotations

import json
import os
import secrets
import signal
import sys
import tempfile
import threading
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Literal

import oskeychain
from keychain_names import LEGACY_SERVICE, PRIMARY_SERVICE, service_names

CredentialStatus = Literal["unknown", "available", "missing", "blocked"]
_MAX_FRAME_BYTES = 128 * 1024


class ProviderCredentialSession:
    def __init__(
        self,
        *,
        address: str | None = None,
        family: str | None = None,
        authkey: bytes | None = None,
    ) -> None:
        self._authkey = authkey or secrets.token_bytes(32)
        self._family = family or ("AF_PIPE" if os.name == "nt" else "AF_UNIX")
        self._temp_dir: Path | None = None
        if address:
            self._address = address
        elif self._family == "AF_PIPE":
            self._address = rf"\\.\pipe\better-agent-credentials-{secrets.token_hex(16)}"
        else:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="better-agent-credentials-"))
            self._temp_dir.chmod(0o700)
            self._address = str(self._temp_dir / "session.sock")
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._states: dict[str, tuple[CredentialStatus, str]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._listener = Listener(
            address=self._address,
            family=self._family,
            authkey=self._authkey,
        )
        if self._temp_dir is not None:
            Path(self._address).chmod(0o600)
        self._thread = threading.Thread(
            target=self._serve,
            name="provider-credential-session",
            daemon=True,
        )
        self._thread.start()

    def backend_env(self) -> dict[str, str]:
        return {
            "BETTER_AGENT_CREDENTIAL_SESSION_ADDRESS": self._address,
            "BETTER_AGENT_CREDENTIAL_SESSION_FAMILY": self._family,
            "BETTER_AGENT_CREDENTIAL_SESSION_AUTH": self._authkey.hex(),
        }

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stopping.set()
        try:
            conn = Client(self._address, family=self._family, authkey=self._authkey)
            conn.send_bytes(b'{"op":"shutdown"}')
            conn.close()
        except (OSError, EOFError):
            pass
        self._thread.join(timeout=5)
        self._thread = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        self._states.clear()
        if self._temp_dir is not None:
            try:
                Path(self._address).unlink()
            except FileNotFoundError:
                pass
            self._temp_dir.rmdir()

    def _provider_lock(self, provider_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(provider_id, threading.Lock())

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                conn = self._listener.accept()
            except AuthenticationError:
                continue
            except (OSError, EOFError):
                return
            try:
                payload = conn.recv_bytes(maxlength=_MAX_FRAME_BYTES)
                request = json.loads(payload.decode("utf-8"))
                response = self._handle(request)
                conn.send_bytes(json.dumps(response, separators=(",", ":")).encode("utf-8"))
            except (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                try:
                    conn.send_bytes(b'{"status":"blocked","error":"invalid request"}')
                except (EOFError, OSError):
                    pass
            finally:
                conn.close()

    def _handle(self, request: object) -> dict[str, str]:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        op = request.get("op")
        if op == "shutdown":
            self._stopping.set()
            return {"status": "unknown"}
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
        account = f"provider:{provider_id}"
        try:
            for service in service_names(PRIMARY_SERVICE, LEGACY_SERVICE):
                value = oskeychain.get(service, account)
                if value:
                    value = value[:-1] if value.endswith("\n") else value
                    self._states[provider_id] = ("available", value)
                    return {"status": "available", "value": value}
        except RuntimeError:
            self._states[provider_id] = ("blocked", "")
            return {"status": "blocked"}
        self._states[provider_id] = ("missing", "")
        return {"status": "missing"}

    def _store(self, provider_id: str, value: str) -> dict[str, str]:
        if self._states.get(provider_id, ("unknown", ""))[0] == "blocked":
            return {"status": "blocked"}
        account = f"provider:{provider_id}"
        try:
            for service in service_names(PRIMARY_SERVICE, LEGACY_SERVICE):
                oskeychain.store(service, account, value)
        except RuntimeError:
            self._states[provider_id] = ("blocked", "")
            return {"status": "blocked"}
        self._states[provider_id] = ("available", value)
        return {"status": "available"}

    def _delete(self, provider_id: str) -> dict[str, str]:
        if self._states.get(provider_id, ("unknown", ""))[0] == "blocked":
            return {"status": "blocked"}
        account = f"provider:{provider_id}"
        try:
            for service in service_names(PRIMARY_SERVICE, LEGACY_SERVICE):
                oskeychain.delete(service, account)
        except RuntimeError:
            self._states[provider_id] = ("blocked", "")
            return {"status": "blocked"}
        self._states[provider_id] = ("missing", "")
        return {"status": "missing"}


def _serve_from_environment() -> int:
    address = os.environ.pop("BETTER_AGENT_CREDENTIAL_SESSION_ADDRESS", "")
    family = os.environ.pop("BETTER_AGENT_CREDENTIAL_SESSION_FAMILY", "")
    auth_hex = os.environ.pop("BETTER_AGENT_CREDENTIAL_SESSION_AUTH", "")
    if not address or family not in {"AF_UNIX", "AF_PIPE"} or not auth_hex:
        raise RuntimeError("credential session environment is incomplete")
    session = ProviderCredentialSession(
        address=address,
        family=family,
        authkey=bytes.fromhex(auth_hex),
    )
    auth_hex = ""
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    session.start()
    try:
        stopped.wait()
    finally:
        session.stop()
    return 0


if __name__ == "__main__":
    sys.exit(_serve_from_environment())

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from multiprocessing import Pipe
from typing import Literal

import oskeychain
from provider_credentials import (
    ProviderCredentialAccessBlocked,
    ProviderCredentialCandidate,
    ProviderCredentialStore,
)

logger = logging.getLogger(__name__)

CredentialStatus = Literal["unknown", "available", "missing", "blocked"]
_MAX_FRAME_BYTES = 128 * 1024


@dataclass(frozen=True)
class _ProviderCredentialState:
    status: CredentialStatus
    value: str = ""
    blocked_candidate: ProviderCredentialCandidate | None = None

    def response(self) -> dict[str, str]:
        response = {"status": self.status}
        if self.status == "available":
            response["value"] = self.value
        return response


_UNKNOWN_CREDENTIAL_STATE = _ProviderCredentialState("unknown")


class ProviderCredentialBroker:
    def __init__(
        self,
        credential_store: ProviderCredentialStore | None = None,
    ) -> None:
        oskeychain.disable_native_user_interaction()
        self._states: dict[str, _ProviderCredentialState] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._keychain_lock = threading.Lock()
        self._credential_store = credential_store or ProviderCredentialStore()

    def open_session(self) -> "ProviderCredentialSession":
        return ProviderCredentialSession(self)

    def clear(self) -> None:
        self._states.clear()
        self._locks.clear()

    def _provider_lock(self, provider_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(provider_id, threading.Lock())

    def handle(self, request: object) -> dict[str, str | bool]:
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
            return {"status": self._state(provider_id).status}
        if op == "clone":
            target_provider_id = request.get("target_provider_id")
            if (
                not isinstance(target_provider_id, str)
                or not target_provider_id
                or len(target_provider_id) > 128
                or any(
                    character in target_provider_id
                    for character in "\0\r\n"
                )
                or target_provider_id == provider_id
            ):
                raise ValueError("invalid target_provider_id")
            with ExitStack() as locks:
                for locked_provider_id in sorted(
                    (provider_id, target_provider_id)
                ):
                    locks.enter_context(
                        self._provider_lock(locked_provider_id)
                    )
                return self._clone(provider_id, target_provider_id)
        with self._provider_lock(provider_id):
            if op == "read":
                return self._read(provider_id)
            if op == "retry":
                return self._retry(provider_id)
            if op == "migrate_flat":
                return self._migrate_flat(provider_id)
            if op == "store":
                value = request.get("value")
                if not isinstance(value, str) or not value or len(value) > 128 * 1024:
                    raise ValueError("invalid credential")
                return self._store(provider_id, value)
            if op == "compare_set":
                value = request.get("value")
                expected_value = request.get("expected_value")
                if (
                    not isinstance(value, str)
                    or len(value) > 128 * 1024
                    or not isinstance(expected_value, str)
                    or len(expected_value) > 128 * 1024
                ):
                    raise ValueError("invalid credential compare-and-set")
                return self._compare_set(
                    provider_id,
                    expected_value,
                    value,
                )
            if op == "delete":
                return self._delete(provider_id)
        raise ValueError("unsupported operation")

    def _clone(
        self,
        source_provider_id: str,
        target_provider_id: str,
    ) -> dict[str, str]:
        source = self._read(source_provider_id)
        if source["status"] != "available":
            return {"status": source["status"]}
        return self._store(target_provider_id, source["value"])

    def _read(self, provider_id: str) -> dict[str, str]:
        state = self._state(provider_id)
        if state.status in {"available", "missing"}:
            return state.response()
        # "unknown" or "blocked": probe the keychain. A blocked state is
        # re-probed on every read so transient OS denials (locked keychain,
        # ACL churn) heal as soon as access returns, without a restart.
        try:
            with self._keychain_lock:
                value = self._credential_store.read(provider_id)
            if value:
                return self._remember(provider_id, "available", value=value)
        except ProviderCredentialAccessBlocked as exc:
            return self._remember_blocked(
                provider_id,
                exc,
                blocked_candidate=exc.candidate,
            )
        except RuntimeError as exc:
            return self._remember_blocked(provider_id, exc)
        return self._remember(provider_id, "missing")

    def _retry(self, provider_id: str) -> dict[str, str]:
        candidate = self._state(provider_id).blocked_candidate
        if candidate is None:
            self._states.pop(provider_id, None)
            discovered = self._read(provider_id)
            if discovered["status"] != "blocked":
                return discovered
            candidate = self._state(provider_id).blocked_candidate
            if candidate is None:
                return discovered
        try:
            with self._keychain_lock:
                with oskeychain.native_user_interaction():
                    value = self._credential_store.retry_candidate(
                        provider_id,
                        candidate,
                    )
                if value:
                    value = self._credential_store.adopt_candidate(
                        provider_id,
                        candidate,
                        value,
                    )
        except ProviderCredentialAccessBlocked as exc:
            return self._remember(
                provider_id,
                "blocked",
                blocked_candidate=exc.candidate,
            )
        except RuntimeError:
            return self._remember(
                provider_id,
                "blocked",
                blocked_candidate=candidate,
            )
        if value:
            return self._remember(provider_id, "available", value=value)
        self._states.pop(provider_id, None)
        return self._read(provider_id)

    def _store(self, provider_id: str, value: str) -> dict[str, str]:
        try:
            with self._keychain_lock:
                self._credential_store.store(provider_id, value)
        except RuntimeError:
            return self._remember(provider_id, "blocked")
        self._remember(provider_id, "available", value=value)
        return {"status": "available"}

    def _compare_set(
        self,
        provider_id: str,
        expected_value: str,
        value: str,
    ) -> dict[str, str | bool]:
        try:
            with self._keychain_lock:
                current = self._credential_store.read(provider_id)
                if current != expected_value:
                    status = "available" if current else "missing"
                    self._remember(
                        provider_id,
                        status,
                        value=current,
                    )
                    return {"status": status, "applied": False}
                if value:
                    self._credential_store.store(provider_id, value)
                else:
                    self._credential_store.delete(provider_id)
        except ProviderCredentialAccessBlocked as exc:
            self._remember_blocked(
                provider_id,
                exc,
                blocked_candidate=exc.candidate,
            )
            return {"status": "blocked", "applied": False}
        except RuntimeError:
            self._remember(provider_id, "blocked")
            return {"status": "blocked", "applied": False}
        status = "available" if value else "missing"
        self._remember(provider_id, status, value=value)
        return {"status": status, "applied": True}

    def _migrate_flat(self, provider_id: str) -> dict[str, str]:
        try:
            with self._keychain_lock:
                value = self._credential_store.migrate_flat(provider_id)
        except RuntimeError:
            return self._remember(provider_id, "blocked")
        if not value:
            return self._remember(provider_id, "missing")
        return self._remember(provider_id, "available", value=value)

    def _delete(self, provider_id: str) -> dict[str, str]:
        if self._state(provider_id).status == "blocked":
            return {"status": "blocked"}
        try:
            with self._keychain_lock:
                self._credential_store.delete(provider_id)
        except RuntimeError:
            return self._remember(provider_id, "blocked")
        return self._remember(provider_id, "missing")

    def _state(self, provider_id: str) -> _ProviderCredentialState:
        return self._states.get(provider_id, _UNKNOWN_CREDENTIAL_STATE)

    def _remember_blocked(
        self,
        provider_id: str,
        cause: BaseException,
        *,
        blocked_candidate: ProviderCredentialCandidate | None = None,
    ) -> dict[str, str]:
        # WARNING only on the transition into blocked; re-probes that stay
        # blocked log at DEBUG so a long outage doesn't flood the log.
        level = (
            logging.DEBUG
            if self._state(provider_id).status == "blocked"
            else logging.WARNING
        )
        logger.log(
            level,
            "provider credential read blocked for %s: %s",
            provider_id,
            cause.__cause__ or cause,
        )
        return self._remember(
            provider_id,
            "blocked",
            blocked_candidate=blocked_candidate,
        )

    def _remember(
        self,
        provider_id: str,
        status: CredentialStatus,
        *,
        value: str = "",
        blocked_candidate: ProviderCredentialCandidate | None = None,
    ) -> dict[str, str]:
        state = _ProviderCredentialState(status, value, blocked_candidate)
        self._states[provider_id] = state
        return state.response()


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
            except (EOFError, OSError):
                return
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                request = None
                response = {"status": "blocked", "error": "invalid request"}
            except Exception:
                logger.exception("provider credential request failed")
                response = {"status": "blocked", "error": "credential access failed"}
            if isinstance(request, dict) and isinstance(request.get("request_id"), str):
                response["request_id"] = request["request_id"]
            try:
                self._server_connection.send_bytes(
                    json.dumps(response, separators=(",", ":")).encode("utf-8")
                )
            except (EOFError, OSError):
                return

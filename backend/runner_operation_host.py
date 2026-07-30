from __future__ import annotations

import atexit
import hmac
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from env_compat import dual_env_many
from runtime_broker import BrokerRequest, RuntimeBroker

class _InternalTokenAuthority:
    def __init__(self, bootstrap_token: str) -> None:
        self._bootstrap_token = bootstrap_token
        self._current_token = bootstrap_token
        self._lock = threading.Lock()

    def current(self, bootstrap_token: str) -> str | None:
        with self._lock:
            if not self._accepts(bootstrap_token):
                return None
            return self._current_token

    def refresh_after_forbidden(
        self,
        *,
        bootstrap_token: str,
        rejected_token: str,
    ) -> str | None:
        with self._lock:
            if not self._accepts(bootstrap_token):
                return None
            if not hmac.compare_digest(self._current_token, rejected_token):
                return self._current_token
            candidate = _read_backend_internal_token()
            if candidate is None or hmac.compare_digest(candidate, rejected_token):
                return None
            self._current_token = candidate
            return candidate

    def _accepts(self, bootstrap_token: str) -> bool:
        return hmac.compare_digest(self._bootstrap_token, bootstrap_token)


class InternalTokenLease:
    def __init__(
        self,
        bootstrap_token: str,
        authority: _InternalTokenAuthority | None,
    ) -> None:
        self._bootstrap_token = bootstrap_token
        self._authority = authority
        self.token = (
            authority.current(bootstrap_token)
            if authority is not None
            else None
        ) or bootstrap_token
        self._observed_tokens = list(dict.fromkeys((
            bootstrap_token,
            self.token,
        )))

    def refresh_after_forbidden(self) -> bool:
        if self._authority is None:
            return False
        refreshed = self._authority.refresh_after_forbidden(
            bootstrap_token=self._bootstrap_token,
            rejected_token=self.token,
        )
        if refreshed is None or any(
            hmac.compare_digest(refreshed, observed)
            for observed in self._observed_tokens
        ):
            return False
        self.token = refreshed
        self._observed_tokens.append(refreshed)
        return True

    def redact_values(self) -> tuple[str, ...]:
        return tuple(self._observed_tokens)


def _read_backend_internal_token() -> str | None:
    from internal_token_file import read_private_token
    from paths import bc_home

    return read_private_token(bc_home() / "internal_token")


_ACTIVE_TOKEN_AUTHORITY: _InternalTokenAuthority | None = None


def _install_internal_token_authority(
    bootstrap_token: str,
) -> _InternalTokenAuthority:
    global _ACTIVE_TOKEN_AUTHORITY
    if _ACTIVE_TOKEN_AUTHORITY is not None:
        raise RuntimeError("runner internal token authority is already active")
    authority = _InternalTokenAuthority(bootstrap_token)
    _ACTIVE_TOKEN_AUTHORITY = authority
    return authority


def internal_token_lease(bootstrap_token: str) -> InternalTokenLease:
    return InternalTokenLease(
        bootstrap_token,
        _ACTIVE_TOKEN_AUTHORITY,
    )


def hydrate_runner_inputs(inputs: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    from runner_errors import RuntimeBootstrapUnavailable

    bootstrap = (
        os.environ.pop("BETTER_AGENT_RUNTIME_BOOTSTRAP", "")
        or os.environ.pop("BETTER_CLAUDE_RUNTIME_BOOTSTRAP", "")
    ).strip()
    if not bootstrap:
        raise RuntimeBootstrapUnavailable("runner runtime bootstrap is unavailable")
    from better_agent_sdk.runtime_transport import RuntimeTransport

    try:
        response = RuntimeTransport(bootstrap).request(
            {"version": 1, "kind": "catalog"}
        )
    except OSError as e:
        # Broker socket gone — the one-shot lease was retired (backend
        # shutdown or 30s expiry) before the handshake. Provider turn
        # never started.
        raise RuntimeBootstrapUnavailable(str(e)) from e
    secret = str(response.get("secret") or "")
    if not secret:
        raise RuntimeBootstrapUnavailable(
            "runner runtime bootstrap returned no secret"
        )
    inputs["internal_token"] = secret
    hydration = response.get("runtime_hydration")
    if hydration is not None:
        if type(hydration) is not dict:
            raise RuntimeError("runner runtime hydration is invalid")
        inputs["_runtime_hydration"] = hydration
    authority = _install_internal_token_authority(secret)
    host = _RunnerOperationHost(run_dir, inputs, authority)
    global _ACTIVE_HOST
    _ACTIVE_HOST = host
    try:
        address = host.start()
        os.environ.update(
            dual_env_many({"BETTER_CLAUDE_RUNTIME_BROKER": address})
        )
        for name in ("BETTER_AGENT_INTERNAL_TOKEN", "BETTER_CLAUDE_INTERNAL_TOKEN"):
            os.environ.pop(name, None)
    except Exception:
        stop_active_host()
        raise
    atexit.register(stop_active_host)
    return inputs


# The host owns a listener socket (and, on the long-path branch, a temp
# dir). `runner_exit.hard_exit` bypasses atexit, so the teardown needs an
# explicit entry point too; both routes land here and it is idempotent.
_ACTIVE_HOST: Any = None


def stop_active_host() -> None:
    global _ACTIVE_HOST, _ACTIVE_TOKEN_AUTHORITY
    host, _ACTIVE_HOST = _ACTIVE_HOST, None
    _ACTIVE_TOKEN_AUTHORITY = None
    if host is None:
        return
    host.stop()


class _RunnerOperationHost:
    def __init__(
        self,
        run_dir: Path,
        inputs: dict[str, Any],
        token_authority: _InternalTokenAuthority,
    ) -> None:
        self._backend_url = str(inputs.get("backend_url") or "").rstrip("/")
        self._token_authority = token_authority
        self._bootstrap_token = str(inputs["internal_token"])
        self._context = {
            "app_session_id": str(inputs.get("app_session_id") or ""),
            "run_id": run_dir.name,
            "provider_id": str(
                inputs.get("provider_id") or inputs.get("provider_kind") or ""
            ),
            "cwd": str(inputs.get("cwd") or ""),
        }
        self._broker = RuntimeBroker(run_dir / "runtime", self._handle)

    def start(self) -> str:
        if not self._backend_url:
            raise RuntimeError("runner backend URL is unavailable")
        return self._broker.start()

    def stop(self) -> None:
        self._broker.stop()

    def _handle(self, request: BrokerRequest) -> dict[str, Any]:
        body = {
            **self._context,
            "request": request.model_dump(mode="json"),
        }
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        lease = InternalTokenLease(
            self._bootstrap_token,
            self._token_authority,
        )
        deadline = time.monotonic() + 24 * 60 * 60

        def request_once() -> dict[str, Any]:
            http_request = urllib.request.Request(
                self._backend_url + "/api/internal/runtime-operations",
                data=encoded,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Token": lease.token,
                },
            )
            with urllib.request.urlopen(
                http_request,
                timeout=max(1.0, deadline - time.monotonic()),
            ) as response:
                return json.loads(response.read().decode("utf-8"))

        while True:
            try:
                value = request_once()
                break
            except urllib.error.HTTPError as error:
                if (
                    error.code == 403
                    and time.monotonic() < deadline
                    and lease.refresh_after_forbidden()
                ):
                    continue
                from loopback_http import raise_loopback_http_error

                raise_loopback_http_error(
                    error,
                    redact_values=lease.redact_values(),
                )
        if not isinstance(value, dict):
            raise RuntimeError("runtime operation endpoint returned invalid data")
        return value

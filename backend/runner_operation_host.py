from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import urllib.request
from typing import Any

from env_compat import dual_env_many
from runtime_broker import BrokerRequest, RuntimeBroker


def hydrate_runner_inputs(inputs: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    bootstrap = (
        os.environ.pop("BETTER_AGENT_RUNTIME_BOOTSTRAP", "")
        or os.environ.pop("BETTER_CLAUDE_RUNTIME_BOOTSTRAP", "")
    ).strip()
    if not bootstrap:
        raise RuntimeError("runner runtime bootstrap is unavailable")
    from better_agent_sdk.runtime_transport import RuntimeTransport

    response = RuntimeTransport(bootstrap).request(
        {"version": 1, "kind": "catalog"}
    )
    secret = str(response.get("secret") or "")
    if not secret:
        raise RuntimeError("runner runtime bootstrap returned no secret")
    inputs["internal_token"] = secret
    host = _RunnerOperationHost(run_dir, inputs, secret)
    address = host.start()
    os.environ.update(
        dual_env_many({"BETTER_CLAUDE_RUNTIME_BROKER": address})
    )
    for name in ("BETTER_AGENT_INTERNAL_TOKEN", "BETTER_CLAUDE_INTERNAL_TOKEN"):
        os.environ.pop(name, None)
    atexit.register(host.stop)
    return inputs


class _RunnerOperationHost:
    def __init__(
        self,
        run_dir: Path,
        inputs: dict[str, Any],
        internal_token: str,
    ) -> None:
        self._backend_url = str(inputs.get("backend_url") or "").rstrip("/")
        self._internal_token = internal_token
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
        http_request = urllib.request.Request(
            self._backend_url + "/api/internal/runtime-operations",
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": self._internal_token,
            },
        )
        with urllib.request.urlopen(http_request, timeout=24 * 60 * 60) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("runtime operation endpoint returned invalid data")
        return value

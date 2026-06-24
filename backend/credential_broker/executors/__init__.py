"""Sink executors.

A new sink kind registers here. Each new kind MUST come with its own threat
analysis: in particular, never pass a secret through a child-process
environment (readable via ``ps -E`` / ``/proc`` and inherited by
grandchildren) — use a pipe / fd / stdin instead. The ``http`` executor is
"consuming": the secret stays in broker memory and goes out over TLS, never
into a child process.
"""

from __future__ import annotations

from credential_broker.executors.base import ExecResult, SinkExecutor
from credential_broker.executors.exec import ExecSinkExecutor
from credential_broker.executors.http import HttpExecutor
from credential_broker.executors.local_keychain import LocalKeychainExecutor

_REGISTRY: dict[str, SinkExecutor] = {
    "http": HttpExecutor(),
    "local_keychain": LocalKeychainExecutor(),
    "exec": ExecSinkExecutor(),
}


def get_executor(sink_kind: str) -> SinkExecutor:
    ex = _REGISTRY.get(sink_kind)
    if ex is None:
        raise KeyError(f"no executor for sink_kind={sink_kind!r}")
    return ex


__all__ = ["ExecResult", "SinkExecutor", "get_executor"]

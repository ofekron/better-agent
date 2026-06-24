"""SinkExecutor contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecResult:
    """Result of running a frozen operation. Every field that could carry
    the secret is scanned by the output guard before this reaches a caller."""

    ok: bool
    status: Optional[int] = None  # protocol status (e.g. HTTP status code)
    body: str = ""  # response body returned to the caller
    stderr: str = ""  # diagnostic channel (also guard-scanned)
    error: str = ""  # error message if ok is False (also guard-scanned)


class SinkExecutor:
    """Executes a validated descriptor with the (transient) secret value.

    Implementations MUST keep the secret in-process: never write it to a log,
    never place it in a child-process environment, never return it. The
    `secret` argument is live plaintext — use it and let it fall out of
    scope; do not stash it.
    """

    kind: str = ""

    def execute(self, descriptor: dict, secret: str | dict[str, str]) -> ExecResult:  # pragma: no cover
        raise NotImplementedError

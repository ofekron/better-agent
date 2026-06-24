"""Output-echo guard.

The broker's result is the ONE thing that flows back to Claude. If an
operation's response (or its stderr / error message / status) contains any
bound secret value, returning it would leak the secret into Claude's context
and the event pipeline. So before any result leaves the broker, every text
field is scanned for every secret; on a hit the whole result is refused
(fail-closed) — we never try to redact-and-return, because partial
redaction is easy to get wrong.
"""

from __future__ import annotations

from credential_broker.executors.base import ExecResult


class OutputEchoError(Exception):
    """Raised when a result would echo the secret back to the caller."""


def _contains(haystack: str, secret: str) -> bool:
    return bool(secret) and secret in haystack


def guard(result: ExecResult, secret: str | dict[str, str]) -> ExecResult:
    """Return ``result`` unchanged if it does not contain any secret;
    otherwise raise OutputEchoError. Scans body, stderr, and error."""
    secrets = secret.values() if isinstance(secret, dict) else [secret]
    for field in (result.body, result.stderr, result.error):
        text = field or ""
        for value in secrets:
            if _contains(text, value):
                raise OutputEchoError(
                    "operation result contained a secret value; refused (fail-closed)"
                )
    return result

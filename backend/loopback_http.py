"""Transport for internal producers (runners, stdio MCP tool subprocesses)
calling the runtime's internal API.

Every request travels over the runtime app-endpoint descriptor
(`runtime_endpoints.read_app_endpoint()`) — the per-home unix socket on
POSIX, loopback TCP on Windows — never through the browser-facing BFF
port. Internal calls must not depend on the BFF being alive.
"""

from __future__ import annotations

import json
import random
import time
from typing import Callable

import internal_request_auth
import runtime_endpoints

_HTTP_ERROR_BODY_LIMIT = 4096

# Bounded jittered-exponential backoff for connection-level retries
# (e.g. an MCP tool call landing in the runtime's restart window).
# Mirrors the shape of _DISPATCH_RETRY_BACKOFF in jsonl_tailer.py and
# the runners' own _post_loopback_sync backoff.
_RETRY_BACKOFF: tuple[float, ...] = (0.1, 1.0, 5.0, 15.0, 30.0)
_RETRY_JITTER = 0.3

# Connection-level failures worth retrying: socket errors, and the
# descriptor being briefly absent while the runtime restarts.
LOOPBACK_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    runtime_endpoints.RuntimeEndpointError,
)


class LoopbackHTTPStatusError(RuntimeError):
    """Terminal HTTP >= 400 response from the runtime's internal API."""

    def __init__(self, status: int, body: bytes):
        super().__init__(f"HTTP {status}")
        self.code = status
        self.body = body


def loopback_http_error_message(e: LoopbackHTTPStatusError) -> str:
    text = e.body[:_HTTP_ERROR_BODY_LIMIT].decode("utf-8", "replace").strip()
    if text:
        try:
            data = json.loads(text)
        except Exception:
            return text
        if isinstance(data, dict):
            for key in ("detail", "error", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, (dict, list)):
                    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return text
    return str(e)


def raise_loopback_http_error(e: LoopbackHTTPStatusError) -> None:
    raise RuntimeError(f"HTTP {e.code}: {loopback_http_error_message(e)}") from e


def request_internal(
    method: str,
    url_path: str,
    body: bytes | None,
    *,
    internal_token: str,
    timeout: float,
) -> bytes:
    """One request to the runtime internal API over the app-endpoint
    descriptor. Raises ``LoopbackHTTPStatusError`` for HTTP >= 400 and
    one of ``LOOPBACK_RETRYABLE_ERRORS`` on connection/descriptor
    failure. Callers own their retry policy."""
    descriptor = runtime_endpoints.read_app_endpoint()
    headers = {"Content-Type": "application/json"}
    # HMAC request signing: `internal_token` is the signing key; the raw
    # secret never travels as a bearer header.
    headers.update(internal_request_auth.sign(internal_token, method, url_path, body))
    status, raw = runtime_endpoints.http_request(
        descriptor,
        method,
        url_path,
        body=body,
        headers=headers,
        timeout=timeout,
    )
    if status >= 400:
        raise LoopbackHTTPStatusError(status, raw)
    return raw


def loopback_request(
    method: str,
    url_path: str,
    body: bytes | None,
    *,
    internal_token: str,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """``request_internal`` with bounded jittered-exponential retry on
    connection failures. Status errors propagate immediately: a 4xx/5xx
    from a live runtime is a real error, not a transient outage."""
    last_exc: Exception | None = None
    for i, base in enumerate(_RETRY_BACKOFF):
        try:
            return request_internal(
                method, url_path, body,
                internal_token=internal_token, timeout=timeout,
            )
        except LoopbackHTTPStatusError:
            raise
        except LOOPBACK_RETRYABLE_ERRORS as exc:
            last_exc = exc
            if i + 1 == len(_RETRY_BACKOFF):
                break
            jitter = 1.0 + random.uniform(-_RETRY_JITTER, _RETRY_JITTER)
            sleep(base * jitter)
    assert last_exc is not None
    raise last_exc

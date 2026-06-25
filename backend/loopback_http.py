from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Callable

_HTTP_ERROR_BODY_LIMIT = 4096

# Bounded jittered-exponential backoff for connection-refused/URLError
# retries (e.g. an MCP tool call landing in the backend's restart
# window). Mirrors the shape of _DISPATCH_RETRY_BACKOFF in
# jsonl_tailer.py and the runner's own _post_loopback_sync backoff.
_RETRY_BACKOFF: tuple[float, ...] = (0.1, 1.0, 5.0, 15.0, 30.0)
_RETRY_JITTER = 0.3


def loopback_http_error_message(e: urllib.error.HTTPError) -> str:
    raw = b""
    try:
        raw = e.read(_HTTP_ERROR_BODY_LIMIT + 1)
    except Exception:
        raw = b""
    text = raw[:_HTTP_ERROR_BODY_LIMIT].decode("utf-8", "replace").strip()
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


def raise_loopback_http_error(e: urllib.error.HTTPError) -> None:
    raise RuntimeError(f"HTTP {e.code}: {loopback_http_error_message(e)}") from e


def loopback_urlopen(
    req: urllib.request.Request,
    *,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """``urlopen`` with bounded jittered-exponential retry on connection
    failures (``URLError``) — e.g. a stdio MCP server's loopback call landing
    in the backend's restart window. ``HTTPError`` propagates immediately: a
    4xx/5xx from a live backend is a real error, not a transient outage."""
    last_exc: urllib.error.URLError | None = None
    for i, base in enumerate(_RETRY_BACKOFF):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as exc:
            last_exc = exc
            if i + 1 == len(_RETRY_BACKOFF):
                break
            jitter = 1.0 + random.uniform(-_RETRY_JITTER, _RETRY_JITTER)
            sleep(base * jitter)
    assert last_exc is not None
    raise last_exc

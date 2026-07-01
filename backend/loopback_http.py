from __future__ import annotations

import json
import urllib.error

_HTTP_ERROR_BODY_LIMIT = 4096


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

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from paths import ba_home

_PREVIEW_LIMIT = 240
_ERROR_LIMIT = 500
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "total_tokens",
    "duration_ms",
)


def _log_path() -> Path:
    return ba_home() / "logs" / "llm_calls.jsonl"


def _preview(value: Any, limit: int = _PREVIEW_LIMIT) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", "\n")
    text = " ".join(part for part in text.split() if part)
    return text[:limit]


def _normalize_usage(usage: Any) -> dict[str, int | float]:
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int | float] = {}
    for key in _TOKEN_KEYS:
        value = usage.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            out[key] = value
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def append_call(
    *,
    source: str,
    reason: str,
    provider_id: Optional[str] = None,
    provider_kind: Optional[str] = None,
    provider_name: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    app_session_id: Optional[str] = None,
    provider_session_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    run_id: Optional[str] = None,
    prompt: Optional[str] = None,
    token_usage: Optional[dict] = None,
    success: Optional[bool] = None,
    error: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    timestamp: Optional[datetime | str] = None,
) -> dict:
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    record = {
        "id": f"llm_{uuid.uuid4().hex[:16]}",
        "timestamp": ts or datetime.now().isoformat(),
        "source": _preview(source, 80) or "unknown",
        "reason": _preview(reason, 160) or "unknown",
        "provider_id": _preview(provider_id, 120) or None,
        "provider_kind": _preview(provider_kind, 80) or None,
        "provider_name": _preview(provider_name, 120) or None,
        "model": _preview(model, 160) or None,
        "reasoning_effort": _preview(reasoning_effort, 80) or None,
        "app_session_id": _preview(app_session_id, 120) or None,
        "provider_session_id": _preview(provider_session_id, 180) or None,
        "trace_id": _preview(trace_id, 80) or None,
        "run_id": _preview(run_id, 120) or None,
        "prompt_preview": _preview(prompt),
        "token_usage": _normalize_usage(token_usage),
        "success": success if isinstance(success, bool) else None,
        "error": _preview(error, _ERROR_LIMIT) or None,
        "metadata": _sanitize_metadata(metadata or {}),
    }
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return record


def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, bool) or value is None:
            out[key[:80]] = value
        elif isinstance(value, (int, float)):
            out[key[:80]] = value
        else:
            out[key[:80]] = _preview(value, 160)
    return out


def iter_calls() -> Iterable[dict]:
    path = _log_path()
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                yield data

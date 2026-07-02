"""Dispatch a provisioned-session fork and extract its reply text.

Two dispatch modes:
  - `http`        — POST /api/internal/ask-fork (the provisioned session may
                    live in another process; needs an internal token).
  - `in_process`  — call `coordinator.run_delegation` directly (the framework
                    runs inside the backend; no token, no loopback).

Both return the same result-payload shape (`success`, `error`, `jsonl_path`,
byte offsets, `sdk_output`), so `extract_fork_text` is shared.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from typing import Any

import httpx

from provisioning.config import ProvisionedConfig
from provisioning.spec import ProvisionedSessionSpec


async def dispatch(
    spec: ProvisionedSessionSpec,
    cfg: ProvisionedConfig,
    *,
    base_session_id: str,
    caller_session_id: str,
    instructions: str,
    provision_prompt: str,
) -> dict:
    """Run one fork off the provisioned base. Returns the result payload."""
    if cfg.dispatch == "http":
        return await _dispatch_http(
            spec, cfg,
            base_session_id=base_session_id,
            caller_session_id=caller_session_id,
            instructions=instructions,
            provision_prompt=provision_prompt,
        )
    return await _dispatch_in_process(
        spec, cfg,
        base_session_id=base_session_id,
        caller_session_id=caller_session_id,
        instructions=instructions,
        provision_prompt=provision_prompt,
    )


# ── http ──────────────────────────────────────────────────────────────

async def _dispatch_http(
    spec: ProvisionedSessionSpec,
    cfg: ProvisionedConfig,
    *,
    base_session_id: str,
    caller_session_id: str,
    instructions: str,
    provision_prompt: str,
) -> dict:
    if not cfg.internal_token:
        raise RuntimeError(f"{spec.env_prefix} http dispatch needs an internal token")
    payload = {
        "app_session_id": caller_session_id,
        "instructions": instructions,
        "worker_session_id": base_session_id,
        "worker_description": cfg.worker_description,
        "model": cfg.model,
        "cwd": cfg.cwd,
        "client_delegation_id": f"{spec.key}_{uuid.uuid4().hex[:10]}",
        "run_mode": cfg.run_mode,
        "worker_registry_cwd": cfg.cwd,
        "ephemeral": cfg.run_mode == "fork" and spec.ephemeral_forks,
        "machine_completion": spec.machine_completion,
        "provision_prompt": provision_prompt,
        "include_events": True,
    }
    last_error = f"{spec.key} provisioned dispatch failed"
    for attempt in range(1, spec.retry_attempts + 1):
        started = time.monotonic()
        try:
            result = await _post_ask_fork(cfg, payload, timeout=spec.effective_dispatch_timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            duration = time.monotonic() - started
            last_error = type(exc).__name__
            _log_attempt(spec, cfg, attempt, duration, error=last_error)
            if attempt == spec.retry_attempts:
                raise
            await _sleep_before_retry(spec, attempt, last_error)
            continue
        duration = time.monotonic() - started
        success = bool(result.get("success"))
        last_error = str(result.get("error") or last_error)
        _log_attempt(
            spec, cfg, attempt, duration,
            status=200 if success else 500,
        )
        if success:
            return result
        if attempt == spec.retry_attempts or not _error_is_transient(result):
            raise RuntimeError(last_error)
        await _sleep_before_retry(spec, attempt, last_error)
    raise RuntimeError(last_error)


async def _post_ask_fork(
    cfg: ProvisionedConfig, payload: dict, *, timeout: float
) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            cfg.backend_url.rstrip("/") + "/api/internal/ask-fork",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": cfg.internal_token,
            },
        )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


# ── in_process ────────────────────────────────────────────────────────

async def _dispatch_in_process(
    spec: ProvisionedSessionSpec,
    cfg: ProvisionedConfig,
    *,
    base_session_id: str,
    caller_session_id: str,
    instructions: str,
    provision_prompt: str,
) -> dict:
    from main import coordinator as _coordinator
    return await _coordinator.run_delegation(
        app_session_id=caller_session_id,
        instructions=instructions,
        worker_session_id=base_session_id,
        worker_description=cfg.worker_description,
        model=cfg.model,
        cwd=cfg.cwd,
        client_delegation_id=f"{spec.key}_{uuid.uuid4().hex[:10]}",
        run_mode=cfg.run_mode,
        worker_registry_cwd=cfg.cwd,
        ephemeral=cfg.run_mode == "fork" and spec.ephemeral_forks,
        machine_completion=spec.machine_completion,
        provision_prompt=provision_prompt,
        include_events=True,
    )


# ── shared helpers ────────────────────────────────────────────────────

def _error_is_transient(result: dict) -> bool:
    text = " ".join(
        str(result.get(key) or "") for key in ("error", "sdk_output")
    ).lower()
    return any(
        marker in text
        for marker in (
            "server_error", "temporarily overloaded", "try again", "timeout",
            "timed out", "connection refused", "connection reset", "529", " 5xx",
        )
    )


async def _sleep_before_retry(spec: ProvisionedSessionSpec, attempt: int, reason: str) -> None:
    delay = spec.retry_backoff[min(attempt - 1, len(spec.retry_backoff) - 1)]
    print(
        f"{spec.key} provisioned dispatch transient failure "
        f"attempt={attempt}/{spec.retry_attempts}; retrying in {delay:g}s: {reason}",
        file=sys.stderr, flush=True,
    )
    await asyncio.sleep(delay)


def _log_attempt(
    spec: ProvisionedSessionSpec,
    cfg: ProvisionedConfig,
    attempt: int,
    duration_s: float,
    *,
    status: int | None = None,
    error: str | None = None,
) -> None:
    row = {
        "event": "provisioned_dispatch",
        "spec": spec.key,
        "ts": time.time(),
        "attempt": attempt,
        "max_attempts": spec.retry_attempts,
        "model": cfg.model,
        "duration_s": round(duration_s, 3),
    }
    if status is not None:
        row["status"] = status
    if error is not None:
        row["error"] = error
    parts = [f"{spec.key}", f"attempt={attempt}/{spec.retry_attempts}", f"model={cfg.model}"]
    if status is not None:
        parts.append(f"status={status}")
    if error is not None:
        parts.append(f"error={error}")
    parts.append(f"duration_s={row['duration_s']:.3f}")
    print(" ".join(parts), file=sys.stderr, flush=True)
    import os
    path = os.environ.get(f"{spec.env_prefix}_PERF_PATH")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ── reply extraction ──────────────────────────────────────────────────

def extract_fork_text(result: dict) -> str:
    """Pull the fork's assistant text out of the result payload: prefer
    `sdk_output`, else read the jsonl byte window the engine returned."""
    sdk_output = result.get("sdk_output")
    if isinstance(sdk_output, str) and sdk_output.strip():
        return sdk_output.strip()
    path = result.get("jsonl_path")
    if not isinstance(path, str) or not path:
        return ""
    start = max(0, int(result.get("new_byte_offset") or 1) - 1)
    end = int(result.get("total_bytes_now") or 0)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            raw = f.read(max(0, end - start) if end > start else -1)
    except OSError:
        return ""
    return _extract_text_from_jsonl(raw.decode("utf-8", errors="replace"), path)


def _extract_text_from_jsonl(raw: str, namespace: str) -> str:
    candidates: list[str] = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        extracted = _extract_assistant_text_from_row(row)
        if extracted:
            candidates.append(extracted)
    if candidates:
        return candidates[-1].strip()
    return _extract_text_via_normalizer(raw, namespace)


def _extract_text_via_normalizer(raw: str, namespace: str) -> str:
    try:
        from codex_native import CodexRolloutNormalizer
        from event_shape import extract_output_text
    except Exception:
        return ""
    normalizer = CodexRolloutNormalizer(namespace=f"provisioning:{namespace}")
    events: list[dict] = []
    for line in raw.splitlines():
        for event in normalizer.normalize_line(line):
            events.append({"type": "agent_message", "data": event})
    return extract_output_text(events)


def _extract_assistant_text_from_row(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    if row.get("type") == "event_msg" and payload.get("type") == "agent_message":
        message = payload.get("message")
        return message if isinstance(message, str) else ""
    if row.get("type") == "response_item" and payload.get("type") == "message":
        if payload.get("role") != "assistant":
            return ""
        return _text_from_content(payload.get("content"))
    if row.get("type") == "assistant":
        return _text_from_content((row.get("message") or {}).get("content"))
    return ""


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or block.get("input_text")
        if isinstance(text, str):
            parts.append(text)
    return " ".join(parts).strip()

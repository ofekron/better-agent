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
import logging
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import delegation_status_store
import httpx
from runs_dir import read_best_complete, runs_root

from provisioning.config import ProvisionedConfig
from provisioning.spec import ProvisionedSessionSpec

logger = logging.getLogger(__name__)

_AUTHORIZED_TOOL_PROFILE_TTL_SECONDS = 900.0
_AUTHORIZED_TOOL_PROFILE_DISPATCHES: dict[str, tuple[str, float]] = {}
_AUTHORIZED_TOOL_PROFILE_LOCK = threading.Lock()


def authorize_tool_profile_dispatch(client_delegation_id: str, profile: str) -> None:
    client_delegation_id = str(client_delegation_id or "").strip()
    profile = str(profile or "").strip()
    if not client_delegation_id or not profile:
        return
    with _AUTHORIZED_TOOL_PROFILE_LOCK:
        _cleanup_authorized_tool_profiles_locked(time.monotonic())
        _AUTHORIZED_TOOL_PROFILE_DISPATCHES[client_delegation_id] = (
            profile,
            time.monotonic() + _AUTHORIZED_TOOL_PROFILE_TTL_SECONDS,
        )


def is_authorized_tool_profile_dispatch(client_delegation_id: str, profile: str) -> bool:
    client_delegation_id = str(client_delegation_id or "").strip()
    profile = str(profile or "").strip()
    if not client_delegation_id or not profile:
        return False
    with _AUTHORIZED_TOOL_PROFILE_LOCK:
        _cleanup_authorized_tool_profiles_locked(time.monotonic())
        entry = _AUTHORIZED_TOOL_PROFILE_DISPATCHES.get(client_delegation_id)
    return bool(entry and entry[0] == profile)


def _cleanup_authorized_tool_profiles_locked(now: float) -> None:
    stale = [
        client_delegation_id
        for client_delegation_id, (_profile, expires_at) in _AUTHORIZED_TOOL_PROFILE_DISPATCHES.items()
        if expires_at <= now
    ]
    for client_delegation_id in stale:
        _AUTHORIZED_TOOL_PROFILE_DISPATCHES.pop(client_delegation_id, None)


def client_delegation_id_for_request(spec_key: str, request_id: str) -> str:
    safe = "".join(ch for ch in str(request_id or "") if ch.isalnum() or ch in ("-", "_"))
    if not safe:
        return f"{spec_key}_{uuid.uuid4().hex[:10]}"
    return f"{spec_key}_{safe[:64]}"


async def dispatch(
    spec: ProvisionedSessionSpec,
    cfg: ProvisionedConfig,
    *,
    base_session_id: str,
    caller_session_id: str,
    instructions: str,
    provision_prompt: str,
    client_delegation_id: str = "",
) -> dict:
    """Run one fork off the provisioned base. Returns the result payload."""
    if cfg.dispatch == "http":
        return await _dispatch_http(
            spec, cfg,
            base_session_id=base_session_id,
            caller_session_id=caller_session_id,
            instructions=instructions,
            provision_prompt=provision_prompt,
            client_delegation_id=client_delegation_id,
        )
    return await _dispatch_in_process(
        spec, cfg,
        base_session_id=base_session_id,
        caller_session_id=caller_session_id,
        instructions=instructions,
        provision_prompt=provision_prompt,
        client_delegation_id=client_delegation_id,
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
    client_delegation_id: str = "",
) -> dict:
    if not cfg.internal_token:
        raise RuntimeError(f"{spec.env_prefix} http dispatch needs an internal token")
    client_delegation_id = client_delegation_id or client_delegation_id_for_request(spec.key, "")
    authorize_tool_profile_dispatch(client_delegation_id, spec.tool_profile)
    payload = {
        "app_session_id": caller_session_id,
        "instructions": instructions,
        "worker_session_id": base_session_id,
        "worker_description": cfg.worker_description,
        "model": cfg.model,
        "cwd": cfg.cwd,
        "client_delegation_id": client_delegation_id,
        "run_mode": cfg.run_mode,
        "worker_registry_cwd": cfg.cwd,
        "ephemeral": cfg.run_mode == "fork" and spec.ephemeral_forks,
        "machine_completion": spec.machine_completion,
        "provision_prompt": provision_prompt,
        "provisioned_tool_profile": spec.tool_profile,
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
        except httpx.HTTPStatusError as exc:
            duration = time.monotonic() - started
            status_code = exc.response.status_code
            last_error = f"HTTPStatusError:{status_code}"
            _log_attempt(spec, cfg, attempt, duration, error=last_error)
            # Only server-side failures are transient; 4xx (incl. 429 — the
            # rate-limit message path owns that) fails fast.
            if status_code < 500 or attempt == spec.retry_attempts:
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
    client_delegation_id: str = "",
) -> dict:
    from main import coordinator as _coordinator
    return await _coordinator.run_delegation(
        app_session_id=caller_session_id,
        instructions=instructions,
        worker_session_id=base_session_id,
        worker_description=cfg.worker_description,
        model=cfg.model,
        cwd=cfg.cwd,
        client_delegation_id=client_delegation_id or client_delegation_id_for_request(spec.key, ""),
        run_mode=cfg.run_mode,
        worker_registry_cwd=cfg.cwd,
        ephemeral=cfg.run_mode == "fork" and spec.ephemeral_forks,
        machine_completion=spec.machine_completion,
        provision_prompt=provision_prompt,
        provisioned_tool_profile=spec.tool_profile,
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


def recover_delegation_result(client_delegation_id: str) -> dict[str, Any] | None:
    status = delegation_status_store.read_status(str(client_delegation_id or ""))
    if not isinstance(status, dict):
        return None
    result = status.get("result")
    if isinstance(result, dict) and result.get("success"):
        return result
    run_dir_value = status.get("provider_run_dir")
    if not isinstance(run_dir_value, str) or not run_dir_value:
        return None
    run_dir = _owned_run_dir(run_dir_value)
    if run_dir is None:
        return None
    complete = read_best_complete(run_dir)
    if not isinstance(complete, dict) or not complete.get("success"):
        return None
    sdk_output = complete.get("sdk_output")
    if not isinstance(sdk_output, str) or not sdk_output.strip():
        sdk_output = complete.get("final_assistant_text")
    if not isinstance(sdk_output, str) or not sdk_output.strip():
        return None
    return {
        "success": True,
        "worker_session_id": status.get("worker_agent_session_id"),
        "worker_description": status.get("worker_description"),
        "fork_agent_sid": complete.get("session_id") or status.get("fork_agent_sid"),
        "run_mode": status.get("run_mode"),
        "ephemeral": status.get("ephemeral"),
        "jsonl_path": status.get("jsonl_path"),
        "new_byte_offset": status.get("new_byte_offset") or 1,
        "total_bytes_now": status.get("total_bytes_now") or 0,
        "token_usage": complete.get("token_usage"),
        "sdk_output": sdk_output,
    }


def request_delegation_cancel(client_delegation_id: str) -> bool:
    delegation_id = str(client_delegation_id or "").strip()
    if not delegation_id:
        return False
    status = delegation_status_store.read_status(delegation_id)
    if isinstance(status, dict) and _delegation_status_terminal(status):
        return False
    delegation_status_store.write_status(
        delegation_id,
        cancel_requested=True,
        cancel_requested_at=time.time(),
    )
    status = delegation_status_store.read_status(delegation_id)
    if not isinstance(status, dict):
        return False
    return _cancel_delegation_status_run(status)


def _delegation_status_terminal(status: dict[str, Any]) -> bool:
    return status.get("status") == "complete" or isinstance(status.get("result"), dict)


def _cancel_delegation_status_run(status: dict[str, Any]) -> bool:
    run_id = status.get("provider_run_id")
    if not isinstance(run_id, str) or not run_id:
        return False
    run_dir_value = status.get("provider_run_dir")
    run_dir = _owned_run_dir(run_dir_value) if isinstance(run_dir_value, str) else None
    if run_dir is None or run_dir.name != run_id:
        return False
    provider_id = status.get("provider_id")
    if not isinstance(provider_id, str) or not provider_id:
        provider_id = _provider_id_from_run_dir(run_dir)
    if isinstance(provider_id, str) and provider_id:
        try:
            from provider import get_provider

            provider = get_provider(provider_id)
            if provider.cancel_turn(run_id):
                return True
        except Exception:
            logger.debug(
                "request_delegation_cancel provider cancel failed run_id=%s provider_id=%s",
                run_id,
                provider_id,
                exc_info=True,
            )
    if run_dir is None:
        return False
    try:
        (run_dir / "cancel").touch()
    except OSError:
        logger.debug("request_delegation_cancel sentinel write failed run_id=%s", run_id, exc_info=True)
        return False
    return True


def _provider_id_from_run_dir(run_dir: Path | None) -> str:
    if run_dir is None:
        return ""
    try:
        data = json.loads((run_dir / "backend_state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    provider_id = data.get("provider_id") if isinstance(data, dict) else ""
    return provider_id if isinstance(provider_id, str) else ""


def _owned_run_dir(value: str) -> Path | None:
    try:
        path = Path(value).resolve()
        root = runs_root().resolve()
    except OSError:
        return None
    if path == root or root in path.parents:
        return path
    return None


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

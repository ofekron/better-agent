"""The public entry point: run one provisioned-session fork.

`run(spec, query, ctx)` resolves config, ensures a clean primed base +
caller, dispatches one fork carrying only the per-call instructions, and
returns the extracted reply text plus the spec-parsed value.

`run_sync` is the sync wrapper for out-of-process callers (e.g. the
requirement-analysis pipeline) — it drives the coroutine on a private loop
in a worker thread so it works whether or not the caller already has a loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
import threading
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from queue import SimpleQueue
from typing import Any

import perf
from provisioning.config import ProvisionedConfig, resolve_config
from provisioning.dispatch import (
    client_delegation_id_for_request,
    dispatch,
    extract_fork_text,
)
from provisioning.lifecycle import ensure_caller, ensure_session
from provisioning.spec import ProvisionedSessionSpec

_LIFECYCLE_LOCKS: dict[tuple[str, str, str, str, str], threading.Lock] = {}
_LIFECYCLE_LOCKS_GUARD = threading.Lock()
logger = logging.getLogger(__name__)


@dataclass
class ProvisionedResult:
    text: str            # raw fork reply text
    value: Any           # spec.parse_result(text, ctx)
    config: ProvisionedConfig
    base_session_id: str
    caller_session_id: str
    dispatch_result: dict


async def ensure_warm_base(
    spec: ProvisionedSessionSpec,
    cfg: ProvisionedConfig,
    ctx: dict | None = None,
) -> str:
    """Return a provisioned base session whose provider sid is initialized.

    `ensure_session` owns find/create/recycle. This helper adds the missing
    warm step for callers that need to mint their own user-facing forks rather
    than dispatch through `run(...)`: if the base has no provider sid yet, run
    the spec's one-time provision prompt through the normal target-init path.
    """
    ctx = dict(ctx or {})
    async with _async_acquired_lifecycle_lock(spec, cfg):
        return await _ensure_ready_base_locked(spec, cfg, ctx)


async def run(
    spec: ProvisionedSessionSpec,
    query: str,
    ctx: dict | None = None,
    *,
    model: str | None = None,
) -> ProvisionedResult:
    """Provision-and-fork `query` through `spec`. Raises on dispatch failure
    (after spec.retries). Parse-level failures are the spec's to express in
    `value` (e.g. a `{error: ...}` payload)."""
    ctx = dict(ctx or {})
    with perf.timed(f"provisioning.{spec.key}.resolve_config"):
        cfg = resolve_config(spec, model=model)
    ctx.setdefault("worker_description", cfg.worker_description)
    with perf.timed(f"provisioning.{spec.key}.ensure_lifecycle"):
        base_session_id, caller_session_id = await _ensure_ready_lifecycle(
            spec, cfg, ctx,
        )
    debug_request_id = _debug_request_id(ctx)
    client_delegation_id = client_delegation_id_for_request(
        spec.key,
        debug_request_id,
    )
    if debug_request_id:
        ctx["client_delegation_id"] = client_delegation_id
    if debug_request_id:
        logger.info(
            "provisioned_dispatch_start spec=%s request_id=%s base_session_id=%s "
            "caller_session_id=%s provider_id=%s model=%s",
            spec.key,
            debug_request_id,
            base_session_id,
            caller_session_id,
            cfg.provider_id,
            cfg.model,
        )

    with perf.timed(f"provisioning.{spec.key}.build_prompts"):
        instructions = spec.build_instructions(query, ctx)
        provision_prompt = spec.build_provision_prompt(ctx)
    try:
        with perf.timed(f"provisioning.{spec.key}.dispatch"):
            result = await dispatch(
                spec, cfg,
                base_session_id=base_session_id,
                caller_session_id=caller_session_id,
                instructions=instructions,
                provision_prompt=provision_prompt,
                client_delegation_id=client_delegation_id,
            )
    except Exception as exc:
        if debug_request_id:
            logger.warning(
                "provisioned_dispatch_failed spec=%s request_id=%s base_session_id=%s "
                "caller_session_id=%s provider_id=%s model=%s error_type=%s",
                spec.key,
                debug_request_id,
                base_session_id,
                caller_session_id,
                cfg.provider_id,
                cfg.model,
                type(exc).__name__,
            )
        raise
    if debug_request_id:
        logger.info(
            "provisioned_dispatch_returned spec=%s request_id=%s base_session_id=%s "
            "caller_session_id=%s provider_session_id=%s success=%s",
            spec.key,
            debug_request_id,
            base_session_id,
            caller_session_id,
            _dispatch_provider_session_id(result),
            bool(result.get("success")),
        )
    if not result.get("success"):
        raise RuntimeError(
            str(result.get("error") or f"{spec.key} provisioned dispatch failed")
        )
    with perf.timed(f"provisioning.{spec.key}.extract_fork_text"):
        text = extract_fork_text(result)
    with perf.timed(f"provisioning.{spec.key}.parse_result"):
        value = spec.parse_result(text, ctx)
    return ProvisionedResult(
        text=text,
        value=value,
        config=cfg,
        base_session_id=base_session_id,
        caller_session_id=caller_session_id,
        dispatch_result=result,
    )


def _lifecycle_lock(spec: ProvisionedSessionSpec, cfg: ProvisionedConfig) -> threading.Lock:
    key = (spec.key, cfg.cwd, cfg.provider_id, cfg.model, cfg.node_id)
    with _LIFECYCLE_LOCKS_GUARD:
        lock = _LIFECYCLE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LIFECYCLE_LOCKS[key] = lock
        return lock


async def _ensure_ready_lifecycle(
    spec: ProvisionedSessionSpec,
    cfg: ProvisionedConfig,
    ctx: dict,
) -> tuple[str, str]:
    async with _async_acquired_lifecycle_lock(spec, cfg):
        base_session_id = await _ensure_ready_base_locked(spec, cfg, ctx)
        with perf.timed(f"provisioning.{spec.key}.ensure_caller"):
            caller_session_id = await asyncio.to_thread(ensure_caller, spec, cfg)
    return base_session_id, caller_session_id


async def _ensure_ready_base_locked(
    spec: ProvisionedSessionSpec,
    cfg: ProvisionedConfig,
    ctx: dict,
) -> str:
    with perf.timed(f"provisioning.{spec.key}.ensure_session"):
        base_session_id = await asyncio.to_thread(ensure_session, spec, cfg)
    try:
        from session_manager import manager as session_manager
    except Exception as exc:
        raise RuntimeError("provisioning cannot load base session") from exc
    base = await asyncio.to_thread(session_manager.get, base_session_id) or {}
    if base.get("agent_session_id"):
        return base_session_id

    with perf.timed(f"provisioning.{spec.key}.warm_base"):
        from main import coordinator as _coordinator

        cancel_event = asyncio.Event()
        _coordinator.init_cancel_events[base_session_id] = (
            "__provisioning__",
            cancel_event,
        )
        try:
            agent_sid = await _coordinator._init_target_agent_session(
                bc_session=base,
                model=cfg.model,
                cwd=cfg.cwd,
                description=cfg.worker_description,
                cancel_event=cancel_event,
                provision_prompt=spec.build_provision_prompt(ctx),
                provisioned_tool_profile=spec.tool_profile,
            )
        finally:
            _coordinator.init_cancel_events.pop(base_session_id, None)
    if not agent_sid:
        raise RuntimeError(f"{spec.key} base did not initialize")

    current = await asyncio.to_thread(session_manager.get, base_session_id) or {}
    current_sid = str(current.get("agent_session_id") or "").strip()
    if current_sid:
        return base_session_id
    persist_task = asyncio.create_task(asyncio.to_thread(
        session_manager.set_agent_sid,
        base_session_id,
        spec.orchestration_mode,
        agent_sid,
        provider_id=cfg.provider_id,
        model=cfg.model,
    ))
    try:
        await asyncio.shield(persist_task)
    except asyncio.CancelledError:
        await asyncio.shield(persist_task)
        raise
    persisted = await asyncio.to_thread(session_manager.get, base_session_id) or {}
    if str(persisted.get("agent_session_id") or "").strip() != agent_sid:
        raise RuntimeError(f"{spec.key} base provider session was not persisted")
    return base_session_id


def _debug_request_id(ctx: dict | None) -> str:
    if not isinstance(ctx, dict):
        return ""
    value = ctx.get("_debug_request_id")
    return value if isinstance(value, str) else ""


def _dispatch_provider_session_id(result: dict) -> str:
    for key in ("session_id", "provider_session_id", "agent_session_id", "fork_agent_sid"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""




@asynccontextmanager
async def _async_acquired_lifecycle_lock(
    spec: ProvisionedSessionSpec, cfg: ProvisionedConfig,
):
    lock = _lifecycle_lock(spec, cfg)
    timeout = max(0.0, float(spec.provision_timeout))
    acquired = await asyncio.to_thread(lock.acquire, True, timeout)
    if not acquired:
        raise TimeoutError(
            f"{spec.key} provisioned lifecycle lock timed out after {timeout:g}s"
        )
    try:
        yield
    finally:
        lock.release()

@contextmanager
def _acquired_lifecycle_lock(spec: ProvisionedSessionSpec, cfg: ProvisionedConfig):
    lock = _lifecycle_lock(spec, cfg)
    timeout = max(0.0, float(spec.provision_timeout))
    acquired = lock.acquire(timeout=timeout)
    if not acquired:
        raise TimeoutError(
            f"{spec.key} provisioned lifecycle lock timed out after {timeout:g}s"
        )
    try:
        yield
    finally:
        lock.release()


def _sync_timeout_seconds(spec: ProvisionedSessionSpec) -> float:
    """Total run_sync budget = lifecycle phase (provision_timeout) + one
    dispatch budget per attempt + retry backoff + slack. Sizing the total as
    provision_timeout alone starves dispatch, whose per-attempt allowance is
    itself up to provision_timeout."""
    backoff = sum(float(delay) for delay in spec.retry_backoff[: max(0, spec.retry_attempts - 1)])
    dispatch_budget = spec.effective_dispatch_timeout * max(1, spec.retry_attempts)
    return max(0.0, float(spec.provision_timeout) + dispatch_budget + backoff + 0.5)


def run_sync(
    spec: ProvisionedSessionSpec,
    query: str,
    ctx: dict | None = None,
    *,
    model: str | None = None,
) -> ProvisionedResult:
    """Sync entry point — runs `run(...)` on a private loop in a worker
    thread, so callers without an event loop (or already inside one) both work."""
    results: SimpleQueue[tuple[str, Any]] = SimpleQueue()

    def _target() -> None:
        try:
            results.put(("value", asyncio.run(run(spec, query, ctx, model=model))))
        except BaseException as exc:  # noqa: BLE001 — re-raised on join
            results.put(("error", exc))

    t = threading.Thread(target=_target, name=f"provisioning-{spec.key}")
    t.daemon = True
    t.start()
    timeout = _sync_timeout_seconds(spec)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not results.empty():
            kind, value = results.get()
            if kind == "error":
                raise value
            return value
        t.join(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
    raise TimeoutError(f"{spec.key} provisioned run timed out after {timeout:g}s")

"""Worker-pool machinery: the tag-scoped prompt queue and its per-tag
dispatch processor, idle/affinity target selection, the blocking
ask-result handoff, and the `<worker_pool>` peer context injected into a
pool worker's provisioning prompt.

A pool is addressed by tag, never by session id, so everything here is
selection + queueing over the worker registry; worker CRUD itself lives
in `workers_api`. The coordinator is injected by the composition root
(see `configure`).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from html import escape
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

import internal_guards
from communication_modes import IN_TURN_REPLY_INSTRUCTION
from provider_validation import api_optional_pool_affinity_key as _api_optional_pool_affinity_key
from session_helpers import session_lite as _session_lite
from session_manager import manager as session_manager

router = APIRouter()
logger = logging.getLogger(__name__)

_coordinator_ref: Any = None


def configure(*, coordinator: Any) -> None:
    """Bind the collaborators this router needs."""
    global _coordinator_ref
    _coordinator_ref = coordinator


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="worker-pool API is not configured")
    return _coordinator_ref


_POOL_PROCESSORS: dict[str, asyncio.Task] = {}
_POOL_ASK_WAITERS: dict[str, list[asyncio.Future]] = {}


def _normalize_pool_context_tags(value) -> list[str]:
    from stores import worker_store as _ws

    return _ws.normalize_tags(value)


def _pool_worker_specs_for_prompt(specs: list, default_cwd: str) -> list[dict]:
    out: list[dict] = []
    for raw in specs:
        if not isinstance(raw, dict):
            continue
        tags = _normalize_pool_context_tags(raw.get("tags"))
        if not tags:
            continue
        key = str(raw.get("role_key") or raw.get("description") or "").strip()
        if not key:
            continue
        out.append({
            "name": f"worker:{key}",
            "description": str(raw.get("description") or f"worker:{key}").strip(),
            "cwd": str(raw.get("cwd") or default_cwd).strip(),
            "orchestration_mode": str(raw.get("orchestration_mode") or "native").strip(),
            "tags": tags,
        })
    return out


def _pool_worker_context_for_prompt(*, body: dict, bc_session_id: str, description: str) -> str:
    tags = _normalize_pool_context_tags(body.get("tags"))
    if not tags:
        return ""
    peers_by_name: dict[str, dict] = {}
    for worker in body.get("pool_worker_specs") or []:
        if not set(tags).intersection(_normalize_pool_context_tags(worker.get("tags"))):
            continue
        peers_by_name[str(worker.get("name") or "")] = worker
    from stores import worker_store as _ws

    for worker in _ws.list_workers(""):
        worker_tags = _normalize_pool_context_tags(worker.get("tags"))
        if not set(tags).intersection(worker_tags):
            continue
        name = str(worker.get("name") or worker.get("agent_session_id") or "").strip()
        if not name:
            continue
        peers_by_name[name] = {
            "name": name,
            "description": name,
            "cwd": str(worker.get("cwd") or "").strip(),
            "orchestration_mode": str(worker.get("orchestration_mode") or "native").strip(),
            "tags": worker_tags,
            "agent_session_id": str(worker.get("agent_session_id") or "").strip(),
        }
    lines = [
        "<worker_pool>",
        f"<self session_id=\"{escape(bc_session_id, quote=True)}\" "
        f"description=\"{escape(description, quote=True)}\" "
        f"tags=\"{escape(', '.join(tags), quote=True)}\" />",
        "<peers>",
    ]
    for peer in sorted(peers_by_name.values(), key=lambda item: str(item.get("name") or "")):
        lines.append(
            "<peer "
            f"name=\"{escape(str(peer.get('name') or ''), quote=True)}\" "
            f"session_id=\"{escape(str(peer.get('agent_session_id') or ''), quote=True)}\" "
            f"cwd=\"{escape(str(peer.get('cwd') or ''), quote=True)}\" "
            f"mode=\"{escape(str(peer.get('orchestration_mode') or 'native'), quote=True)}\" "
            f"tags=\"{escape(', '.join(_normalize_pool_context_tags(peer.get('tags'))), quote=True)}\" "
            f"description=\"{escape(str(peer.get('description') or ''), quote=True)}\" "
            "/>"
        )
    lines.extend([
        "</peers>",
        "<messaging>",
        "Use mssg(target_session_id, message) to coordinate with pool peers that have a session_id.",
        IN_TURN_REPLY_INSTRUCTION,
        "Use inbox(recipient_session_id, message) to return final results requested by async ask or delegate_task.",
        "Call inbox() to read your own pending results.",
        "</messaging>",
        "</worker_pool>",
    ])
    return "\n".join(lines)


@router.post("/api/internal/worker-pools/enqueue")
async def internal_enqueue_worker_pool_prompt(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_role_internal('team-orchestration')

    tag = str((body or {}).get("tag") or "").strip()
    sender_session_id = str((body or {}).get("sender_session_id") or "").strip()
    prompt = str((body or {}).get("prompt") or "").strip()
    if not tag or not sender_session_id or not prompt:
        raise HTTPException(status_code=400, detail="tag, sender_session_id, and prompt are required")
    if not await _session_lite(sender_session_id):
        raise HTTPException(status_code=404, detail="sender_session_id does not exist")
    try:
        queued = await _enqueue_worker_pool_message(
            tag=tag,
            sender_session_id=sender_session_id,
            prompt=prompt,
            expect_inbox_response=bool((body or {}).get("expect_inbox_response")),
            pool_affinity_key=_api_optional_pool_affinity_key((body or {}).get("pool_affinity_key")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **queued}


async def _enqueue_worker_pool_message(
    *,
    tag: str,
    sender_session_id: str,
    prompt: str,
    expect_inbox_response: bool,
    pool_affinity_key: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    wait_for_ask_response: bool = False,
    ask_id: str = "",
) -> dict:
    import team_messaging
    from stores import worker_store as _ws

    if expect_inbox_response:
        sender = await _session_lite(sender_session_id)
        if not sender:
            raise ValueError("sender_session_id does not exist")
        team_messaging.validate_inbox_response_session("sender", sender)

    item = {
        "id": str(uuid.uuid4()),
        "tag": tag,
        "sender_session_id": sender_session_id,
        "prompt": prompt,
        "expect_inbox_response": expect_inbox_response,
        "pool_affinity_key": pool_affinity_key,
        "provider_id": provider_id,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "runner": runner,
        "wait_for_ask_response": wait_for_ask_response,
        "ask_id": ask_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    queued = await asyncio.to_thread(_ws.enqueue_pool_task, tag, item)
    _ensure_worker_pool_processor(tag)
    await _coordinator().broadcast_workers_changed(None)
    return queued


def _ensure_worker_pool_processor(tag: str) -> None:
    clean = str(tag or "").strip()
    if not clean:
        return
    task = _POOL_PROCESSORS.get(clean)
    if task is None or task.done():
        _POOL_PROCESSORS[clean] = asyncio.create_task(
            _process_worker_pool_queue(clean),
            name=f"worker-pool-{clean}",
        )


async def _process_worker_pool_queue(tag: str) -> None:
    from stores import worker_store as _ws

    while True:
        item = await asyncio.to_thread(_ws.peek_pool_task, tag)
        if not item:
            return
        target = await asyncio.to_thread(
            _pick_pool_worker_for_sender,
            tag,
            str(item.get("sender_session_id") or ""),
            str(item.get("pool_affinity_key") or ""),
            True,
        )
        if not target:
            await asyncio.sleep(1)
            continue
        try:
            if item.get("wait_for_ask_response"):
                result = await _coordinator().ask_team_message(
                    sender_session_id=str(item.get("sender_session_id") or ""),
                    target_session_id=target["agent_session_id"],
                    message=str(item.get("prompt") or ""),
                    ask_id=str(item.get("ask_id") or ""),
                    provider_id=str(item.get("provider_id") or ""),
                    model=str(item.get("model") or ""),
                    reasoning_effort=str(item.get("reasoning_effort") or ""),
                    runner=str(item.get("runner") or ""),
                    target_selector={
                        "kind": "pool",
                        "value": tag,
                        "pool_affinity_key": str(item.get("pool_affinity_key") or ""),
                    },
                )
                # Pop and broadcast BEFORE waking any waiter: a caller unblocked
                # by _complete_pool_ask_waiters can synchronously check pool
                # state, so the item must already be gone by the time it wakes.
                await asyncio.to_thread(_ws.pop_pool_task, tag, str(item.get("id") or ""))
                await _coordinator().broadcast_workers_changed(None)
                ask_id = str(item.get("ask_id") or "")
                if ask_id:
                    import ask_status_store

                    await ask_status_store.write_status_async(ask_id, result=result)
                _complete_pool_ask_waiters(ask_id, result)
                continue
            else:
                await _coordinator().submit_team_message(
                    sender_session_id=str(item.get("sender_session_id") or ""),
                    target_session_id=target["agent_session_id"],
                    message=str(item.get("prompt") or ""),
                    detach=True,
                    expect_inbox_response=bool(item.get("expect_inbox_response")),
                    provider_id=str(item.get("provider_id") or ""),
                    model=str(item.get("model") or ""),
                    reasoning_effort=str(item.get("reasoning_effort") or ""),
                    runner=str(item.get("runner") or ""),
                    target_selector={
                        "kind": "pool",
                        "value": tag,
                        "pool_affinity_key": str(item.get("pool_affinity_key") or ""),
                    },
                )
        except Exception as exc:
            logger.exception(
                "worker pool dispatch failed tag=%s item_id=%s target_session_id=%s",
                tag,
                item.get("id"),
                target.get("agent_session_id"),
            )
            failure = await asyncio.to_thread(
                _ws.record_pool_task_failure,
                tag,
                str(item.get("id") or ""),
                str(exc),
            )
            await _coordinator().broadcast_workers_changed(None)
            if failure.get("action") == "failed" and item.get("wait_for_ask_response"):
                result = {"success": False, "error": str(exc) or exc.__class__.__name__}
                ask_id = str(item.get("ask_id") or "")
                if ask_id:
                    import ask_status_store

                    await ask_status_store.write_status_async(ask_id, result=result)
                _complete_pool_ask_waiters(ask_id, result)
            if failure.get("action") == "requeued" and int(failure.get("queued_count") or 0) <= 1:
                return
            continue
        await asyncio.to_thread(_ws.pop_pool_task, tag, str(item.get("id") or ""))
        await _coordinator().broadcast_workers_changed(None)


async def _pool_ask_status(ask_id: str) -> dict | None:
    if not ask_id:
        return None
    import ask_status_store

    return await asyncio.to_thread(ask_status_store.read_status, ask_id)


async def _pool_ask_result_if_done(ask_id: str) -> dict | None:
    status = await _pool_ask_status(ask_id)
    if isinstance(status, dict) and isinstance(status.get("result"), dict):
        return status["result"]
    return None


async def _wait_for_pool_ask_result(ask_id: str, queued: dict) -> dict:
    existing = await _pool_ask_result_if_done(ask_id)
    if existing is not None:
        return existing

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    waiters = _POOL_ASK_WAITERS.setdefault(ask_id, [])
    waiters.append(future)
    try:
        existing = await _pool_ask_result_if_done(ask_id)
        if existing is not None:
            return existing
        result = await asyncio.wait_for(future, timeout=24 * 60 * 60)
        if isinstance(result, dict):
            return result
        return {
            "success": False,
            "error": "ask failed",
            "queued": True,
            "pool_queue_item_id": ((queued or {}).get("item") or {}).get("id"),
        }
    finally:
        waiters = _POOL_ASK_WAITERS.get(ask_id) or []
        if future in waiters:
            waiters.remove(future)
        if not waiters:
            _POOL_ASK_WAITERS.pop(ask_id, None)


def _complete_pool_ask_waiters(ask_id: str, result: dict) -> None:
    if not ask_id:
        return
    waiters = list(_POOL_ASK_WAITERS.get(ask_id) or [])
    for future in waiters:
        if future.done():
            continue
        future.get_loop().call_soon_threadsafe(future.set_result, result)


def _pick_idle_pool_worker(tag: str) -> dict | None:
    from stores import worker_store as _ws

    candidates = []
    for worker in _ws.list_workers(""):
        if tag not in _ws.normalize_tags(worker.get("tags")):
            continue
        sid = str(worker.get("agent_session_id") or "")
        session = session_manager.get_lite(sid)
        if not session:
            continue
        if not session.get("agent_session_id"):
            continue
        if _coordinator().turn_manager.is_running_cached(sid):
            continue
        if session.get("queued_prompts"):
            continue
        candidates.append({**worker, "name": session.get("name") or worker.get("name")})
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.get("last_active") or "")[0]


def _pool_worker_by_session_id(tag: str, worker_session_id: str) -> dict | None:
    from stores import worker_store as _ws

    wanted = str(worker_session_id or "").strip()
    if not wanted:
        return None
    for worker in _ws.list_workers(""):
        if str(worker.get("agent_session_id") or "") != wanted:
            continue
        if tag not in _ws.normalize_tags(worker.get("tags")):
            return None
        session = session_manager.get_lite(wanted)
        if not session:
            return None
        if not session.get("agent_session_id"):
            return None
        return {**worker, "name": session.get("name") or worker.get("name")}
    return None


def _pick_pool_worker_for_sender(
    tag: str,
    sender_session_id: str,
    pool_affinity_key: str,
    require_idle: bool,
) -> dict | None:
    clean_key = str(pool_affinity_key or "").strip()
    if clean_key:
        from stores import pool_affinity_store as _pas

        bound_id = _pas.get_binding(tag, sender_session_id, clean_key)
        if bound_id:
            bound = _pool_worker_by_session_id(tag, bound_id)
            if bound:
                if not require_idle:
                    return bound
                if not _coordinator().turn_manager.is_running_cached(bound_id):
                    session = session_manager.get_lite(bound_id)
                    if session and not session.get("queued_prompts"):
                        return bound
                return None
            _pas.clear_binding(tag, sender_session_id, clean_key)
    target = _pick_idle_pool_worker(tag)
    if target and clean_key:
        from stores import pool_affinity_store as _pas

        _pas.bind(tag, sender_session_id, clean_key, str(target.get("agent_session_id") or ""))
    return target


def _find_worker_by_agent_session_id(agent_session_id: str) -> dict | None:
    from stores import worker_store as _ws

    wanted = str(agent_session_id or "").strip()
    if not wanted:
        return None
    for worker in _ws.list_workers(""):
        if str(worker.get("agent_session_id") or "") == wanted:
            return worker
    return None

"""Agent-to-agent messaging over the internal loopback: fire-and-forget
`mssg`, blocking `ask` (both modes), turn cancellation, the TestApe QA
wrappers, and the credential-broker request/execute pair.

The coordinator and the worker-pool lookups main.py still owns are
injected by the composition root — see `configure`.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

import config_store
import extension_store
import internal_extension_api
import internal_guards
from communication_modes import (
    ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC,
    normalize_ask_mode,
)
from hot_path_executor import hot_path
from i18n import t
from internal_orchestration_api import (
    internal_create_session,
    internal_create_sub_session,
)
from provider_validation import (
    api_optional_pool_affinity_key as _api_optional_pool_affinity_key,
    resolve_provider_id_ref as _resolve_provider_id_ref,
    validate_optional_run_selector as _validate_optional_run_selector,
)
from session_helpers import session_exists as _session_exists, session_lite as _session_lite

router = APIRouter()

_coordinator_ref: Any = None
# Worker-pool lookups still owned by main.py; bound as module globals so a
# test can patch either side and the call sites read the same names they
# did in main.py.
_pick_pool_worker_for_sender: Optional[Callable[..., Any]] = None
_find_worker_by_agent_session_id: Optional[Callable[[str], Any]] = None
_enqueue_worker_pool_message: Optional[Callable[..., Awaitable[dict]]] = None
_pool_ask_status: Optional[Callable[[str], Awaitable[Optional[dict]]]] = None
_ensure_worker_pool_processor: Optional[Callable[[str], None]] = None
_wait_for_pool_ask_result: Optional[Callable[[str, dict], Awaitable[dict]]] = None


def configure(
    *,
    coordinator: Any,
    pick_pool_worker_for_sender: Callable[..., Any],
    find_worker_by_agent_session_id: Callable[[str], Any],
    enqueue_worker_pool_message: Callable[..., Awaitable[dict]],
    pool_ask_status: Callable[[str], Awaitable[Optional[dict]]],
    ensure_worker_pool_processor: Callable[[str], None],
    wait_for_pool_ask_result: Callable[[str, dict], Awaitable[dict]],
) -> None:
    """Bind the collaborators this router needs."""
    global _coordinator_ref, _pick_pool_worker_for_sender
    global _find_worker_by_agent_session_id, _enqueue_worker_pool_message
    global _pool_ask_status, _ensure_worker_pool_processor
    global _wait_for_pool_ask_result
    _coordinator_ref = coordinator
    _pick_pool_worker_for_sender = pick_pool_worker_for_sender
    _find_worker_by_agent_session_id = find_worker_by_agent_session_id
    _enqueue_worker_pool_message = enqueue_worker_pool_message
    _pool_ask_status = pool_ask_status
    _ensure_worker_pool_processor = ensure_worker_pool_processor
    _wait_for_pool_ask_result = wait_for_pool_ask_result


def _coordinator() -> Any:
    if _coordinator_ref is None:
        raise HTTPException(status_code=503, detail="internal messaging API is not configured")
    return _coordinator_ref


@router.post("/api/internal/mssg")
async def internal_mssg(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    durable = await internal_extension_api.maybe_run_core_mcp_job("mssg", body)
    if durable is not None:
        return durable
    return await _handle_internal_mssg(body)


@router.post("/api/internal/stop-turn")
async def internal_stop_turn(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    caller_session_id = str(body.get("caller_session_id") or "").strip()
    target_session_id = str(body.get("target_session_id") or "").strip()
    if not caller_session_id or not target_session_id:
        raise HTTPException(
            status_code=400,
            detail="caller_session_id and target_session_id are required",
        )
    if not await _session_exists(caller_session_id):
        raise HTTPException(status_code=404, detail="caller session not found")
    if not await _session_exists(target_session_id):
        raise HTTPException(status_code=404, detail="target session not found")
    try:
        stopped = await _coordinator().turn_manager.cancel_turn_created_by(
            target_session_id,
            caller_session_id,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail=str(exc),
        ) from exc
    if not stopped:
        raise HTTPException(status_code=409, detail="target session has no running turn")
    return {
        "success": True,
        "stopped": True,
        "target_session_id": target_session_id,
    }


async def _handle_internal_mssg(body: dict) -> dict[str, Any]:
    sender_session_id = str(body.get("sender_session_id") or "").strip()
    message = str(body.get("message") or "").strip()
    if not sender_session_id or not message:
        raise HTTPException(
            status_code=400,
            detail="sender_session_id, one target, and message are required",
        )
    try:
        requested_provider_id = await _resolve_provider_id_ref(
            str(body.get("provider_id") or "").strip(),
        )
        requested_model = str(body.get("model") or "").strip()
        await _validate_optional_run_selector(
            sender_session_id,
            requested_provider_id,
            requested_model,
        )
        target_session_id = await _resolve_communication_target(body)
        return await _coordinator().submit_team_message(
            sender_session_id=sender_session_id,
            target_session_id=target_session_id,
            message=message,
            detach=True,
            provider_id=requested_provider_id,
            model=requested_model,
            reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
            runner=str(body.get("runner") or "").strip(),
            collapse_key=str(body.get("collapse_key") or "").strip(),
            collapse_policy=str(body.get("collapse_policy") or "").strip(),
            target_selector=_communication_target_selector(body),
            queue_turn=_body_bool(body, "queue_turn", default=False),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _ask_continue_and_expect_inbox_back_async(
    body: dict,
    sender_session_id: str,
    message: str,
    requested_provider_id: str,
    requested_model: str,
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any]:
    target_worker_pool = str(body.get("target_worker_pool") or "").strip()
    pool_affinity_key = _api_optional_pool_affinity_key(body.get("pool_affinity_key"))
    has_exact_target = (
        str(body.get("target_session_id") or "").strip()
        or str(body.get("target_worker_id") or "").strip()
    )
    if target_worker_pool and not has_exact_target:
        target = await hot_path.run(
            "communication.ask_async.pick_pool_worker",
            _pick_pool_worker_for_sender,
            target_worker_pool,
            sender_session_id,
            pool_affinity_key,
            True,
        )
        if not target:
            queued = await _enqueue_worker_pool_message(
                tag=target_worker_pool,
                sender_session_id=sender_session_id,
                prompt=message,
                expect_inbox_response=True,
                pool_affinity_key=pool_affinity_key,
                provider_id=requested_provider_id,
                model=requested_model,
                reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
                runner=str(body.get("runner") or "").strip(),
            )
            return {"success": True, "queued": True, **queued}
        target_session_id = str(target.get("agent_session_id") or "")
    else:
        target_session_id = await _resolve_communication_target(body)
    defer_turn_start = background_tasks is not None
    result = await _coordinator().submit_team_message(
        sender_session_id=sender_session_id,
        target_session_id=target_session_id,
        message=message,
        detach=True,
        expect_inbox_response=True,
        provider_id=requested_provider_id,
        model=requested_model,
        reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
        runner=str(body.get("runner") or "").strip(),
        target_selector=_communication_target_selector(body),
        start_turn=not defer_turn_start,
    )
    if background_tasks is not None:
        background_tasks.add_task(
            _coordinator().start_session_processor_async,
            target_session_id,
        )
    return result


async def _ask_wait_and_grab_last_assistant_mssg_in_turn(
    body: dict,
    sender_session_id: str,
    message: str,
    requested_provider_id: str,
    requested_model: str,
    *,
    user_initiated: bool = False,
) -> dict[str, Any]:
    target_worker_pool = str(body.get("target_worker_pool") or "").strip()
    pool_affinity_key = _api_optional_pool_affinity_key(body.get("pool_affinity_key"))
    pool_target_selector = {
        "kind": "pool",
        "value": target_worker_pool,
        "pool_affinity_key": pool_affinity_key,
    }
    has_exact_target = (
        str(body.get("target_session_id") or "").strip()
        or str(body.get("target_worker_id") or "").strip()
    )
    if target_worker_pool and not has_exact_target:
        ask_id = str(body.get("ask_id") or "")
        if ask_id:
            import ask_status_store

            await asyncio.to_thread(
                ask_status_store.claim_route,
                ask_id,
                sender_session_id=sender_session_id,
                target_session_id="",
                target_selector=pool_target_selector,
            )
            status = await _pool_ask_status(ask_id)
            if isinstance(status, dict):
                if isinstance(status.get("result"), dict):
                    return status["result"]
                if status.get("pool_queue_item_id"):
                    _ensure_worker_pool_processor(str(status.get("pool_tag") or target_worker_pool))
                    return await _wait_for_pool_ask_result(ask_id, status)
        target = await asyncio.to_thread(
            _pick_pool_worker_for_sender,
            target_worker_pool,
            sender_session_id,
            pool_affinity_key,
            True,
        )
        if not target:
            ask_id = ask_id or f"ask_{uuid.uuid4().hex[:10]}"
            import ask_status_store

            await asyncio.to_thread(
                ask_status_store.claim_route,
                ask_id,
                sender_session_id=sender_session_id,
                target_session_id="",
                target_selector=pool_target_selector,
            )
            queued = await _enqueue_worker_pool_message(
                tag=target_worker_pool,
                sender_session_id=sender_session_id,
                prompt=message,
                expect_inbox_response=False,
                pool_affinity_key=pool_affinity_key,
                provider_id=requested_provider_id,
                model=requested_model,
                reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
                runner=str(body.get("runner") or "").strip(),
                wait_for_ask_response=True,
                ask_id=ask_id,
            )
            await ask_status_store.write_status_async(
                ask_id,
                pool_queue_item_id=str(((queued or {}).get("item") or {}).get("id") or ""),
                pool_tag=target_worker_pool,
                sender_session_id=sender_session_id,
            )
            return await _wait_for_pool_ask_result(ask_id, queued)
        target_session_id = str(target.get("agent_session_id") or "")
    else:
        target_session_id = await _resolve_communication_target(body)
    return await _coordinator().ask_team_message(
        sender_session_id=sender_session_id,
        target_session_id=target_session_id,
        message=message,
        user_initiated=user_initiated,
        ask_id=str(body.get("ask_id") or ""),
        provider_id=requested_provider_id,
        model=requested_model,
        reasoning_effort=str(body.get("reasoning_effort") or "").strip(),
        runner=str(body.get("runner") or "").strip(),
        target_selector=_communication_target_selector(body),
    )


async def _handle_internal_ask(
    body: dict,
    background_tasks: BackgroundTasks | None = None,
    *,
    trusted_user_initiated: bool = False,
) -> dict[str, Any]:
    sender_session_id = str(body.get("sender_session_id") or "").strip()
    message = str(body.get("message") or "").strip()
    if not sender_session_id or not message:
        raise HTTPException(
            status_code=400,
            detail="sender_session_id, one target, and message are required",
        )
    try:
        requested_provider_id = await _resolve_provider_id_ref(
            str(body.get("provider_id") or "").strip(),
        )
        requested_model = str(body.get("model") or "").strip()
        await _validate_optional_run_selector(
            sender_session_id,
            requested_provider_id,
            requested_model,
        )
        mode = normalize_ask_mode(body.get("mode"))
        if mode == ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC:
            return await _ask_continue_and_expect_inbox_back_async(
                body,
                sender_session_id,
                message,
                requested_provider_id,
                requested_model,
                background_tasks,
            )
        return await _ask_wait_and_grab_last_assistant_mssg_in_turn(
            body,
            sender_session_id,
            message,
            requested_provider_id,
            requested_model,
            user_initiated=trusted_user_initiated,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _resolve_communication_target(body: dict) -> str:
    sender_session_id = str((body or {}).get("sender_session_id") or "").strip()
    target_session_id = str((body or {}).get("target_session_id") or "").strip()
    target_worker_id = str((body or {}).get("target_worker_id") or "").strip()
    target_worker_pool = str((body or {}).get("target_worker_pool") or "").strip()
    pool_affinity_key = _api_optional_pool_affinity_key((body or {}).get("pool_affinity_key"))
    targets = [value for value in (target_session_id, target_worker_id, target_worker_pool) if value]
    if len(targets) != 1:
        raise HTTPException(status_code=400, detail="exactly one target is required")
    if target_session_id:
        return target_session_id
    if target_worker_id:
        worker = await hot_path.run(
            "communication.resolve_target.find_worker",
            _find_worker_by_agent_session_id,
            target_worker_id,
        )
        if not worker:
            raise HTTPException(status_code=404, detail="target_worker_id does not exist")
        return str(worker.get("agent_session_id") or "")
    target = await hot_path.run(
        "communication.resolve_target.pick_pool_worker",
        _pick_pool_worker_for_sender,
        target_worker_pool,
        sender_session_id,
        pool_affinity_key,
        False,
    )
    if not target:
        raise HTTPException(status_code=409, detail="no idle worker in target_worker_pool")
    return str(target.get("agent_session_id") or "")


def _body_bool(body: dict, key: str, *, default: bool = False) -> bool:
    """Parse a boolean from a request body field, accepting real bools and
    common string spellings; returns `default` when absent."""
    value = (body or {}).get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _communication_target_selector(body: dict) -> dict:
    target_session_id = str((body or {}).get("target_session_id") or "").strip()
    target_worker_id = str((body or {}).get("target_worker_id") or "").strip()
    target_worker_pool = str((body or {}).get("target_worker_pool") or "").strip()
    pool_affinity_key = _api_optional_pool_affinity_key((body or {}).get("pool_affinity_key"))
    if target_session_id:
        return {"kind": "session", "value": target_session_id}
    if target_worker_id:
        return {"kind": "worker", "value": target_worker_id}
    if target_worker_pool:
        selector = {"kind": "pool", "value": target_worker_pool}
        if pool_affinity_key:
            selector["pool_affinity_key"] = pool_affinity_key
        return selector
    return {}


@router.post("/api/internal/ask")
async def internal_ask(
    body: dict,
    background_tasks: BackgroundTasks,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    durable = await internal_extension_api.maybe_run_core_mcp_job("ask", body)
    if durable is not None:
        return durable
    try:
        return await _handle_internal_ask(body, background_tasks)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="ask timed out")


@router.post("/api/internal/testape/qa-ask")
async def internal_testape_qa_ask(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    x_testape_qa_token: str = Header(..., alias="X-TestApe-QA-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import testape_qa_runtime
    try:
        runtime_id = testape_qa_runtime.authorize(x_testape_qa_token)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not str(body.get("target_session_id") or "").strip():
        raise HTTPException(status_code=400, detail="target_session_id is required")
    if body.get("target_worker_id") or body.get("target_worker_pool"):
        raise HTTPException(status_code=400, detail="TestApe QA asks require an exact session")
    body = {**body, "_testape_qa_runtime_id": runtime_id}
    durable = await internal_extension_api.maybe_run_core_mcp_job("testape-qa-ask", body)
    if durable is not None:
        return durable
    try:
        return await _handle_internal_testape_qa_ask(body)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="ask timed out")


async def _handle_internal_testape_qa_ask(body: dict) -> dict[str, Any]:
    import testape_qa_runtime
    runtime_id = str(body.get("_testape_qa_runtime_id") or "").strip()
    sender_session_id = str(body.get("sender_session_id") or "").strip()
    target_session_id = str(body.get("target_session_id") or "").strip()
    try:
        await asyncio.to_thread(
            testape_qa_runtime.assert_owned,
            runtime_id,
            sender_session_id,
            target_session_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return await _handle_internal_ask(body, trusted_user_initiated=True)


@router.post("/api/internal/testape/qa-create-session")
async def internal_testape_qa_create_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    x_testape_qa_token: str = Header(..., alias="X-TestApe-QA-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import testape_qa_runtime
    try:
        runtime_id = testape_qa_runtime.authorize(x_testape_qa_token)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    result = await internal_create_session(body, x_internal_token)
    await asyncio.to_thread(testape_qa_runtime.claim, runtime_id, result["session_id"])
    return result


@router.post("/api/internal/testape/qa-create-sub-session")
async def internal_testape_qa_create_sub_session(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    x_testape_qa_token: str = Header(..., alias="X-TestApe-QA-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    import testape_qa_runtime
    try:
        runtime_id = testape_qa_runtime.authorize(x_testape_qa_token)
        await asyncio.to_thread(
            testape_qa_runtime.assert_owned,
            runtime_id,
            str(body.get("sender_session_id") or "").strip(),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    result = await internal_create_sub_session(body, x_internal_token)
    await asyncio.to_thread(
        testape_qa_runtime.claim,
        runtime_id,
        result["target_session_id"],
    )
    return result


@router.post("/api/internal/test/force-context-overflow")
async def internal_force_context_overflow(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    if body.get("confirm") != "FORCE_CONTEXT_OVERFLOW_FOR_TESTING":
        raise HTTPException(status_code=400, detail="invalid confirmation")

    app_session_id = str(body.get("app_session_id") or "").strip()
    if not app_session_id:
        raise HTTPException(status_code=400, detail="app_session_id is required")
    session = await _session_lite(app_session_id)
    if not session:
        raise HTTPException(status_code=404, detail=t("error.session_not_found"))

    _coordinator().turn_manager.force_context_overflow_once(app_session_id)

    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return {"success": True, "armed": True, "submitted": False}

    await asyncio.to_thread(config_store.apply_env_vars)
    item_id = await _coordinator().submit_prompt_async(app_session_id, {
        "prompt": prompt,
        "app_session_id": app_session_id,
        "model": session.get("model"),
        "cwd": session.get("cwd"),
        "ws_callback": None,
        "images": None,
        "files": None,
        "orchestration_mode": session.get("orchestration_mode"),
        "client_id": body.get("client_id"),
        "source": "internal_test",
        "user_initiated": True,
    })
    return {
        "success": True,
        "armed": True,
        "submitted": True,
        "queued_id": item_id,
    }


@router.post("/api/internal/credential/request")
async def internal_credential_request(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(
        extension_store.extension_id_for_role('credential-broker')
    )
    """Invoked by a runner's `credential_request` SDK MCP tool. The provider
    (via the model) proposes an operation that needs a user secret. We
    validate + pin-check it and persist a PENDING consent for the user to
    approve; we NEVER receive or return the secret here. Returns the public
    consent view (consent_id + computed sink + risk) or an error.
    """
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    from credential_broker import broker as _broker
    import config_store as _cfg

    app_session_id = (body.get("app_session_id") or "").strip()
    descriptor = body.get("descriptor")
    if not app_session_id or not isinstance(descriptor, dict):
        return {"ok": False, "error": "app_session_id and descriptor are required"}
    provider_id = str(descriptor.get("provider_id") or "")
    allowed_sinks = _cfg.get_allowed_sinks(provider_id)
    try:
        view = _broker.request_consent(
            app_session_id=app_session_id,
            descriptor_raw=descriptor,
            allowed_sinks=allowed_sinks,
        )
    except _broker.BrokerError as e:
        return {"ok": False, "error": str(e)}
    await _coordinator().broadcast_credential_consent_changed(app_session_id)
    return {
        "ok": True,
        "consent_id": view["consent_id"],
        "sink": view["sink"],
        "status": view["status"],
        "message": (
            "A credential consent request was created and is awaiting user "
            "approval. Once approved, call credential_execute with this "
            "consent_id. You will never see the secret value."
        ),
    }


@router.post("/api/internal/credential/execute")
async def internal_credential_execute(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    internal_guards.require_builtin_runtime_extension(
        extension_store.extension_id_for_role('credential-broker')
    )
    """Invoked by a runner's `credential_execute` SDK MCP tool. Runs the
    frozen operation for an approved consent. The caller supplies ONLY the
    consent_id (+ optional presence proof) — never a descriptor or secret.
    Returns a guarded result with no secret in it."""
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    from credential_broker import broker as _broker

    consent_id = (body.get("consent_id") or "").strip()
    if not consent_id:
        return {"ok": False, "error": "consent_id is required"}
    try:
        result = _broker.execute(consent_id, proof=body.get("proof"))
    except _broker.BrokerError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "result": result}

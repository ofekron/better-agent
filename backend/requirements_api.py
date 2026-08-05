"""Internal HTTP surface for the requirements processor and its indexes.

The routes here fan out to `requirement_context` and the supervised
query runner. `fire_processed_requirements_for_caller` and
`get_processed_requirements_results_for_caller` are parameterised by
caller rather than bound to the internal route, because the extension
MCP bridge drives the same job lifecycle with a different caller
identity.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException

import extension_jobs
import extension_store
import perf
import requirements_run_metrics
from internal_guards import require_builtin_runtime_extension, require_internal
from requirements_query_runner import (
    PROCESSOR_RESULT_TIMEOUT_SECONDS,
    REQUIREMENTS_PROCESSOR_EXECUTOR,
    REQUIREMENTS_SEARCH_EXECUTOR,
    run_requirements_processor_query,
    run_requirements_query,
    run_supervised_requirements_search,
)
from requirements_vector_contract import validate_unit_vector_bounds

logger = logging.getLogger(__name__)

router = APIRouter(tags=["requirements"])

_PROCESSOR_MILESTONE_MESSAGES = {
    "processor_started": "Requirements processor worker started",
    "processor_attempt_started": "Requirements processor attempt started",
    "provisioning_started": "Resolving requirements processor runtime",
    "configuration_resolved": "Requirements processor runtime resolved",
    "lifecycle_started": "Preparing requirements processor session",
    "lifecycle_lock_waiting": "Waiting for requirements session lifecycle ownership",
    "lifecycle_lock_acquired": "Requirements session lifecycle ownership acquired",
    "base_session_resolving": "Resolving requirements processor base session",
    "base_registry_lookup": "Looking up requirements processor base registry entry",
    "base_cleanliness_check": "Validating requirements processor base session",
    "base_session_recycling": "Recycling requirements processor base session",
    "base_session_record_creating": "Creating requirements processor base session record",
    "base_registry_registering": "Registering requirements processor base session",
    "base_worker_projection_sync": "Synchronizing requirements processor worker projection",
    "base_session_resolved": "Requirements processor base session resolved",
    "base_session_warming": "Warming requirements processor base provider session",
    "base_session_warmed": "Requirements processor base provider session warmed",
    "base_session_persisting": "Persisting requirements processor base session",
    "base_session_ready": "Requirements processor base session ready",
    "caller_session_resolving": "Resolving requirements processor caller session",
    "caller_session_ready": "Requirements processor caller session ready",
    "lifecycle_ready": "Requirements processor session ready",
    "dispatch_started": "Dispatching requirements processor turn",
    "delegation_resolving": "Resolving requirements processor delegation",
    "delegation_queued": "Requirements processor runner queued",
    "delegation_run_state_registering": "Registering requirements processor run state",
    "delegation_lock_waiting": "Waiting for requirements delegation ownership",
    "delegation_lock_acquired": "Requirements delegation ownership acquired",
    "delegation_fork_resolving": "Resolving requirements processor fork",
    "delegation_provider_resolving": "Resolving requirements processor provider",
    "delegation_team_context_resolving": "Building requirements processor team context",
    "delegation_recovery_waiting": "Waiting for provider recovery readiness",
    "delegation_root_persist_flushing": "Persisting requirements processor session state",
    "delegation_runner_starting": "Starting requirements processor runner",
    "runner_started": "Requirements processor runner started",
    "native_session_started": "Requirements processor native session started",
    "dispatch_complete": "Requirements processor turn returned",
    "result_parsed": "Requirements processor result parsed",
}

_PROCESSOR_MILESTONE_FIELDS = frozenset({
    "provider_id",
    "model",
    "base_session_id",
    "caller_session_id",
    "delegation_id",
    "provider_run_id",
    "worker_pid",
    "worker_agent_session_id",
    "native_session_id",
    "attempt",
})


def _validate_processed_requirements_body(body: dict) -> dict[str, Any]:
    require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="query must be a non-empty string")
    cwd = body.get("cwd", "")
    if not isinstance(cwd, str):
        raise HTTPException(status_code=400, detail="cwd must be a string")
    cwds = body.get("cwds")
    if cwds is not None and (
        not isinstance(cwds, list) or any(not isinstance(item, str) for item in cwds)
    ):
        raise HTTPException(status_code=400, detail="cwds must be a list of strings")
    all_projects = body.get("all_projects", False)
    if not isinstance(all_projects, bool):
        raise HTTPException(status_code=400, detail="all_projects must be a boolean")
    return {
        "query": query,
        "cwd": cwd,
        "cwds": cwds,
        "all_projects": all_projects,
    }


def _requirements_query_debug_fields(payload: dict[str, Any]) -> dict[str, Any]:
    query = payload.get("query") if isinstance(payload.get("query"), str) else ""
    query_bytes = query.encode("utf-8", "surrogatepass")
    return {
        "query_sha256": hashlib.sha256(query_bytes).hexdigest()[:16],
        "query_len": len(query),
        "cwd": payload.get("cwd") or "",
        "cwds_count": len(payload.get("cwds") or []),
        "all_projects": bool(payload.get("all_projects")),
    }


async def _cancel_requirements_processor_delegation(request_id: str, delegation_id: str) -> None:
    if not delegation_id:
        return
    from provisioning.dispatch import cancel_delegation_and_wait

    terminated = await cancel_delegation_and_wait(delegation_id)
    logger.info(
        "requirements_processor_cancel_stop request_id=%s delegation_id=%s terminated=%s",
        request_id,
        delegation_id,
        terminated,
    )
    if not terminated:
        raise TimeoutError("requirements processor cancellation was not acknowledged")


async def _run_processed_requirements_payload(
    payload: dict[str, Any],
    *,
    request_id: str = "",
    queue_admission: bool = False,
) -> dict[str, Any]:
    import requirement_context
    if queue_admission:
        import startup_recovery_gate
        await _mark_requirements_job_phase(
            request_id,
            "waiting_for_recovery",
            "Waiting for backend recovery",
        )
        await startup_recovery_gate.wait_for_recovery_ready(timeout=None)
    debug_fields = _requirements_query_debug_fields(payload)
    delegation_id = ""
    if request_id:
        delegation_id = str(payload.get("delegation_id") or "").strip()
        if not delegation_id:
            delegation_id = extension_jobs.delegation_id(
                "requirements",
                "processed",
                request_id,
                requirement_context.GET_REQUIREMENTS_PROCESSOR_KEY,
            )
    await _mark_requirements_job_phase(
        request_id,
        "preparing_local_context",
        "Preparing local requirement context",
    )
    await run_requirements_query(
        "requirements.processed.prepare",
        requirement_context.prepare_requirements_local_read_context,
        executor=REQUIREMENTS_SEARCH_EXECUTOR,
        requires_projection=True,
    )
    try:
        # Poll-driven jobs queue for a worker instead of failing admission at
        # the sync hot-path budget. wait=True fires and the sync endpoint keep
        # the short budget so the caller's own timeout stays authoritative.
        admission_kwargs = (
            {"admission_timeout_seconds": PROCESSOR_RESULT_TIMEOUT_SECONDS}
            if queue_admission
            else {}
        )
        processed = await run_requirements_processor_query(
            "requirements.processed.processor",
            requirement_context._run_requirements_processor,
            executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
            **admission_kwargs,
            **payload,
            debug_request_id=request_id,
            delegation_id=delegation_id,
            on_queued=functools.partial(
                _mark_requirements_job_phase,
                request_id,
                "queued_for_processor",
                "Waiting for a requirements processor slot",
            ),
            on_admitted=functools.partial(
                _mark_requirements_job_phase,
                request_id,
                "processor_admitted",
                "Requirements processor capacity admitted",
            ),
            milestone_callback=functools.partial(
                _persist_requirements_processor_milestone,
                request_id,
            ),
            on_caller_cancelled=functools.partial(
                _cancel_requirements_processor_delegation,
                request_id,
                delegation_id,
            ),
        )
    except TimeoutError as exc:
        if request_id:
            logger.warning(
                "requirements_async_processor_timeout request_id=%s query_sha256=%s "
                "query_len=%s cwd=%s cwds_count=%s all_projects=%s",
                request_id,
                debug_fields["query_sha256"],
                debug_fields["query_len"],
                debug_fields["cwd"],
                debug_fields["cwds_count"],
                debug_fields["all_projects"],
            )
        recovered = await asyncio.to_thread(
            requirement_context.recover_processed_requirements_from_delegation,
            request_id=request_id,
            payload={**payload, "delegation_id": delegation_id},
        )
        processed = recovered or requirement_context.processor_failure_result(exc)
    await _mark_requirements_job_phase(
        request_id,
        "finalizing",
        "Finalizing processed requirements",
    )
    response, output_report = await run_requirements_query(
        "requirements.processed.finalize",
        requirement_context.build_processed_requirements_response_with_report,
        executor=REQUIREMENTS_SEARCH_EXECUTOR,
        **payload,
        processed=processed,
    )
    await asyncio.to_thread(
        requirements_run_metrics.record_output,
        request_id,
        output_report,
    )
    return response


async def _mark_requirements_job_phase(request_id: str, phase: str, message: str) -> None:
    if not request_id:
        return
    try:
        queue_fields = _requirements_processor_queue_fields()
        await asyncio.to_thread(
            extension_jobs.persist_phase,
            "requirements",
            "processed",
            request_id,
            phase,
            message,
            **queue_fields,
        )
        await asyncio.to_thread(
            requirements_run_metrics.record_milestone,
            request_id,
            phase,
            queue_fields,
        )
    except (OSError, TypeError, ValueError):
        logger.warning(
            "requirements_async_phase_persist_failed request_id=%s phase=%s",
            request_id,
            phase,
        )


def _persist_requirements_processor_milestone(
    request_id: str,
    milestone: str,
    fields: dict[str, Any],
) -> None:
    if not request_id:
        return
    message = _PROCESSOR_MILESTONE_MESSAGES.get(milestone)
    if message is None:
        logger.warning(
            "requirements_processor_unknown_milestone request_id=%s milestone=%s",
            request_id,
            milestone,
        )
        return
    safe_fields = {
        key: value
        for key, value in fields.items()
        if key in _PROCESSOR_MILESTONE_FIELDS
    }
    native_path = fields.get("native_session_file_path")
    if isinstance(native_path, str) and native_path:
        safe_fields["native_session_file_paths"] = [native_path]
    try:
        extension_jobs.persist_phase(
            "requirements",
            "processed",
            request_id,
            milestone,
            message,
            **safe_fields,
            **_requirements_processor_queue_fields(),
        )
        requirements_run_metrics.record_milestone(
            request_id,
            milestone,
            safe_fields,
        )
    except (OSError, TypeError, ValueError):
        logger.warning(
            "requirements_processor_milestone_persist_failed request_id=%s milestone=%s",
            request_id,
            milestone,
        )


def _requirements_processor_queue_fields() -> dict[str, Any]:
    try:
        from requirements_query_runner import processor_admission_state

        state = processor_admission_state()
    except Exception:
        logger.debug("requirements processor queue state unavailable", exc_info=True)
        return {}
    return {
        "processor_queue_depth": int(state.get("queue_depth") or 0),
        "processor_active_permits": int(state.get("active_permits") or 0),
        "processor_available_permits": int(state.get("available_permits") or 0),
        "processor_oldest_queue_age_ms": float(state.get("oldest_queue_age_ms") or 0.0),
    }


async def _recover_requirements_async_result(
    request_id: str,
) -> dict[str, Any] | None:
    import requirement_context

    record = extension_jobs.read_record("requirements", "processed", request_id)
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    from provisioning.dispatch import recover_delegation_result

    dispatch_result = await asyncio.to_thread(
        recover_delegation_result,
        str(record.get("delegation_id") or ""),
    )
    if dispatch_result is None:
        return None
    await _mark_requirements_job_phase(
        request_id,
        "recovering_processor_result",
        "Recovering completed processor result",
    )
    recovered = await asyncio.to_thread(
        requirement_context.parse_processed_requirements_from_dispatch_result,
        request_id=request_id,
        payload={**payload, "delegation_id": record.get("delegation_id")},
        dispatch_result=dispatch_result,
    )
    if recovered is None:
        return None
    await _mark_requirements_job_phase(
        request_id,
        "finalizing",
        "Finalizing processed requirements",
    )
    result, output_report = await run_requirements_query(
        "requirements.processed.recover_finalize",
        requirement_context.build_processed_requirements_response_with_report,
        executor=REQUIREMENTS_SEARCH_EXECUTOR,
        **payload,
        processed=recovered,
    )
    await asyncio.to_thread(
        requirements_run_metrics.record_output,
        request_id,
        output_report,
    )
    return await asyncio.to_thread(
        extension_jobs.persist_complete,
        "requirements",
        "processed",
        request_id,
        result,
    )


@router.post("/api/internal/get-requirements")
async def internal_get_requirements(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_internal()
    payload = _validate_processed_requirements_body(body)
    return await _run_processed_requirements_payload(payload)

@router.post("/api/internal/get-requirements/fire")
async def internal_fire_get_requirements(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_internal()
    return await fire_processed_requirements_for_caller(body, caller_extension="internal")


async def fire_processed_requirements_for_caller(
    body: dict,
    *,
    caller_extension: str,
) -> dict[str, Any]:
    payload = _validate_processed_requirements_body(body)
    wait = body.get("wait", False)
    if not isinstance(wait, bool):
        raise HTTPException(status_code=400, detail="wait must be a boolean")

    extension_jobs.cleanup("requirements", "processed")
    idempotency_key = body.get("idempotency_key", "")
    if not isinstance(idempotency_key, str) or len(idempotency_key) > 128:
        raise HTTPException(status_code=400, detail="invalid idempotency_key")
    if idempotency_key and any(
        not (ch.isalnum() or ch in ("-", "_")) for ch in idempotency_key
    ):
        raise HTTPException(status_code=400, detail="invalid idempotency_key")
    canonical_payload = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8", "surrogatepass")
    payload_digest = hashlib.sha256(canonical_payload).hexdigest()
    request_id = (
        hashlib.sha256(
            f"{caller_extension}\0{idempotency_key}".encode("utf-8", "surrogatepass")
        ).hexdigest()
        if idempotency_key
        else uuid.uuid4().hex
    )
    import requirement_context

    delegation_id = extension_jobs.delegation_id(
        "requirements",
        "processed",
        request_id,
        requirement_context.GET_REQUIREMENTS_PROCESSOR_KEY,
    )
    debug_fields = _requirements_query_debug_fields(payload)
    logger.info(
        "requirements_async_fire request_id=%s query_sha256=%s query_len=%s "
        "cwd=%s cwds_count=%s all_projects=%s wait=%s",
        request_id,
        debug_fields["query_sha256"],
        debug_fields["query_len"],
        debug_fields["cwd"],
        debug_fields["cwds_count"],
        debug_fields["all_projects"],
        wait,
    )
    metadata = {
        "delegation_id": delegation_id,
        "phase": "created",
        "message": "Requirements job created",
        "requirements_metrics": requirements_run_metrics.new_metrics(),
        **_requirements_processor_queue_fields(),
    }
    if idempotency_key:
        try:
            found = extension_jobs.get_or_fire_idempotent(
                "requirements", "processed", request_id, payload,
                functools.partial(_run_processed_requirements_payload, queue_admission=not wait),
                payload_digest=payload_digest,
                caller_extension=caller_extension,
                metadata=metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(found, dict):
            return found
        task = found
    else:
        task = extension_jobs.fire(
            "requirements", "processed", request_id, payload,
            functools.partial(_run_processed_requirements_payload, queue_admission=not wait),
            metadata=metadata,
        )
    if not wait:
        record = extension_jobs.read_record("requirements", "processed", request_id)
        if isinstance(record, dict):
            return extension_jobs.response_from_record(record)
        return {"success": True, "id": request_id, "status": "running", "ready": False}
    try:
        result = await asyncio.shield(task)
    except Exception as exc:
        return {"success": False, "id": request_id, "status": "failed", "ready": True, "error": str(exc)}
    return await asyncio.to_thread(
        extension_jobs.persist_complete,
        "requirements",
        "processed",
        request_id,
        result,
    )


@router.post("/api/internal/get-requirements/results")
async def internal_get_requirements_results(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_internal()
    return await get_processed_requirements_results_for_caller(
        body, caller_extension="internal",
    )


async def get_processed_requirements_results_for_caller(
    body: dict,
    *,
    caller_extension: str,
) -> dict[str, Any]:
    require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    request_id = body.get("id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise HTTPException(status_code=400, detail="id is required")
    wait = body.get("wait", 0.0)
    if not isinstance(wait, (int, float)) or isinstance(wait, bool) or wait < 0:
        raise HTTPException(status_code=400, detail="wait must be a non-negative number of seconds")

    extension_jobs.cleanup("requirements", "processed")
    request_id = request_id.strip()
    try:
        extension_jobs.job_path("requirements", "processed", request_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record = extension_jobs.read_record("requirements", "processed", request_id)
    record_caller = record.get("caller_extension") if isinstance(record, dict) else None
    if record_caller and record_caller != caller_extension:
        raise HTTPException(status_code=404, detail="unknown id")
    recovered = await _recover_requirements_async_result(request_id)
    if recovered is not None:
        return recovered
    found = extension_jobs.get_or_resume(
        "requirements",
        "processed",
        request_id,
        functools.partial(_run_processed_requirements_payload, queue_admission=True),
    )
    if found is None:
        return {"success": False, "error": "unknown id"}
    if isinstance(found, dict):
        return _with_running_requirements_native_session_files(found, request_id)
    task = found
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=float(wait))
    except asyncio.TimeoutError:
        record = extension_jobs.read_record("requirements", "processed", request_id)
        if isinstance(record, dict):
            response = extension_jobs.response_from_record(record)
            return _with_running_requirements_native_session_files(response, request_id)
        response = {"success": True, "id": request_id, "status": "running", "ready": False}
        return _with_running_requirements_native_session_files(response, request_id)
    except Exception as exc:
        return {"success": False, "id": request_id, "status": "failed", "ready": True, "error": str(exc)}
    return await asyncio.to_thread(
        extension_jobs.persist_complete,
        "requirements",
        "processed",
        request_id,
        result,
    )


def _with_running_requirements_native_session_files(
    response: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    if response.get("ready") is not False:
        return response

    import delegation_status_store
    import requirement_context

    delegation_id = str(response.get("delegation_id") or "").strip()
    if not delegation_id:
        delegation_id = extension_jobs.delegation_id(
            "requirements",
            "processed",
            request_id,
            requirement_context.GET_REQUIREMENTS_PROCESSOR_KEY,
        )
    status = delegation_status_store.read_status(delegation_id)
    path = status.get("jsonl_path") if isinstance(status, dict) else None
    record = extension_jobs.read_record("requirements", "processed", request_id)
    persisted_paths = record.get("native_session_file_paths") if isinstance(record, dict) else None
    paths = (
        [item for item in persisted_paths if isinstance(item, str) and item]
        if isinstance(persisted_paths, list)
        else []
    )
    if isinstance(path, str) and path and path not in paths:
        paths.append(path)
    return {**response, "native_session_file_paths": paths}


@router.post("/api/internal/get-requirements/unit-fts")
async def internal_requirements_unit_fts(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    require_internal()
    payload = _validate_processed_requirements_body(body)
    fields = body.get("fields")
    if fields is not None and (
        not isinstance(fields, list) or any(not isinstance(field, str) for field in fields)
    ):
        raise HTTPException(status_code=400, detail="fields must be a list of strings")
    include_all_fields = body.get("include_all_fields", False)
    if not isinstance(include_all_fields, bool):
        raise HTTPException(status_code=400, detail="include_all_fields must be a boolean")

    return await run_supervised_requirements_search(
        "requirements.unit_fts",
        "unit_fts",
        query=payload["query"],
        cwd=payload["cwd"],
        cwds=payload["cwds"],
        all_projects=payload["all_projects"],
        fields=fields,
        include_all_fields=include_all_fields,
    )


@router.post("/api/internal/get-requirements/unit-vector")
async def internal_requirements_unit_vector(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    require_internal()
    payload = _validate_processed_requirements_body(body)
    fields = body.get("fields")
    if fields is not None and (
        not isinstance(fields, list) or any(not isinstance(field, str) for field in fields)
    ):
        raise HTTPException(status_code=400, detail="fields must be a list of strings")
    include_all_fields = body.get("include_all_fields", False)
    if not isinstance(include_all_fields, bool):
        raise HTTPException(status_code=400, detail="include_all_fields must be a boolean")
    try:
        top_k, min_score = validate_unit_vector_bounds(
            body.get("top_k"),
            body.get("min_score"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await run_supervised_requirements_search(
        "requirements.unit_vector",
        "unit_vector",
        query=payload["query"],
        cwd=payload["cwd"],
        cwds=payload["cwds"],
        all_projects=payload["all_projects"],
        top_k=top_k,
        min_score=min_score,
        fields=fields,
        include_all_fields=include_all_fields,
    )


@router.post("/api/internal/get-requirements/thread-fts")
async def internal_requirements_thread_fts(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    require_internal()
    payload = _validate_processed_requirements_body(body)
    fields = body.get("fields")
    if fields is not None and (
        not isinstance(fields, list) or any(not isinstance(field, str) for field in fields)
    ):
        raise HTTPException(status_code=400, detail="fields must be a list of strings")
    include_all_fields = body.get("include_all_fields", False)
    if not isinstance(include_all_fields, bool):
        raise HTTPException(status_code=400, detail="include_all_fields must be a boolean")

    return await run_supervised_requirements_search(
        "requirements.thread_fts",
        "thread_fts",
        query=payload["query"],
        cwd=payload["cwd"],
        cwds=payload["cwds"],
        all_projects=payload["all_projects"],
        fields=fields,
        include_all_fields=include_all_fields,
    )


@router.post("/api/internal/get-requirements/thread-vector")
async def internal_requirements_thread_vector(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    require_internal()
    payload = _validate_processed_requirements_body(body)
    fields = body.get("fields")
    if fields is not None and (
        not isinstance(fields, list) or any(not isinstance(field, str) for field in fields)
    ):
        raise HTTPException(status_code=400, detail="fields must be a list of strings")
    include_all_fields = body.get("include_all_fields", False)
    if not isinstance(include_all_fields, bool):
        raise HTTPException(status_code=400, detail="include_all_fields must be a boolean")

    response = await run_supervised_requirements_search(
        "requirements.thread_vector",
        "thread_vector_search",
        query=payload["query"],
        cwd=payload["cwd"],
        cwds=payload["cwds"],
        all_projects=payload["all_projects"],
        fields=fields,
        include_all_fields=include_all_fields,
    )
    if response.get("reason") in {"source_missing", "index_not_ready_or_stale"}:
        import requirement_prewarm

        requirement_prewarm.request_thread_vector_projection()
    return response


@router.post("/api/internal/get-requirements/index-sql")
async def internal_requirements_index_sql(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    require_internal()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    sql = body.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise HTTPException(status_code=400, detail="sql must be a non-empty string")

    response = await run_supervised_requirements_search(
        "requirements.index_sql",
        "index_sql",
        sql=sql,
    )
    from fastapi.responses import JSONResponse

    serialize_started = time.perf_counter()
    rendered = JSONResponse(response)
    perf.record(
        "requirements.index_sql.response_serialize",
        (time.perf_counter() - serialize_started) * 1000.0,
    )

    class _TimedIndexSqlResponse(JSONResponse):
        async def __call__(self, scope, receive, send):
            wire_started = time.perf_counter()
            try:
                await super().__call__(scope, receive, send)
            finally:
                perf.record(
                    "requirements.index_sql.socket_write",
                    (time.perf_counter() - wire_started) * 1000.0,
                )

    timed = _TimedIndexSqlResponse(content=None)
    timed.body = rendered.body
    timed.raw_headers = rendered.raw_headers
    return timed

@router.post("/api/internal/get-requirements/search")
async def internal_search_requirements(
    body: dict,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_builtin_runtime_extension(extension_store.extension_id_for_role('requirements'))
    require_internal()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    rg_args = body.get("rg_args")
    query = body.get("query", "")
    if rg_args is not None and (
        not isinstance(rg_args, list) or any(not isinstance(arg, str) for arg in rg_args)
    ):
        raise HTTPException(status_code=400, detail="rg_args must be a list of strings")
    if not isinstance(query, str):
        raise HTTPException(status_code=400, detail="query must be a string")
    if rg_args is not None and query.strip():
        raise HTTPException(status_code=400, detail="provide either rg_args or query, not both")
    if rg_args is None and not query.strip():
        raise HTTPException(status_code=400, detail="rg_args or query is required")
    cwd = body.get("cwd", "")
    if not isinstance(cwd, str):
        raise HTTPException(status_code=400, detail="cwd must be a string")
    cwds = body.get("cwds")
    if cwds is not None and (
        not isinstance(cwds, list) or any(not isinstance(item, str) for item in cwds)
    ):
        raise HTTPException(status_code=400, detail="cwds must be a list of strings")
    all_projects = body.get("all_projects", False)
    if not isinstance(all_projects, bool):
        raise HTTPException(status_code=400, detail="all_projects must be a boolean")
    fields = body.get("fields")
    if fields is not None and (
        not isinstance(fields, list) or any(not isinstance(field, str) for field in fields)
    ):
        raise HTTPException(status_code=400, detail="fields must be a list of strings")
    include_all_fields = body.get("include_all_fields", False)
    if not isinstance(include_all_fields, bool):
        raise HTTPException(status_code=400, detail="include_all_fields must be a boolean")
    include_unprocessed_prompts = body.get("include_unprocessed_prompts", False)
    if not isinstance(include_unprocessed_prompts, bool):
        raise HTTPException(status_code=400, detail="include_unprocessed_prompts must be a boolean")
    provider_native_only = body.get("provider_native_only", True)
    if not isinstance(provider_native_only, bool):
        raise HTTPException(status_code=400, detail="provider_native_only must be a boolean")
    compare = body.get("compare", False)
    if not isinstance(compare, bool):
        raise HTTPException(status_code=400, detail="compare must be a boolean")
    max_matches = body.get("max_matches")
    if max_matches is not None and (
        not isinstance(max_matches, int) or isinstance(max_matches, bool) or max_matches <= 0
    ):
        raise HTTPException(status_code=400, detail="max_matches must be a positive integer when provided")

    return await run_supervised_requirements_search(
        "requirements.search",
        "unit_rg",
        rg_args=rg_args,
        query=query,
        cwd=cwd,
        cwds=cwds,
        all_projects=all_projects,
        fields=fields,
        include_all_fields=include_all_fields,
        include_unprocessed_prompts=include_unprocessed_prompts,
        provider_native_only=provider_native_only,
        compare=compare,
        max_matches=max_matches,
    )


# ── Project structure edit session ──────────────────────────────

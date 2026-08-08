from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response
from starlette.requests import ClientDisconnect

import extension_backend_transport
from extension_backend_transport import (
    ResponseTooLarge,
    RESPONSE_BODY_MAX_BYTES,
    RoundtripResult as _RoundtripResult,
    get_handle as _get_handle,
    get_semaphore as _get_semaphore,
    roundtrip as _roundtrip,
    roundtrip_blocking as _roundtrip_blocking,
    shutdown_persistent_backends,
    _extension_sdk_env,
    _host_env,
)
import extension_store
import perf

logger = logging.getLogger(__name__)

REQUEST_BODY_MAX_BYTES = 2 * 1024 * 1024
_HOST_TIMEOUT_SECONDS = 300
_CLIENT_CLOSED_REQUEST_STATUS = 499
_DEFAULT_MAX_CONCURRENCY = 8
# Responses at or below this size are decoded inline (base64 + JSON parse is
# cheap enough not to warrant a thread hop); larger ones are moved off the
# event loop so a big payload can't stall unrelated requests.
_DECODE_OFFLOAD_THRESHOLD_BYTES = 256 * 1024
# Allowlist of request headers forwarded to an extension backend subprocess.
# Fail-closed: anything not listed here is dropped, so a future secret header
# (auth, cookie, internal token, entitlement, …) can never leak to extension
# code merely because nobody remembered to add it to a denylist.
_ALLOWED_REQUEST_HEADERS = {
    b"content-type",
    b"accept",
    b"accept-encoding",
    b"accept-language",
    b"user-agent",
    b"x-request-id",
    b"idempotency-key",
}
_GET_COALESCE_HEADER_NAMES = frozenset({
    b"accept",
    b"accept-encoding",
    b"accept-language",
})
_BLOCKED_RESPONSE_HEADERS = {
    "content-length",
    "set-cookie",
    "transfer-encoding",
}
_METHODS_WITH_REQUEST_BODY = {"POST", "PUT", "PATCH", "DELETE"}
_EMPTY_B64 = ""


async def _report_worker_incident(
    *,
    kind: str,
    extension_id: str,
    activation_id: str,
    elapsed_seconds: float,
    path: str | None = None,
) -> bool:
    try:
        import node_client
        client = node_client.get()
    except Exception:
        return False
    import extension_incident_outbox
    try:
        incident = extension_incident_outbox.enqueue(
            kind=kind,
            extension_id=extension_id,
            activation_id=activation_id,
            elapsed_seconds=elapsed_seconds,
            path=path,
        )
    except extension_incident_outbox.IncidentOutboxFull:
        logger.error("extension incident outbox full; dropping %s for %s", kind, extension_id)
        return True
    try:
        await client.send_extension_incident(incident)
    except Exception as exc:
        logger.warning(
            "extension incident send failed; queued for reconnect replay: %s",
            exc,
        )
    return True


def _resolve_host_timeout(spec: dict[str, Any], path: str) -> float:
    """How long THIS host waits for one roundtrip to ``path``: the route's
    manifest-declared budget, else the host default. Binds the host's default to
    the shared resolution in ``extension_store`` — the lookup rules live there so
    the quarantine path cannot judge a route by a different budget."""
    return extension_store.resolve_route_timeout(
        spec.get("backend_timeouts"), path, default_seconds=_HOST_TIMEOUT_SECONDS
    )


def _resolve_max_concurrency(spec: dict[str, Any]) -> int:
    """Per-extension bulkhead capacity: the manifest's declared
    ``backend_max_concurrency``, else the host default. Protects the child
    process from an unbounded concurrent-request burst."""
    value = spec.get("backend_max_concurrency")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return _DEFAULT_MAX_CONCURRENCY


def _allows_backend_exit_retry(spec: dict[str, Any], path: str) -> bool:
    retry_paths = spec.get("backend_retry_on_exit")
    if not isinstance(retry_paths, list):
        return False
    normalized = path.strip("/")
    return normalized in {str(item).strip("/") for item in retry_paths}


def _safe_request_headers(request: Request) -> list[tuple[str, str]]:
    return [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in request.headers.raw
        if key.lower() in _ALLOWED_REQUEST_HEADERS
    ]


def _get_coalesce_headers(safe_headers: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (key.lower(), value)
        for key, value in safe_headers
        if key.lower().encode("latin-1") in _GET_COALESCE_HEADER_NAMES
    )


async def _read_limited_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > REQUEST_BODY_MAX_BYTES:
                raise HTTPException(status_code=413, detail="Extension request body is too large")
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise HTTPException(
            status_code=_CLIENT_CLOSED_REQUEST_STATUS,
            detail="Client disconnected while reading extension request body",
        ) from exc
    return b"".join(chunks)


@dataclass(frozen=True)
class _ChildTiming:
    queue_dispatch_ms: float
    decode_ms: float
    build_ms: float
    asgi_ms: float
    response_collect_ms: float
    response_encode_ms: float

    @property
    def measured_ms(self) -> float:
        return (
            self.queue_dispatch_ms
            + self.decode_ms
            + self.build_ms
            + self.asgi_ms
            + self.response_collect_ms
            + self.response_encode_ms
        )


# Cache of resolved extension backend specs, invalidated by store fingerprint.
_SPEC_CACHE_GUARD = threading.Lock()
_SPEC_CACHE: dict[str, tuple[extension_store.StoreFingerprint, dict[str, Any] | None]] = {}
# Single-flight map for coalesced GET requests, keyed per event loop.
_GET_INFLIGHT_GUARD = threading.Lock()
_GET_INFLIGHT: dict[
    tuple[str, str, str, tuple[tuple[str, str], ...], str],
    tuple[asyncio.AbstractEventLoop, asyncio.Task],
] = {}


def backend_entrypoint_spec_cached(extension_id: str) -> dict[str, Any] | None:
    fingerprint = extension_store.store_fingerprint()
    with _SPEC_CACHE_GUARD:
        cached = _SPEC_CACHE.get(extension_id)
        if cached is not None and cached[0] == fingerprint:
            spec = cached[1]
            return dict(spec) if spec is not None else None
    spec = extension_store.backend_entrypoint_spec(extension_id)
    with _SPEC_CACHE_GUARD:
        _SPEC_CACHE[extension_id] = (
            fingerprint,
            dict(spec) if spec is not None else None,
        )
    return spec


def _clear_spec_cache(extension_id: str | None = None) -> None:
    with _SPEC_CACHE_GUARD:
        if extension_id is None:
            _SPEC_CACHE.clear()
        else:
            _SPEC_CACHE.pop(extension_id, None)


def evict_persistent_backend(extension_id: str) -> None:
    """Kill + drop the persistent backend process for an extension, and drop
    its cached spec. Call on disable/uninstall so a deactivated extension
    stops serving."""
    _clear_spec_cache(extension_id)
    extension_backend_transport.evict_persistent_backend(extension_id)


async def _record_slow_call(
    spec: dict[str, Any], activation_id: str, path: str, elapsed_seconds: float
) -> None:
    # Same per-route threshold the store judges the incident by, so this skips
    # only calls that provably cannot become one — including a route whose
    # declared budget is below the global floor.
    if elapsed_seconds < extension_store.slow_call_threshold(
        spec.get("backend_timeouts"), path
    ):
        return
    extension_id = spec["extension_id"]
    if await _report_worker_incident(
        kind="slow_backend_call",
        extension_id=extension_id,
        activation_id=activation_id,
        elapsed_seconds=elapsed_seconds,
        path=path,
    ):
        return
    disabled = await asyncio.to_thread(
        extension_store.record_slow_backend_call,
        extension_id,
        activation_id=activation_id,
        elapsed_seconds=elapsed_seconds,
        path=path,
    )
    if not disabled:
        return
    import extension_api
    await extension_api._broadcast_extension_changed(*extension_api.EXTENSION_CATALOG_TOPICS)
    logger.warning(
        "extension backend needs a user health decision after repeated slow calls: %s",
        extension_id,
    )


async def _record_timeout(
    extension_id: str, activation_id: str, elapsed_seconds: float
) -> None:
    if await _report_worker_incident(
        kind="backend_timeout",
        extension_id=extension_id,
        activation_id=activation_id,
        elapsed_seconds=elapsed_seconds,
    ):
        return
    disabled = await asyncio.to_thread(
        extension_store.record_backend_timeout,
        extension_id,
        activation_id=activation_id,
        elapsed_seconds=elapsed_seconds,
    )
    if not disabled:
        return
    import extension_api
    await extension_api._broadcast_extension_changed(*extension_api.EXTENSION_CATALOG_TOPICS)
    logger.warning(
        "extension backend needs a user health decision after repeated timeouts: %s",
        extension_id,
    )


def _validated_child_timing(
    result: dict[str, Any], *, request_id: str, roundtrip_ms: float
) -> _ChildTiming | None:
    timing = result.get("timing")
    if not isinstance(timing, dict) or timing.get("version") != 1:
        return None
    if timing.get("request_id") != request_id:
        return None
    epoch = timing.get("process_epoch_ns")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        return None
    values: list[float] = []
    for key in (
        "queue_dispatch_ns",
        "decode_ns",
        "build_ns",
        "asgi_ns",
        "response_collect_ns",
        "response_encode_ns",
    ):
        value = timing.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if not math.isfinite(value) or value < 0:
            return None
        values.append(value / 1_000_000.0)
    child = _ChildTiming(*values)
    tolerance_ms = max(1.0, roundtrip_ms * 0.01)
    if child.measured_ms > roundtrip_ms + tolerance_ms:
        return None
    return child


def _record_child_timing(child: _ChildTiming, roundtrip_ms: float) -> None:
    perf.record("extension.backend.child.queue_dispatch", child.queue_dispatch_ms)
    perf.record("extension.backend.child.decode", child.decode_ms)
    perf.record("extension.backend.child.build", child.build_ms)
    perf.record("extension.backend.child.asgi", child.asgi_ms)
    perf.record("extension.backend.child.response_collect", child.response_collect_ms)
    perf.record("extension.backend.child.response_encode", child.response_encode_ms)
    perf.record("extension.backend.transport_residual", max(0.0, roundtrip_ms - child.measured_ms))


def _decode_backend_response_payload(
    line: bytes,
) -> tuple[dict[str, Any], int, bytes, dict[str, str]]:
    result = json.loads(line.decode("utf-8"))
    status = int(result["status"])
    content = base64.b64decode(str(result.get("body") or ""))
    headers = {
        str(key): str(value)
        for key, value in (result.get("headers") or [])
        if str(key).lower() not in _BLOCKED_RESPONSE_HEADERS
    }
    return result, status, content, headers


async def _build_backend_response(roundtrip: _RoundtripResult) -> Response:
    with perf.timed("extension.backend.invoke.decode"):
        try:
            if len(roundtrip.line) > _DECODE_OFFLOAD_THRESHOLD_BYTES:
                result, status, content, headers = await asyncio.to_thread(
                    _decode_backend_response_payload, roundtrip.line
                )
            else:
                result, status, content, headers = _decode_backend_response_payload(roundtrip.line)
            if result.get("id") != roundtrip.request_id:
                raise ValueError("extension backend response id mismatch")
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Extension backend returned an invalid response") from exc

    with perf.timed("extension.backend.invoke.response"):
        child_timing = _validated_child_timing(
            result,
            request_id=roundtrip.request_id,
            roundtrip_ms=roundtrip.elapsed_ms,
        )
        if child_timing is None:
            perf.record_count("extension.backend.child_timing_invalid")
        else:
            _record_child_timing(child_timing, roundtrip.elapsed_ms)
        if status >= 500:
            headers = {"content-type": "text/plain"}
            content = b"Extension backend failed"
        return Response(content=content, status_code=status, headers=headers)


async def _acquire_bulkhead(
    handle: extension_backend_transport._BackendProc,
    spec: dict[str, Any],
    *,
    extension_id: str,
    activation_id: str,
    deadline: float,
    invocation_started: float,
) -> asyncio.Semaphore:
    """Acquire the per-extension bulkhead permit, counting the wait against
    the whole-call deadline. On timeout, judged and reported exactly like a
    transport timeout (same breaker, same 504)."""
    capacity = _resolve_max_concurrency(spec)
    semaphore = _get_semaphore(handle, capacity)
    wait_started = time.monotonic()
    remaining = deadline - wait_started
    if remaining > 0:
        try:
            await asyncio.wait_for(semaphore.acquire(), remaining)
        except TimeoutError as exc:
            perf.record("extension.backend.semaphore_wait", (time.monotonic() - wait_started) * 1000.0)
            perf.record(
                f"extension.backend.semaphore_wait.{extension_id}",
                (time.monotonic() - wait_started) * 1000.0,
            )
            await _record_timeout(extension_id, activation_id, time.monotonic() - invocation_started)
            raise HTTPException(status_code=504, detail="Extension backend timed out") from exc
        perf.record("extension.backend.semaphore_wait", (time.monotonic() - wait_started) * 1000.0)
        perf.record(
            f"extension.backend.semaphore_wait.{extension_id}",
            (time.monotonic() - wait_started) * 1000.0,
        )
        return semaphore
    await _record_timeout(extension_id, activation_id, time.monotonic() - invocation_started)
    raise HTTPException(status_code=504, detail="Extension backend timed out")


async def _invoke_backend(
    spec: dict[str, Any],
    *,
    method: str,
    path: str,
    body_bytes: bytes,
    query_b64: str,
    safe_headers: list[tuple[str, str]],
    base_url: str,
) -> Response:
    """Dispatch one request to the extension's persistent backend process and
    return its Response. The router is loaded once per process (amortized over
    many requests); requests are multiplexed over the pipe so they run
    concurrently — a slow route does not block others. A per-extension
    bulkhead bounds concurrent in-flight calls to the child; the semaphore
    wait and the roundtrip share one deadline (the route's timeout budget). On
    expiry the request is abandoned (504) without killing the process; on
    process exit the next request respawns it. While the extension is
    pending a user health decision, requests fast-fail (503) instead of
    dispatching at all.

    Shared by the public ``/api/extensions/{id}/backend/*`` route (sourced from
    a FastAPI Request) and the inter-extension call endpoint (sourced from a
    JSON body). Callers must have already authenticated."""
    extension_id = spec["extension_id"]
    if extension_store.has_pending_health_decision(extension_id):
        raise HTTPException(
            status_code=503,
            detail="Extension backend is pending a health decision",
            headers={"Retry-After": "60"},
        )

    with perf.timed("extension.backend.invoke.payload"):
        body_b64 = (
            base64.b64encode(body_bytes).decode("ascii")
            if body_bytes
            else _EMPTY_B64
        )
        request_payload = {
            "method": method,
            "path": "/" + path.lstrip("/"),
            "query_string": query_b64,
            "headers": safe_headers,
            "body": body_b64,
        }
    activation_id = extension_store.activation_identity(extension_id)
    with perf.timed("extension.backend.invoke.handle"):
        handle = _get_handle(spec)
    with perf.timed("extension.backend.invoke.timeout"):
        timeout = _resolve_host_timeout(spec, path)
    invocation_started = time.monotonic()
    deadline = invocation_started + timeout

    semaphore = await _acquire_bulkhead(
        handle,
        spec,
        extension_id=extension_id,
        activation_id=activation_id,
        deadline=deadline,
        invocation_started=invocation_started,
    )
    handle.in_flight += 1
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            await _record_timeout(extension_id, activation_id, time.monotonic() - invocation_started)
            raise HTTPException(status_code=504, detail="Extension backend timed out")
        try:
            with perf.timed("extension.backend.invoke.roundtrip"):
                roundtrip = await _roundtrip(handle, spec, base_url, request_payload, remaining)
        except TimeoutError as exc:
            await _record_timeout(extension_id, activation_id, time.monotonic() - invocation_started)
            raise HTTPException(status_code=504, detail="Extension backend timed out") from exc
        except ResponseTooLarge as exc:
            evict_persistent_backend(extension_id)
            raise HTTPException(
                status_code=500,
                detail=f"Extension backend response exceeded the {RESPONSE_BODY_MAX_BYTES} byte size limit",
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("extension backend failed: %s", extension_id)
            raise HTTPException(status_code=500, detail="Extension backend failed") from exc

        if not roundtrip.line and _allows_backend_exit_retry(spec, path):
            evict_persistent_backend(extension_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await _record_timeout(extension_id, activation_id, time.monotonic() - invocation_started)
                raise HTTPException(status_code=504, detail="Extension backend timed out")
            try:
                with perf.timed("extension.backend.invoke.retry_after_exit"):
                    retry_handle = _get_handle(spec)
                    roundtrip = await _roundtrip(retry_handle, spec, base_url, request_payload, remaining)
            except TimeoutError as exc:
                await _record_timeout(extension_id, activation_id, time.monotonic() - invocation_started)
                raise HTTPException(status_code=504, detail="Extension backend timed out") from exc
            except ResponseTooLarge as exc:
                evict_persistent_backend(extension_id)
                raise HTTPException(
                    status_code=500,
                    detail=f"Extension backend response exceeded the {RESPONSE_BODY_MAX_BYTES} byte size limit",
                ) from exc

        if not roundtrip.line:
            evict_persistent_backend(extension_id)
            raise HTTPException(status_code=500, detail="Extension backend process exited")

        response = await _build_backend_response(roundtrip)
        # Judged on host-observed end-to-end elapsed (wait + dispatch +
        # response), not the child's self-reported ASGI time: a slow queue or
        # transport is exactly the kind of degradation the breaker exists to
        # catch, and a misbehaving/compromised child could under-report its
        # own asgi_ms to dodge quarantine.
        await _record_slow_call(spec, activation_id, path, time.monotonic() - invocation_started)
        return response
    finally:
        handle.in_flight -= 1
        semaphore.release()


async def dispatch_extension_backend_request(
    extension_id: str,
    path: str,
    request: Request,
    *,
    backend_spec: dict[str, Any] | None = None,
) -> Response:
    spec = backend_spec if backend_spec is not None else backend_entrypoint_spec_cached(extension_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="Extension is not installed")

    method = str(getattr(request, "method", "POST") or "POST").upper()
    body = (
        await _read_limited_body(request)
        if method in _METHODS_WITH_REQUEST_BODY
        else b""
    )
    query_b64 = (
        base64.b64encode(request.scope.get("query_string", b"")).decode("ascii")
        if request.scope.get("query_string")
        else _EMPTY_B64
    )
    safe_headers = _safe_request_headers(request)
    base_url = str(request.base_url).rstrip("/")
    if method != "GET" or body:
        return await _invoke_backend(
            spec,
            method=method,
            path=path,
            body_bytes=body,
            query_b64=query_b64,
            safe_headers=safe_headers,
            base_url=base_url,
        )
    return await _invoke_backend_get_coalesced(
        spec,
        method=method,
        path=path,
        body_bytes=body,
        query_b64=query_b64,
        safe_headers=safe_headers,
        base_url=base_url,
    )


def _clone_response(response: Response) -> Response:
    return Response(
        content=response.body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )


async def _invoke_backend_get_coalesced(
    spec: dict[str, Any],
    *,
    method: str,
    path: str,
    body_bytes: bytes,
    query_b64: str,
    safe_headers: list[tuple[str, str]],
    base_url: str,
) -> Response:
    loop = asyncio.get_running_loop()
    key = (
        str(spec["extension_id"]),
        path,
        query_b64,
        _get_coalesce_headers(safe_headers),
        base_url,
    )
    with _GET_INFLIGHT_GUARD:
        existing = _GET_INFLIGHT.get(key)
        if existing is not None and existing[0] is loop and not existing[1].done():
            task = existing[1]
            owner = False
        else:
            task = loop.create_task(
                _invoke_backend(
                    spec,
                    method=method,
                    path=path,
                    body_bytes=body_bytes,
                    query_b64=query_b64,
                    safe_headers=safe_headers,
                    base_url=base_url,
                )
            )
            _GET_INFLIGHT[key] = (loop, task)
            owner = True
    try:
        response = await task
        return response if owner else _clone_response(response)
    finally:
        if owner:
            with _GET_INFLIGHT_GUARD:
                current = _GET_INFLIGHT.get(key)
                if current is not None and current[1] is task:
                    _GET_INFLIGHT.pop(key, None)


async def invoke_extension_backend(
    extension_id: str,
    path: str,
    *,
    method: str = "POST",
    body_bytes: bytes = b"",
    base_url: str = "",
) -> Response:
    """Invoke an extension's backend handler from core (inter-extension calls).

    Same trust boundary as :func:`dispatch_extension_backend_request` — the
    caller must already be authenticated (internal token + active extension).
    Lets one extension reach another's exposed surface without core baking in
    any feature logic."""
    spec = backend_entrypoint_spec_cached(extension_id)
    if spec is None:
        surface_status = extension_store.backend_surface_status(extension_id)
        if surface_status == "unavailable":
            raise HTTPException(
                status_code=503,
                detail="Extension backend is unavailable",
                headers={"Retry-After": "60"},
            )
        raise HTTPException(status_code=404, detail="Extension has no backend surface")
    return await _invoke_backend(
        spec,
        method=method,
        path=path,
        body_bytes=body_bytes,
        query_b64=_EMPTY_B64,
        safe_headers=[("content-type", "application/json")] if body_bytes else [],
        base_url=base_url,
    )


_CORE_BACKEND_CONTRACTS = {
    "requirements.reconcile": {
        "role": "requirements",
        "path": "internal/reconcile",
        "header": ("x-better-agent-core-contract", "requirements-reconcile-v1"),
    },
}


async def invoke_core_backend_contract(
    contract: str,
    *,
    body_bytes: bytes,
) -> Response:
    spec = _CORE_BACKEND_CONTRACTS.get(contract)
    if spec is None:
        raise ValueError("unknown core extension backend contract")
    extension_id = await asyncio.to_thread(
        extension_store.extension_id_for_role,
        spec["role"],
    )
    if extension_id is None:
        return Response(
            content=b'{"status":"extension_absent"}',
            status_code=200,
            media_type="application/json",
        )
    backend_spec = await asyncio.to_thread(
        backend_entrypoint_spec_cached,
        extension_id,
    )
    if backend_spec is None:
        return Response(
            content=b'{"status":"extension_unavailable"}',
            status_code=503,
            media_type="application/json",
        )
    return await _invoke_backend(
        backend_spec,
        method="POST",
        path=spec["path"],
        body_bytes=body_bytes,
        query_b64=_EMPTY_B64,
        safe_headers=[
            ("content-type", "application/json"),
            spec["header"],
        ],
        base_url="",
    )


def invoke_extension_backend_sync(
    extension_id: str,
    path: str,
    *,
    method: str = "POST",
    body_bytes: bytes = b"",
    base_url: str = "",
) -> tuple[int, bytes]:
    spec = backend_entrypoint_spec_cached(extension_id)
    if spec is None:
        surface_status = extension_store.backend_surface_status(extension_id)
        if surface_status == "unavailable":
            return 503, b'{"detail":"Extension backend is unavailable","retry_after":60}'
        return 404, b'{"detail":"Extension has no backend surface"}'
    request_payload = {
        "method": method,
        "path": "/" + path.lstrip("/"),
        "query_string": _EMPTY_B64,
        "headers": [("content-type", "application/json")] if body_bytes else [],
        "body": base64.b64encode(body_bytes).decode("ascii") if body_bytes else _EMPTY_B64,
    }
    try:
        roundtrip = _roundtrip_blocking(
            _get_handle(spec), spec, base_url, request_payload, _resolve_host_timeout(spec, path)
        )
    except TimeoutError:
        return 504, b""
    except ResponseTooLarge:
        evict_persistent_backend(extension_id)
        return 500, b""
    if not roundtrip.line and _allows_backend_exit_retry(spec, path):
        evict_persistent_backend(extension_id)
        try:
            roundtrip = _roundtrip_blocking(
                _get_handle(spec), spec, base_url, request_payload, _resolve_host_timeout(spec, path)
            )
        except TimeoutError:
            return 504, b""
        except ResponseTooLarge:
            evict_persistent_backend(extension_id)
            return 500, b""
    if not roundtrip.line:
        evict_persistent_backend(extension_id)
        return 500, b""
    try:
        result = json.loads(roundtrip.line.decode("utf-8"))
        if result.get("id") != roundtrip.request_id:
            return 500, b""
        return int(result["status"]), base64.b64decode(str(result.get("body") or ""))
    except Exception:
        return 500, b""


_NAMED_CORE_DESTINATIONS = {
    "assistant.lag-report": ("ofek-dev.assistant", "assistant/bug-report"),
}


def invoke_named_core_destination_sync(
    capability: str,
    *,
    body_bytes: bytes,
    base_url: str = "",
) -> tuple[int, bytes]:
    """Invoke a fixed core-owned destination; durable data never selects a route."""
    destination = _NAMED_CORE_DESTINATIONS.get(capability)
    if destination is None:
        return 404, b'{"detail":"Unknown core destination"}'
    extension_id, path = destination
    record = extension_store.get_extension(extension_id)
    if (
        record is not None
        and record.get("enabled") is False
        and not record.get("quarantine")
    ):
        return 410, b'{"detail":"Core destination is disabled"}'
    return invoke_extension_backend_sync(
        extension_id,
        path,
        method="POST",
        body_bytes=body_bytes,
        base_url=base_url,
    )

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response

from env_compat import dual_env_many
import extension_store

logger = logging.getLogger(__name__)

_MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
_HOST_TIMEOUT_SECONDS = 30
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
_BLOCKED_RESPONSE_HEADERS = {
    "content-length",
    "set-cookie",
    "transfer-encoding",
}


def _resolve_host_timeout(spec: dict[str, Any], path: str) -> float:
    """Per-route timeout for one extension-backend roundtrip. Looks up the
    request subpath in the manifest-declared ``backend_timeouts`` (exact match,
    then longest segment-prefix, then ``default``), falling back to the global
    ``_HOST_TIMEOUT_SECONDS``."""
    timeouts = spec.get("backend_timeouts")
    if not isinstance(timeouts, dict) or not timeouts:
        return _HOST_TIMEOUT_SECONDS
    p = path.strip("/")
    chosen = timeouts.get(p)
    if not isinstance(chosen, (int, float)) or isinstance(chosen, bool):
        best_len = -1
        for key, value in timeouts.items():
            if key == "default" or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            k = str(key).strip("/")
            if (p == k or p.startswith(k + "/")) and len(k) > best_len:
                best_len, chosen = len(k), value
        if best_len < 0:
            chosen = timeouts.get("default")
    if isinstance(chosen, (int, float)) and not isinstance(chosen, bool) and chosen > 0:
        return float(chosen)
    return _HOST_TIMEOUT_SECONDS


def _host_env() -> dict[str, str]:
    env = {
        "PYTHONIOENCODING": "utf-8",
    }
    path = os.environ.get("PATH")
    if path:
        env["PATH"] = path
    marketplace_base_url = os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL")
    if marketplace_base_url:
        env["BETTER_AGENT_MARKETPLACE_BASE_URL"] = marketplace_base_url
    return env


def _extension_sdk_env(spec: dict[str, Any], base_url: str) -> dict[str, str]:
    env: dict[str, str] = dual_env_many({
        "BETTER_CLAUDE_EXTENSION_ID": str(spec["extension_id"]),
        "BETTER_CLAUDE_BACKEND_URL": base_url,
    })
    effective = spec.get("effective_permissions") or {}
    if effective.get("internal_loopback") is True:
        from orchestrator import get_active_coordinator

        coordinator = get_active_coordinator()
        if coordinator is not None:
            # Per-extension token: the backend derives identity from this
            # secret, so the extension can never impersonate another.
            token = coordinator.mint_extension_token(str(spec["extension_id"]))
            env.update(dual_env_many({"BETTER_CLAUDE_INTERNAL_TOKEN": token}))
        else:
            import extension_token_registry
            env.update(dual_env_many({
                "BETTER_CLAUDE_INTERNAL_TOKEN": extension_token_registry.mint(str(spec["extension_id"])),
            }))
    sdk_path = str(spec.get("sdk_pythonpath") or "")
    if sdk_path:
        env["PYTHONPATH"] = sdk_path
    return env


def _safe_request_headers(request: Request) -> list[tuple[str, str]]:
    return [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in request.headers.raw
        if key.lower() in _ALLOWED_REQUEST_HEADERS
    ]


async def _read_limited_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_REQUEST_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Extension request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass
class _BackendProc:
    """One long-lived backend subprocess for an extension. The handle and its
    lock are stable for the extension's lifetime; ``proc`` (a blocking
    ``subprocess.Popen``) is replaced when the process dies and is restarted
    under the same lock. Loop-independent: proc I/O runs in executor threads, so
    a handle created on one event loop serves requests on any loop (TestClient
    uses a fresh loop per request; uvicorn uses one)."""
    extension_id: str
    proc: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)


# extension_id -> its persistent backend handle (lazy-started, shared across requests)
_PERSISTENT_PROCS: dict[str, _BackendProc] = {}
# Guards _PERSISTENT_PROCS dict access (multiple loops/threads may touch it).
_PROCS_GUARD = threading.Lock()
_SPEC_CACHE_GUARD = threading.Lock()
_SPEC_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any] | None]] = {}


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


def _spawn_persistent_proc(spec: dict[str, Any], base_url: str) -> Any:
    """Start the persistent host for ``spec`` and send it the extension spec
    (install path / entrypoint / source) as the first stdin line. The router is
    loaded once inside the host; subsequent stdin lines are requests. Blocking —
    run in an executor thread, never on the event loop."""
    host = Path(__file__).with_name("extension_backend_host.py")
    spec_payload = {
        "extension_id": spec["extension_id"],
        "install_path": spec["install_path"],
        "entrypoint": spec["entrypoint"],
        "entrypoint_kind": spec.get("entrypoint_kind") or "file",
        "source": spec["source"],
    }
    proc = subprocess.Popen(
        [sys.executable, str(host), "--persistent"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        env={**_host_env(), **_extension_sdk_env(spec, base_url)},
        cwd=str(spec["install_path"]),
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(spec_payload).encode("utf-8") + b"\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        logger.exception("extension backend subprocess failed to start: %s", spec["extension_id"])
        raise RuntimeError("Extension backend failed to start") from exc
    return proc


def _get_handle(spec: dict[str, Any]) -> _BackendProc:
    """Return the persistent handle for an extension, creating a (proc-less) one
    on first use. The proc itself is spawned lazily inside _roundtrip."""
    key = spec["extension_id"]
    with _PROCS_GUARD:
        handle = _PERSISTENT_PROCS.get(key)
        if handle is None:
            handle = _BackendProc(extension_id=key)
            _PERSISTENT_PROCS[key] = handle
        return handle


def _roundtrip(handle: _BackendProc, spec: dict[str, Any], base_url: str, request_payload: dict[str, Any]) -> bytes:
    """Write one request line + read one response line from the persistent proc.
    Runs in an executor thread; the handle lock serializes requests to this
    extension's single process. Restarts the process if it died."""
    with handle.lock:
        proc = handle.proc
        if proc is None or proc.poll() is not None:
            proc = _spawn_persistent_proc(spec, base_url)
            handle.proc = proc
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps(request_payload).encode("utf-8") + b"\n")
            proc.stdin.flush()
            return proc.stdout.readline()
        except (BrokenPipeError, OSError):
            return b""


def _roundtrip_with_timeout(
    handle: _BackendProc,
    spec: dict[str, Any],
    base_url: str,
    request_payload: dict[str, Any],
    timeout: float,
) -> bytes:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_roundtrip, handle, spec, base_url, request_payload)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        if handle.proc is not None and handle.proc.poll() is None:
            handle.proc.kill()
            try:
                handle.proc.wait(timeout=1)
            except Exception:
                pass
        evict_persistent_backend(spec["extension_id"])
        return b""
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def evict_persistent_backend(extension_id: str) -> None:
    """Kill + drop the persistent backend process for an extension. Call on
    disable/uninstall so a deactivated extension stops serving."""
    _clear_spec_cache(extension_id)
    with _PROCS_GUARD:
        handle = _PERSISTENT_PROCS.pop(extension_id, None)
    if handle is not None and handle.proc is not None and handle.proc.poll() is None:
        handle.proc.kill()


def shutdown_persistent_backends() -> None:
    """Kill every persistent backend process (app shutdown). Sync — safe to call
    from an async lifespan directly."""
    for extension_id in list(_PERSISTENT_PROCS.keys()):
        evict_persistent_backend(extension_id)


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
    many requests); requests to one extension are serialized by its handle lock.
    On timeout or crash the process is killed and the next request restarts it.

    Shared by the public ``/api/extensions/{id}/backend/*`` route (sourced from
    a FastAPI Request) and the inter-extension call endpoint (sourced from a
    JSON body). Callers must have already authenticated."""
    request_payload = {
        "method": method,
        "path": "/" + path.lstrip("/"),
        "query_string": query_b64,
        "headers": safe_headers,
        "body": base64.b64encode(body_bytes).decode("ascii"),
    }
    handle = _get_handle(spec)
    try:
        response_line = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, _roundtrip, handle, spec, base_url, request_payload
            ),
            timeout=_resolve_host_timeout(spec, path),
        )
    except TimeoutError as exc:
        # Kill the stuck process so the blocked roundtrip thread unblocks and
        # the next request restarts under the lock.
        if handle.proc is not None and handle.proc.poll() is None:
            handle.proc.kill()
            try:
                handle.proc.wait(timeout=1)
            except Exception:
                pass
        raise HTTPException(status_code=504, detail="Extension backend timed out") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("extension backend failed: %s", spec["extension_id"])
        raise HTTPException(status_code=500, detail="Extension backend failed") from exc

    if not response_line:
        evict_persistent_backend(spec["extension_id"])
        raise HTTPException(status_code=500, detail="Extension backend process exited")

    try:
        result = json.loads(response_line.decode("utf-8"))
        status = int(result["status"])
        content = base64.b64decode(str(result.get("body") or ""))
        headers = {
            str(key): str(value)
            for key, value in (result.get("headers") or [])
            if str(key).lower() not in _BLOCKED_RESPONSE_HEADERS
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Extension backend returned an invalid response") from exc

    if status >= 500:
        headers = {"content-type": "text/plain"}
        content = b"Extension backend failed"
    return Response(content=content, status_code=status, headers=headers)


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

    body = await _read_limited_body(request)
    return await _invoke_backend(
        spec,
        method=request.method,
        path=path,
        body_bytes=body,
        query_b64=base64.b64encode(request.scope.get("query_string", b"")).decode("ascii"),
        safe_headers=_safe_request_headers(request),
        base_url=str(request.base_url).rstrip("/"),
    )


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
        raise HTTPException(status_code=404, detail="Extension has no backend surface")
    return await _invoke_backend(
        spec,
        method=method,
        path=path,
        body_bytes=body_bytes,
        query_b64=base64.b64encode(b"").decode("ascii"),
        safe_headers=[("content-type", "application/json")] if body_bytes else [],
        base_url=base_url,
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
        return 404, b""
    request_payload = {
        "method": method,
        "path": "/" + path.lstrip("/"),
        "query_string": base64.b64encode(b"").decode("ascii"),
        "headers": [("content-type", "application/json")] if body_bytes else [],
        "body": base64.b64encode(body_bytes).decode("ascii"),
    }
    line = _roundtrip_with_timeout(
        _get_handle(spec), spec, base_url, request_payload, _resolve_host_timeout(spec, path)
    )
    if not line:
        evict_persistent_backend(extension_id)
        return 500, b""
    try:
        result = json.loads(line.decode("utf-8"))
        return int(result["status"]), base64.b64decode(str(result.get("body") or ""))
    except Exception:
        return 500, b""

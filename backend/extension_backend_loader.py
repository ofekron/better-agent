from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response
from starlette.requests import ClientDisconnect

from env_compat import dual_env_many
import extension_store

logger = logging.getLogger(__name__)

_MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
_HOST_TIMEOUT_SECONDS = 300
_CLIENT_CLOSED_REQUEST_STATUS = 499
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
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > _MAX_REQUEST_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Extension request body is too large")
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise HTTPException(
            status_code=_CLIENT_CLOSED_REQUEST_STATUS,
            detail="Client disconnected while reading extension request body",
        ) from exc
    return b"".join(chunks)


@dataclass
class _Channel:
    """One spawned subprocess plus the multiplexing state bound to it. Many
    concurrent requests share the single stdin/stdout pipe: each request is
    tagged with a unique id, a dedicated reader thread demuxes response lines
    back to the waiting request's queue by id, so a slow route never
    head-of-line-blocks another. ``lock`` guards only ``pending`` membership and
    ``alive`` (held briefly); ``write_lock`` serializes stdin writes (one whole
    line at a time) and is deliberately separate so a blocked write never holds
    the lock the reader needs to deliver responses. A channel is immutable once
    dead: on process exit the reader marks ``alive=False`` and fails every
    in-flight waiter; the next request spawns a fresh channel."""
    proc: Any
    lock: threading.Lock = field(default_factory=threading.Lock)
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    pending: dict = field(default_factory=dict)
    alive: bool = True


@dataclass
class _BackendProc:
    """Stable per-extension handle for the extension's lifetime. ``channel`` (the
    live subprocess + its multiplex state) is replaced under ``lifecycle_lock``
    when the process dies. Loop-independent: proc I/O runs in a reader thread and
    requests block on a queue, so a handle created on one event loop serves
    requests on any loop (TestClient uses a fresh loop per request; uvicorn uses
    one)."""
    extension_id: str
    channel: Any = None
    lifecycle_lock: threading.Lock = field(default_factory=threading.Lock)


# Dedicated executor for the blocking roundtrip wait. Isolated from the event
# loop's default executor so many long-blocking extension calls (e.g. a 900s
# session search) cannot starve unrelated core run_in_executor work. Beyond this
# many concurrent extension calls queue rather than running in parallel.
_ROUNDTRIP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=64, thread_name_prefix="ext-backend"
)

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
    # Drain stderr continuously: an undrained PIPE fills (~64KB) and blocks the
    # subprocess, which with multiplexing would stall every in-flight request on
    # this channel. Logged at debug only (off by default) to avoid leaking
    # extension output into core logs in normal operation.
    threading.Thread(
        target=_drain_stderr, args=(str(spec["extension_id"]), proc.stderr), daemon=True
    ).start()
    return proc


def _drain_stderr(extension_id: str, stream: Any) -> None:
    if stream is None:
        return
    try:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                logger.debug("extension backend stderr [%s]: %s", extension_id, text[:500])
    except Exception:
        pass


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


def _reader_loop(channel: _Channel) -> None:
    """Demux response lines for one channel's subprocess. Reads whole lines from
    stdout, routes each to the waiting request's queue by echoed id, and on
    process exit (empty read) marks the channel dead and fails every in-flight
    waiter so they don't hang. Late responses for an abandoned (timed-out)
    request find no pending entry and are dropped."""
    stdout = channel.proc.stdout
    while True:
        try:
            line = stdout.readline()
        except (OSError, ValueError):
            line = b""
        if not line:
            break
        try:
            rid = json.loads(line).get("id")
        except Exception:
            continue
        if not isinstance(rid, str):
            continue
        with channel.lock:
            waiter = channel.pending.get(rid)
        if waiter is not None:
            try:
                waiter.put_nowait(line)
            except queue.Full:
                pass
    with channel.lock:
        channel.alive = False
        waiters = list(channel.pending.values())
        channel.pending.clear()
    for waiter in waiters:
        try:
            waiter.put_nowait(b"")
        except queue.Full:
            pass


def _ensure_channel(handle: _BackendProc, spec: dict[str, Any], base_url: str) -> _Channel:
    """Return the handle's live channel, spawning a fresh subprocess + reader
    thread if none exists or the current one died. Serialized by
    ``lifecycle_lock`` so a burst of first requests spawns exactly one proc."""
    with handle.lifecycle_lock:
        channel = handle.channel
        if channel is not None and channel.alive and channel.proc.poll() is None:
            return channel
        proc = _spawn_persistent_proc(spec, base_url)
        channel = _Channel(proc=proc)
        handle.channel = channel
        threading.Thread(target=_reader_loop, args=(channel,), daemon=True).start()
        return channel


def _roundtrip(
    handle: _BackendProc,
    spec: dict[str, Any],
    base_url: str,
    request_payload: dict[str, Any],
    timeout: float,
) -> bytes:
    """Send one id-tagged request over the multiplexed pipe and wait up to
    ``timeout`` for its response. Concurrent calls share the subprocess without
    serializing. Raises ``TimeoutError`` if the response does not arrive in
    time (the request is abandoned; the subprocess is NOT killed, so other
    in-flight requests keep running). Returns ``b""`` if the process died."""
    rid = uuid.uuid4().hex
    line = json.dumps({**request_payload, "id": rid}).encode("utf-8") + b"\n"
    channel = _ensure_channel(handle, spec, base_url)
    waiter: queue.Queue = queue.Queue(maxsize=1)
    with channel.lock:
        if not channel.alive:
            return b""
        channel.pending[rid] = waiter
    try:
        with channel.write_lock:
            try:
                channel.proc.stdin.write(line)
                channel.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                return b""
        try:
            response = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("extension backend roundtrip timed out") from exc
        return response
    finally:
        with channel.lock:
            channel.pending.pop(rid, None)


def evict_persistent_backend(extension_id: str) -> None:
    """Kill + drop the persistent backend process for an extension. Call on
    disable/uninstall so a deactivated extension stops serving."""
    _clear_spec_cache(extension_id)
    with _PROCS_GUARD:
        handle = _PERSISTENT_PROCS.pop(extension_id, None)
    if handle is None:
        return
    channel = handle.channel
    if channel is not None and channel.proc is not None and channel.proc.poll() is None:
        channel.proc.kill()


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
    many requests); requests are multiplexed over the pipe so they run
    concurrently — a slow route does not block others. On a per-route timeout
    the request is abandoned (504) without killing the process; on process exit
    the next request respawns it.

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
    timeout = _resolve_host_timeout(spec, path)
    try:
        response_line = await asyncio.get_running_loop().run_in_executor(
            _ROUNDTRIP_EXECUTOR, _roundtrip, handle, spec, base_url, request_payload, timeout
        )
    except TimeoutError as exc:
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
    try:
        line = _roundtrip(
            _get_handle(spec), spec, base_url, request_payload, _resolve_host_timeout(spec, path)
        )
    except TimeoutError:
        return 500, b""
    if not line:
        evict_persistent_backend(extension_id)
        return 500, b""
    try:
        result = json.loads(line.decode("utf-8"))
        return int(result["status"]), base64.b64decode(str(result.get("body") or ""))
    except Exception:
        return 500, b""

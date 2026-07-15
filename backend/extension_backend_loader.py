from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response
from starlette.requests import ClientDisconnect

from env_compat import dual_env_many
import extension_store
import perf
from proc_control import process_control

logger = logging.getLogger(__name__)

REQUEST_BODY_MAX_BYTES = 2 * 1024 * 1024
_HOST_TIMEOUT_SECONDS = 300
_GLOBAL_MAX_IN_FLIGHT = 32
_PER_EXTENSION_MAX_IN_FLIGHT = 8
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


def _path_pattern_matches(pattern: str, path_segments: list[str]) -> bool:
    """``pattern`` is ``/``-separated; each segment is a literal or a single
    ``*`` wildcard matching exactly one dynamic path segment (e.g. a resource
    id). Lengths must match — no prefix/suffix bleed across route shapes."""
    pattern_segments = pattern.split("/")
    if len(pattern_segments) != len(path_segments):
        return False
    return all(ps == "*" or ps == seg for ps, seg in zip(pattern_segments, path_segments))


def _resolve_slow_call_grace(spec: dict[str, Any], path: str) -> float:
    """Per-route grace period (seconds) before a call counts as a slow-call
    quarantine strike. Looks up the request path against the manifest-declared
    ``slow_call_grace_seconds`` (exact/wildcard-segment match, most-literal-
    segments-wins, falling back to ``default``); routes with no match keep the
    tight platform-wide ``EXTENSION_SLOW_CALL_SECONDS`` SLA. This is a
    separate field from ``backend_timeouts`` — declaring a longer host
    timeout does not implicitly widen the quarantine SLA. On an exact tie in
    literal-segment count between two matching patterns, the first one in
    manifest dict order wins (``dict`` preserves insertion order; the loop
    below only replaces the current best on a strict improvement)."""
    grace = spec.get("slow_call_grace_seconds")
    if not isinstance(grace, dict) or not grace:
        return extension_store.EXTENSION_SLOW_CALL_SECONDS
    path_segments = path.strip("/").split("/")
    best_value: float | None = None
    best_specificity = -1
    for pattern, value in grace.items():
        if pattern == "default" or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        pattern_segments = str(pattern).split("/")
        if _path_pattern_matches(str(pattern), path_segments):
            specificity = sum(1 for seg in pattern_segments if seg != "*")
            if specificity > best_specificity:
                best_specificity, best_value = specificity, value
    if best_value is None:
        default_value = grace.get("default")
        if isinstance(default_value, (int, float)) and not isinstance(default_value, bool):
            best_value = default_value
    if best_value is None or best_value <= 0:
        return extension_store.EXTENSION_SLOW_CALL_SECONDS
    return max(extension_store.EXTENSION_SLOW_CALL_SECONDS, float(best_value))


def _allows_backend_exit_retry(spec: dict[str, Any], path: str) -> bool:
    retry_paths = spec.get("backend_retry_on_exit")
    if not isinstance(retry_paths, list):
        return False
    normalized = path.strip("/")
    return normalized in {str(item).strip("/") for item in retry_paths}


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
    declared = spec.get("permissions") or {}
    if effective.get("internal_loopback") is True or bool(declared.get("capabilities")):
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
    admission: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(_PER_EXTENSION_MAX_IN_FLIGHT)
    )


@dataclass(frozen=True)
class _RoundtripResult:
    line: bytes
    request_id: str
    elapsed_ms: float
    # Timeouts are reported as a result rather than raised from the executor
    # thread: an awaiter cancelled mid-flight (client disconnect) would leave a
    # raised exception unretrieved in the shielded future, which asyncio logs
    # as "exception in shielded future".
    timed_out: bool = False


@dataclass(frozen=True)
class _ChildTiming:
    queue_dispatch_ms: float
    decode_ms: float
    build_ms: float
    asgi_ms: float
    response_collect_ms: float
    response_encode_ms: float
    cohort_process_cpu_ms: float
    scheduler_max_delay_ms: float
    cohort_overlap_ms: float
    concurrent_requests: int

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

    @property
    def attributable_asgi_ms(self) -> float:
        if self.asgi_ms < extension_store.EXTENSION_SLOW_CALL_SECONDS * 1000.0:
            return self.asgi_ms
        scheduler_starved = self.scheduler_max_delay_ms >= min(250.0, self.asgi_ms * 0.25)
        if scheduler_starved:
            return 0.0
        return self.asgi_ms


# Dedicated executor for the blocking roundtrip wait. Isolated from the event
# loop's default executor so many long-blocking extension calls (e.g. a 900s
# session search) cannot starve unrelated core run_in_executor work. Beyond this
# admission rejects excess calls before executor submission.
_ROUNDTRIP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_GLOBAL_MAX_IN_FLIGHT, thread_name_prefix="ext-backend"
)
_GLOBAL_ADMISSION = threading.BoundedSemaphore(_GLOBAL_MAX_IN_FLIGHT)

# extension_id -> its persistent backend handle (lazy-started, shared across requests)
_PERSISTENT_PROCS: dict[str, _BackendProc] = {}
# Guards _PERSISTENT_PROCS dict access (multiple loops/threads may touch it).
_PROCS_GUARD = threading.Lock()
_SPEC_CACHE_GUARD = threading.Lock()
_SPEC_CACHE: dict[str, tuple[extension_store.StoreFingerprint, dict[str, Any] | None]] = {}
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
        "max_concurrency": _PER_EXTENSION_MAX_IN_FLIGHT,
    }
    proc = subprocess.Popen(
        [sys.executable, str(host), "--persistent"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        env={**_host_env(), **_extension_sdk_env(spec, base_url)},
        cwd=str(spec["install_path"]),
        **process_control().detach_spawn_kwargs(),
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
) -> _RoundtripResult:
    """Send one id-tagged request over the multiplexed pipe and wait up to
    ``timeout`` for its response. Concurrent calls share the subprocess without
    serializing. A response that does not arrive in time returns a
    ``timed_out`` result (the request is abandoned; the subprocess is NOT
    killed, so other in-flight requests keep running). Returns ``b""`` if the
    process died."""
    rid = uuid.uuid4().hex
    started = time.monotonic()
    line = json.dumps({**request_payload, "id": rid}).encode("utf-8") + b"\n"
    channel = _ensure_channel(handle, spec, base_url)
    waiter: queue.Queue = queue.Queue(maxsize=1)
    with channel.lock:
        if not channel.alive:
            return _RoundtripResult(b"", rid, (time.monotonic() - started) * 1000.0)
        channel.pending[rid] = waiter
    try:
        with channel.write_lock:
            try:
                channel.proc.stdin.write(line)
                channel.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                return _RoundtripResult(b"", rid, (time.monotonic() - started) * 1000.0)
        try:
            response = waiter.get(timeout=timeout)
        except queue.Empty:
            return _RoundtripResult(
                b"", rid, (time.monotonic() - started) * 1000.0, timed_out=True
            )
        return _RoundtripResult(response, rid, (time.monotonic() - started) * 1000.0)
    finally:
        with channel.lock:
            channel.pending.pop(rid, None)


def _acquire_admission(handle: _BackendProc) -> bool:
    if not _GLOBAL_ADMISSION.acquire(blocking=False):
        return False
    if handle.admission.acquire(blocking=False):
        return True
    _GLOBAL_ADMISSION.release()
    return False


def _release_admission(handle: _BackendProc) -> None:
    handle.admission.release()
    _GLOBAL_ADMISSION.release()


def _roundtrip_sync_admitted(
    handle: _BackendProc,
    spec: dict[str, Any],
    base_url: str,
    request_payload: dict[str, Any],
    timeout: float,
) -> _RoundtripResult:
    if not _acquire_admission(handle):
        raise BlockingIOError("extension backend capacity is exhausted")
    try:
        result = _roundtrip(handle, spec, base_url, request_payload, timeout)
    finally:
        _release_admission(handle)
    if result.timed_out:
        raise TimeoutError("extension backend roundtrip timed out")
    return result


async def _roundtrip_async_admitted(
    handle: _BackendProc,
    spec: dict[str, Any],
    base_url: str,
    request_payload: dict[str, Any],
    timeout: float,
) -> _RoundtripResult:
    if not _acquire_admission(handle):
        raise BlockingIOError("extension backend capacity is exhausted")
    def _reserved_roundtrip() -> _RoundtripResult:
        try:
            return _roundtrip(handle, spec, base_url, request_payload, timeout)
        finally:
            _release_admission(handle)

    try:
        future = asyncio.get_running_loop().run_in_executor(
            _ROUNDTRIP_EXECUTOR,
            _reserved_roundtrip,
        )
    except BaseException:
        _release_admission(handle)
        raise
    # The shield keeps admission accounting intact when the awaiter is
    # cancelled; the roundtrip completes with a plain result even on timeout,
    # so an abandoned future never holds an unretrieved exception.
    result = await asyncio.shield(future)
    if result.timed_out:
        raise TimeoutError("extension backend roundtrip timed out")
    return result


def _kill_and_reap(proc: Any, *, wait: bool) -> None:
    def _kill_tree() -> None:
        controller = process_control()
        controller.kill_detached_descendant_groups(proc.pid)
        controller.kill_tree(proc)

    if wait:
        _kill_tree()
        return
    threading.Thread(target=_kill_tree, daemon=True).start()


def evict_persistent_backend(extension_id: str, *, wait: bool = False) -> None:
    """Kill + drop the persistent backend process for an extension. Call on
    disable/uninstall so a deactivated extension stops serving."""
    _clear_spec_cache(extension_id)
    with _PROCS_GUARD:
        handle = _PERSISTENT_PROCS.pop(extension_id, None)
    if handle is None:
        return
    channel = handle.channel
    if channel is not None and channel.proc is not None:
        with channel.lock:
            channel.alive = False
        _kill_and_reap(channel.proc, wait=wait)


def shutdown_persistent_backends() -> None:
    """Kill every persistent backend process (app shutdown). Sync — safe to call
    from an async lifespan directly."""
    for extension_id in list(_PERSISTENT_PROCS.keys()):
        evict_persistent_backend(extension_id, wait=True)


async def _record_slow_call(
    extension_id: str, activation_id: str, elapsed_seconds: float, minimum_seconds: float
) -> None:
    if elapsed_seconds < minimum_seconds:
        return
    disabled = await asyncio.to_thread(
        extension_store.record_slow_backend_call,
        extension_id,
        activation_id=activation_id,
        elapsed_seconds=elapsed_seconds,
        minimum_seconds=minimum_seconds,
    )
    if not disabled:
        return
    import extension_api
    await extension_api._broadcast_extensions_changed()
    logger.error(
        "extension backend quarantined after repeated slow calls: %s",
        extension_id,
    )


async def _record_timeout(
    extension_id: str, activation_id: str, elapsed_seconds: float
) -> None:
    disabled = await asyncio.to_thread(
        extension_store.record_backend_timeout,
        extension_id,
        activation_id=activation_id,
        elapsed_seconds=elapsed_seconds,
    )
    if not disabled:
        return
    import extension_api
    await extension_api._broadcast_extensions_changed()
    logger.error("extension backend quarantined after repeated timeouts: %s", extension_id)


def _validated_child_timing(
    result: dict[str, Any], *, request_id: str, roundtrip_ms: float
) -> _ChildTiming | None:
    timing = result.get("timing")
    if not isinstance(timing, dict) or timing.get("version") != 3:
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
        "cohort_process_cpu_ns",
        "scheduler_max_delay_ns",
        "cohort_overlap_ns",
    ):
        value = timing.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if not math.isfinite(value) or value < 0:
            return None
        values.append(value / 1_000_000.0)
    concurrent_requests = timing.get("concurrent_requests")
    if (
        isinstance(concurrent_requests, bool)
        or not isinstance(concurrent_requests, int)
        or concurrent_requests < 1
        or concurrent_requests > 10_000
    ):
        return None
    child = _ChildTiming(*values, concurrent_requests)
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
    perf.record("extension.backend.child.cohort_process_cpu", child.cohort_process_cpu_ms)
    perf.record("extension.backend.child.request_owned_cpu_available", 0.0)
    perf.record("extension.backend.child.scheduler_max_delay", child.scheduler_max_delay_ms)
    perf.record("extension.backend.child.concurrent_requests", float(child.concurrent_requests))
    perf.record("extension.backend.child.cohort_overlap", child.cohort_overlap_ms)
    perf.record("extension.backend.child.attributable_asgi", child.attributable_asgi_ms)
    if child.asgi_ms >= extension_store.EXTENSION_SLOW_CALL_SECONDS * 1000.0 and child.attributable_asgi_ms == 0.0:
        perf.record_count("extension.backend.child.system_starvation_excluded")
    perf.record("extension.backend.transport_residual", max(0.0, roundtrip_ms - child.measured_ms))


async def _invoke_backend(
    spec: dict[str, Any],
    *,
    method: str,
    path: str,
    body_bytes: bytes,
    query_b64: str,
    safe_headers: list[tuple[str, str]],
    base_url: str,
    timeout_ceiling: float | None = None,
) -> Response:
    """Dispatch one request to the extension's persistent backend process and
    return its Response. The router is loaded once per process (amortized over
    many requests); requests are multiplexed over the pipe so they run
    concurrently — a slow route does not block others. On a per-route timeout
    the request is abandoned (504) without killing the process; on process exit
    the next request respawns it.

    ``timeout_ceiling`` caps the TOTAL invocation (including the exit-retry)
    regardless of the manifest-declared route timeout. Latency-budgeted hot
    paths own their budget here — never by cancelling the awaited call from
    outside, which would abandon the roundtrip mid-flight and bypass timeout
    accounting.

    Shared by the public ``/api/extensions/{id}/backend/*`` route (sourced from
    a FastAPI Request) and the inter-extension call endpoint (sourced from a
    JSON body). Callers must have already authenticated."""
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
    extension_id = spec["extension_id"]
    activation_id = extension_store.activation_identity(extension_id)
    with perf.timed("extension.backend.invoke.handle"):
        handle = _get_handle(spec)
    with perf.timed("extension.backend.invoke.timeout"):
        timeout = _resolve_host_timeout(spec, path)
        if timeout_ceiling is not None:
            timeout = min(timeout, timeout_ceiling)
    invocation_started = time.monotonic()
    deadline = None if timeout_ceiling is None else invocation_started + timeout_ceiling
    try:
        with perf.timed("extension.backend.invoke.roundtrip"):
            roundtrip = await _roundtrip_async_admitted(
                handle,
                spec,
                base_url,
                request_payload,
                timeout,
            )
    except BlockingIOError as exc:
        perf.record_count("extension.backend.overloaded")
        raise HTTPException(
            status_code=503,
            detail="Extension backend is busy",
            headers={"Retry-After": "1"},
        ) from exc
    except TimeoutError as exc:
        await _record_timeout(extension_id, activation_id, time.monotonic() - invocation_started)
        raise HTTPException(status_code=504, detail="Extension backend timed out") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("extension backend failed: %s", spec["extension_id"])
        raise HTTPException(status_code=500, detail="Extension backend failed") from exc

    if not roundtrip.line and _allows_backend_exit_retry(spec, path):
        evict_persistent_backend(spec["extension_id"])
        try:
            with perf.timed("extension.backend.invoke.retry_after_exit"):
                retry_timeout = timeout
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("extension backend call budget exhausted")
                    retry_timeout = min(timeout, remaining)
                retry_handle = _get_handle(spec)
                roundtrip = await _roundtrip_async_admitted(
                    retry_handle,
                    spec,
                    base_url,
                    request_payload,
                    retry_timeout,
                )
        except BlockingIOError as exc:
            perf.record_count("extension.backend.overloaded")
            raise HTTPException(
                status_code=503,
                detail="Extension backend is busy",
                headers={"Retry-After": "1"},
            ) from exc
        except TimeoutError as exc:
            await _record_timeout(extension_id, activation_id, time.monotonic() - invocation_started)
            raise HTTPException(status_code=504, detail="Extension backend timed out") from exc

    if not roundtrip.line:
        evict_persistent_backend(spec["extension_id"])
        raise HTTPException(status_code=500, detail="Extension backend process exited")

    try:
        with perf.timed("extension.backend.invoke.decode"):
            result = json.loads(roundtrip.line.decode("utf-8"))
            if result.get("id") != roundtrip.request_id:
                raise ValueError("extension backend response id mismatch")
            status = int(result["status"])
            content = base64.b64decode(str(result.get("body") or ""))
            headers = {
                str(key): str(value)
                for key, value in (result.get("headers") or [])
                if str(key).lower() not in _BLOCKED_RESPONSE_HEADERS
            }
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
            await _record_slow_call(
                extension_id,
                activation_id,
                child_timing.attributable_asgi_ms / 1000.0,
                _resolve_slow_call_grace(spec, path),
            )
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
    timeout_ceiling: float | None = None,
) -> Response:
    """Invoke an extension's backend handler from core (inter-extension calls).

    Same trust boundary as :func:`dispatch_extension_backend_request` — the
    caller must already be authenticated (internal token + active extension).
    Lets one extension reach another's exposed surface without core baking in
    any feature logic. ``timeout_ceiling`` caps the total invocation for
    latency-budgeted callers (see :func:`_invoke_backend`)."""
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
        timeout_ceiling=timeout_ceiling,
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
        roundtrip = _roundtrip_sync_admitted(
            _get_handle(spec), spec, base_url, request_payload, _resolve_host_timeout(spec, path)
        )
    except BlockingIOError:
        return 503, b'{"detail":"Extension backend is busy","retry_after":1}'
    except TimeoutError:
        return 504, b""
    if not roundtrip.line and _allows_backend_exit_retry(spec, path):
        evict_persistent_backend(extension_id)
        try:
            roundtrip = _roundtrip_sync_admitted(
                _get_handle(spec), spec, base_url, request_payload, _resolve_host_timeout(spec, path)
            )
        except BlockingIOError:
            return 503, b'{"detail":"Extension backend is busy","retry_after":1}'
        except TimeoutError:
            return 504, b""
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
    "assistant.lag-report": (extension_store.ASSISTANT_EXTENSION_ID, "assistant/bug-report"),
}


class DestinationAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ABSENT = "absent"
    NO_SURFACE = "no_surface"
    UNKNOWN_DESTINATION = "unknown_destination"


@dataclass(frozen=True)
class NamedCoreDestinationOutcome:
    status: int
    content: bytes
    availability: DestinationAvailability
    retry_after: float | None = None

    @property
    def destination_unavailable(self) -> bool:
        return self.availability in {
            DestinationAvailability.UNAVAILABLE,
            DestinationAvailability.ABSENT,
            DestinationAvailability.NO_SURFACE,
        }


def _response_retry_after(status: int, content: bytes) -> float | None:
    if status not in {429, 503}:
        return None
    try:
        value = json.loads(content).get("retry_after")
        retry_after = float(value)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not math.isfinite(retry_after) or retry_after < 0:
        return None
    return retry_after


def dispatch_named_core_destination_sync(
    capability: str,
    *,
    body_bytes: bytes,
    base_url: str = "",
) -> NamedCoreDestinationOutcome:
    """Dispatch to a fixed destination with trusted store-owned availability."""
    destination = _NAMED_CORE_DESTINATIONS.get(capability)
    if destination is None:
        return NamedCoreDestinationOutcome(
            404, b'{"detail":"Unknown core destination"}',
            DestinationAvailability.UNKNOWN_DESTINATION,
        )
    extension_id, path = destination
    surface_status = extension_store.backend_surface_status(extension_id)
    if surface_status != "ready":
        availability = DestinationAvailability(surface_status)
        if availability is DestinationAvailability.UNAVAILABLE:
            return NamedCoreDestinationOutcome(
                503,
                b'{"detail":"Extension backend is unavailable","retry_after":60}',
                availability,
                retry_after=60.0,
            )
        return NamedCoreDestinationOutcome(
            404, b'{"detail":"Extension has no backend surface"}', availability,
        )
    status, content = invoke_extension_backend_sync(
        extension_id,
        path,
        method="POST",
        body_bytes=body_bytes,
        base_url=base_url,
    )
    retry_after = _response_retry_after(status, content)
    return NamedCoreDestinationOutcome(
        status, content, DestinationAvailability.AVAILABLE, retry_after=retry_after,
    )


def invoke_named_core_destination_sync(
    capability: str,
    *,
    body_bytes: bytes,
    base_url: str = "",
) -> tuple[int, bytes]:
    """Invoke a fixed core-owned destination; durable data never selects a route."""
    outcome = dispatch_named_core_destination_sync(
        capability, body_bytes=body_bytes, base_url=base_url,
    )
    return outcome.status, outcome.content

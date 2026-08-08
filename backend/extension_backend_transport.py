from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from env_compat import dual_env_many
from execution_environment import isolated_subprocess_environment
import perf
from paths import ba_home
from thread_loop_host import ThreadLoopHost

logger = logging.getLogger(__name__)

# Decoded extension-response body cap, symmetric with the request-side
# ``extension_backend_loader.REQUEST_BODY_MAX_BYTES``. Wire frames are
# newline-delimited base64(body)+JSON(headers/status/timing) envelopes, so the
# raw line is inflated relative to the decoded body; ``_LINE_MAX_BYTES`` is the
# corresponding cap on that raw line, sized generously above the base64
# inflation (~4/3) plus headers/timing overhead so a response that decodes to
# exactly the cap is never falsely rejected.
RESPONSE_BODY_MAX_BYTES = 16 * 1024 * 1024
_LINE_MAX_BYTES = (RESPONSE_BODY_MAX_BYTES * 4 // 3) + 64 * 1024
# The underlying asyncio StreamReader's own hard limit: set comfortably above
# our cap so our explicit, cleanly-handled check fires first in the normal
# case; a pathological line big enough to exceed even this raises
# asyncio.LimitOverrunError, handled identically to our own cap.
_STREAM_READ_LIMIT = _LINE_MAX_BYTES + 64 * 1024

_EXTENSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,127}$")

_DEFAULT_MAX_CONCURRENCY = 8


class ResponseTooLarge(RuntimeError):
    """The extension's response line exceeded RESPONSE_BODY_MAX_BYTES. The
    channel that produced it is torn down — an extension misbehaving this way
    is treated as unhealthy, not retried."""


def _stable_extension_paths(extension_id: str) -> tuple[Path, Path]:
    if _EXTENSION_ID_RE.fullmatch(extension_id) is None:
        raise RuntimeError("extension id is unsafe for stable storage")
    roots = (
        ba_home() / "extension-state" / extension_id,
        ba_home() / "extension-data" / extension_id,
    )
    resolved: list[Path] = []
    for path in roots:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise RuntimeError("extension stable path is unsafe")
        resolved.append(path.resolve())
    return resolved[0], resolved[1]


def _host_env() -> dict[str, str]:
    env = {
        **isolated_subprocess_environment(),
        "BETTER_AGENT_HOME": str(ba_home()),
        "PYTHONIOENCODING": "utf-8",
        **dual_env_many({"BETTER_CLAUDE_HOME": ba_home()}),
    }
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


@dataclass
class _Channel:
    """One spawned subprocess plus the multiplexing state bound to it, all
    owned exclusively by the IO loop thread. Many concurrent requests share
    the single stdin/stdout pipe: each request is tagged with a unique id; the
    reader task demuxes response lines back to the waiting request's Future by
    id, so a slow route never head-of-line-blocks another. ``write_lock``
    serializes stdin writes (one whole line at a time). A channel is immutable
    once dead: on process exit (or an oversized response) the reader marks
    ``alive=False`` and resolves every in-flight waiter so callers don't hang;
    the next request spawns a fresh channel."""
    proc: asyncio.subprocess.Process
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    alive: bool = True


@dataclass
class _BackendProc:
    """Stable per-extension handle for the extension's lifetime. ``channel``
    (the live subprocess + its multiplex state) is replaced under
    ``lifecycle_lock`` when the process dies; both live on the shared IO loop.
    ``semaphore`` is the per-extension bulkhead — created once, independent of
    channel respawns, so the concurrency budget survives a process restart."""
    extension_id: str
    channel: _Channel | None = None
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    semaphore: asyncio.Semaphore | None = None
    in_flight: int = 0


@dataclass(frozen=True)
class RoundtripResult:
    line: bytes
    request_id: str
    elapsed_ms: float


_PERSISTENT_PROCS: dict[str, _BackendProc] = {}
_PROCS_GUARD = threading.Lock()

# One dedicated background loop owns every persistent extension backend
# process's I/O (spawn, reader task, writes) — reused rather than
# hand-rolled: `ThreadLoopHost` is this codebase's single implementation of
# "a thread, a loop, cross-loop submission" (also used by
# `extension_ui_manager`, `native_index_manager`, `recovery_manager`,
# `file_delivery`). A dedicated loop thread (not a caller's transient loop)
# is required here because two real caller shapes cannot supply a stable
# loop of their own: `invoke_extension_backend_sync`'s callers
# (`provider_transport.py`, `session_event_extensions.py`) run on dedicated
# worker threads with no running loop at all, and both this repo's test
# harnesses and this module's own test suite drive the async path through
# many short-lived `asyncio.run()` calls while expecting ONE persistent
# subprocess (and its reader task) to survive across all of them. Bridging
# via `run_coroutine_threadsafe` + `wrap_future` never blocks a thread
# either way, so a request-serving loop calling in pays no thread-blocking
# cost regardless of which loop the transport itself runs on.
_HOST = ThreadLoopHost(
    name="extension-backend-io",
    executor_workers=1,
    io_thread_name_prefix="ext-backend-io",
)


def _ensure_host_started() -> ThreadLoopHost:
    if not _HOST.running:
        _HOST.start()
    return _HOST


async def _bridge(coro: Any) -> Any:
    """Run `coro` on the IO host loop and await its result without blocking
    any thread, regardless of which loop this coroutine itself runs on."""
    host = _ensure_host_started()

    async def _factory() -> Any:
        return await coro

    return await host.run(_factory)


def _bridge_blocking(coro: Any) -> Any:
    """Sync bridge for legacy sync call sites that are already off the event
    loop entirely (dedicated worker threads). Blocks only the calling thread —
    never the IO host loop or any request-serving loop."""
    host = _ensure_host_started()
    concurrent_future = asyncio.run_coroutine_threadsafe(coro, host.loop)
    return concurrent_future.result()


def get_handle(spec: dict[str, Any]) -> _BackendProc:
    """Return the persistent handle for an extension, creating a (proc-less)
    one on first use. The proc itself is spawned lazily inside `roundtrip`."""
    key = spec["extension_id"]
    with _PROCS_GUARD:
        handle = _PERSISTENT_PROCS.get(key)
        if handle is None:
            handle = _BackendProc(extension_id=key)
            _PERSISTENT_PROCS[key] = handle
        return handle


def get_semaphore(handle: _BackendProc, capacity: int) -> asyncio.Semaphore:
    """Lazily create the per-extension bulkhead semaphore. Created once per
    handle (not re-sized on later calls) — a manifest concurrency change takes
    effect on the next `evict_persistent_backend` (which drops the handle
    entirely), consistent with every other spec-driven, spawn-time setting."""
    with _PROCS_GUARD:
        if handle.semaphore is None:
            handle.semaphore = asyncio.Semaphore(capacity)
            perf.register_queue(
                f"extension.backend.inflight.{handle.extension_id}",
                _make_inflight_getter(handle),
            )
        return handle.semaphore


def _make_inflight_getter(handle: _BackendProc) -> Callable[[], int]:
    def _get() -> int:
        return handle.in_flight
    return _get


async def _kill_channel(channel: _Channel, *, exc: BaseException | None) -> None:
    """Mark a channel dead and resolve every in-flight waiter. Runs on the IO
    loop. `exc` is None for a plain process exit (resolves waiters with
    b"", the existing "process died" sentinel); an exception instance (e.g.
    ResponseTooLarge) is raised into every waiter instead."""
    channel.alive = False
    waiters = list(channel.pending.values())
    channel.pending.clear()
    for waiter in waiters:
        if waiter.done():
            continue
        if exc is None:
            waiter.set_result(b"")
        else:
            waiter.set_exception(exc)
    proc = channel.proc
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _reader_loop(channel: _Channel) -> None:
    """Demux response lines for one channel's subprocess. Reads whole lines
    from stdout, routes each to the waiting request's Future by echoed id, and
    on process exit or an oversized line tears the channel down so no waiter
    hangs. Late responses for an abandoned (timed-out) request find no pending
    entry and are dropped."""
    stdout = channel.proc.stdout
    try:
        while True:
            try:
                line = await stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                await _kill_channel(
                    channel,
                    exc=ResponseTooLarge(
                        "extension backend response exceeded the size limit"
                    ),
                )
                return
            if not line:
                break
            if len(line) > _LINE_MAX_BYTES:
                await _kill_channel(
                    channel,
                    exc=ResponseTooLarge(
                        "extension backend response exceeded the size limit"
                    ),
                )
                return
            try:
                rid = json.loads(line).get("id")
            except Exception:
                continue
            if not isinstance(rid, str):
                continue
            waiter = channel.pending.get(rid)
            if waiter is not None and not waiter.done():
                waiter.set_result(line)
    finally:
        await _kill_channel(channel, exc=None)


async def _drain_stderr(extension_id: str, stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    try:
        while True:
            raw = await stream.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                logger.debug("extension backend stderr [%s]: %s", extension_id, text[:500])
    except Exception:
        pass


async def _spawn_channel(spec: dict[str, Any], base_url: str) -> _Channel:
    """Start the persistent host for `spec` and send it the extension spec
    (install path / entrypoint / source) as the first stdin line. Runs on the
    IO loop. The router is loaded once inside the host; subsequent stdin
    lines are requests."""
    host = Path(__file__).with_name("extension_backend_host.py")
    state_path, data_path = _stable_extension_paths(str(spec["extension_id"]))
    spec_payload = {
        "extension_id": spec["extension_id"],
        "install_path": spec["install_path"],
        "state_path": str(state_path),
        "data_path": str(data_path),
        "entrypoint": spec["entrypoint"],
        "entrypoint_kind": spec.get("entrypoint_kind") or "file",
        "source": spec["source"],
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(host),
        "--persistent",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**_host_env(), **_extension_sdk_env(spec, base_url)},
        cwd=str(spec["install_path"]),
        limit=_STREAM_READ_LIMIT,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(spec_payload).encode("utf-8") + b"\n")
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        logger.exception("extension backend subprocess failed to start: %s", spec["extension_id"])
        raise RuntimeError("Extension backend failed to start") from exc
    channel = _Channel(proc=proc)
    asyncio.create_task(_reader_loop(channel))
    asyncio.create_task(_drain_stderr(str(spec["extension_id"]), proc.stderr))
    return channel


async def _ensure_channel(handle: _BackendProc, spec: dict[str, Any], base_url: str) -> _Channel:
    """Return the handle's live channel, spawning a fresh subprocess + reader
    task if none exists or the current one died. Runs on the IO loop, guarded
    by `lifecycle_lock` so a burst of first requests spawns exactly one proc."""
    async with handle.lifecycle_lock:
        channel = handle.channel
        if channel is not None and channel.alive and channel.proc.returncode is None:
            return channel
        channel = await _spawn_channel(spec, base_url)
        handle.channel = channel
        return channel


async def _send_and_wait(channel: _Channel, rid: str, line: bytes, timeout: float) -> bytes:
    """Runs entirely on the IO loop: the Future this awaits is created and
    resolved on the same loop as the reader task, so no cross-loop Future
    ever exists."""
    if not channel.alive:
        return b""
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    channel.pending[rid] = future
    try:
        async with channel.write_lock:
            try:
                assert channel.proc.stdin is not None
                channel.proc.stdin.write(line)
                await channel.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return b""
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("extension backend roundtrip timed out") from exc
    finally:
        channel.pending.pop(rid, None)


async def roundtrip(
    handle: _BackendProc,
    spec: dict[str, Any],
    base_url: str,
    request_payload: dict[str, Any],
    timeout: float,
) -> RoundtripResult:
    """Send one id-tagged request over the multiplexed pipe and wait up to
    `timeout` for its response. Concurrent calls share the subprocess without
    serializing and without blocking any thread. Raises `TimeoutError` if the
    response does not arrive in time (the request is abandoned; the
    subprocess is NOT killed, so other in-flight requests keep running).
    Raises `ResponseTooLarge` if the response exceeds the size cap (the
    channel IS torn down in that case). Returns a result with `line == b""`
    if the process died."""
    rid = uuid.uuid4().hex
    started = time.monotonic()
    line = json.dumps({**request_payload, "id": rid}).encode("utf-8") + b"\n"

    async def _do() -> bytes:
        channel = await _ensure_channel(handle, spec, base_url)
        return await _send_and_wait(channel, rid, line, timeout)

    response_line = await _bridge(_do())
    return RoundtripResult(response_line, rid, (time.monotonic() - started) * 1000.0)


def roundtrip_blocking(
    handle: _BackendProc,
    spec: dict[str, Any],
    base_url: str,
    request_payload: dict[str, Any],
    timeout: float,
) -> RoundtripResult:
    """Sync entry point for callers that are already off the event loop
    (dedicated worker threads). Blocks the calling thread only."""
    rid = uuid.uuid4().hex
    started = time.monotonic()
    line = json.dumps({**request_payload, "id": rid}).encode("utf-8") + b"\n"

    async def _do() -> bytes:
        channel = await _ensure_channel(handle, spec, base_url)
        return await _send_and_wait(channel, rid, line, timeout)

    response_line = _bridge_blocking(_do())
    return RoundtripResult(response_line, rid, (time.monotonic() - started) * 1000.0)


def evict_persistent_backend(extension_id: str) -> None:
    """Kill + drop the persistent backend process for an extension. Call on
    disable/uninstall so a deactivated extension stops serving. Dropping the
    handle also drops its bulkhead semaphore — the next request re-resolves
    concurrency from the (possibly updated) manifest."""
    with _PROCS_GUARD:
        handle = _PERSISTENT_PROCS.pop(extension_id, None)
    if handle is None:
        return
    perf.unregister_queue(f"extension.backend.inflight.{extension_id}")
    channel = handle.channel
    if channel is not None and channel.proc is not None and channel.proc.returncode is None:
        try:
            channel.proc.kill()
        except ProcessLookupError:
            pass


def shutdown_persistent_backends() -> None:
    """Kill every persistent backend process (app shutdown) and stop the IO
    host thread. Sync and blocking (`ThreadLoopHost.stop` joins its thread) —
    call via `asyncio.to_thread` from an async lifespan so it never blocks
    the caller's loop."""
    for extension_id in list(_PERSISTENT_PROCS.keys()):
        evict_persistent_backend(extension_id)
    _HOST.stop()

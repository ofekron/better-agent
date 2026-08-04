"""Per-account OAuth login/logout for subscription providers.

Each provider record can be signed into a distinct subscription account
directly from the settings UI by spawning the provider's own login/logout
CLI with the record's isolated credential env (`CLAUDE_CONFIG_DIR` for
claude, `CODEX_HOME` for codex) — the same SSOT
(`config_store.provider_credential_env`) the spawn path uses.

Desktop/loopback-only by design: the CLI opens the OS browser and binds a
localhost OAuth callback, so the user's browser must share the machine
with the backend. The frontend gates the button on a loopback session;
the routes additionally enforce a server-side loopback check so a remote
authenticated client cannot trigger a server-side browser spawn or wipe
credentials. Security: the spawned argv is a fixed kind->suffix mapping,
never built from caller input, so there is no command-injection surface;
the credential env is injected per-spawn only, never into `os.environ`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional, TypeAlias

import config_store
import provider_runtime_facts
from paths import ba_home, user_home
from proc_control import process_control
from process_identity import capture_process_identity

logger = logging.getLogger(__name__)

BroadcastFn = Callable[[], Awaitable[None]]

# Fixed argv suffix per kind. Never assemble these from caller input.
_AUTH_COMMANDS: dict[str, dict[str, list[str]]] = {
    "claude": {
        "login": ["auth", "login"],
        "logout": ["auth", "logout"],
        "status": ["auth", "status"],
    },
    "codex": {
        "login": ["login"],
        "logout": ["logout"],
        "status": ["login", "status"],
    },
}

_BINARY_NAME: dict[str, str] = {"claude": "claude", "codex": "codex"}

# In-flight flow states (transient; live only in `_states`).
STATE_IDLE = "idle"
STATE_LOGIN_RUNNING = "login_running"
STATE_LOGIN_SUCCESS = "login_success"
STATE_LOGIN_FAILED = "login_failed"
STATE_LOGOUT_RUNNING = "logout_running"
STATE_LOGOUT_FAILED = "logout_failed"
STATE_LOGGED_OUT = "logged_out"

_RUNNING_STATES = (STATE_LOGIN_RUNNING, STATE_LOGOUT_RUNNING)

# Upper bound on a single OAuth flow. The browser exchange legitimately
# takes a while, but a flow left hanging (closed tab, dropped callback)
# must not wedge the record busy forever or orphan the subprocess.
LOGIN_TIMEOUT_SECONDS = 300.0
_STATUS_TIMEOUT_SECONDS = 20.0
_CANCEL_CONFIRM_SECONDS = 2.0
# How long a cached authoritative auth-status is trusted. Invalidated
# immediately on login/logout completion.
_AUTH_CACHE_TTL_SECONDS = 120.0
_STATUS_GLOBAL_CONCURRENCY = 2
_STATUS_KIND_CONCURRENCY = 1

# Transient per-process registries. `_states` tracks in-flight progress
# only; the durable "is this account logged in" truth is the provider's
# own status subcommand, cached in `_auth_cache`.
_states: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}
_procs: dict[str, asyncio.subprocess.Process] = {}
_tasks: set = set()
_auth_cache: dict[str, tuple[float, Optional[bool], AuthFingerprint]] = {}
_pending_refresh: set[str] = set()
_status_global_semaphore: asyncio.Semaphore | None = None
_status_kind_semaphores: dict[str, asyncio.Semaphore] = {}
_status_admission_loop: asyncio.AbstractEventLoop | None = None
_status_tasks: set[asyncio.Task] = set()
_status_procs: dict[object, asyncio.subprocess.Process] = {}
_status_cleanup_tasks: dict[object, asyncio.Task] = {}
_status_shutting_down = False
_config_change_broadcast: BroadcastFn | None = None
_config_change_loop: asyncio.AbstractEventLoop | None = None
_config_reconcile_task: asyncio.Task | None = None
_config_reconcile_requested = False

AuthFingerprint: TypeAlias = tuple[
    str,
    str,
    int,
    str,
    str,
    tuple[str, str] | None,
]
_provider_auth_fingerprints: dict[str, AuthFingerprint] = {}
_projection_lock = threading.RLock()
_flow_fingerprints: dict[str, AuthFingerprint] = {}
_committed_config_changes: set[str] = set()


def supports_auth(provider: dict) -> bool:
    """True for subscription-mode claude/codex records — the kinds whose
    own CLI exposes an OAuth login subcommand pointed at an isolated
    credential dir."""
    if provider.get("mode") != "subscription":
        return False
    return (provider.get("kind") or "claude") in _AUTH_COMMANDS


def _auth_fingerprint(provider: dict) -> AuthFingerprint | None:
    if not supports_auth(provider):
        return None
    provider_id = str(provider.get("id") or "")
    generation = str(provider.get("generation") or "")
    try:
        revision = int(provider.get("revision"))
    except (TypeError, ValueError):
        return None
    if not provider_id or not generation:
        return None
    credential = config_store.provider_credential_env(provider)
    return (
        provider_id,
        generation,
        revision,
        str(provider.get("kind") or "claude"),
        str(provider.get("mode") or ""),
        credential,
    )


def configure_config_change_broadcast(broadcast: BroadcastFn) -> None:
    global _config_change_broadcast
    _config_change_broadcast = broadcast


def bind_config_change_loop() -> None:
    global _config_change_loop
    _config_change_loop = asyncio.get_running_loop()
    provider_runtime_facts.bind_loop()
    _request_config_reconcile()


def provider_config_committed() -> None:
    """Synchronously advance auth authority inside the config commit lock."""
    try:
        state = config_store.list_providers()
        current = {
            str(record.get("id") or ""): fingerprint
            for record in state.get("providers", [])
            if isinstance(record, dict)
            and record.get("id")
            and (fingerprint := _auth_fingerprint(record)) is not None
        }
    except Exception:
        logger.exception("provider auth committed-config snapshot failed closed")
        current = {}
    with _projection_lock:
        changed = {
            provider_id
            for provider_id in (
                set(_provider_auth_fingerprints)
                | set(current)
                | set(_auth_cache)
                | set(_flow_fingerprints)
            )
            if _provider_auth_fingerprints.get(provider_id)
            != current.get(provider_id)
            or (
                provider_id in _flow_fingerprints
                and _flow_fingerprints[provider_id] != current.get(provider_id)
            )
        }
        _committed_config_changes.update(changed)
        obsolete_procs = [
            proc
            for provider_id, proc in _procs.items()
            if provider_id in changed and proc.returncode is None
        ]
        for provider_id in changed:
            _auth_cache.pop(provider_id, None)
            _states.pop(provider_id, None)
            _flow_fingerprints.pop(provider_id, None)
        _provider_auth_fingerprints.clear()
        _provider_auth_fingerprints.update(current)
    for proc in obsolete_procs:
        _kill_proc(proc)


def notify_provider_config_changed() -> None:
    loop = _config_change_loop
    if loop is None or loop.is_closed() or _status_shutting_down:
        return
    loop.call_soon_threadsafe(_request_config_reconcile)


def _request_config_reconcile() -> None:
    global _config_reconcile_requested, _config_reconcile_task
    if _status_shutting_down or _config_change_loop is not asyncio.get_running_loop():
        return
    _config_reconcile_requested = True
    if _config_reconcile_task is not None and not _config_reconcile_task.done():
        return
    _config_reconcile_task = asyncio.create_task(
        _reconcile_provider_config(),
        name="provider_auth:config-reconcile",
    )

    def _reconcile_completed(task: asyncio.Task) -> None:
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error(
                "provider auth config reconciliation failed",
                exc_info=(type(error), error, error.__traceback__),
            )
        if _config_reconcile_requested and not _status_shutting_down:
            _request_config_reconcile()

    _config_reconcile_task.add_done_callback(_reconcile_completed)


async def _reconcile_provider_config() -> None:
    global _config_reconcile_requested
    while _config_reconcile_requested and not _status_shutting_down:
        _config_reconcile_requested = False
        state = await asyncio.to_thread(config_store.list_providers)
        providers = {
            str(record.get("id") or ""): record
            for record in state.get("providers", [])
            if isinstance(record, dict) and record.get("id")
        }
        current = {
            provider_id: fingerprint
            for provider_id, record in providers.items()
            if (fingerprint := _auth_fingerprint(record)) is not None
        }
        with _projection_lock:
            changed = {
                provider_id
                for provider_id in (
                    set(_provider_auth_fingerprints)
                    | set(current)
                    | set(_flow_fingerprints)
                    | set(_auth_cache)
                )
                if _provider_auth_fingerprints.get(provider_id)
                != current.get(provider_id)
                or (
                    provider_id in _flow_fingerprints
                    and _flow_fingerprints[provider_id] != current.get(provider_id)
                )
                or (
                    provider_id in _auth_cache
                    and _auth_cache[provider_id][2] != current.get(provider_id)
                )
            }
            changed.update(_committed_config_changes)
            _committed_config_changes.difference_update(changed)
        if not changed:
            continue
        for provider_id in changed:
            _invalidate_auth(provider_id)
            with _projection_lock:
                flow_fingerprint = _flow_fingerprints.get(provider_id)
                proc = _procs.get(provider_id)
            if flow_fingerprint != current.get(provider_id):
                if proc is not None and proc.returncode is None:
                    _kill_proc(proc)
            clear_state(provider_id)
        with _projection_lock:
            _provider_auth_fingerprints.clear()
            _provider_auth_fingerprints.update(current)
        broadcast = _config_change_broadcast
        if broadcast is not None:
            await broadcast()
        if broadcast is not None:
            for provider_id in sorted(changed & set(current)):
                maybe_schedule_refresh(provider_id, broadcast)


def _lock_for(provider_id: str) -> asyncio.Lock:
    return _locks.setdefault(provider_id, asyncio.Lock())


def _set_state(provider_id: str, status: str, message: str = "") -> None:
    with _projection_lock:
        _states[provider_id] = {"status": status, "message": message}


def _flow_is_current(provider_id: str, fingerprint: AuthFingerprint) -> bool:
    with _projection_lock:
        return (
            _flow_fingerprints.get(provider_id) == fingerprint
            and _provider_auth_fingerprints.get(provider_id) == fingerprint
        )


def clear_state(provider_id: str) -> None:
    """Drop all transient state for a record (called on provider delete)."""
    with _projection_lock:
        proc = _procs.get(provider_id)
    if proc is not None and proc.returncode is None:
        _kill_proc(proc)
    _locks.pop(provider_id, None)
    with _projection_lock:
        _states.pop(provider_id, None)
        _auth_cache.pop(provider_id, None)
        _flow_fingerprints.pop(provider_id, None)


def _cached_auth(provider_id: str) -> Optional[bool]:
    with _projection_lock:
        entry = _auth_cache.get(provider_id)
        current = _provider_auth_fingerprints.get(provider_id)
    if not entry:
        return None
    ts, value, fingerprint = entry
    if fingerprint != current or time.monotonic() - ts > _AUTH_CACHE_TTL_SECONDS:
        return None
    return value


def _invalidate_auth(provider_id: str) -> None:
    with _projection_lock:
        _auth_cache.pop(provider_id, None)


def login_state(provider_id: str) -> dict:
    """Snapshot of the record's in-flight auth-flow state + the cached
    durable auth-status, for UI serialization. Cheap: no subprocess."""
    with _projection_lock:
        state = _states.get(provider_id, {})
        if (
            _flow_fingerprints.get(provider_id)
            != _provider_auth_fingerprints.get(provider_id)
        ):
            state = {}
    return {
        "status": state.get("status", STATE_IDLE),
        "message": state.get("message", ""),
        "authenticated": _cached_auth(provider_id),
    }


def attach_login_state(record: dict) -> dict:
    """Attach the record's auth state for UI serialization. Called from
    the provider list/broadcast path in main.py."""
    if not supports_auth(record):
        return record
    record = dict(record)
    record["login_state"] = login_state(record.get("id") or "")
    return record


def _build_env(provider: dict) -> dict[str, str]:
    """Env for the login/logout/status subprocess. Mirrors the provider's
    `build_env` isolation + transport finalization but skips
    `require_runtime_credential` — the whole point is to run before any
    credential exists. Routes through the shared transport finalizer so a
    login is subject to the same proxy/CA interposition (and fail-closed
    on misconfig) as every other provider spawn."""
    from provider_transport import apply_provider_transport, ProviderTransportError

    env = os.environ.copy()
    # `HOME` is spoofable; pin it from the passwd db exactly like the
    # canonical claude builder so login writes to the same home later
    # sessions read.
    env["HOME"] = str(user_home())
    # Clear cross-provider credential env so it can't leak another
    # account into this login.
    for var in (
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    ):
        env.pop(var, None)
    cred = config_store.provider_credential_env(provider)
    if cred:
        env[cred[0]] = cred[1]
    try:
        env = apply_provider_transport(
            env,
            provider_id=provider.get("id") or "",
            provider_kind=provider.get("kind") or "claude",
            provider_mode=provider.get("mode") or "subscription",
        )
    except ProviderTransportError:
        # Fail closed: a misconfigured transport means the OAuth exchange
        # can't reach the provider correctly; surface as a login error
        # rather than proceeding unproxied.
        raise
    return env


def _resolve_binary(kind: str) -> Optional[str]:
    from cli_paths import resolve_cli_binary

    return resolve_cli_binary(_BINARY_NAME[kind])


def _prepare_spawn(provider: dict, suffix_key: str) -> Optional[tuple[list[str], dict]]:
    kind = provider.get("kind") or "claude"
    binary = _resolve_binary(kind)
    if not binary:
        return None
    cmd = [binary, *_AUTH_COMMANDS[kind][suffix_key]]
    return cmd, _build_env(provider)


async def _spawn(provider: dict, suffix_key: str) -> Optional[asyncio.subprocess.Process]:
    prepared = await asyncio.to_thread(_prepare_spawn, provider, suffix_key)
    if prepared is None:
        return None
    cmd, env = prepared
    # Detach as its own process-group root (SSOT) so a later force_kill
    # reaches the CLI and every helper it spawned (browser launchers, etc.)
    # as a unit — the same tree-kill model runners use.
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        **process_control().detach_spawn_kwargs(),
    )


def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole process tree rooted at the spawned CLI (SSOT).
    process_control.force_kill already swallows ProcessLookupError /
    PermissionError / OSError on both platforms, so it does not raise."""
    process_control().force_kill(proc.pid)


def _markers_dir() -> Path:
    return ba_home() / "provider_auth"


# Provider ids are uuid4, but an imported record can carry an arbitrary id;
# refuse anything that isn't a safe filename component so a crafted id
# ("../x") can't escape the markers dir.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _marker_path(provider_id: str) -> Optional[Path]:
    if not _SAFE_ID.match(provider_id or ""):
        return None
    return _markers_dir() / f"{provider_id}.json"


def _write_marker(provider_id: str, proc: asyncio.subprocess.Process) -> None:
    path = _marker_path(provider_id)
    if path is None:
        return
    # Record the process's creation time so a later reap can tell the
    # original orphan from an unrelated process the OS recycled the pid
    # into (deterministic; survives reboot where pids restart low).
    create_time = _process_create_time(proc.pid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a crash mid-write leaves the old file (or none), never a
        # truncated marker that masks an orphan.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "pid": proc.pid,
                    "provider_id": provider_id,
                    "create_time": create_time,
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:
        logger.warning("provider_auth: failed to write pid marker for %s", provider_id)


def _clear_marker(provider_id: str) -> None:
    path = _marker_path(provider_id)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _process_create_time(pid: int) -> Optional[float]:
    identity = capture_process_identity(pid)
    return identity.create_time if identity is not None else None


def reap_orphaned_logins() -> None:
    """Force-kill any OAuth login/logout CLI that outlived a backend crash,
    then unlink its marker. Safe-by-construction:

    - a marker for a record currently in `_procs` is a live, intentional
      flow -> left untouched (not an orphan);
    - a marker whose pid is gone -> unlinked, no kill;
    - a marker whose pid is alive but whose create_time no longer matches
      what we stored at spawn -> the PID was reused by an UNRELATED
      process (guaranteed reachable after a reboot). Unlink, NO kill.
      (Substring/command-line heuristics are deliberately NOT used: this
      repo's own path contains "claude", so they false-positive on the
      backend's own workers. create_time is deterministic.)

    The marker is unlinked BEFORE the kill so a kill that takes down the
    backend's own group can't repeat on the next boot."""
    d = _markers_dir()
    if not d.is_dir():
        return
    # Sweep stale atomic-write temp files first (a crash between write_text
    # and os.replace leaves a .json.tmp the marker glob below skips).
    for tmp in d.glob("*.json.tmp"):
        _safe_unlink(tmp)
    pc = process_control()
    for marker in d.glob("*.json"):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            provider_id = str(data.get("provider_id") or "")
            expected_ct = data.get("create_time")
        except Exception:
            pid, provider_id, expected_ct = 0, "", None
        # Live, intentional flow (startup race) — leave it alone.
        if provider_id and provider_id in _procs:
            continue
        if not pid or not pc.pid_alive(pid):
            _safe_unlink(marker)
            continue
        # Deterministic identity: the create_time we recorded at spawn must
        # still match. A mismatch means the original died and the OS reused
        # the pid for something else — must not kill.
        current_ct = _process_create_time(pid)
        if expected_ct is None or current_ct is None or current_ct != expected_ct:
            logger.warning(
                "provider_auth: skipping reap pid=%d (identity mismatch / "
                "unverifiable); clearing stale marker", pid,
            )
            _safe_unlink(marker)
            continue
        _safe_unlink(marker)  # unlink BEFORE kill so a self-hit can't repeat
        try:
            pc.force_kill(pid)
            logger.info("provider_auth: reaped orphaned login pid=%d", pid)
        except Exception:
            logger.warning("provider_auth: failed to reap orphan pid=%d", pid)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def reopen_status_probes() -> None:
    global _status_shutting_down
    _status_shutting_down = False


def _status_admission(kind: str) -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
    global _status_admission_loop, _status_global_semaphore
    loop = asyncio.get_running_loop()
    if _status_admission_loop is not loop:
        other_tasks = _status_tasks - {asyncio.current_task()}
        if other_tasks or _status_procs:
            raise RuntimeError("provider auth status admission changed event loops while active")
        _status_admission_loop = loop
        _status_global_semaphore = asyncio.Semaphore(_STATUS_GLOBAL_CONCURRENCY)
        _status_kind_semaphores.clear()
    if _status_global_semaphore is None:
        _status_global_semaphore = asyncio.Semaphore(_STATUS_GLOBAL_CONCURRENCY)
    kind_semaphore = _status_kind_semaphores.setdefault(
        kind,
        asyncio.Semaphore(_STATUS_KIND_CONCURRENCY),
    )
    return kind_semaphore, _status_global_semaphore


async def _acquire_status_admission(
    kind: str,
) -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
    kind_semaphore, global_semaphore = _status_admission(kind)
    await kind_semaphore.acquire()
    try:
        await global_semaphore.acquire()
    except BaseException:
        kind_semaphore.release()
        raise
    return kind_semaphore, global_semaphore


async def _kill_and_reap_status_proc(
    proc: asyncio.subprocess.Process,
) -> None:
    if proc.returncode is None:
        _kill_proc(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=_CANCEL_CONFIRM_SECONDS)
    except ProcessLookupError:
        if proc.returncode is None:
            raise
    except asyncio.TimeoutError as exc:
        if proc.returncode is None:
            raise RuntimeError(
                f"provider auth status process {proc.pid} did not reap"
            ) from exc


async def _reap_owned_status(
    token: object,
    proc: asyncio.subprocess.Process,
) -> None:
    await _kill_and_reap_status_proc(proc)
    if proc.returncode is None:
        raise RuntimeError(
            f"provider auth status process {proc.pid} remains live after reap"
        )
    _status_procs.pop(token, None)


def _status_cleanup_task(
    token: object,
    proc: asyncio.subprocess.Process,
) -> asyncio.Task:
    existing = _status_cleanup_tasks.get(token)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(
        _reap_owned_status(token, proc),
        name=f"provider_auth:status:reap:{proc.pid}",
    )
    _status_cleanup_tasks[token] = task

    def _remove_completed(completed: asyncio.Task) -> None:
        if completed.cancelled() or completed.exception() is not None:
            return
        if _status_cleanup_tasks.get(token) is completed:
            _status_cleanup_tasks.pop(token, None)

    task.add_done_callback(_remove_completed)
    return task


async def _await_status_cleanup(
    token: object,
    proc: asyncio.subprocess.Process,
) -> None:
    cleanup = _status_cleanup_task(token, proc)
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    await cleanup
    if cancelled:
        raise asyncio.CancelledError


async def _spawn_owned_status(
    provider: dict,
    token: object,
) -> Optional[asyncio.subprocess.Process]:
    spawn_task = asyncio.create_task(
        _spawn(provider, "status"),
        name="provider_auth:status:spawn",
    )
    try:
        proc = await asyncio.shield(spawn_task)
    except asyncio.CancelledError:
        try:
            proc = await spawn_task
        except Exception:
            raise
        if proc is not None:
            _status_procs[token] = proc
            await _await_status_cleanup(token, proc)
        raise
    if proc is not None:
        _status_procs[token] = proc
    return proc


async def _status_authenticated(
    provider: dict,
    expected_fingerprint: AuthFingerprint | None = None,
) -> bool:
    """Authoritative logged-in check via the provider's own status
    subcommand. Returns True on a 0-exit (authenticated). Any failure
    (binary missing, non-zero, timeout) is treated as not-logged-in."""
    if _status_shutting_down:
        return False
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("provider auth status probe requires an asyncio task")
    _status_tasks.add(task)
    kind = provider.get("kind") or "claude"
    kind_semaphore: asyncio.Semaphore | None = None
    global_semaphore: asyncio.Semaphore | None = None
    token = object()
    try:
        kind_semaphore, global_semaphore = await _acquire_status_admission(kind)
        if _status_shutting_down:
            return False
        if expected_fingerprint is not None:
            current = await asyncio.to_thread(
                config_store.get_provider,
                expected_fingerprint[0],
            )
            if _auth_fingerprint(current or {}) != expected_fingerprint:
                return False
        try:
            proc = await _spawn_owned_status(provider, token)
        except FileNotFoundError:
            return False
        except Exception:
            return False
        if proc is None:
            return False
        try:
            await asyncio.wait_for(
                proc.communicate(),
                timeout=_STATUS_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await _kill_and_reap_status_proc(proc)
            return False
        return proc.returncode == 0
    finally:
        try:
            proc = _status_procs.get(token)
            if proc is not None:
                if proc.returncode is None:
                    await _await_status_cleanup(token, proc)
                else:
                    _status_procs.pop(token, None)
        finally:
            if global_semaphore is not None:
                global_semaphore.release()
            if kind_semaphore is not None:
                kind_semaphore.release()
            _status_tasks.discard(task)


async def shutdown_status_probes() -> None:
    global _status_shutting_down, _config_change_loop
    global _config_reconcile_requested, _config_reconcile_task
    _status_shutting_down = True
    _config_change_loop = None
    await provider_runtime_facts.shutdown()
    _config_reconcile_requested = False
    reconcile = _config_reconcile_task
    _config_reconcile_task = None
    if reconcile is not None and not reconcile.done():
        reconcile.cancel()
        await asyncio.gather(reconcile, return_exceptions=True)
    current = asyncio.current_task()
    tasks = [task for task in tuple(_status_tasks) if task is not current]
    for task in tasks:
        task.cancel()
    cleanup = [
        _status_cleanup_task(token, proc)
        for token, proc in tuple(_status_procs.items())
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    cleanup_results = await asyncio.gather(*cleanup, return_exceptions=True)
    failures = [
        result for result in cleanup_results if isinstance(result, BaseException)
    ]
    if _status_procs or failures:
        pids = sorted(proc.pid for proc in _status_procs.values())
        raise RuntimeError(
            f"provider auth status shutdown left owned processes: {pids}"
        )


async def _commit_auth_status(
    provider_id: str,
    fingerprint: AuthFingerprint,
    authenticated: bool,
) -> bool:
    def apply() -> None:
        with _projection_lock:
            _auth_cache[provider_id] = (
                time.monotonic(),
                authenticated,
                fingerprint,
            )
            _provider_auth_fingerprints[provider_id] = fingerprint
        _notify_catalog_auth_changed(provider_id)
        provider_runtime_facts.notify_auth_committed()

    committed = await asyncio.to_thread(
        config_store.apply_if_provider_matches,
        provider_id,
        lambda current: _auth_fingerprint(current) == fingerprint,
        apply,
    )
    if not committed:
        _request_config_reconcile()
        return False
    return True


async def refresh_auth_status(
    provider_id: str,
    broadcast: BroadcastFn,
) -> Optional[bool]:
    """Recompute and cache the authoritative auth-status for a record,
    then broadcast so the UI reflects the durable truth (not just
    in-flight progress). Called after login/logout and at backend boot."""
    provider = await asyncio.to_thread(config_store.get_provider, provider_id)
    fingerprint = _auth_fingerprint(provider or {})
    if provider is None or fingerprint is None:
        return None
    try:
        authenticated = await _status_authenticated(provider, fingerprint)
    except Exception as exc:
        logger.warning("provider_auth status check failed for %s: %s", provider_id, exc)
        authenticated = False
    if not await _commit_auth_status(
        provider_id,
        fingerprint,
        authenticated,
    ):
        return None
    await broadcast()
    return authenticated


def maybe_schedule_refresh(provider_id: str, broadcast: BroadcastFn) -> None:
    """Lazily refresh the durable auth-status for a record the first time
    the UI observes it without a cached value (e.g. right after a backend
    restart). Debounced per-record so a single status subprocess runs at
    most once until it resolves."""
    if provider_id in _pending_refresh:
        return
    if _cached_auth(provider_id) is not None:
        return
    if not supports_auth(config_store.get_provider(provider_id) or {}):
        return
    _pending_refresh.add(provider_id)

    async def _run() -> None:
        published = False
        try:
            published = await refresh_auth_status(provider_id, broadcast) is not None
        finally:
            _pending_refresh.discard(provider_id)
            if (
                not published
                and not _status_shutting_down
                and _cached_auth(provider_id) is None
            ):
                maybe_schedule_refresh(provider_id, broadcast)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _pending_refresh.discard(provider_id)
        return
    task = loop.create_task(_run(), name=f"provider_auth:status:{provider_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _forget_proc(provider_id: str, proc: asyncio.subprocess.Process) -> None:
    with _projection_lock:
        if _procs.get(provider_id) is proc:
            _procs.pop(provider_id, None)


async def _discard_registered_flow(
    provider_id: str,
    fingerprint: AuthFingerprint,
    proc: asyncio.subprocess.Process,
) -> None:
    _kill_proc(proc)
    await proc.wait()
    with _projection_lock:
        if (
            _procs.get(provider_id) is proc
            and _flow_fingerprints.get(provider_id) == fingerprint
        ):
            _procs.pop(provider_id, None)
            _flow_fingerprints.pop(provider_id, None)
            _states.pop(provider_id, None)
    _clear_marker(provider_id)


def _notify_catalog_auth_changed(provider_id: str) -> None:
    try:
        import model_catalog_refresh

        model_catalog_refresh.notify_provider_auth_changed(provider_id)
    except Exception:
        logger.exception(
            "model catalog auth-change notify failed for %s",
            provider_id,
        )


async def _monitor(
    provider_id: str,
    action: str,
    proc: asyncio.subprocess.Process,
    fingerprint: AuthFingerprint,
    broadcast: BroadcastFn,
) -> None:
    """Await a login/logout subprocess with a bounded timeout, classify
    the outcome, and broadcast. Any exception (crash, timeout, transport
    error) resolves to a failed state so the record can never wedge busy."""
    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=LOGIN_TIMEOUT_SECONDS
            )
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            _kill_proc(proc)
            if _flow_is_current(provider_id, fingerprint):
                _set_state(provider_id, STATE_LOGIN_FAILED, "login timed out")
                # The exchange may have completed server-side before the kill;
                # recompute durable auth status rather than trusting the cache.
                await refresh_auth_status(provider_id, broadcast)
            return
    except Exception as exc:
        if _flow_is_current(provider_id, fingerprint):
            _set_state(provider_id, STATE_LOGIN_FAILED, f"login error: {exc}")
            await broadcast()
        return
    finally:
        _forget_proc(provider_id, proc)
        _clear_marker(provider_id)

    if not _flow_is_current(provider_id, fingerprint):
        return

    tail = ""
    for stream in (stderr, stdout):
        if stream:
            try:
                tail = stream.decode("utf-8", "replace").strip()[-400:]
            except Exception:
                pass
            if tail:
                break

    if action == "logout":
        if exit_code == 0:
            _set_state(provider_id, STATE_LOGGED_OUT)
        else:
            _set_state(provider_id, STATE_LOGOUT_FAILED, tail or "logout failed")
        _invalidate_auth(provider_id)
        await refresh_auth_status(provider_id, broadcast)
        return

    # login: confirm authoritatively rather than trusting exit code alone,
    # since a browser-closed-midflow can still exit 0 on some providers.
    authenticated = False
    status_fingerprint: AuthFingerprint | None = None
    if exit_code == 0:
        fresh = await asyncio.to_thread(config_store.get_provider, provider_id)
        fresh = fresh or {}
        status_fingerprint = _auth_fingerprint(fresh)
        if status_fingerprint == fingerprint:
            try:
                authenticated = await _status_authenticated(fresh, fingerprint)
            except Exception as exc:
                logger.warning("provider_auth status check failed for %s: %s", provider_id, exc)
    _invalidate_auth(provider_id)
    if status_fingerprint != fingerprint or not await _commit_auth_status(
        provider_id,
        fingerprint,
        authenticated,
    ):
        return
    if _flow_is_current(provider_id, fingerprint) and authenticated:
        _set_state(provider_id, STATE_LOGIN_SUCCESS)
    elif _flow_is_current(provider_id, fingerprint):
        _set_state(provider_id, STATE_LOGIN_FAILED, tail or "login did not complete")
    else:
        return
    await broadcast()


async def _start(
    provider_id: str,
    action: str,
    running_state: str,
    broadcast: BroadcastFn,
) -> dict:
    provider = config_store.get_provider(provider_id)
    if provider is None:
        return {"ok": False, "error": "not_found"}
    if not supports_auth(provider):
        return {"ok": False, "error": "unsupported"}
    fingerprint = _auth_fingerprint(provider)
    if fingerprint is None:
        return {"ok": False, "error": "unsupported"}

    async with _lock_for(provider_id):
        # State-based guard: the lock serializes the start critical section,
        # and this check blocks a second spawn while a prior flow is still
        # running. `_set_state(running)` below happens inside the same
        # locked section with no await between, so the check-then-set is
        # atomic against concurrent callers.
        if _states.get(provider_id, {}).get("status") in _RUNNING_STATES:
            return {"ok": False, "error": "busy"}
        try:
            proc = await _spawn(provider, action)
        except FileNotFoundError:
            _set_state(provider_id, STATE_LOGIN_FAILED, "provider CLI not found")
            await broadcast()
            return {"ok": False, "error": "binary_missing"}
        except Exception as exc:
            # Includes transport-misconfig (fail closed) and spawn errors.
            _set_state(provider_id, STATE_LOGIN_FAILED, f"login could not start: {exc}")
            await broadcast()
            return {"ok": False, "error": "spawn_failed"}
        if proc is None:
            _set_state(provider_id, STATE_LOGIN_FAILED, "provider CLI not found")
            await broadcast()
            return {"ok": False, "error": "binary_missing"}
        def register() -> None:
            with _projection_lock:
                _procs[provider_id] = proc
                _flow_fingerprints[provider_id] = fingerprint
                _write_marker(provider_id, proc)
                _states[provider_id] = {
                    "status": running_state,
                    "message": "",
                }

        registration = asyncio.create_task(
            asyncio.to_thread(
                config_store.apply_if_provider_matches,
                provider_id,
                lambda current: _auth_fingerprint(current) == fingerprint,
                register,
            ),
            name=f"provider_auth:{action}:register:{provider_id}",
        )
        try:
            still_current = await asyncio.shield(registration)
        except asyncio.CancelledError:
            still_current = await registration
            if still_current:
                await _discard_registered_flow(provider_id, fingerprint, proc)
            else:
                _kill_proc(proc)
                await proc.wait()
            raise
        if not still_current:
            _kill_proc(proc)
            await proc.wait()
            return {"ok": False, "error": "obsolete"}
        task = asyncio.create_task(
            _monitor(provider_id, action, proc, fingerprint, broadcast),
            name=f"provider_auth:{action}:{provider_id}",
        )
        # Strong reference so the monitor task is not GC'd mid-flow.
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    if not _flow_is_current(provider_id, fingerprint):
        return {"ok": False, "error": "obsolete"}
    await broadcast()
    return {"ok": True, "state": login_state(provider_id)}


async def start_login(provider_id: str, broadcast: BroadcastFn) -> dict:
    return await _start(provider_id, "login", STATE_LOGIN_RUNNING, broadcast)


async def start_logout(provider_id: str, broadcast: BroadcastFn) -> dict:
    return await _start(provider_id, "logout", STATE_LOGOUT_RUNNING, broadcast)


async def cancel(provider_id: str) -> bool:
    """Kill an in-flight login/logout subprocess tree for a record and
    confirm it died. Returns True only after the process has actually
    exited (reaped), so the caller cannot mistake "signal sent" for "port
    freed". The monitor's finally clears the marker regardless."""
    with _projection_lock:
        proc = _procs.get(provider_id)
    if proc is None or proc.returncode is not None:
        return False
    _kill_proc(proc)
    try:
        # wait() reaps the (now-signalled) process, clearing any zombie that
        # would otherwise keep a callback port-looking-alive. Bounded so a
        # stubborn process can't block the cancel request.
        await asyncio.wait_for(proc.wait(), timeout=_CANCEL_CONFIRM_SECONDS)
        return True
    except asyncio.TimeoutError:
        return False


def shutdown_all() -> None:
    """Kill every in-flight auth subprocess tree. Called at backend shutdown
    so no `claude auth login` / `codex login` (or its helpers) is orphaned.

    Markers are deliberately NOT cleared here: a kill whose signal didn't
    fully take (permission, race) must leave the marker so the next
    startup's reap (identity-checked) gets another chance — otherwise the
    orphan becomes permanently invisible. Normal monitor completion and
    reap clear markers; a clean restart resolves any leftover as dead-pid."""
    with _projection_lock:
        procs = list(_procs.values())
        _procs.clear()
    for proc in procs:
        if proc.returncode is None:
            _kill_proc(proc)

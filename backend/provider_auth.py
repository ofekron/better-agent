"""Per-account OAuth login/logout for subscription providers.

Each provider record can be signed into a distinct subscription account
directly from the settings UI by spawning the provider's own login/logout
CLI with the record's isolated credential env (`CLAUDE_CONFIG_DIR` for
claude, `CODEX_HOME` for codex) — the same SSOT
(`config_store.provider_credential_env`) the spawn path uses.

Desktop-only by design: the CLI opens the OS browser and binds a
localhost OAuth callback, so the user's browser must share the machine
with the backend. The frontend gates the button on a loopback/desktop
session; this module does not second-guess that.

Security: the spawned argv is a fixed kind->suffix mapping, never built
from caller input, so there is no command-injection surface. The
credential env is injected per-spawn only, never into `os.environ`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, Optional

import config_store

logger = logging.getLogger(__name__)

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

# States surfaced to the UI via the provider's `login_state` field.
STATE_IDLE = "idle"
STATE_LOGIN_RUNNING = "login_running"
STATE_LOGIN_SUCCESS = "login_success"
STATE_LOGIN_FAILED = "login_failed"
STATE_LOGOUT_RUNNING = "logout_running"
STATE_LOGGED_OUT = "logged_out"

# In-memory registry (per backend process). Not persisted: a restart
# loses transient login progress, which is correct — the credentials
# themselves live in each record's config_dir, not here.
_states: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}
_procs: dict[str, asyncio.subprocess.Process] = {}

BroadcastFn = Callable[[], Awaitable[None]]


def supports_auth(provider: dict) -> bool:
    """True for subscription-mode claude/codex records — the kinds whose
    own CLI exposes an OAuth login subcommand pointed at an isolated
    credential dir."""
    if provider.get("mode") != "subscription":
        return False
    return (provider.get("kind") or "claude") in _AUTH_COMMANDS


def login_state(provider_id: str) -> dict:
    """Snapshot of the record's auth-flow state for UI rendering."""
    return dict(_states.get(provider_id, {"status": STATE_IDLE, "message": ""}))


def _lock_for(provider_id: str) -> asyncio.Lock:
    return _locks.setdefault(provider_id, asyncio.Lock())


def _set_state(provider_id: str, status: str, message: str = "") -> None:
    _states[provider_id] = {"status": status, "message": message}


def _build_env(provider: dict) -> dict[str, str]:
    """Env for the login/logout subprocess. Mirrors the provider's
    `build_env` isolation but skips `require_runtime_credential` — the
    whole point is to run before any credential exists. The credential
    dir override comes from the shared SSOT."""
    env = os.environ.copy()
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
    return env


def _resolve_binary(kind: str) -> Optional[str]:
    from cli_paths import resolve_cli_binary

    return resolve_cli_binary({"claude": "claude", "codex": "codex"}[kind])


async def _run_status(provider: dict) -> bool:
    """Authoritative logged-in check via the provider's own status
    subcommand. Returns True on a 0-exit (authenticated). Any failure
    (binary missing, non-zero, timeout) is treated as not-logged-in."""
    kind = provider.get("kind") or "claude"
    binary = _resolve_binary(kind)
    if not binary:
        return False
    cmd = [binary, *_AUTH_COMMANDS[kind]["status"]]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_env(provider),
        )
    except FileNotFoundError:
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=20)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False
    return proc.returncode == 0


async def _monitor(
    provider_id: str,
    provider: dict,
    action: str,
    proc: asyncio.subprocess.Process,
    broadcast: BroadcastFn,
) -> None:
    """Await a login/logout subprocess, classify the outcome via the
    provider's status subcommand, and broadcast the final state."""
    kind = provider.get("kind") or "claude"
    try:
        stdout, stderr = await proc.communicate()
        exit_code = proc.returncode
    finally:
        _procs.pop(provider_id, None)

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
        # logout has no separate status; trust the exit code.
        if exit_code == 0:
            _set_state(provider_id, STATE_LOGGED_OUT)
        else:
            _set_state(provider_id, STATE_LOGIN_FAILED, tail or "logout failed")
        await broadcast()
        return

    # login: confirm authoritatively rather than trusting exit code alone,
    # since a browser-closed-midflow can still exit 0 on some providers.
    authenticated = False
    if exit_code == 0:
        # Re-fetch the record so a concurrent config edit is reflected.
        fresh = config_store.get_provider(provider_id) or provider
        try:
            authenticated = await _run_status(fresh)
        except Exception as exc:
            logger.warning("provider_auth status check failed for %s: %s", provider_id, exc)
    if authenticated:
        _set_state(provider_id, STATE_LOGIN_SUCCESS)
    else:
        _set_state(
            provider_id,
            STATE_LOGIN_FAILED,
            tail or f"{kind} login did not complete",
        )
    await broadcast()


def _is_running(provider_id: str) -> bool:
    return _states.get(provider_id, {}).get("status") in (
        STATE_LOGIN_RUNNING,
        STATE_LOGOUT_RUNNING,
    )


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
    kind = provider.get("kind") or "claude"

    lock = _lock_for(provider_id)
    async with lock:
        # State-based guard: the lock serializes the start critical section,
        # and this check blocks a second spawn while a prior flow's
        # subprocess is still running (the lock is NOT held for the flow's
        # lifetime — only state is).
        if _is_running(provider_id):
            return {"ok": False, "error": "busy"}
        binary = _resolve_binary(kind)
        if not binary:
            _set_state(provider_id, STATE_LOGIN_FAILED, f"{kind} CLI not found")
            await broadcast()
            return {"ok": False, "error": "binary_missing"}
        cmd = [binary, *_AUTH_COMMANDS[kind][action]]
        _set_state(provider_id, running_state)
        await broadcast()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_build_env(provider),
            )
        except FileNotFoundError:
            _set_state(provider_id, STATE_LOGIN_FAILED, f"{kind} CLI not found")
            await broadcast()
            return {"ok": False, "error": "binary_missing"}
        _procs[provider_id] = proc
        # Keep a strong reference so the monitor isn't GC'd mid-flow.
        asyncio.create_task(
            _monitor(provider_id, provider, action, proc, broadcast),
            name=f"provider_auth:{action}:{provider_id}",
        )
    return {"ok": True, "state": login_state(provider_id)}


async def start_login(provider_id: str, broadcast: BroadcastFn) -> dict:
    return await _start(provider_id, "login", STATE_LOGIN_RUNNING, broadcast)


async def start_logout(provider_id: str, broadcast: BroadcastFn) -> dict:
    return await _start(provider_id, "logout", STATE_LOGOUT_RUNNING, broadcast)


def detach_login_state(record: dict) -> dict:
    """Attach the record's auth-flow state for UI serialization. Called
    from the provider list/broadcast path in main.py."""
    if not supports_auth(record):
        return record
    record = dict(record)
    record["login_state"] = login_state(record.get("id") or "")
    return record

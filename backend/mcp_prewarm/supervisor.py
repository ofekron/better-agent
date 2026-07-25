"""Backend-side lifecycle owner for pre-warmed extension MCP daemons.

Runs inside the main backend process (imported by `turn_manager.py`).
Spawns/reuses/kills the long-lived daemon subprocess described in
`daemon_process.py` and polls its `state.json` readiness artifact --
never a blind sleep -- racing against process death, mirroring
`ClaudeProvider._bootstrap_run`'s poll-vs-death pattern.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_prewarm import paths as mp_paths

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.05
_STOP_GRACE_SECONDS = 2.0
_DAEMON_MODULE = Path(__file__).resolve().parent / "daemon_process.py"

# The backend process is these daemons' real OS parent (via `Popen`), so an
# unreaped dead child becomes a zombie that keeps responding to
# `os.kill(pid, 0)` until something calls `waitpid` on it -- `_pid_alive`
# would then report a zombie as alive forever. Tracking every spawned
# `Popen` here lets `_pid_alive` reap opportunistically on every check.
_TRACKED_POPENS: dict[int, subprocess.Popen] = {}
_TRACKED_POPENS_LOCK = threading.Lock()


def _reap_tracked() -> None:
    with _TRACKED_POPENS_LOCK:
        for pid in list(_TRACKED_POPENS):
            if _TRACKED_POPENS[pid].poll() is not None:
                del _TRACKED_POPENS[pid]


@dataclass
class DaemonReadyResult:
    ready: bool
    socket_path: str | None = None


def compute_fingerprint(real_config: dict[str, Any], extension_record: dict[str, Any]) -> str:
    """Binds daemon identity to both the installed package (so an
    extension update forces teardown+respawn) and the resolved
    entrypoint (module/args), reusing `_runtime_package_fingerprint`
    as the single source of package-version truth instead of
    re-deriving it."""
    from extension_store import _runtime_package_fingerprint

    package_fingerprint = _runtime_package_fingerprint(extension_record) or ""
    material = json.dumps(
        {
            "package": package_fingerprint,
            "command": real_config.get("command"),
            "args": real_config.get("args"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _pid_alive(pid: Any) -> bool:
    _reap_tracked()
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    with _TRACKED_POPENS_LOCK:
        tracked = _TRACKED_POPENS.get(pid_int)
    if tracked is not None:
        return tracked.poll() is None
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else -- treated as alive
    return True


def _read_state(state_file: Path) -> dict[str, Any] | None:
    try:
        raw = state_file.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _STOP_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _cleanup_files(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _spawn(
    *,
    session_id: str,
    extension_id: str,
    server_name: str,
    real_config: dict[str, Any],
    fingerprint: str,
    idle_timeout_seconds: float,
    socket_file: Path,
    state_file: Path,
) -> subprocess.Popen:
    spawn_config = {
        "command": real_config.get("command"),
        "args": list(real_config.get("args") or []),
        "socket_path": str(socket_file),
        "state_path": str(state_file),
        "fingerprint": fingerprint,
        "idle_timeout_seconds": idle_timeout_seconds,
        "session_id": session_id,
        "extension_id": extension_id,
        "server_name": server_name,
    }
    spawn_config_file = mp_paths.spawn_config_path(session_id, extension_id, server_name)
    tmp_path = spawn_config_file.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(spawn_config), encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, spawn_config_file)

    _cleanup_files(state_file, socket_file)

    # Same env the cold-spawned server would have held (secrets included,
    # no more) -- this process is standing in for that exact subprocess,
    # just longer-lived. `os.environ` supplies the backend's own PATH/
    # PYTHONPATH so this script (and its `mcp_prewarm` sibling imports)
    # resolves the same way every other backend-spawned script does.
    env = {**os.environ, **dict(real_config.get("env") or {})}

    log_file = mp_paths.log_path(session_id, extension_id, server_name)
    log_fp = log_file.open("ab")
    try:
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:  # pragma: no cover - Windows detach
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        popen = subprocess.Popen(
            [sys.executable, str(_DAEMON_MODULE), str(spawn_config_file)],
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=log_fp,
            env=env,
            **kwargs,
        )
    finally:
        log_fp.close()
    with _TRACKED_POPENS_LOCK:
        _TRACKED_POPENS[popen.pid] = popen
    return popen


async def ensure_daemon_ready(
    session_id: str,
    extension_id: str,
    server_name: str,
    real_config: dict[str, Any],
    fingerprint: str,
    *,
    bound_seconds: float,
    idle_timeout_seconds: float = 20 * 60.0,
) -> DaemonReadyResult:
    socket_file = mp_paths.socket_path(session_id, extension_id, server_name)
    state_file = mp_paths.state_path(session_id, extension_id, server_name)

    state = _read_state(state_file)
    if (
        state
        and state.get("ready")
        and state.get("fingerprint") == fingerprint
        and _pid_alive(state.get("pid"))
        and socket_file.exists()
    ):
        return DaemonReadyResult(ready=True, socket_path=str(socket_file))

    if state and state.get("pid") and _pid_alive(state.get("pid")):
        _terminate(int(state["pid"]))

    try:
        popen = _spawn(
            session_id=session_id,
            extension_id=extension_id,
            server_name=server_name,
            real_config=real_config,
            fingerprint=fingerprint,
            idle_timeout_seconds=idle_timeout_seconds,
            socket_file=socket_file,
            state_file=state_file,
        )
    except OSError:
        logger.exception(
            "mcp_prewarm: failed to spawn daemon for %s/%s/%s",
            session_id, extension_id, server_name,
        )
        return DaemonReadyResult(ready=False)

    deadline = time.monotonic() + bound_seconds
    while time.monotonic() < deadline:
        state = _read_state(state_file)
        if (
            state
            and state.get("ready")
            and state.get("fingerprint") == fingerprint
            and socket_file.exists()
        ):
            return DaemonReadyResult(ready=True, socket_path=str(socket_file))
        if popen.poll() is not None:
            # Died before writing ready state -- fail closed, never fall
            # back to a real cold-spawn command for this turn.
            return DaemonReadyResult(ready=False)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    # Bound expired without readiness -- fail closed immediately rather
    # than blocking the caller through `_terminate`'s stop-grace wait;
    # a best-effort SIGTERM is enough here, the NEXT `ensure_daemon_ready`
    # call for this target will detect and fully clean up any straggler.
    try:
        os.kill(popen.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return DaemonReadyResult(ready=False)

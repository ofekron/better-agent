"""On-disk layout for pre-warmed extension MCP daemons.

One unix socket per (session_id, extension_id, server_name) -- never
shared across sessions, so a daemon's lifetime and secrets are scoped
to exactly the session that spawned it. On Windows there is no unix
socket; `daemon_process.py`/`supervisor.py` use loopback TCP plus a
per-daemon-instance connect secret instead (see `tcp_transport.py`).
`os.chmod`/`Path.chmod` on Windows only toggles the read-only
attribute -- it does NOT restrict which local users/processes can
read a file the way POSIX mode bits do. This directory layout (and
the 0700/0600 modes below) is therefore a real access-control boundary
on macOS/Linux, but on Windows it is best-effort only; Windows ACLs
that would actually lock the directory down are out of scope for this
first pass. The TCP connect secret in `state.json` is the real
isolation boundary on Windows, not these directory permissions.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from paths import bc_home

_DIR_MODE = 0o700
# AF_UNIX paths are capped at ~104 bytes on macOS/BSD (108 on Linux).
# `bc_home()` can itself be a long tempdir in tests, so leave headroom.
_MAX_SOCKET_PATH_BYTES = 90


def root() -> Path:
    return bc_home() / "mcp_daemons"


def _secure_mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(_DIR_MODE)
    return path


def daemon_dir(session_id: str, extension_id: str, server_name: str) -> Path:
    # Each level chmod'd 0700 individually -- a parent already existing
    # with looser permissions (e.g. pre-dating this subsystem) must not
    # leave a child directory world/group readable.
    _secure_mkdir(root())
    session_dir = _secure_mkdir(root() / session_id)
    extension_dir = _secure_mkdir(session_dir / extension_id)
    return _secure_mkdir(extension_dir / server_name)


def socket_path(session_id: str, extension_id: str, server_name: str) -> Path:
    candidate = daemon_dir(session_id, extension_id, server_name) / "server.sock"
    if len(os.fsencode(candidate)) < _MAX_SOCKET_PATH_BYTES:
        return candidate
    # Deterministic short fallback under the system tempdir -- both the
    # supervisor and the daemon call this same function, so they always
    # agree on the path without any extra coordination file.
    digest = hashlib.sha256(
        f"{session_id}\0{extension_id}\0{server_name}".encode("utf-8")
    ).hexdigest()[:24]
    fallback_dir = Path(tempfile.gettempdir()) / f"ba-mcp-{os.getuid()}"
    _secure_mkdir(fallback_dir)
    return fallback_dir / f"{digest}.sock"


def state_path(session_id: str, extension_id: str, server_name: str) -> Path:
    return daemon_dir(session_id, extension_id, server_name) / "state.json"


def spawn_config_path(session_id: str, extension_id: str, server_name: str) -> Path:
    return daemon_dir(session_id, extension_id, server_name) / "spawn_config.json"


def log_path(session_id: str, extension_id: str, server_name: str) -> Path:
    return daemon_dir(session_id, extension_id, server_name) / "daemon.log"

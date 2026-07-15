"""Runtime app-endpoint descriptor (plan Phase 3).

The decoupled runtime serves the full REST/WS app on an internal
endpoint: a per-home unix socket on POSIX (short per-user dir — same
AF_UNIX path-cap constraint as the IPC socket) or 127.0.0.1 with a
launcher-chosen port on Windows. The launcher that binds the endpoint
writes this descriptor under `ba_home()/runtime`; the BFF and other
local clients read it. Fail closed when absent — no guessing, no
default port probing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import runtime_ipc
import runtime_ownership

_DESCRIPTOR_NAME = "app_endpoint.json"


class RuntimeEndpointError(RuntimeError):
    pass


def descriptor_path() -> Path:
    return runtime_ownership.runtime_dir() / _DESCRIPTOR_NAME


def app_socket_path() -> Path:
    """POSIX socket for the runtime app (distinct from the IPC socket)."""
    return runtime_ipc.socket_dir() / f"{runtime_ipc.home_digest()}-app.sock"


def _validated_descriptor(descriptor: Any, path: Path) -> dict[str, Any]:
    if not isinstance(descriptor, dict):
        raise RuntimeEndpointError(f"malformed endpoint descriptor at {path}")
    kind = descriptor.get("kind")
    if kind == "uds" and descriptor.get("path") == str(app_socket_path()):
        return descriptor
    port = descriptor.get("port")
    if (
        kind == "tcp"
        and descriptor.get("host") == "127.0.0.1"
        and isinstance(port, int)
        and not isinstance(port, bool)
        and 1 <= port <= 65535
    ):
        return descriptor
    raise RuntimeEndpointError(f"unsupported endpoint descriptor at {path}: {descriptor!r}")


def write_app_endpoint(descriptor: dict[str, Any]) -> None:
    runtime_ownership.ensure_runtime_dir()
    path = descriptor_path()
    validated = _validated_descriptor(descriptor, path)
    path.write_text(json.dumps(validated, indent=1), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def clear_app_endpoint() -> None:
    try:
        descriptor_path().unlink()
    except OSError:
        pass


def http_request(
    descriptor: dict[str, Any],
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    """Stdlib request against the runtime app endpoint (UDS or loopback
    TCP). For launcher/status checks and tests — app traffic goes
    through the BFF's httpx client, not this."""
    import http.client
    import socket
    import stat as stat_module

    if descriptor.get("kind") == "uds":
        uds_path = descriptor["path"]
        # Fail closed on a squatted/replaced socket: it must exist, be a
        # socket, and be owned by the current effective user (POSIX only —
        # the uds descriptor kind never validates on Windows).
        try:
            socket_stat = os.stat(uds_path)
        except OSError as exc:
            raise RuntimeEndpointError(
                f"runtime app socket unavailable at {uds_path}"
            ) from exc
        if not stat_module.S_ISSOCK(socket_stat.st_mode):
            raise RuntimeEndpointError(
                f"runtime app endpoint at {uds_path} is not a unix socket"
            )
        if socket_stat.st_uid != os.geteuid():
            raise RuntimeEndpointError(
                f"runtime app socket at {uds_path} is not owned by the current user"
            )

        class _UDSConnection(http.client.HTTPConnection):
            def connect(self) -> None:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                sock.connect(uds_path)
                self.sock = sock

        connection: http.client.HTTPConnection = _UDSConnection(
            "better-agent-runtime", timeout=timeout
        )
    else:
        connection = http.client.HTTPConnection(
            descriptor["host"], descriptor["port"], timeout=timeout
        )
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def http_get(
    descriptor: dict[str, Any], path: str, *, timeout: float = 5.0
) -> tuple[int, bytes]:
    return http_request(descriptor, "GET", path, timeout=timeout)


def read_app_endpoint() -> dict[str, Any]:
    path = descriptor_path()
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeEndpointError(
            f"runtime app endpoint descriptor unavailable at {path}; "
            "is the runtime running? (better-agent start-runtime)"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeEndpointError(f"malformed endpoint descriptor at {path}") from exc
    return _validated_descriptor(descriptor, path)

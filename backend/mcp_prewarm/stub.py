"""The command the CLI actually cold-spawns every turn in place of the
real extension MCP server. Stdlib-only, no `mcp`/`pydantic` imports --
it never parses JSON-RPC, so it can never duplicate or drift from the
daemon's tool schemas (the daemon owns all MCP protocol handling). Just
a byte pipe: stdin -> daemon socket, daemon socket -> stdout. Starts
and begins proxying in single-digit milliseconds since there is
nothing to import or initialize beyond the stdlib.

Uses raw fd reads (`os.read`) rather than `sys.stdin.buffer.read(n)` --
a `BufferedReader.read(n)` blocks until `n` bytes are available (or
EOF), which would sit on a partial JSON-RPC line indefinitely instead
of forwarding it immediately.
"""

from __future__ import annotations

import os
import socket
import sys
import threading

_SOCKET_ENV_PRIMARY = "BETTER_AGENT_MCP_DAEMON_SOCKET"
_SOCKET_ENV_LEGACY = "BETTER_CLAUDE_MCP_DAEMON_SOCKET"
_CHUNK = 65536


def _resolve_socket_path() -> str:
    return os.environ.get(_SOCKET_ENV_PRIMARY) or os.environ.get(_SOCKET_ENV_LEGACY) or ""


def _pump_stdin_to_socket(sock: socket.socket, errors: list) -> None:
    try:
        while True:
            chunk = os.read(sys.stdin.fileno(), _CHUNK)
            if not chunk:
                break
            sock.sendall(chunk)
    except OSError:
        errors.append(True)
    finally:
        # Propagate stdin EOF onward as a socket half-close so the
        # daemon (like a real stdio MCP server seeing its stdin close)
        # knows no more requests are coming and can end its side --
        # which is what lets the socket->stdout direction below
        # observe its own EOF instead of blocking forever.
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _pump_socket_to_stdout(sock: socket.socket, errors: list) -> None:
    try:
        while True:
            chunk = sock.recv(_CHUNK)
            if not chunk:
                break
            os.write(sys.stdout.fileno(), chunk)
    except OSError:
        errors.append(True)


def main() -> int:
    socket_path = _resolve_socket_path()
    if not socket_path:
        print("mcp_prewarm stub: no daemon socket path in env", file=sys.stderr)
        return 1

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
    except OSError as exc:
        print(f"mcp_prewarm stub: connect failed: {exc}", file=sys.stderr)
        return 1

    errors: list = []
    to_socket = threading.Thread(target=_pump_stdin_to_socket, args=(sock, errors), daemon=True)
    from_socket = threading.Thread(target=_pump_socket_to_stdout, args=(sock, errors), daemon=True)
    to_socket.start()
    from_socket.start()

    # Wait for BOTH directions to hit clean EOF -- the CLI closing
    # stdin only means no more requests are coming, not that the
    # daemon's in-flight reply stream is done; only the socket side
    # closing (daemon done / crashed) ends that direction.
    to_socket.join()
    from_socket.join()

    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

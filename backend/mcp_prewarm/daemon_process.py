"""Long-lived daemon that keeps ONE extension's MCP server warm across
turns, serving it over a unix socket instead of stdio so the CLI's
per-turn stub (`stub.py`) can attach in single-digit milliseconds
instead of paying cold interpreter/import startup on every turn.

Invoked as: `python3 daemon_process.py <spawn_config.json path>`.
Never invoked directly by a user -- only by `supervisor._spawn`.

Loads the extension's module or package-confined Python file via the SAME
command/args `_runtime_mcp_server_config_for_item` already resolved, calls
its `build_server()` factory, and drives either its FastMCP wrapper's
low-level server or a returned low-level `Server` directly against each
accepted connection with an independent
`initialize` handshake per connection -- this is what makes the
daemon safely multi-tenant across turns/sessions that reuse it,
unlike FastMCP's own 1:1 `.run("stdio")`.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ dir

import anyio
from anyio.abc import SocketAttribute

import mcp.types as types
from mcp.shared.message import SessionMessage

from mcp_prewarm import tcp_transport
from runtime_broker import _require_posix_peer

_LINE_MAX_BYTES = 32 * 1024 * 1024


class _Activity:
    def __init__(self) -> None:
        self.last_active = time.monotonic()
        self.connections = 0

    def touch(self) -> None:
        self.last_active = time.monotonic()


def _load_spawn_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_entrypoint(args: list[str]) -> tuple[Any, list[str]]:
    if len(args) < 2 or args[0] != "-m":
        raise RuntimeError("warm_pool requires a Python module or script entrypoint")
    if args[1] != "better_agent_sdk.script_entrypoint":
        return importlib.import_module(args[1]), args[2:]
    if len(args) < 4:
        raise RuntimeError("warm_pool script entrypoint is incomplete")
    from better_agent_sdk.script_entrypoint import load_script_module

    return load_script_module(args[2], args[3], args[4:]), args[4:]


def _build_mcp_server(args: list[str]) -> Any:
    module, extra_args = _load_entrypoint(args)
    build_server = getattr(module, "build_server", None)
    if not callable(build_server):
        raise RuntimeError("warm_pool entrypoint must expose build_server()")
    previous_argv = sys.argv
    try:
        sys.argv = [str(getattr(module, "__file__", "mcp-server")), *extra_args]
        built = build_server()
    finally:
        sys.argv = previous_argv
    server = getattr(built, "_mcp_server", built)
    if not callable(getattr(server, "run", None)) or not callable(
        getattr(server, "create_initialization_options", None)
    ):
        raise RuntimeError("build_server() must return FastMCP or mcp.server.Server")
    return server


def _write_state(state_path: Path, payload: dict[str, Any]) -> None:
    tmp_path = state_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, state_path)


def _session_exists(session_id: str) -> bool:
    from paths import bc_home

    sessions_dir = bc_home() / "sessions"
    return (sessions_dir / f"{session_id}.json").exists() or (sessions_dir / session_id).is_dir()


async def _pump_connection(stream: Any, mcp_server: Any, init_options: Any, activity: _Activity) -> None:
    try:
        _require_posix_peer(stream.extra(SocketAttribute.raw_socket))
    except PermissionError:
        await stream.aclose()
        return
    await _pump_connection_core(stream, mcp_server, init_options, activity)


async def _pump_tcp_connection(
    stream: Any, mcp_server: Any, init_options: Any, activity: _Activity, connect_secret: str,
) -> None:
    # Loopback TCP has no unix-socket-style peer-uid check available, so
    # the first bytes on every accepted connection MUST be the shared
    # secret; anything else (wrong secret, EOF, garbage) closes the
    # connection immediately with no proxying and no error detail leaked.
    try:
        received = await tcp_transport.read_secret_frame(stream.receive)
    except (ConnectionError, ValueError, anyio.EndOfStream):
        await stream.aclose()
        return
    if not tcp_transport.secrets_match(connect_secret, received):
        await stream.aclose()
        return
    await _pump_connection_core(stream, mcp_server, init_options, activity)


async def _pump_connection_core(stream: Any, mcp_server: Any, init_options: Any, activity: _Activity) -> None:
    activity.touch()
    activity.connections += 1
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)
    buffer = bytearray()

    async def reader() -> None:
        nonlocal buffer
        try:
            async with read_stream_writer:
                while True:
                    try:
                        chunk = await stream.receive(65536)
                    except anyio.EndOfStream:
                        break
                    activity.touch()
                    buffer += chunk
                    if len(buffer) > _LINE_MAX_BYTES:
                        raise RuntimeError("mcp_prewarm daemon: oversized JSON-RPC line")
                    while b"\n" in buffer:
                        raw_line, _, rest = bytes(buffer).partition(b"\n")
                        buffer = bytearray(rest)
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:
                            await read_stream_writer.send(exc)
                            continue
                        await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:
            pass
        except (anyio.BrokenResourceError, OSError):
            pass

    async def writer() -> None:
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    activity.touch()
                    data = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                    await stream.send((data + "\n").encode("utf-8"))
        except anyio.ClosedResourceError:
            pass
        except (anyio.BrokenResourceError, OSError):
            pass

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
            tg.start_soon(writer)
            try:
                # Independent initialize/session state per connection --
                # the daemon is shared across turns, so each turn's CLI
                # process gets its own fresh MCP protocol session even
                # though the underlying FastMCP tool registry is shared.
                await mcp_server.run(read_stream, write_stream, init_options)
            finally:
                tg.cancel_scope.cancel()
    finally:
        activity.connections -= 1
        activity.touch()
        try:
            await stream.aclose()
        except OSError:
            pass


async def _idle_reaper(activity: _Activity, idle_timeout_seconds: float, session_id: str) -> None:
    check_interval = min(1.0, max(0.1, idle_timeout_seconds / 20))
    while True:
        await anyio.sleep(check_interval)
        idle_expired = (
            activity.connections == 0
            and (time.monotonic() - activity.last_active) >= idle_timeout_seconds
        )
        if idle_expired or not _session_exists(session_id):
            # `anyio`'s asyncio backend runs a blocking `accept()` in a
            # non-daemon worker thread with `abandon_on_cancel=True` --
            # cancelling the task group leaves that thread genuinely
            # stuck in the syscall (nothing ever connects to unblock
            # it), and Python's interpreter shutdown joins non-daemon
            # threads before exiting, so a clean `return`/cancel here
            # would hang the process forever. `os._exit` terminates
            # immediately at the OS level without waiting on it.
            os._exit(0)


async def _serve(config: dict[str, Any]) -> None:
    mcp_server = _build_mcp_server(list(config["args"]))
    init_options = mcp_server.create_initialization_options()

    state_path = Path(config["state_path"])
    activity = _Activity()
    transport = config.get("transport", "unix")

    async with anyio.create_task_group() as tg:
        tg.start_soon(
            _idle_reaper, activity, float(config["idle_timeout_seconds"]),
            config["session_id"],
        )

        if transport == "tcp":
            await _serve_tcp(config, state_path, mcp_server, init_options, activity, tg)
        else:
            await _serve_unix(config, state_path, mcp_server, init_options, activity, tg)


async def _serve_unix(
    config: dict[str, Any], state_path: Path, mcp_server: Any, init_options: Any,
    activity: _Activity, tg: Any,
) -> None:
    socket_path = Path(config["socket_path"])
    listener = await anyio.create_unix_listener(path=socket_path)
    os.chmod(socket_path, 0o600)

    _write_state(state_path, {
        "pid": os.getpid(),
        "transport": "unix",
        "socket_path": str(socket_path),
        "fingerprint": config["fingerprint"],
        "ready": True,
        "started_at": time.time(),
    })

    async def _accept_loop() -> None:
        async with listener:
            while True:
                stream = await listener.accept()
                tg.start_soon(_pump_connection, stream, mcp_server, init_options, activity)

    tg.start_soon(_accept_loop)


async def _serve_tcp(
    config: dict[str, Any], state_path: Path, mcp_server: Any, init_options: Any,
    activity: _Activity, tg: Any,
) -> None:
    # Ephemeral port (0) avoids port collisions across concurrently
    # spawned daemons -- there is no natural per-target port to pick,
    # unlike the unix-socket path where the on-disk path already
    # encodes (session, extension, server).
    multi_listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    listener = multi_listener.listeners[0]
    host, port = listener.extra(SocketAttribute.local_address)
    connect_secret = tcp_transport.generate_connect_secret()

    _write_state(state_path, {
        "pid": os.getpid(),
        "transport": "tcp",
        "host": host,
        "port": port,
        "connect_secret": connect_secret,
        "fingerprint": config["fingerprint"],
        "ready": True,
        "started_at": time.time(),
    })

    async def _accept_loop() -> None:
        async with listener:
            while True:
                stream = await listener.accept()
                tg.start_soon(
                    _pump_tcp_connection, stream, mcp_server, init_options, activity, connect_secret,
                )

    tg.start_soon(_accept_loop)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: daemon_process.py <spawn_config.json>", file=sys.stderr)
        return 2
    config = _load_spawn_config(Path(sys.argv[1]))
    try:
        anyio.run(_serve, config)
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

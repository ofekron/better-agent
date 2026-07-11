"""Local IPC transport for the better-agent runtime (plan Phase 2).

Endpoint and credential both derive from `ba_home()/runtime`, so
`BETTER_AGENT_HOME` isolation isolates the transport: POSIX serves a
unix socket inside the 0700 runtime dir; Windows serves a per-home
named pipe (name includes a hash of the home path).

Auth is mandatory: `multiprocessing.connection`'s HMAC
challenge/response against a runtime-minted 0600 token file — the
token never crosses the wire, and unauthenticated peers are rejected
before any payload is parsed. Frames are length-bounded JSON via
`send_bytes`/`recv_bytes` (never pickle). Unknown ops, malformed
frames, oversized frames, missing tokens, and foreign-owned socket
paths all fail closed. There is deliberately NO TCP/HTTP fallback.

Known Phase 2 limitations, tracked for the daemon-ownership phase:
one connection per client call (no pooling/batching yet), no explicit
Windows pipe DACL (the HMAC token is the authority), and a same-uid
peer can stall the accept loop mid-handshake (same-uid is already
inside the OS trust boundary).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Any, Callable

from paths import ba_home
import runtime_ownership

SCHEMA_VERSION = 1
_SOCKET_NAME = "runtime.sock"
_TOKEN_NAME = "ipc.token"
_MAX_FRAME_BYTES = 8 * 1024 * 1024


class RuntimeIPCError(RuntimeError):
    pass


class RuntimeIPCAuthError(RuntimeIPCError):
    pass


def endpoint_address() -> str:
    if os.name == "nt":
        digest = hashlib.sha256(str(ba_home()).encode("utf-8")).hexdigest()[:16]
        return rf"\\.\pipe\better-agent-runtime-{digest}"
    return str(runtime_ownership.runtime_dir() / _SOCKET_NAME)


def _family() -> str:
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


def token_path() -> Path:
    return runtime_ownership.runtime_dir() / _TOKEN_NAME


def _assert_runtime_dir_safe() -> None:
    """Refuse symlinked or foreign-owned runtime dirs (POSIX)."""
    if os.name == "nt":
        return
    path = runtime_ownership.runtime_dir()
    if path.is_symlink():
        raise RuntimeIPCError(f"runtime dir is a symlink: {path}")
    st = path.stat()
    if st.st_uid != os.geteuid():
        raise RuntimeIPCError(f"runtime dir owned by uid {st.st_uid}, not us: {path}")


def mint_token() -> bytes:
    """Server-side: create the token file if missing and return the token."""
    runtime_ownership.ensure_runtime_dir()
    _assert_runtime_dir_safe()
    path = token_path()
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(32))
    if os.name != "nt":
        path.chmod(0o600)
    return read_token()


def read_token() -> bytes:
    """Client-side: load the token; fail closed when absent or empty."""
    path = token_path()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeIPCAuthError(f"runtime ipc token unavailable at {path}") from exc
    if not token:
        raise RuntimeIPCAuthError(f"runtime ipc token empty at {path}")
    return token.encode("utf-8")


def _error_response(exc: BaseException) -> dict[str, Any]:
    # Message text only — never tracebacks or env, they can carry secrets.
    return {
        "ok": False,
        "error": str(exc) or exc.__class__.__name__,
        "error_type": "ValueError" if isinstance(exc, ValueError) else "RuntimeError",
    }


class RuntimeIPCServer:
    """Serves runtime service ops over the local endpoint.

    Transport only: it does not itself require the session-root writer
    lock (all built-in ops are reads); the daemon entry pairs it with
    writer ownership.
    """

    def __init__(self) -> None:
        self._listener: Listener | None = None
        self._stop = threading.Event()
        self.on_shutdown_request: Callable[[], None] | None = None
        self._handlers: dict[str, Callable[[dict], Any]] = {
            "ping": self._op_ping,
            "operation_status": self._op_operation_status,
            "shutdown": self._op_shutdown,
        }

    # ── ops ───────────────────────────────────────────────────────

    def _op_ping(self, args: dict) -> dict:
        return {
            "service": "better-agent-runtime",
            "schema_version": SCHEMA_VERSION,
            "pid": os.getpid(),
            "endpoint": endpoint_address(),
        }

    def _op_operation_status(self, args: dict) -> dict:
        from runtime_client import runtime

        return runtime.operation_status(
            str(args.get("kind") or ""), str(args.get("operation_id") or "")
        )

    def _op_shutdown(self, args: dict) -> dict:
        self._stop.set()
        callback = self.on_shutdown_request
        if callback is not None:
            callback()
        return {"stopping": True, "pid": os.getpid()}

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> str:
        token = mint_token()
        address = endpoint_address()
        if os.name != "nt" and Path(address).exists():
            if _endpoint_alive():
                raise RuntimeIPCError(f"runtime ipc endpoint already served: {address}")
            Path(address).unlink()
        self._listener = Listener(address, family=_family(), authkey=token)
        if os.name != "nt":
            os.chmod(address, 0o600)
        threading.Thread(
            target=self._accept_loop, name="runtime-ipc-accept", daemon=True
        ).start()
        return address

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if os.name != "nt":
            try:
                Path(endpoint_address()).unlink()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                conn = listener.accept()
            except AuthenticationError:
                continue  # rejected peer; keep serving
            except (OSError, EOFError):
                if self._stop.is_set():
                    return
                continue
            threading.Thread(
                target=self._serve_connection,
                args=(conn,),
                name="runtime-ipc-conn",
                daemon=True,
            ).start()

    def _serve_connection(self, conn: Connection) -> None:
        with conn:
            while not self._stop.is_set():
                try:
                    raw = conn.recv_bytes(_MAX_FRAME_BYTES)
                except (EOFError, OSError):
                    return
                response = self._handle_frame(raw)
                try:
                    conn.send_bytes(json.dumps(response).encode("utf-8"))
                except (OSError, ValueError):
                    return

    def _handle_frame(self, raw: bytes) -> dict[str, Any]:
        try:
            frame = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _error_response(ValueError(f"malformed frame: {exc}"))
        if not isinstance(frame, dict):
            return _error_response(ValueError("frame must be a JSON object"))
        op = str(frame.get("op") or "")
        args = frame.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _error_response(ValueError("args must be a JSON object"))
        handler = self._handlers.get(op)
        if handler is None:
            return _error_response(ValueError(f"unknown op: {op!r}"))
        try:
            return {"ok": True, "result": handler(args)}
        except Exception as exc:  # noqa: BLE001 — boundary: map, never crash the server
            return _error_response(exc)


def _endpoint_alive() -> bool:
    try:
        RuntimeIPCClient().ping()
        return True
    except RuntimeIPCAuthError:
        return True  # something answered the handshake — endpoint is live
    except RuntimeIPCError:
        return False


class RuntimeIPCClient:
    """One authenticated connection per call; raises fail-closed errors."""

    def _connect(self) -> Connection:
        token = read_token()
        address = endpoint_address()
        if os.name != "nt":
            _assert_runtime_dir_safe()
            try:
                st = os.stat(address)
            except OSError as exc:
                raise RuntimeIPCError(
                    f"runtime ipc endpoint unavailable: {address}"
                ) from exc
            if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid():
                raise RuntimeIPCError(f"runtime ipc endpoint not trusted: {address}")
        try:
            return Client(address, family=_family(), authkey=token)
        except AuthenticationError as exc:
            raise RuntimeIPCAuthError("runtime ipc authentication failed") from exc
        except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            raise RuntimeIPCError(
                f"runtime ipc endpoint unavailable: {address}"
            ) from exc

    def call(self, op: str, **args: Any) -> Any:
        conn = self._connect()
        with conn:
            conn.send_bytes(json.dumps({"op": op, "args": args}).encode("utf-8"))
            try:
                raw = conn.recv_bytes(_MAX_FRAME_BYTES)
            except (EOFError, OSError) as exc:
                raise RuntimeIPCError("runtime ipc connection dropped") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeIPCError("runtime ipc returned a malformed frame") from exc
        if not isinstance(payload, dict):
            raise RuntimeIPCError("runtime ipc returned a non-object frame")
        if not payload.get("ok"):
            message = str(payload.get("error") or "runtime ipc call failed")
            if payload.get("error_type") == "ValueError":
                raise ValueError(message)
            raise RuntimeIPCError(message)
        return payload.get("result")

    def ping(self) -> dict:
        result = self.call("ping")
        if not isinstance(result, dict) or result.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeIPCError(
                f"runtime ipc schema mismatch: got {result!r}, want {SCHEMA_VERSION}"
            )
        return result

    def operation_status(self, kind: str, operation_id: str) -> dict:
        return self.call("operation_status", kind=kind, operation_id=operation_id)

    def shutdown(self) -> dict:
        return self.call("shutdown")

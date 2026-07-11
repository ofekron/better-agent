"""Local IPC transport for the better-agent runtime (plan Phase 2).

Endpoint and credential both derive from `ba_home()`, so
`BETTER_AGENT_HOME` isolation isolates the transport. The token file
lives under the 0700 `ba_home()/runtime` dir. The socket cannot — an
AF_UNIX path is capped (~104 bytes on macOS) and homes can be deep —
so POSIX serves it from a short per-user 0700 dir whose socket NAME is
a hash of the home path (different home ⇒ different socket, same
isolation property); Windows uses a per-home-hash named pipe.

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

SCHEMA_VERSION = 2  # v2: frames carry a scoped per-call token
_TOKEN_NAME = "ipc.token"
# Session roots can run tens of MB; still bounded so a bad peer cannot
# force unbounded allocation.
_MAX_FRAME_BYTES = 64 * 1024 * 1024

# Deny-by-default op → required scope (`None` = unauthenticated-safe).
# An op missing from this map cannot be served at all.
_OP_SCOPES: dict[str, str | None] = {
    "ping": None,
    "operation_status": "read",
    "list_sessions": "read",
    "session_snapshot": "read",
    "events_catchup": "read",
    "submit_prompt": "write",
    "shutdown": "control",
}
# Frame arg naming a session, for session-scoped token enforcement.
_OP_SESSION_ARG: dict[str, str] = {
    "session_snapshot": "session_id",
    "events_catchup": "session_id",
    "submit_prompt": "app_session_id",
}


class RuntimeIPCError(RuntimeError):
    pass


class RuntimeIPCAuthError(RuntimeIPCError):
    pass


def home_digest() -> str:
    return hashlib.sha256(str(ba_home()).encode("utf-8")).hexdigest()[:16]


def socket_dir() -> Path:
    """Short per-user dir for AF_UNIX sockets (pure path, no side effects)."""
    import tempfile

    return Path(tempfile.gettempdir()) / f"better-agent-runtime-{os.geteuid()}"


def endpoint_address() -> str:
    if os.name == "nt":
        return rf"\\.\pipe\better-agent-runtime-{home_digest()}"
    return str(socket_dir() / f"{home_digest()}.sock")


def _family() -> str:
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


def token_path() -> Path:
    return runtime_ownership.runtime_dir() / _TOKEN_NAME


def _assert_dir_safe(path: Path, label: str) -> None:
    """Refuse symlinked or foreign-owned trust dirs (POSIX)."""
    if os.name == "nt":
        return
    if path.is_symlink():
        raise RuntimeIPCError(f"{label} is a symlink: {path}")
    st = path.stat()
    if st.st_uid != os.geteuid():
        raise RuntimeIPCError(f"{label} owned by uid {st.st_uid}, not us: {path}")


def ensure_socket_dir() -> Path:
    """Server-side: create the per-user socket dir 0700 and verify it."""
    path = socket_dir()
    if os.name != "nt":
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
        _assert_dir_safe(path, "runtime socket dir")
    return path


def mint_token() -> bytes:
    """Server-side: create the token file if missing and return the token."""
    _assert_dir_safe(runtime_ownership.ensure_runtime_dir(), "runtime dir")
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


def _require_safe_id(value: Any, field: str) -> str:
    raw = str(value or "")
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))
    if not cleaned or cleaned != raw:
        raise ValueError(f"{field} must be a non-empty safe id")
    return raw


def _error_response(exc: BaseException) -> dict[str, Any]:
    # Message text only — never tracebacks or env, they can carry secrets.
    if isinstance(exc, PermissionError):
        error_type = "PermissionError"
    elif isinstance(exc, ValueError):
        error_type = "ValueError"
    else:
        error_type = "RuntimeError"
    return {
        "ok": False,
        "error": str(exc) or exc.__class__.__name__,
        "error_type": error_type,
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
        self._resolver: Any = None
        self.on_shutdown_request: Callable[[], None] | None = None
        self._handlers: dict[str, Callable[[dict], Any]] = {
            "ping": self._op_ping,
            "operation_status": self._op_operation_status,
            "list_sessions": self._op_list_sessions,
            "session_snapshot": self._op_session_snapshot,
            "events_catchup": self._op_events_catchup,
            "submit_prompt": self._op_submit_prompt,
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

    # Read-only snapshot/catch-up ops (plan Phase 4): the projection/BFF
    # side reads these instead of touching the session root itself.

    def _op_events_catchup(self, args: dict) -> dict:
        from event_ingester import event_ingester

        session_id = _require_safe_id(args.get("session_id"), "session_id")
        after_seq = args.get("after_seq", 0)
        limit = args.get("limit", 500)
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 2000:
            raise ValueError("limit must be an integer in 1..2000")
        events, total_count, has_more = event_ingester.read_events(
            session_id, after_seq=after_seq, limit=limit
        )
        next_seq = after_seq
        for event in events:
            seq = event.get("seq")
            if isinstance(seq, int) and seq > next_seq:
                next_seq = seq
        return {
            "events": events,
            "total_count": total_count,
            "has_more": has_more,
            "next_seq": next_seq,
            "schema_version": SCHEMA_VERSION,
        }

    def _op_list_sessions(self, args: dict) -> dict:
        import session_store

        # Cold-process endpoints (a fresh daemon) have an empty summary
        # index; warm it first — matches the REST layer's behavior —
        # and report completeness truthfully so a partial list is never
        # mistaken for the full one.
        session_store.wait_for_summary_index(30.0)
        return {
            "sessions": session_store.list_sessions(),
            "index_complete": session_store.summary_index_snapshot_complete(),
        }

    def _op_session_snapshot(self, args: dict) -> dict:
        import session_store
        from event_ingester import event_ingester

        session_id = _require_safe_id(args.get("session_id"), "session_id")
        # `events_high_water` is the journal head at snapshot time — an
        # upper bound for progress/staleness checks. It is NOT a no-miss
        # resume cursor: session persistence is debounced, so the file
        # can lag the journal. Gapless projection resume needs the
        # snapshot-embedded per-sid cursor (Phase 4, daemon-hosted
        # in-memory owner); until then consumers replay from 0 and rely
        # on uid dedup.
        seq_by_sid = event_ingester.max_seq_by_sid(session_id)
        high_water = max(seq_by_sid.values(), default=0)
        session = session_store.get_session(session_id)
        return {
            "found": session is not None,
            "session": session,
            "events_high_water": high_water,
        }

    # Write ops (plan Phase 3): routed through the RuntimeClient facade;
    # they fail closed with a RuntimeError frame when this process hosts
    # no live coordinator (e.g. the skeleton daemon).

    def _op_submit_prompt(self, args: dict) -> dict:
        from runtime_client import runtime

        app_session_id = _require_safe_id(args.get("app_session_id"), "app_session_id")
        params = args.get("params")
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
        queued_id = runtime.submit_prompt_threadsafe(app_session_id, params)
        return {"queued_id": queued_id}

    def _op_shutdown(self, args: dict) -> dict:
        # Only endpoints whose host opted in (the daemon) accept remote
        # shutdown; the monolith serves without a callback and refuses,
        # so a token holder cannot stop the backend's endpoint.
        callback = self.on_shutdown_request
        if callback is None:
            raise ValueError("shutdown is not permitted on this endpoint")
        # Close the listener NOW so no new connection can land in a
        # half-accepted handshake after the stop flag is set; the reply
        # to this caller still goes out on its already-open connection.
        self.stop()
        callback()
        return {"stopping": True, "pid": os.getpid()}

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> str:
        token = mint_token()
        import runtime_tokens

        # Admin authority is a token DISTINCT from the transport
        # connect-secret; holding `token` (the HMAC authkey) alone grants
        # no op authority.
        runtime_tokens.ensure_admin_token()
        self._resolver = runtime_tokens.TokenResolver()
        ensure_socket_dir()
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
        if handler is None or op not in _OP_SCOPES:
            return _error_response(ValueError(f"unknown op: {op!r}"))
        required_scope = _OP_SCOPES[op]
        if required_scope is not None:
            record = self._resolver.resolve(frame.get("token")) if self._resolver else None
            if record is None:
                return _error_response(PermissionError("invalid runtime token"))
            if required_scope not in (record.get("scopes") or []):
                return _error_response(
                    PermissionError(f"token lacks scope {required_scope!r}")
                )
            bound_session = record.get("session_id")
            if bound_session:
                session_arg = _OP_SESSION_ARG.get(op)
                if session_arg is None:
                    # Session-scoped tokens may only touch ops that name
                    # their session — cross-session reads (list_sessions,
                    # operation_status) are denied outright.
                    return _error_response(
                        PermissionError("session-scoped token cannot use this op")
                    )
                if str(args.get(session_arg) or "") != bound_session:
                    return _error_response(
                        PermissionError("token is scoped to a different session")
                    )
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
    """One authenticated connection per call; raises fail-closed errors.

    `scoped_token` selects the per-call authority (layer 2); when None
    the first-party ADMIN token (distinct from the transport
    connect-secret) is sent. The transport HMAC (layer 1) always uses
    the connect-secret: scoped clients are still same-home locals, just
    with narrowed op authority and no admin token."""

    def __init__(self, scoped_token: str | None = None) -> None:
        self._scoped_token = scoped_token

    def _connect(self) -> Connection:
        token = read_token()
        address = endpoint_address()
        if os.name != "nt":
            directory = socket_dir()
            if not directory.exists():
                raise RuntimeIPCError(
                    f"runtime ipc endpoint unavailable: {address}"
                )
            _assert_dir_safe(directory, "runtime socket dir")
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
        if self._scoped_token is not None:
            token = self._scoped_token
        else:
            import runtime_tokens

            # First-party default: the admin authority token (NOT the
            # transport connect-secret). Empty when absent — ping still
            # works; scoped ops fail closed.
            token = runtime_tokens.read_admin_token_or_empty()
        with conn:
            conn.send_bytes(
                json.dumps({"op": op, "args": args, "token": token}).encode("utf-8")
            )
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
            error_type = payload.get("error_type")
            if error_type == "ValueError":
                raise ValueError(message)
            if error_type == "PermissionError":
                raise RuntimeIPCAuthError(message)
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

    def list_sessions(self) -> list[dict]:
        result = self.call("list_sessions")
        return list(result.get("sessions") or []) if isinstance(result, dict) else []

    def session_snapshot(self, session_id: str) -> dict:
        return self.call("session_snapshot", session_id=session_id)

    def events_catchup(
        self, session_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> dict:
        return self.call(
            "events_catchup",
            session_id=session_id,
            after_seq=after_seq,
            limit=limit,
        )

    def submit_prompt(self, app_session_id: str, params: dict) -> str:
        result = self.call("submit_prompt", app_session_id=app_session_id, params=params)
        if not isinstance(result, dict) or not result.get("queued_id"):
            raise RuntimeIPCError("submit_prompt returned no queued_id")
        return str(result["queued_id"])

    def shutdown(self) -> dict:
        return self.call("shutdown")

from __future__ import annotations

import json
import math
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from chat_projection_store import ChatProjectionStoreError
from chat_projection_store_owner_path import secure_open


MIN_TIMEOUT_SECONDS = 0.05
MAX_TIMEOUT_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_FRAME_BYTES = 64 * 1024 * 1024
MAX_REQUEST_ID = 2**63 - 1


def encode_frame(payload: Any, limit: int = DEFAULT_FRAME_BYTES) -> bytearray:
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    encoded = bytearray()
    try:
        for chunk in encoder.iterencode(payload):
            chunk_bytes = chunk.encode("utf-8")
            if len(encoded) + len(chunk_bytes) > limit:
                raise ChatProjectionStoreError("ipc_too_large", "projection owner frame limit exceeded")
            encoded.extend(chunk_bytes)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ChatProjectionStoreError("owner_protocol_error", "projection owner frame is invalid") from exc
    return encoded


def send_frame(channel: socket.socket, payload: Mapping[str, Any], *, limit: int = DEFAULT_FRAME_BYTES) -> None:
    encoded = encode_frame(payload, limit)
    channel.sendall(struct.pack("!I", len(encoded)) + encoded)


def _receive_exact(channel: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = channel.recv(length - len(chunks))
        if not chunk:
            raise ChatProjectionStoreError("owner_unavailable", "projection owner exited")
        chunks.extend(chunk)
    return bytes(chunks)


def receive_frame(channel: socket.socket, *, limit: int = DEFAULT_FRAME_BYTES) -> Mapping[str, Any]:
    size = struct.unpack("!I", _receive_exact(channel, 4))[0]
    if size > limit:
        raise ChatProjectionStoreError("ipc_too_large", "projection owner frame limit exceeded")

    def strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            _receive_exact(channel, size).decode("utf-8"), object_pairs_hook=strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite number: {value}")),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ChatProjectionStoreError("owner_protocol_error", "invalid projection owner frame") from exc
    if not isinstance(payload, Mapping):
        raise ChatProjectionStoreError("owner_protocol_error", "invalid projection owner frame")
    return payload


class OwnerClient:
    def __init__(
        self, *, root_path: Path, path: Path, owner_script: Path, owner_arguments: tuple[str, ...],
        validate_result: Callable[[str, Any, Mapping[str, Any]], Any],
        ipc_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        startup_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_error_text_bytes: int = 4_096,
    ) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._poisoned = False
        self._next_request_id = 1
        self._validate_result = validate_result
        self._max_error_text_bytes = max_error_text_bytes
        self.ipc_timeout_seconds = self._validate_timeout(ipc_timeout_seconds, "IPC")
        self.startup_timeout_seconds = self._validate_timeout(startup_timeout_seconds, "owner startup")
        self.path, self.parent_fd, self.file_fd, created = secure_open(root_path, path)
        self.process: subprocess.Popen | None = None
        self.channel: socket.socket | None = None
        parent_channel, child_channel = socket.socketpair()
        parent_channel.settimeout(self.startup_timeout_seconds)
        launcher = "import os,runpy,sys;sys.argv=sys.argv[1:];sys.path.insert(0,os.path.dirname(sys.argv[0]));runpy.run_path(sys.argv[0],run_name='__main__')"
        command = [
            sys.executable, "-I", "-c", launcher, str(owner_script.resolve()), "--projection-owner",
            str(child_channel.fileno()), str(self.parent_fd), str(self.file_fd), self.path.name,
            *owner_arguments,
        ]
        startup_response_received = False
        try:
            self.process = subprocess.Popen(
                command, pass_fds=(child_channel.fileno(), self.parent_fd, self.file_fd),
                env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            child_channel.close()
            self.channel = parent_channel
            response = receive_frame(parent_channel)
            startup_response_received = True
            if set(response) == {"error"} and isinstance(response["error"], Mapping) and set(response["error"]) == {"code", "detail"}:
                raise ChatProjectionStoreError(str(response["error"]["code"]), str(response["error"]["detail"]))
            if response != {"ready": True}:
                raise ChatProjectionStoreError("owner_protocol_error", "projection owner did not initialize")
            parent_channel.settimeout(self.ipc_timeout_seconds)
        except BaseException as exc:
            parent_channel.close()
            child_channel.close()
            self._terminate_process()
            if created:
                try:
                    os.unlink(self.path.name, dir_fd=self.parent_fd)
                except OSError:
                    pass
            self._close_handles()
            if startup_response_received and isinstance(exc, ChatProjectionStoreError):
                raise
            raise ChatProjectionStoreError("owner_start_failed", "projection owner failed to start") from exc

    @staticmethod
    def _validate_timeout(value: float, label: str) -> float:
        if (
            not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
            or not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS
        ):
            detail = "owner startup timeout is invalid" if label == "owner startup" else f"IPC timeout must be {MIN_TIMEOUT_SECONDS}..{MAX_TIMEOUT_SECONDS} seconds"
            raise ChatProjectionStoreError("invalid_input", detail)
        return value

    def rpc(self, operation: str, **arguments: Any) -> Any:
        if self._poisoned or self._closed:
            raise ChatProjectionStoreError("owner_unavailable", "projection owner exited")
        with self._lock:
            if self._poisoned or self._closed or self.channel is None or self.process is None or self.process.poll() is not None:
                self.poison()
                raise ChatProjectionStoreError("owner_unavailable", "projection owner exited")
            request_id = self._next_request_id
            if request_id > MAX_REQUEST_ID:
                self.poison()
                raise ChatProjectionStoreError("owner_protocol_error", "request id exhausted")
            self._next_request_id += 1
            dispatched = False
            try:
                send_frame(self.channel, {"request_id": request_id, "operation": operation, "arguments": arguments})
                dispatched = True
                response = receive_frame(self.channel)
                return self._validate_response(response, request_id, operation, arguments)
            except ChatProjectionStoreError as exc:
                domain = exc.__cause__ if exc.code == "owner_domain_error" else None
                if not isinstance(domain, ChatProjectionStoreError):
                    self.poison()
                else:
                    if domain.code in {"insecure_store_file", "path_race", "owner_protocol_error", "owner_internal_error"}:
                        self.poison()
                    raise domain
                if operation == "commit" and dispatched:
                    raise ChatProjectionStoreError("commit_outcome_unknown", "owner response was lost after commit dispatch") from exc
                raise
            except (OSError, TimeoutError, UnicodeError) as exc:
                self.poison()
                code = "commit_outcome_unknown" if operation == "commit" and dispatched else "owner_unavailable"
                raise ChatProjectionStoreError(code, "projection owner response unavailable") from exc

    def _validate_response(self, response: Mapping[str, Any], request_id: int, operation: str, arguments: Mapping[str, Any]) -> Any:
        base = {"request_id", "operation"}
        if response.get("request_id") != request_id or response.get("operation") != operation:
            raise ChatProjectionStoreError("owner_protocol_error", "owner response correlation mismatch")
        if set(response) == base | {"error"}:
            error = response["error"]
            if not isinstance(error, Mapping) or set(error) != {"code", "detail"}:
                raise ChatProjectionStoreError("owner_protocol_error", "invalid owner error envelope")
            if not all(self._valid_error_text(error.get(key)) for key in ("code", "detail")):
                raise ChatProjectionStoreError("owner_protocol_error", "invalid owner error envelope")
            wrapped = ChatProjectionStoreError("owner_domain_error", "owner returned a domain error")
            wrapped.__cause__ = ChatProjectionStoreError(error["code"], error["detail"])
            raise wrapped
        if set(response) != base | {"result"}:
            raise ChatProjectionStoreError("owner_protocol_error", "invalid owner result envelope")
        return self._validate_result(operation, response["result"], arguments)

    def _valid_error_text(self, value: Any) -> bool:
        if not isinstance(value, str) or not value:
            return False
        try:
            return len(value.encode("utf-8")) <= self._max_error_text_bytes
        except UnicodeError:
            return False

    def poison(self) -> None:
        self._poisoned = True
        channel, self.channel = self.channel, None
        self._terminate_process()
        if channel is not None:
            channel.close()
        self._close_handles()

    def _terminate_process(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()

    def _close_handles(self) -> None:
        for name in ("file_fd", "parent_fd"):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            close_error = None
            try:
                if not self._poisoned:
                    self.rpc("close")
            except ChatProjectionStoreError as exc:
                close_error = exc
                if not self._poisoned:
                    self.poison()
            process = self.process
            if process is not None and process.poll() is None:
                try:
                    process.wait(timeout=self.ipc_timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    close_error = close_error or ChatProjectionStoreError("owner_unavailable", "projection owner did not close")
                    close_error.__cause__ = exc
                    self.poison()
            channel, self.channel = self.channel, None
            self.process = None
            if channel is not None:
                channel.close()
            self._close_handles()
            self._closed = True
            if close_error is not None:
                raise close_error


def serve_owner(
    channel_fd: int, directory_fd: int, file_fd: int, basename: str,
    create_store: Callable[[int, int, str], Any], dispatch: Callable[[Any, str, Mapping[str, Any], int], Any],
    mutate_result: Callable[[socket.socket, int, str, Any], tuple[Any, bool]], response_limit: int,
) -> None:
    os.environ.clear()
    os.fchdir(directory_fd)
    channel = socket.socket(fileno=channel_fd)
    store = None
    try:
        store = create_store(directory_fd, file_fd, basename)
        send_frame(channel, {"ready": True})
        while True:
            request = receive_frame(channel)
            if set(request) != {"request_id", "operation", "arguments"}:
                raise ChatProjectionStoreError("owner_protocol_error", "request shape is invalid")
            request_id, operation, arguments = request["request_id"], request["operation"], request["arguments"]
            if type(request_id) is not int or not 0 <= request_id <= MAX_REQUEST_ID:
                raise ChatProjectionStoreError("owner_protocol_error", "request id is invalid")
            if not isinstance(operation, str) or not isinstance(arguments, Mapping):
                raise ChatProjectionStoreError("owner_protocol_error", "request shape is invalid")
            try:
                result = dispatch(store, operation, arguments, request_id)
                result, skip_normal = mutate_result(channel, request_id, operation, result)
                if skip_normal:
                    continue
                try:
                    send_frame(channel, {"request_id": request_id, "operation": operation, "result": result}, limit=response_limit)
                except ChatProjectionStoreError as exc:
                    if exc.code != "ipc_too_large":
                        raise
                    raise ChatProjectionStoreError("response_too_large", "owner result exceeds response budget") from exc
                if operation == "close":
                    break
            except ChatProjectionStoreError as exc:
                send_frame(channel, {"request_id": request_id, "operation": operation, "error": {"code": exc.code, "detail": exc.detail}})
            except BaseException:
                send_frame(channel, {"request_id": request_id, "operation": operation, "error": {"code": "owner_internal_error", "detail": "owner operation failed"}})
    except ChatProjectionStoreError as exc:
        if exc.code == "owner_unavailable":
            os._exit(1)
        send_frame(channel, {"error": {"code": exc.code, "detail": exc.detail}})
    except BaseException:
        try:
            send_frame(channel, {"error": {"code": "owner_init_failed", "detail": "owner initialization failed"}})
        except BaseException:
            pass
    finally:
        if store is not None and store._connection is not None:
            store._connection.close()
        channel.close()
        os.close(file_fd)
        os.close(directory_fd)

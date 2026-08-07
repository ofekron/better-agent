#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import tempfile
import time

import pytest

from better_agent_sdk.runtime_transport import RuntimeTransport

import runtime_broker as rb
from runtime_broker import (
    BrokerRequest,
    RuntimeBroker,
    _decode,
    _encode,
    _recv_exact,
    _recv_frame,
    _require_posix_peer,
    _send_frame,
)


def test_every_win32_call_has_a_declared_prototype() -> None:
    """Untyped ctypes calls truncate 64-bit handles and SID pointers.

    An undeclared `ConvertSidToStringSidW` raised
    `OverflowError: int too long to convert` on the peer's SID address,
    which tore down the broker pipe and made every run on a Windows node
    fail. This is dead code on POSIX, so guard the class of bug at the
    source instead: each kernel32/advapi32 function the peer check calls
    must have argtypes declared.
    """
    import ast

    source = Path(rb.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def _owner_name(node: ast.expr) -> str:
        # Matches both `kernel32.Foo(...)` and `ctypes.windll.kernel32.Foo(...)`.
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    called: set[str] = set()
    declared: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if _owner_name(node.func.value) in ("kernel32", "advapi32"):
                called.add(node.func.attr)
        # kernel32.Foo.argtypes = [...]
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "argtypes"
                    and isinstance(target.value, ast.Attribute)
                ):
                    declared.add(target.value.attr)

    assert called, "no Win32 calls found — did the peer check move?"
    missing = sorted(called - declared)
    assert not missing, f"Win32 calls without declared argtypes: {missing}"


def test_error_reply_to_a_dead_peer_does_not_raise() -> None:
    """Reporting a failure must not become a second, fatal failure.

    The serve loops reply to a failed request on the same connection. A
    client that already disconnected turned that reply into
    `BrokenPipeError` raised out of the except block, killing the serving
    thread — one bad request took the whole broker down.
    """
    from runtime_broker import _reply_error

    def dead_peer(_payload: bytes) -> None:
        raise BrokenPipeError("[WinError 232] The pipe is being closed")

    _reply_error(dead_peer, ValueError("original failure"))

    delivered: list[bytes] = []
    _reply_error(delivered.append, ValueError("original failure"))
    assert delivered, "a live peer must still receive the error reply"
    assert b"original failure" in delivered[0]


@pytest.fixture
def broker(tmp_path: Path):
    """A live POSIX broker whose handler echoes operation + payload back."""
    received: list[BrokerRequest] = []

    def handle(request: BrokerRequest) -> dict:
        received.append(request)
        return {"success": True, "operation": request.operation, "payload": request.payload}

    instance = RuntimeBroker(tmp_path / "sock", handle)
    instance.start()
    instance.received = received  # type: ignore[attr-defined]
    yield instance
    instance.stop()


def test_unix_roundtrip_invokes_handler_and_returns_payload(broker: RuntimeBroker) -> None:
    transport = RuntimeTransport(broker.address)
    result = transport.request(
        {
            "version": 1,
            "kind": "invoke",
            "operation": "example_read",
            "payload": {"value": "ok"},
        }
    )
    assert result == {
        "success": True,
        "operation": "example_read",
        "payload": {"value": "ok"},
    }
    assert broker.received[0].operation == "example_read"  # type: ignore[attr-defined]


def test_accepted_request_kinds_round_trip(broker: RuntimeBroker) -> None:
    transport = RuntimeTransport(broker.address)
    for kind in ("catalog", "status", "cancel"):
        result = transport.request({"version": 1, "kind": kind})
        assert result == {"success": True, "operation": "", "payload": None}


def test_unknown_field_is_rejected(broker: RuntimeBroker) -> None:
    # BrokerRequest is extra=forbid; the serve loop turns the validation
    # error into a failure reply, which the transport surfaces as RuntimeError.
    transport = RuntimeTransport(broker.address)
    with pytest.raises(RuntimeError, match="extra"):
        transport.request({"version": 1, "kind": "invoke", "unexpected": True})


def test_unsupported_protocol_version_returns_error(broker: RuntimeBroker) -> None:
    transport = RuntimeTransport(broker.address)
    with pytest.raises(RuntimeError, match="protocol version"):
        transport.request({"version": 2, "kind": "invoke"})


def test_unsupported_kind_returns_error(broker: RuntimeBroker) -> None:
    transport = RuntimeTransport(broker.address)
    with pytest.raises(RuntimeError, match="request kind"):
        transport.request({"version": 1, "kind": "bogus"})


def test_start_rejects_already_started_broker(tmp_path: Path) -> None:
    broker = RuntimeBroker(tmp_path / "sock", lambda _r: {})
    broker.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            broker.start()
    finally:
        broker.stop()


def test_start_rejects_symlink_directory(tmp_path: Path) -> None:
    # __init__ resolves the directory (following symlinks), so a symlinked
    # home never reaches the guard through the public API. Set the symlink
    # precondition directly to prove start() still refuses to bind one.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    broker = RuntimeBroker(tmp_path / "unused", lambda _r: {})
    broker._directory = link
    with pytest.raises(RuntimeError, match="invalid"):
        broker.start()


def test_short_directory_path_binds_socket_in_place(tmp_path: Path) -> None:
    # A short home keeps the encoded socket path under the 96-byte cap, so the
    # broker binds inside the home directly instead of falling back to /tmp.
    home = Path(tempfile.mkdtemp(prefix="ba", dir="/tmp"))
    try:
        broker = RuntimeBroker(home, lambda _r: {})
        address = broker.start()
        try:
            sock_path = Path(address.removeprefix("unix:"))
            assert sock_path.parent == home
            assert broker._socket_directory is None
        finally:
            broker.stop()
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_stop_on_never_started_broker_is_a_noop() -> None:
    broker = RuntimeBroker(Path("/unused"), lambda _r: {})
    broker.stop()  # no thread, no listener, empty address -> must not raise


def test_stop_tolerates_already_removed_socket_directory(tmp_path: Path) -> None:
    # The fallback socket directory can disappear before stop() runs; teardown
    # must treat that as success rather than raising.
    deep = tmp_path / ("x" * 64)
    deep.mkdir()
    broker = RuntimeBroker(deep, lambda _r: {})
    broker.start()
    assert broker._socket_directory is not None
    shutil.rmtree(broker._socket_directory)  # simulate external cleanup
    broker.stop()
    assert broker._socket_directory is None


def test_serve_loop_survives_accept_timeout(broker: RuntimeBroker) -> None:
    # accept() has a 0.5s timeout; an idle broker must keep serving after the
    # TimeoutError (the loop continues) rather than tearing down. There is no
    # event to await — the timeout itself is the production idle signal — so
    # wait one cycle, then prove a real request still round-trips.
    time.sleep(0.75)
    transport = RuntimeTransport(broker.address)
    assert transport.request({"version": 1, "kind": "status"})["success"] is True


def test_start_reports_bind_failure(tmp_path: Path, monkeypatch) -> None:
    # _serve_unix runs in a thread; a bind failure must propagate out of
    # start() as a "failed to start" error rather than hanging the caller.
    real_socket = rb.socket.socket

    class FailingSocket(real_socket):
        def bind(self, *args, **kwargs) -> None:
            raise OSError("injected bind failure")

    monkeypatch.setattr(rb.socket, "socket", FailingSocket)
    broker = RuntimeBroker(tmp_path / "sock", lambda _r: {})
    with pytest.raises(RuntimeError, match="failed to start"):
        broker.start()


def test_stop_removes_socket_file(tmp_path: Path) -> None:
    broker = RuntimeBroker(tmp_path / "sock", lambda _r: {})
    address = broker.start()
    sock_path = Path(address.removeprefix("unix:"))
    assert sock_path.exists()
    broker.stop()
    assert not sock_path.exists()


def test_long_directory_path_uses_tmp_fallback(tmp_path: Path) -> None:
    # The candidate socket name is ~44 bytes; a long home path pushes the
    # encoded socket path past the 96-byte cap, forcing the /tmp fallback so
    # the broker still binds. stop() must then tear that fallback dir down.
    deep = tmp_path / ("x" * 64)
    deep.mkdir()
    broker = RuntimeBroker(deep, lambda _r: {})
    address = broker.start()
    try:
        sock_path = Path(address.removeprefix("unix:"))
        assert broker._socket_directory is not None
        assert sock_path.parent == broker._socket_directory
        assert str(sock_path.parent.parent) == str(Path(tempfile.gettempdir()))
    finally:
        broker.stop()
    assert broker._socket_directory is None


def test_require_posix_peer_accepts_same_user() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("SO_PEERCRED acceptance is Linux-specific")

    class FakeConn:
        def getsockopt(self, _level: int, _opt: int, _buflen: int) -> bytes:
            return struct.pack("3i", 0, os.getuid(), 0)

    _require_posix_peer(FakeConn())  # type: ignore[arg-type]


def test_require_posix_peer_rejects_different_user() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("SO_PEERCRED rejection is Linux-specific")

    class FakeConn:
        def getsockopt(self, _level: int, _opt: int, _buflen: int) -> bytes:
            return struct.pack("3i", 0, os.getuid() + 4242, 0)

    with pytest.raises(PermissionError, match="different user"):
        _require_posix_peer(FakeConn())  # type: ignore[arg-type]


def test_decode_rejects_non_object_payload() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        _decode(json.dumps([1, 2, 3]).encode("utf-8"))


def test_decode_rejects_oversize_message() -> None:
    with pytest.raises(ValueError, match="too large"):
        _decode(b"x" * (rb._MAX_MESSAGE_BYTES + 1))


def test_encode_rejects_oversize_response() -> None:
    with pytest.raises(ValueError, match="too large"):
        _encode({"k": "v" * (rb._MAX_MESSAGE_BYTES + 1)})


def test_encode_decode_roundtrip() -> None:
    encoded = _encode({"a": 1, "b": [2, 3]})
    assert _decode(encoded) == {"a": 1, "b": [2, 3]}


def test_recv_frame_rejects_oversize_request() -> None:
    server, client = socket.socketpair()
    try:
        client.sendall(struct.pack("!I", rb._MAX_MESSAGE_BYTES + 1))
        with pytest.raises(ValueError, match="too large"):
            _recv_frame(server)
    finally:
        server.close()
        client.close()


def test_recv_exact_raises_on_closed_connection() -> None:
    server, client = socket.socketpair()
    try:
        client.close()  # peer gone -> recv returns b""
        with pytest.raises(ConnectionError, match="closed"):
            _recv_exact(server, 16)
    finally:
        server.close()


def test_send_frame_prefixes_length_and_payload() -> None:
    server, client = socket.socketpair()
    try:
        _send_frame(server, b"payload")
        size = struct.unpack("!I", _recv_exact(client, 4))[0]
        assert size == 7
        assert _recv_exact(client, size) == b"payload"
    finally:
        server.close()
        client.close()

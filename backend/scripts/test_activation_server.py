"""Promoted pytest coverage for desktop/activation_server.py — marketplace
deep-link activation handoff.

activation_server.py owns the single-instance desktop activation channel: an
ActivationServer binds a loopback listener, authenticates forwarded activation
events with an HMAC-compared secret, and enforces owner exclusivity via an
O_EXCL owner file + pid liveness. forward_activation delivers a deep-link
event to the running owner over that channel.

It was previously only exercised by desktop/test_marketplace_deep_link.py
(a standalone runner, NOT pytest-collected), so it measured 0% in the unit
tier. This module ports that coverage into pytest and closes every
POSIX-reachable branch. The Windows pid-liveness probe is driven through a
fake ctypes.windll kernel32 (winreg-free). One unreachable defensive raise
in _claim_ownership (after an inner try that always exits via raise or
continue) is `# pragma: no cover` in the source — see tech-debt card.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))

import activation_server  # noqa: E402
from activation_server import (  # noqa: E402
    MAX_MESSAGE_BYTES,
    ActivationServer,
    _process_alive,
    _valid_event,
    forward_activation,
    forward_activation_when_ready,
)


def _token(byte: int = 7) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip("=")


def _pair_event(intent: str | None = None) -> dict:
    return {"type": "marketplace_pair", "intent": intent or _token(), "version": 1}


def _write_state(
    state_dir: Path,
    *,
    port: object = 1,
    secret: object = "s",
    version: int = 1,
    raw: str | None = None,
) -> None:
    path = state_dir / "desktop_activation.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return
    path.write_text(
        json.dumps({"version": version, "pid": 1, "port": port, "secret": secret}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _valid_event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("not-a-dict", False),
        (None, False),
        ({}, False),                                   # no type
        ({"type": 1}, False),                          # type not str
        ({"type": "unknown"}, False),                  # unknown type
        ({"type": "activate", "extra": 1}, False),     # activate + extra field
        ({"type": "activate"}, True),                  # activate valid
        ({"type": "marketplace_pair", "version": 1}, False),         # missing intent
        ({"type": "marketplace_pair", "intent": "t"}, False),        # missing version
        ({"type": "marketplace_pair", "intent": "t", "version": 1, "x": 1}, False),  # extra
        ({"type": "marketplace_pair", "intent": "t", "version": 1}, True),           # valid
        ({"type": "marketplace_pair", "intent": 2, "version": 1}, False),            # intent not str
        ({"type": "marketplace_pair", "intent": "t", "version": "1"}, False),        # version not int
        ({"type": "marketplace_pair", "intent": "t", "version": True}, False),       # version is bool
        ({"type": "marketplace_pair", "intent": "t", "version": 2}, False),          # version != 1
    ],
)
def test_valid_event(value, expected):
    assert _valid_event(value) is expected


# ---------------------------------------------------------------------------
# _process_alive — POSIX (host) + Windows (fake kernel32)
# ---------------------------------------------------------------------------


class _FakeKernel32:
    def __init__(self, handle, exit_code, get_exit_ok=True):
        self._handle = handle
        self._exit = exit_code
        self._get_ok = get_exit_ok
        self.closed = []

    def OpenProcess(self, _access, _inherit, _pid):
        return self._handle

    def GetExitCodeProcess(self, _process, output):
        output._obj.value = self._exit
        return 1 if self._get_ok else 0

    def CloseHandle(self, process):
        self.closed.append(process)
        return 1


def _install_windows_kernel(monkeypatch, kernel):
    monkeypatch.setattr(activation_server.os, "name", "nt")
    monkeypatch.setattr(
        ctypes, "windll", type("Windll", (), {"kernel32": kernel})(), raising=False
    )


def test_process_alive_posix_live_process(monkeypatch):
    monkeypatch.setattr(activation_server.os, "name", "posix")
    assert _process_alive(os.getpid()) is True


def test_process_alive_posix_dead_pid(monkeypatch):
    monkeypatch.setattr(activation_server.os, "name", "posix")
    monkeypatch.setattr(
        activation_server.os, "kill", lambda *_: (_ for _ in ()).throw(ProcessLookupError())
    )
    assert _process_alive(999999) is False


def test_process_alive_posix_permission_denied_means_alive(monkeypatch):
    # EPERM on a foreign pid means it exists but is not signalable -> alive.
    monkeypatch.setattr(activation_server.os, "name", "posix")
    monkeypatch.setattr(
        activation_server.os, "kill", lambda *_: (_ for _ in ()).throw(PermissionError())
    )
    assert _process_alive(1) is True


def test_process_alive_windows_still_active(monkeypatch):
    k = _FakeKernel32(handle=123, exit_code=259)
    _install_windows_kernel(monkeypatch, k)
    assert _process_alive(42) is True
    assert k.closed == [123]


def test_process_alive_windows_zero_handle(monkeypatch):
    _install_windows_kernel(monkeypatch, _FakeKernel32(handle=0, exit_code=0))
    assert _process_alive(42) is False


def test_process_alive_windows_get_exit_fails(monkeypatch):
    k = _FakeKernel32(handle=123, exit_code=0, get_exit_ok=False)
    _install_windows_kernel(monkeypatch, k)
    assert _process_alive(42) is False
    assert k.closed == [123]


def test_process_alive_windows_exit_not_still_active(monkeypatch):
    _install_windows_kernel(monkeypatch, _FakeKernel32(handle=123, exit_code=1))
    assert _process_alive(42) is False


# ---------------------------------------------------------------------------
# Ownership (_claim_ownership edge cases via ActivationServer.start)
# ---------------------------------------------------------------------------


def test_claim_ownership_takes_over_dead_owner(tmp_path, monkeypatch):
    (tmp_path / "desktop_activation.owner").write_text(
        json.dumps({"pid": 999999, "secret": "stale"}), encoding="utf-8"
    )
    monkeypatch.setattr(activation_server, "_process_alive", lambda _pid: False)
    server = ActivationServer(tmp_path, lambda _e: None)
    try:
        server.start()  # dead owner unlinked + reclaimed, no RuntimeError
    finally:
        server.close()


def test_claim_ownership_corrupt_owner_file_fails_closed(tmp_path):
    (tmp_path / "desktop_activation.owner").write_text("{not valid json", encoding="utf-8")
    server = ActivationServer(tmp_path, lambda _e: None)
    with pytest.raises(RuntimeError, match="unavailable"):
        server.start()


def test_second_server_cannot_replace_live_owner(tmp_path):
    first = ActivationServer(tmp_path, lambda _e: None)
    second = ActivationServer(tmp_path, lambda _e: None)
    first.start()
    original = (tmp_path / "desktop_activation.owner").read_text(encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second.start()
        assert (tmp_path / "desktop_activation.owner").read_text(encoding="utf-8") == original
    finally:
        second.close()
        first.close()


def test_start_releases_ownership_when_bind_fails(tmp_path, monkeypatch):
    class _BindFailSocket:
        def __init__(self, *a, **k):
            pass

        def bind(self, _addr):
            raise OSError("bind failed")

        def close(self):
            pass

    monkeypatch.setattr(activation_server.socket, "socket", _BindFailSocket)
    server = ActivationServer(tmp_path, lambda _e: None)
    with pytest.raises(OSError):
        server.start()
    assert server._owner_fd is None
    assert not (tmp_path / "desktop_activation.owner").exists()


def test_claim_ownership_unavailable_when_owner_persists(tmp_path, monkeypatch):
    # Both range(2) attempts find an existing owner with a dead pid that keeps
    # reappearing after unlink (a concurrent claimant) -> loop exhausts ->
    # "ownership is unavailable".
    (tmp_path / "desktop_activation.owner").write_text(
        json.dumps({"pid": 999999, "secret": "stale"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        activation_server.os, "open", lambda *a, **k: (_ for _ in ()).throw(FileExistsError())
    )
    monkeypatch.setattr(activation_server, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(Path, "unlink", lambda *a, **k: None)  # owner never actually disappears
    server = ActivationServer(tmp_path, lambda _e: None)
    with pytest.raises(RuntimeError, match="unavailable"):
        server.start()


def test_release_ownership_skips_unlink_on_secret_mismatch(tmp_path):
    server = ActivationServer(tmp_path, lambda _e: None)
    server.start()
    # A different claimant overwrote the owner file -> secret no longer matches.
    (tmp_path / "desktop_activation.owner").write_text(
        json.dumps({"pid": os.getpid(), "secret": "someone-else"}), encoding="utf-8"
    )
    server.close()
    assert (tmp_path / "desktop_activation.owner").exists()  # not unlinked


def test_close_swallows_missing_state_and_owner_files(tmp_path):
    server = ActivationServer(tmp_path, lambda _e: None)
    server.start()
    (tmp_path / "desktop_activation.owner").unlink()
    (tmp_path / "desktop_activation.json").unlink()
    server.close()  # both reads raise FileNotFoundError -> swallowed


# ---------------------------------------------------------------------------
# _serve — driven directly in-process (no accept-loop timing races).
# ---------------------------------------------------------------------------


class _ScriptedListener:
    """accept() returns scripted items in order: exceptions are raised,
    otherwise the item is returned as the connection."""

    def __init__(self, script):
        self._script = list(script)

    def accept(self):
        if not self._script:
            raise OSError("listener closed")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item, ("127.0.0.1", 0)

    def settimeout(self, _t):
        pass

    def close(self):
        pass


class _ServeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSemaphore:
    def __init__(self, acquire_ok):
        self.acquire_ok = acquire_ok
        self.released = 0

    def acquire(self, blocking=False):
        return self.acquire_ok

    def release(self):
        self.released += 1


def test_serve_continues_on_timeout_and_drops_when_semaphore_full(tmp_path):
    server = ActivationServer(tmp_path, lambda _e: None)
    dropped = _ServeConn()
    listener = _ScriptedListener(
        [socket.timeout(), dropped, OSError("listener closed")]
    )
    server._socket = listener
    server._connections = _FakeSemaphore(acquire_ok=False)
    server._serve()  # returns on the OSError
    assert dropped.closed       # semaphore-full arm closed the connection
    assert server._connections.released == 0   # nothing acquired -> nothing released here


# ---------------------------------------------------------------------------
# _handle_connection — driven directly with fake connections.
# ---------------------------------------------------------------------------


class _HandlerConn:
    def __init__(self, recv_data=None, recv_raises=None):
        self._recv = recv_data
        self._raises = recv_raises
        self.sent = []
        self.closed = False

    def settimeout(self, _t):
        pass

    def recv(self, _n):
        if self._raises is not None:
            raise self._raises
        return self._recv

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.closed = True
        return False


def _make_server(tmp_path):
    delivered = []
    server = ActivationServer(tmp_path, delivered.append)
    server._connections = _FakeSemaphore(acquire_ok=True)
    return server, delivered


def test_handle_connection_oversize_dropped(tmp_path):
    server, delivered = _make_server(tmp_path)
    conn = _HandlerConn(recv_data=b"x" * (MAX_MESSAGE_BYTES + 1))
    server._handle_connection(conn)
    assert delivered == [] and conn.sent == []
    assert server._connections.released == 1


def test_handle_connection_malformed_json_dropped(tmp_path):
    server, delivered = _make_server(tmp_path)
    server._handle_connection(_HandlerConn(recv_data=b"not json"))
    assert delivered == []
    assert server._connections.released == 1


def test_handle_connection_non_dict_message_dropped(tmp_path):
    server, delivered = _make_server(tmp_path)
    server._handle_connection(_HandlerConn(recv_data=b"[1, 2, 3]"))
    assert delivered == []
    assert server._connections.released == 1


def test_handle_connection_non_str_secret_dropped(tmp_path):
    server, delivered = _make_server(tmp_path)
    payload = json.dumps({"secret": 5, "event": _pair_event()}).encode()
    conn = _HandlerConn(recv_data=payload)
    server._handle_connection(conn)
    assert delivered == [] and conn.sent == []
    assert server._connections.released == 1


def test_handle_connection_secret_mismatch_dropped(tmp_path):
    server, delivered = _make_server(tmp_path)
    payload = json.dumps({"secret": "wrong", "event": _pair_event()}).encode()
    conn = _HandlerConn(recv_data=payload)
    server._handle_connection(conn)
    assert delivered == [] and conn.sent == []
    assert server._connections.released == 1


def test_handle_connection_invalid_event_dropped(tmp_path):
    server, delivered = _make_server(tmp_path)
    payload = json.dumps(
        {"secret": server._secret, "event": {"type": "activate", "extra": 1}}
    ).encode()
    conn = _HandlerConn(recv_data=payload)
    server._handle_connection(conn)
    assert delivered == [] and conn.sent == []
    assert server._connections.released == 1


def test_handle_connection_valid_event_delivered_and_acked(tmp_path):
    server, delivered = _make_server(tmp_path)
    event = _pair_event()
    payload = json.dumps({"secret": server._secret, "event": event}).encode()
    conn = _HandlerConn(recv_data=payload)
    server._handle_connection(conn)
    assert delivered == [event]
    assert conn.sent == [b'{"ok":true}']
    assert server._connections.released == 1


def test_handle_connection_socket_error_swallowed(tmp_path):
    server, delivered = _make_server(tmp_path)
    server._handle_connection(_HandlerConn(recv_raises=socket.timeout()))
    assert delivered == []
    assert server._connections.released == 1


# ---------------------------------------------------------------------------
# forward_activation — failure modes via monkeypatched create_connection.
# ---------------------------------------------------------------------------


def test_forward_activation_rejects_invalid_event(tmp_path):
    assert forward_activation(tmp_path, {"type": "activate", "extra": 1}) is False


def test_forward_activation_missing_state_fails(tmp_path):
    assert forward_activation(tmp_path, _pair_event()) is False


def test_forward_activation_state_not_dict(tmp_path):
    _write_state(tmp_path, raw="[]")
    assert forward_activation(tmp_path, _pair_event()) is False


def test_forward_activation_wrong_version(tmp_path):
    _write_state(tmp_path, version=2)
    assert forward_activation(tmp_path, _pair_event()) is False


def test_forward_activation_port_not_int(tmp_path):
    _write_state(tmp_path, port="x")
    assert forward_activation(tmp_path, _pair_event()) is False


def test_forward_activation_secret_not_str(tmp_path):
    _write_state(tmp_path, secret=5)
    assert forward_activation(tmp_path, _pair_event()) is False


def test_forward_activation_payload_oversize(tmp_path):
    _write_state(tmp_path)
    big = _pair_event(intent="x" * (MAX_MESSAGE_BYTES + 50))
    assert forward_activation(tmp_path, big) is False


def test_forward_activation_connection_refused(tmp_path, monkeypatch):
    _write_state(tmp_path)
    monkeypatch.setattr(
        activation_server.socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")),
    )
    assert forward_activation(tmp_path, _pair_event()) is False


class _FakeClientConn:
    def __init__(self, recv_data):
        self._recv = recv_data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def sendall(self, _data):
        pass

    def recv(self, _n):
        return self._recv


def test_forward_activation_non_ok_response(tmp_path, monkeypatch):
    _write_state(tmp_path)
    monkeypatch.setattr(
        activation_server.socket,
        "create_connection",
        lambda *a, **k: _FakeClientConn(b'{"ok":false}'),
    )
    assert forward_activation(tmp_path, _pair_event()) is False


def test_forward_activation_recv_not_json(tmp_path, monkeypatch):
    _write_state(tmp_path)
    monkeypatch.setattr(
        activation_server.socket,
        "create_connection",
        lambda *a, **k: _FakeClientConn(b""),
    )
    assert forward_activation(tmp_path, _pair_event()) is False


# ---------------------------------------------------------------------------
# forward_activation_when_ready — polling loop.
# ---------------------------------------------------------------------------


def test_forward_activation_when_ready_success(tmp_path):
    delivered = []
    received = threading.Event()

    def on_activation(event):
        delivered.append(event)
        received.set()

    server = ActivationServer(tmp_path, on_activation)
    try:
        server.start()
        assert forward_activation_when_ready(tmp_path, _pair_event(), timeout=2) is True
        assert received.wait(2)
        assert delivered
    finally:
        server.close()


def test_forward_activation_when_ready_timeout(tmp_path, monkeypatch):
    _write_state(tmp_path)
    monkeypatch.setattr(
        activation_server.socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")),
    )
    assert forward_activation_when_ready(tmp_path, _pair_event(), timeout=0.1) is False


def test_forward_activation_when_ready_succeeds_on_retry(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_forward(_state_dir, _event):
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr(activation_server, "forward_activation", fake_forward)
    assert forward_activation_when_ready(tmp_path, _pair_event(), timeout=2) is True
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Real-server integration (end-to-end wiring).
# ---------------------------------------------------------------------------


def test_activation_forwarding_authenticates_and_delivers(tmp_path):
    delivered = []
    received = threading.Event()

    def on_activation(event):
        delivered.append(event)
        received.set()

    server = ActivationServer(tmp_path, on_activation)
    server.start()
    try:
        event = _pair_event()
        assert forward_activation(tmp_path, event) is True
        assert received.wait(2)
        assert delivered == [event]
        # invalid event rejected client-side before delivery
        assert (
            forward_activation(
                tmp_path,
                {"type": "marketplace_pair", "intent": _token(), "extra": "no"},
            )
            is False
        )
        # owned state file is mode 0o600
        assert os.stat(tmp_path / "desktop_activation.json").st_mode & 0o077 == 0
    finally:
        server.close()
    # close removed the state file owned by this server
    assert not (tmp_path / "desktop_activation.json").exists()

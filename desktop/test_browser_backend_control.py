from __future__ import annotations

import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

import browser_backend_control as bbc


class _ControlServer:
    """One-shot AF_UNIX server: binds a temp path, accepts a single client,
    records the raw request bytes, and replies with a fixed payload."""

    def __init__(self, reply: bytes) -> None:
        self._reply = reply
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._dir = tempfile.mkdtemp(prefix="bbc-test-")
        self.path = Path(self._dir) / "control.sock"
        self.received = b""
        self._accepted = threading.Event()

    def __enter__(self) -> "_ControlServer":
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        try:
            self._sock.settimeout(5.0)
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            self._accepted.set()
            blob = b""
            while b"\n" not in blob:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                blob += chunk
            self.received = blob
            conn.sendall(self._reply)

    def __exit__(self, *exc: object) -> None:
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.path.unlink(missing_ok=True)
        Path(self._dir).rmdir()


def test_request_returns_dict_on_ok() -> None:
    reply = json.dumps({"ok": True, "pid": 4242, "extra": "x"}).encode() + b"\n"
    with _ControlServer(reply) as server:
        result = bbc.request(server.path, {"op": "start", "checkout": "/r"})
    assert result == {"ok": True, "pid": 4242, "extra": "x"}
    req = json.loads(server.received.decode())
    assert req == {"op": "start", "checkout": "/r"}


def test_request_raises_when_not_ok() -> None:
    reply = json.dumps({"ok": False, "error": "nope"}).encode() + b"\n"
    with _ControlServer(reply) as server:
        with pytest.raises(RuntimeError, match="nope"):
            bbc.request(server.path, {"op": "status"})


def test_request_raises_on_non_dict_response() -> None:
    reply = json.dumps([1, 2, 3]).encode() + b"\n"
    with _ControlServer(reply) as server:
        with pytest.raises(RuntimeError, match="invalid response"):
            bbc.request(server.path, {"op": "status"})


def test_request_raises_when_response_too_large() -> None:
    # >32 KiB with no newline trips the size guard before any JSON parse.
    reply = b"x" * (32 * 1024 + 1)
    with _ControlServer(reply) as server:
        with pytest.raises(RuntimeError, match="too large"):
            bbc.request(server.path, {"op": "status"})


def test_main_start_sends_payload_and_prints_pid(capsys: pytest.CaptureFixture[str]) -> None:
    reply = json.dumps({"ok": True, "pid": 7777}).encode() + b"\n"
    with _ControlServer(reply) as server:
        rc = bbc.main([
            "--control", str(server.path),
            "start", "--checkout", "/repo", "--host", "127.0.0.1", "--port", "8000",
        ])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "7777"
    req = json.loads(server.received.decode())
    assert req == {
        "op": "start", "checkout": "/repo", "host": "127.0.0.1", "port": 8000,
    }


def test_main_signal_sends_signal_payload(capsys: pytest.CaptureFixture[str]) -> None:
    reply = json.dumps({"ok": True}).encode() + b"\n"
    with _ControlServer(reply) as server:
        rc = bbc.main(["--control", str(server.path), "signal", "--signal", "INT"])
    assert rc == 0
    assert capsys.readouterr().out == ""
    req = json.loads(server.received.decode())
    assert req == {"op": "signal", "signal": "INT"}


def test_main_status_prints_compact_json(capsys: pytest.CaptureFixture[str]) -> None:
    body = {"ok": True, "running": True, "pid": 9}
    reply = json.dumps(body).encode() + b"\n"
    with _ControlServer(reply) as server:
        rc = bbc.main(["--control", str(server.path), "status"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == json.dumps(body, separators=(",", ":"))
    req = json.loads(server.received.decode())
    assert req == {"op": "status"}


def test_main_shutdown_sends_op(capsys: pytest.CaptureFixture[str]) -> None:
    reply = json.dumps({"ok": True}).encode() + b"\n"
    with _ControlServer(reply) as server:
        rc = bbc.main(["--control", str(server.path), "shutdown"])
    assert rc == 0
    assert capsys.readouterr().out == ""
    req = json.loads(server.received.decode())
    assert req == {"op": "shutdown"}


def test_request_raises_when_connection_closes_before_newline() -> None:
    # Peer closes after partial (non-JSON) bytes with no newline: the recv-empty
    # guard breaks the loop, then the trailing json.loads fails.
    with _ControlServer(b"partial-no-newline") as server:
        with pytest.raises(ValueError):
            bbc.request(server.path, {"op": "status"})


def test_main_rejects_missing_control(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        bbc.main(["status"])


def test_main_rejects_missing_operation(capsys: pytest.CaptureFixture[str]) -> None:
    with _ControlServer(json.dumps({"ok": True}).encode() + b"\n") as server:
        with pytest.raises(SystemExit):
            bbc.main(["--control", str(server.path)])

from __future__ import annotations

import json
import multiprocessing.connection as mp_connection
import queue
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import credential_session_client as client  # noqa: E402


class SilentConnection:
    def __init__(self) -> None:
        self.sent = False
        self.received = False
        self.closed = threading.Event()

    def send_bytes(self, _payload: bytes) -> None:
        self.sent = True

    def poll(self, timeout: float) -> bool:
        assert timeout > 0
        return False

    def recv_bytes(self, *, maxlength: int) -> bytes:
        self.received = True
        raise AssertionError("silent authority must not be read")

    def close(self) -> None:
        self.closed.set()


class BlockingSendConnection:
    def __init__(self) -> None:
        self.send_entered = threading.Event()
        self.release_send = threading.Event()
        self.closed = threading.Event()
        self.send_count = 0

    def send_bytes(self, _payload: bytes) -> None:
        self.send_count += 1
        self.send_entered.set()
        self.release_send.wait()

    def poll(self, _timeout: float) -> bool:
        raise AssertionError("blocked send must not poll for a response")

    def recv_bytes(self, *, maxlength: int) -> bytes:
        raise AssertionError("blocked send must not read a response")

    def close(self) -> None:
        self.closed.set()


class ReplyConnection:
    def __init__(self, *, status: str = "available") -> None:
        self.request_id = ""
        self.status = status
        self.closed = False
        self.sent = False

    def send_bytes(self, payload: bytes) -> None:
        self.sent = True
        request = json.loads(payload.decode("utf-8"))
        self.request_id = request["request_id"]

    def poll(self, _timeout: float) -> bool:
        return True

    def recv_bytes(self, *, maxlength: int) -> bytes:
        return json.dumps(
            {"request_id": self.request_id, "status": self.status}
        ).encode("utf-8")

    def close(self) -> None:
        self.closed = True


class BrokenResponseConnection(ReplyConnection):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.closed_event = threading.Event()

    def poll(self, _timeout: float) -> bool:
        if self.mode == "poll_eof":
            raise EOFError
        return True

    def recv_bytes(self, *, maxlength: int) -> bytes:
        if self.mode == "malformed":
            return b"{"
        if self.mode == "wrong_correlation":
            return json.dumps(
                {"request_id": "another-request", "status": "available"}
            ).encode("utf-8")
        if self.mode == "invalid_status":
            return json.dumps(
                {"request_id": self.request_id, "status": "surprising"}
            ).encode("utf-8")
        return super().recv_bytes(maxlength=maxlength)

    def close(self) -> None:
        self.closed_event.set()


class PartialResponseConnection(ReplyConnection):
    def __init__(self) -> None:
        super().__init__()
        self.receive_entered = threading.Event()
        self.release_receive = threading.Event()
        self.closed_event = threading.Event()

    def recv_bytes(self, *, maxlength: int) -> bytes:
        self.receive_entered.set()
        self.release_receive.wait()
        return super().recv_bytes(maxlength=maxlength)

    def close(self) -> None:
        self.closed_event.set()


def install_connection(monkeypatch, connection) -> None:
    monkeypatch.setattr(client, "_CONNECTION", connection)
    monkeypatch.setattr(client, "_GENERATION_CONNECTION", connection)
    monkeypatch.setattr(client, "_SENDER", None)


def test_windows_inherited_endpoint_uses_pipe_connection(monkeypatch) -> None:
    observed: list[int] = []
    endpoint = object()

    def pipe_connection(handle: int):
        observed.append(handle)
        return endpoint

    monkeypatch.setattr(
        mp_connection,
        "PipeConnection",
        pipe_connection,
        raising=False,
    )

    assert client._connection_from_handle(42, platform_name="nt") is endpoint
    assert observed == [42]


def test_silent_credential_authority_times_out(monkeypatch) -> None:
    connection = SilentConnection()
    install_connection(monkeypatch, connection)
    monkeypatch.setattr(client, "_RESPONSE_TIMEOUT_SECONDS", 0.01)

    try:
        client.request("read", "provider")
    except RuntimeError as exc:
        assert str(exc) == "credential session response timed out"
    else:
        raise AssertionError("silent credential authority must fail closed")

    assert connection.sent
    assert not connection.received
    assert connection.closed.wait(0.5)
    assert not client.available()


def test_transaction_deadline_bounds_blocked_send_and_lock_waiter(monkeypatch) -> None:
    connection = BlockingSendConnection()
    install_connection(monkeypatch, connection)
    monkeypatch.setattr(client, "_RESPONSE_TIMEOUT_SECONDS", 0.05)
    failures: queue.Queue[BaseException] = queue.Queue()

    def request() -> None:
        try:
            client.request("read", "provider")
        except BaseException as exc:
            failures.put(exc)

    first = threading.Thread(target=request)
    started_at = time.monotonic()
    first.start()
    assert connection.send_entered.wait(0.5)
    waiters = [threading.Thread(target=request) for _ in range(19)]
    for waiter in waiters:
        waiter.start()
    first.join(0.5)
    for waiter in waiters:
        waiter.join(0.5)

    try:
        assert not first.is_alive()
        assert all(not waiter.is_alive() for waiter in waiters)
        assert time.monotonic() - started_at < 0.3
        assert connection.send_count == 1
        assert not connection.closed.is_set()
        observed = [failures.get_nowait() for _ in range(20)]
        assert all(isinstance(exc, RuntimeError) for exc in observed)
        assert all("credential session" in str(exc) for exc in observed)
    finally:
        connection.release_send.set()
        first.join(0.5)
        for waiter in waiters:
            waiter.join(0.5)

    assert connection.closed.wait(0.5)


def test_lock_timeout_does_not_retire_healthy_connection(monkeypatch) -> None:
    connection = ReplyConnection()
    install_connection(monkeypatch, connection)
    monkeypatch.setattr(client, "_RESPONSE_TIMEOUT_SECONDS", 0.02)
    failures: queue.Queue[BaseException] = queue.Queue()
    client._LOCK.acquire()

    def request() -> None:
        try:
            client.request("read", "provider")
        except BaseException as exc:
            failures.put(exc)

    caller = threading.Thread(target=request)
    caller.start()
    caller.join(0.5)
    try:
        assert not caller.is_alive()
        failure = failures.get_nowait()
        assert isinstance(failure, RuntimeError)
        assert "timed out" in str(failure)
        assert client._CONNECTION is connection
        assert not connection.closed
    finally:
        client._LOCK.release()
        caller.join(0.5)


def test_oversized_secret_is_rejected_without_sending_or_echoing(monkeypatch) -> None:
    connection = ReplyConnection()
    install_connection(monkeypatch, connection)
    secret = "s" * (128 * 1024)

    try:
        client.request("write", "provider", value=secret)
    except RuntimeError as exc:
        assert "too large" in str(exc)
        assert secret not in str(exc)
    else:
        raise AssertionError("oversized credential request must fail closed")

    assert not connection.sent


@pytest.mark.parametrize(
    "mode, expected_error",
    [
        ("poll_eof", "transport failed"),
        ("malformed", "invalid desktop credential response"),
        ("wrong_correlation", "response correlation failed"),
        ("invalid_status", "invalid desktop credential response"),
    ],
)
def test_protocol_failure_retires_connection(
    monkeypatch,
    mode: str,
    expected_error: str,
) -> None:
    connection = BrokenResponseConnection(mode)
    install_connection(monkeypatch, connection)
    monkeypatch.setattr(client, "_RESPONSE_TIMEOUT_SECONDS", 0.2)

    with pytest.raises(RuntimeError, match=expected_error):
        client.request("read", "provider")

    assert connection.closed_event.wait(0.5)
    assert not client.available()


def test_partial_response_frame_cannot_exceed_transaction_deadline(
    monkeypatch,
) -> None:
    connection = PartialResponseConnection()
    install_connection(monkeypatch, connection)
    monkeypatch.setattr(client, "_RESPONSE_TIMEOUT_SECONDS", 0.05)

    started_at = time.monotonic()
    try:
        with pytest.raises(
            client.CredentialSessionRestartRequired,
            match="request timed out",
        ):
            client.request("read", "provider")
        assert time.monotonic() - started_at < 0.25
        assert connection.receive_entered.is_set()
        assert not client.available()
    finally:
        connection.release_receive.set()

    assert connection.closed_event.wait(0.5)

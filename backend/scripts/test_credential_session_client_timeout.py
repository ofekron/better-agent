from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import credential_session_client as client  # noqa: E402


class SilentConnection:
    def __init__(self) -> None:
        self.sent = False
        self.received = False

    def send_bytes(self, _payload: bytes) -> None:
        self.sent = True

    def poll(self, timeout: float) -> bool:
        assert timeout > 0
        return False

    def recv_bytes(self, *, maxlength: int) -> bytes:
        self.received = True
        raise AssertionError("silent authority must not be read")


def test_silent_credential_authority_times_out(monkeypatch) -> None:
    connection = SilentConnection()
    monkeypatch.setattr(client, "_CONNECTION", connection)
    monkeypatch.setattr(client, "_RESPONSE_TIMEOUT_SECONDS", 0.01)

    try:
        client.request("read", "provider")
    except RuntimeError as exc:
        assert str(exc) == "credential session response timed out"
    else:
        raise AssertionError("silent credential authority must fail closed")

    assert connection.sent
    assert not connection.received

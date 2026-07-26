from __future__ import annotations

import base64
import ctypes
import os
import sys
import tempfile
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import activation_server
from activation_server import ActivationServer, forward_activation
from deep_link import DeepLinkError, parse_deep_link, redact_argv


def _token(byte: int = 7) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip("=")


def test_pair_link_is_strict_and_redacted() -> None:
    raw = f"betteragent://marketplace/pair?v=1&intent={_token()}"
    parsed = parse_deep_link(raw)
    assert parsed.intent == _token()
    assert parsed.as_event()["type"] == "marketplace_pair"
    assert parsed.as_event()["version"] == 1
    assert _token() not in " ".join(redact_argv(["Better Agent", raw]))
    invalid = (
        raw + "&extra=1",
        raw.replace("marketplace", "attacker"),
        raw.replace("v=1", "v=2"),
        raw.replace("?v=1&intent=", "?intent=") + "&v=1",
        raw.replace("?v=1", "?%76=1"),
        raw.replace("betteragent://", "BETTERAGENT://"),
        f"betteragent://marketplace/pair?v=1&intent={_token()[:-1]}",
        f"betteragent://marketplace/pair?v=1&intent={_token()}#fragment",
        f"betteragent://marketplace:bad/pair?v=1&intent={_token()}",
        raw + "$$$",
    )
    for value in invalid:
        try:
            parse_deep_link(value)
        except (DeepLinkError, ValueError):
            continue
        raise AssertionError(f"accepted invalid link: {value}")


def test_activation_forwarding_authenticates_and_delivers() -> None:
    state_dir = Path(tempfile.mkdtemp(prefix="ba-activation-"))
    delivered: list[dict[str, str | int]] = []
    received = threading.Event()

    def on_activation(event: dict[str, str | int]) -> None:
        delivered.append(event)
        received.set()

    server = ActivationServer(state_dir, on_activation)
    server.start()
    try:
        event = {"type": "marketplace_pair", "intent": _token(), "version": 1}
        assert forward_activation(state_dir, event)
        assert received.wait(2)
        assert delivered == [event]
        assert not forward_activation(
            state_dir,
            {"type": "marketplace_pair", "intent": _token(), "extra": "no"},
        )
        assert os.stat(state_dir / "desktop_activation.json").st_mode & 0o077 == 0
    finally:
        server.close()
    assert not (state_dir / "desktop_activation.json").exists()


def test_second_server_cannot_replace_owner_state() -> None:
    state_dir = Path(tempfile.mkdtemp(prefix="ba-activation-owner-"))
    first = ActivationServer(state_dir, lambda _event: None)
    second = ActivationServer(state_dir, lambda _event: None)
    first.start()
    original_state = (state_dir / "desktop_activation.json").read_text(encoding="utf-8")
    try:
        try:
            second.start()
        except RuntimeError:
            pass
        else:
            raise AssertionError("second server acquired active ownership")
        assert (state_dir / "desktop_activation.json").read_text(
            encoding="utf-8"
        ) == original_state
    finally:
        second.close()
        first.close()


def test_malformed_state_fails_closed() -> None:
    state_dir = Path(tempfile.mkdtemp(prefix="ba-activation-state-"))
    (state_dir / "desktop_activation.json").write_text("[]", encoding="utf-8")
    assert not forward_activation(state_dir, {"type": "activate"})


def test_windows_liveness_probe_never_signals_the_owner() -> None:
    class Kernel32:
        def OpenProcess(self, _access, _inherit, _pid):
            return 123

        def GetExitCodeProcess(self, _process, output):
            output._obj.value = 259
            return 1

        def CloseHandle(self, _process):
            return 1

    original_name = activation_server.os.name
    original_windll = getattr(ctypes, "windll", None)
    try:
        activation_server.os.name = "nt"
        ctypes.windll = type("Windll", (), {"kernel32": Kernel32()})()
        assert activation_server._process_alive(42)
    finally:
        activation_server.os.name = original_name
        if original_windll is None:
            del ctypes.windll
        else:
            ctypes.windll = original_windll


if __name__ == "__main__":
    test_pair_link_is_strict_and_redacted()
    test_activation_forwarding_authenticates_and_delivers()
    test_second_server_cannot_replace_owner_state()
    test_malformed_state_fails_closed()
    test_windows_liveness_probe_never_signals_the_owner()

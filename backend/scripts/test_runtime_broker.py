#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import tempfile

from better_agent_sdk.runtime_transport import RuntimeTransport
from runtime_broker import BrokerRequest, RuntimeBroker


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
    import runtime_broker

    source = Path(runtime_broker.__file__).read_text(encoding="utf-8")
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


def main() -> None:
    test_every_win32_call_has_a_declared_prototype()
    received: list[BrokerRequest] = []

    def handle(request: BrokerRequest):
        received.append(request)
        return {"success": True, "operation": request.operation, "payload": request.payload}

    with tempfile.TemporaryDirectory() as raw:
        broker = RuntimeBroker(Path(raw), handle)
        address = broker.start()
        try:
            transport = RuntimeTransport(address)
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
            assert received[0].operation == "example_read"
            try:
                transport.request({"version": 1, "kind": "invoke", "unexpected": True})
            except RuntimeError as exc:
                assert "extra" in str(exc).lower()
            else:
                raise AssertionError("unknown broker request field was accepted")
        finally:
            broker.stop()
        assert not list(Path(raw).glob("*.sock"))
    print("runtime broker tests passed")


if __name__ == "__main__":
    main()

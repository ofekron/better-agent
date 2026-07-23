#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import tempfile

from better_agent_sdk.runtime_transport import RuntimeTransport
from runtime_broker import BrokerRequest, RuntimeBroker


def main() -> None:
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

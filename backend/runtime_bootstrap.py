from __future__ import annotations

import threading

from paths import bc_home
from runtime_broker import BrokerRequest, RuntimeBroker

_LEASE_SECONDS = 30.0
_LOCK = threading.Lock()
_LEASES: dict[str, RuntimeBroker] = {}


def issue(secret: str) -> str:
    value = str(secret or "")
    if not value:
        raise ValueError("runtime bootstrap secret is required")
    consumed = threading.Event()

    def handle(request: BrokerRequest) -> dict[str, object]:
        if request.kind != "catalog" or consumed.is_set():
            raise PermissionError("runtime bootstrap handle is invalid")
        consumed.set()
        return {"success": True, "secret": value}

    broker = RuntimeBroker(bc_home() / "runtime" / "bootstrap", handle)
    address = broker.start()
    with _LOCK:
        _LEASES[address] = broker
    threading.Thread(
        target=_retire,
        args=(address, consumed),
        name="runtime-bootstrap-retire",
        daemon=True,
    ).start()
    return address


def _retire(address: str, consumed: threading.Event) -> None:
    consumed.wait(_LEASE_SECONDS)
    with _LOCK:
        broker = _LEASES.pop(address, None)
    if broker is not None:
        broker.stop()


def active_count() -> int:
    with _LOCK:
        return len(_LEASES)

from __future__ import annotations

import json
import threading

from paths import bc_home
from runtime_broker import BrokerRequest, RuntimeBroker

_LEASE_SECONDS = 30.0
_LOCK = threading.Lock()
_LEASES: dict[str, RuntimeBroker] = {}


def issue(
    secret: str,
    *,
    runtime_hydration: dict[str, object] | None = None,
) -> str:
    value = str(secret or "")
    if not value:
        raise ValueError("runtime bootstrap secret is required")
    if runtime_hydration is not None:
        try:
            hydration = json.loads(json.dumps(
                runtime_hydration,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "runtime bootstrap hydration must be JSON-compatible",
            ) from exc
        if type(hydration) is not dict:
            raise ValueError("runtime bootstrap hydration must be an object")
    else:
        hydration = None
    consumed = threading.Event()

    def handle(request: BrokerRequest) -> dict[str, object]:
        if request.kind != "catalog" or consumed.is_set():
            raise PermissionError("runtime bootstrap handle is invalid")
        consumed.set()
        return {
            "success": True,
            "secret": value,
            "runtime_hydration": hydration,
        }

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

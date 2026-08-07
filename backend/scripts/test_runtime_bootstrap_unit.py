"""Unit tests for ``runtime_bootstrap``.

Covers ``issue()`` input validation plus the full broker roundtrip: the
one-shot catalog handle serves the secret/hydration over the runtime
socket, a second request is rejected once consumed, ``active_count``
tracks live leases, and ``_retire`` stops the broker (or no-ops when the
lease was already reaped) when its lease elapses. The standalone
``test_runtime_bootstrap.py`` script remains as an end-to-end demo that
also proves the secret never reaches disk.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home  # noqa: E402

_test_home.isolate("ba-runtime-bootstrap-unit-")

import pytest  # noqa: E402
import runtime_bootstrap  # noqa: E402
from better_agent_sdk.runtime_transport import RuntimeTransport  # noqa: E402

_CATALOG = {"version": 1, "kind": "catalog"}


@pytest.fixture(autouse=True)
def _stop_leaked_brokers():
    """Stop any broker these tests leased so the global pool stays empty.

    ``issue()`` schedules a daemon ``_retire`` thread that stops the broker
    once consumed (or after the 30s unconsumed-timeout). Tests that hold off
    retirement (or never serve a request) must not leave a broker serving on
    a detached thread, so stop any still-leased broker directly here.
    """
    yield
    with runtime_bootstrap._LOCK:
        brokers = list(runtime_bootstrap._LEASES.values())
        runtime_bootstrap._LEASES.clear()
    for broker in brokers:
        broker.stop()


def _drain_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_issue_requires_a_non_empty_secret():
    with pytest.raises(ValueError, match="secret is required"):
        runtime_bootstrap.issue("")
    # None coerces to an empty string via `str(secret or "")`.
    with pytest.raises(ValueError, match="secret is required"):
        runtime_bootstrap.issue(None)  # type: ignore[arg-type]


def test_issue_rejects_hydration_that_is_not_an_object():
    # A JSON-valid but non-object hydration serializes fine, then fails the
    # `type(hydration) is not dict` shape guard.
    with pytest.raises(ValueError, match="hydration must be an object"):
        runtime_bootstrap.issue("secret", runtime_hydration=["not", "a", "dict"])


def test_issue_rejects_hydration_that_is_not_json_compatible():
    # allow_nan=False makes NaN fail JSON serialization, so the hydration is
    # rejected as not JSON-compatible before any broker is started.
    with pytest.raises(ValueError, match="hydration must be JSON-compatible"):
        runtime_bootstrap.issue("secret", runtime_hydration={"value": float("nan")})


def test_issue_leases_broker_and_serves_secret_with_hydration():
    before = runtime_bootstrap.active_count()
    address = runtime_bootstrap.issue(
        "top-secret", runtime_hydration={"provider_identity": "hydration-secret"}
    )
    # The broker is leased and alive before any request consumes the one-shot
    # handle (the retire thread is parked on `consumed`, not the 30s lease).
    assert runtime_bootstrap.active_count() == before + 1
    response = RuntimeTransport(address).request(_CATALOG)
    assert response["success"] is True
    assert response["secret"] == "top-secret"
    assert response["runtime_hydration"] == {"provider_identity": "hydration-secret"}


def test_issue_without_hydration_serves_null_hydration():
    # No hydration exercises the `else: hydration = None` path; the served
    # response carries a null hydration.
    address = runtime_bootstrap.issue("once")
    response = RuntimeTransport(address).request(_CATALOG)
    assert response["secret"] == "once"
    assert response["runtime_hydration"] is None


def test_consumed_handle_rejects_further_requests(monkeypatch):
    # The retire thread stops the broker the instant the first request sets
    # `consumed`. Neutralize retirement so the one-shot *handle* guard
    # (PermissionError -> success=False -> RuntimeError) is exercised in
    # isolation from lease eviction.
    monkeypatch.setattr(runtime_bootstrap, "_retire", lambda *args: None)
    address = runtime_bootstrap.issue("once")
    first = RuntimeTransport(address).request(_CATALOG)
    assert first["secret"] == "once"
    with pytest.raises(RuntimeError, match="handle is invalid"):
        RuntimeTransport(address).request(_CATALOG)


def test_retire_stops_broker_when_lease_elapses(monkeypatch):
    # Shrink the lease so retirement happens within the test, not 30s. No
    # request is made, so the retire thread reaches the lease-timeout path.
    monkeypatch.setattr(runtime_bootstrap, "_LEASE_SECONDS", 0.05)
    address = runtime_bootstrap.issue("ephemeral")
    socket_path = Path(address.removeprefix("unix:"))
    # The retire thread pops the lease from the pool ...
    assert _drain_until(lambda: runtime_bootstrap.active_count() == 0)
    # ... and then stops the broker, unlinking its socket.
    assert _drain_until(lambda: not socket_path.exists())


def test_retire_is_idempotent_when_lease_already_reaped():
    # _retire must not assume its address is still leased: a second reap (or
    # a retire for an address removed by another path) pops None and skips
    # stop(). A pre-set event makes the wait return immediately.
    already_done = threading.Event()
    already_done.set()
    runtime_bootstrap._retire("unix:/absent/broker.sock", already_done)
    assert runtime_bootstrap.active_count() == 0

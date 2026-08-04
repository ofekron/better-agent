from __future__ import annotations

import asyncio
import sys
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import _test_home
import pytest

_TEST_HOME = Path(_test_home.isolate("ba-test-node-provider-credential-lifecycle-"))
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import node_config_sync  # noqa: E402
import node_link  # noqa: E402
import node_provider_credential_sync as credential_sync  # noqa: E402
import node_store  # noqa: E402
from provider_remote import RemoteProviderProxy  # noqa: E402


def test_connect_replays_credentials_before_runtime_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    connection = object()
    monkeypatch.setattr(node_store, "get_connection", lambda _node_id: connection)
    monkeypatch.setattr(
        node_config_sync,
        "_snapshot_nodes",
        lambda: [{"id": "lenovo", "role": "worker_node", "state": "connected"}],
    )

    async def push(surface, _node_ids):
        order.append(surface.name)
        return {"lenovo"}

    async def replay(node_id, *, version_ready_required):
        assert node_id == "lenovo"
        assert version_ready_required is False
        order.append("credentials")
        return {"node_id": node_id, "provider_credentials": []}

    monkeypatch.setattr(node_config_sync, "_push_surface", push)
    monkeypatch.setattr(credential_sync, "replay_grants", replay)
    monkeypatch.setattr(
        node_config_sync,
        "_set_runtime_ready",
        lambda _node_id, expected: order.append(
            "ready" if expected is connection else "wrong-connection"
        ),
    )

    asyncio.run(node_config_sync._project_connected_node("lenovo"))
    assert order == [
        *[surface.name for surface in node_config_sync.SURFACES],
        "credentials",
        "ready",
    ]


def test_credential_replay_failure_does_not_block_metadata_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready: list[str] = []
    monkeypatch.setattr(node_store, "get_connection", lambda _node_id: object())
    monkeypatch.setattr(
        node_config_sync,
        "_snapshot_nodes",
        lambda: [{"id": "lenovo", "role": "worker_node", "state": "connected"}],
    )

    async def push(_surface, _node_ids):
        return {"lenovo"}

    async def replay(*_args, **_kwargs):
        raise RuntimeError("credential unavailable")

    monkeypatch.setattr(node_config_sync, "_push_surface", push)
    monkeypatch.setattr(credential_sync, "replay_grants", replay)
    monkeypatch.setattr(
        node_config_sync,
        "_set_runtime_ready",
        lambda node_id, _expected: ready.append(node_id),
    )
    asyncio.run(node_config_sync._project_connected_node("lenovo"))
    assert ready == ["lenovo"]


def test_remote_admission_checks_exact_provider_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    ready_connection = SimpleNamespace(runs={})

    async def wait_ready(node_id):
        assert node_id == "lenovo"
        order.append("runtime-ready")
        return ready_connection

    async def ensure(node_id, provider_id, *, expected_connection):
        assert (node_id, provider_id) == ("lenovo", "zai-claude")
        assert expected_connection is ready_connection
        order.append("credential-ready")

    async def send(node_id, payload, *, expected_connection):
        assert node_id == "lenovo"
        assert expected_connection is ready_connection
        assert payload["execution_artifact"]["provider_id"] == "zai-claude"
        order.append("spawn")

    monkeypatch.setattr(node_store, "wait_for_runtime_ready", wait_ready)
    monkeypatch.setattr(node_store, "get_connection", lambda _node_id: ready_connection)
    monkeypatch.setattr(credential_sync, "ensure_for_admission", ensure)
    monkeypatch.setattr(node_link, "send_spawn_run", send)

    proxy = object.__new__(RemoteProviderProxy)
    proxy.node_id = "lenovo"
    proxy._arm_admission_lease = lambda _state, _conn: order.append("lease")
    state = SimpleNamespace(run_id="run-a", admission=Future())
    payload = {"execution_artifact": {"provider_id": "zai-claude"}}

    result = asyncio.run(
        RemoteProviderProxy._dispatch_admission_when_ready(proxy, state, payload)
    )
    assert result is ready_connection
    assert order == ["runtime-ready", "credential-ready", "lease", "spawn"]


def test_provider_change_replays_only_nodes_with_successful_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replayed: list[str] = []
    monkeypatch.setattr(
        node_config_sync,
        "_snapshot_nodes",
        lambda: [
            {"id": "node-a", "role": "worker_node", "state": "connected"},
            {"id": "node-b", "role": "worker_node", "state": "connected"},
        ],
    )

    async def push(surface, node_ids):
        assert surface.name == "providers"
        assert node_ids == ["node-a", "node-b"]
        return {"node-a"}

    async def replay(node_id, *, version_ready_required):
        assert version_ready_required is True
        replayed.append(node_id)

    monkeypatch.setattr(node_config_sync, "_push_surface", push)
    monkeypatch.setattr(credential_sync, "replay_grants", replay)
    node_config_sync._dirty.clear()
    node_config_sync._dirty.add("providers")
    asyncio.run(node_config_sync._push_until_clean())
    assert replayed == ["node-a"]


def test_remote_admission_retries_after_connection_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = SimpleNamespace(runs={})
    second = SimpleNamespace(runs={})
    current = {"connection": first}
    ensured: list[object] = []
    sent: list[object] = []

    async def wait_ready(_node_id):
        return current["connection"]

    async def ensure(_node_id, _provider_id, *, expected_connection):
        ensured.append(expected_connection)
        if expected_connection is first:
            current["connection"] = second
            raise credential_sync.CredentialSyncConnectionChanged(
                "node connection changed during credential sync"
            )

    async def send(_node_id, _payload, *, expected_connection):
        sent.append(expected_connection)

    monkeypatch.setattr(node_store, "wait_for_runtime_ready", wait_ready)
    monkeypatch.setattr(node_store, "get_connection", lambda _node_id: current["connection"])
    monkeypatch.setattr(credential_sync, "ensure_for_admission", ensure)
    monkeypatch.setattr(node_link, "send_spawn_run", send)

    proxy = object.__new__(RemoteProviderProxy)
    proxy.node_id = "lenovo"
    proxy._arm_admission_lease = lambda state, _conn: setattr(
        state, "admission_lease_handle", None
    )
    state = SimpleNamespace(run_id="run-a", admission=Future())
    payload = {"execution_artifact": {"provider_id": "zai-claude"}}

    result = asyncio.run(
        RemoteProviderProxy._dispatch_admission_when_ready(proxy, state, payload)
    )

    assert result is second
    assert ensured == [first, second]
    assert sent == [second]
    assert first.runs == {}


def test_remote_admission_stops_when_cancelled_during_credential_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = SimpleNamespace(runs={})

    async def ensure(_node_id, _provider_id, *, expected_connection):
        assert expected_connection is ready
        state.admission.set_result(None)

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("cancelled admission must not spawn")

    monkeypatch.setattr(node_store, "wait_for_runtime_ready", lambda _node_id: _ready(ready))
    monkeypatch.setattr(node_store, "get_connection", lambda _node_id: ready)
    monkeypatch.setattr(credential_sync, "ensure_for_admission", ensure)
    monkeypatch.setattr(node_link, "send_spawn_run", unexpected)

    proxy = object.__new__(RemoteProviderProxy)
    proxy.node_id = "lenovo"
    state = SimpleNamespace(run_id="run-a", admission=Future())
    payload = {"execution_artifact": {"provider_id": "zai-claude"}}

    result = asyncio.run(
        RemoteProviderProxy._dispatch_admission_when_ready(proxy, state, payload)
    )
    assert result is None
    assert ready.runs == {}


async def _ready(value):
    return value

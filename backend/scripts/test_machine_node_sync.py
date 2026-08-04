from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.anyio

_BACKEND = Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402

_TMP_HOME = _test_home.isolate("bc-test-machine-node-sync-")

import config_store  # noqa: E402
import extension_api  # noqa: E402
import extension_store  # noqa: E402
from headless_request_contract import (  # noqa: E402
    HeadlessAuthority,
    HeadlessRequest,
    admit_headless_request,
)
import node_client  # noqa: E402
import node_link  # noqa: E402
import node_provider_credential_sync  # noqa: E402
import node_registry_store  # noqa: E402
import node_rpc_handlers  # noqa: E402
import node_store  # noqa: E402
import provider_sync_authority  # noqa: E402
import provider_remote  # noqa: E402

logging.disable(logging.CRITICAL)


def _provider_record(
    provider_id: str,
    name: str,
    *,
    mode: str = "subscription",
) -> dict:
    provider = config_store._new_provider_record("claude")
    provider.update({
        "id": provider_id,
        "name": name,
        "mode": mode,
        "default_model": "opus",
    })
    authority = {
        "generation": provider["generation"],
        "revision": provider["revision"],
        "execution_revision": provider["execution_revision"],
    }
    return {
        **config_store._clean_provider_record(provider),
        **authority,
    }


def _provider_sync_payload(
    default_provider_id: str,
    providers: list[dict],
    *,
    provider_api_keys: list[dict] | None = None,
) -> dict:
    payload = {
        "provider_state_authority": provider_sync_authority.new_authority(
            default_provider_id,
            providers,
        ),
        "default_provider_id": default_provider_id,
        "providers": providers,
    }
    if provider_api_keys is not None:
        payload["provider_api_keys"] = provider_api_keys
    return payload


def _fresh_provider_sync(payload: dict) -> dict:
    config_store._config_path().unlink(missing_ok=True)
    with config_store._state_cache_lock:
        config_store._state_cache = None
    return config_store.import_provider_sync_state(payload)


def test_node_projection_replaces_foreign_local_authority() -> None:
    local = _provider_record("local-provider", "Local Provider")
    incoming = _provider_record("primary-provider", "Primary Provider")
    _fresh_provider_sync(_provider_sync_payload(local["id"], [local]))
    result = node_rpc_handlers._rpc_sync_provider_config({
        "provider_state": _provider_sync_payload(incoming["id"], [incoming]),
    })

    assert result["sync_status"] == "applied"
    state = config_store.list_providers()
    assert config_store._load_state()["provider_state_projected"] is True
    assert state["default_provider_id"] == incoming["id"]
    assert [provider["id"] for provider in state["providers"]] == [incoming["id"]]


class _Request:
    def __init__(self, method: str, body: dict | None = None) -> None:
        self.method = method
        self.query_params = {}
        self._body = body

    async def body(self) -> bytes:
        if self._body is None:
            return b""
        return json.dumps(self._body).encode("utf-8")


async def test_sync_providers_all_nodes_reports_node_failures() -> None:
    original_export = config_store.export_provider_sync_state
    original_snapshot = node_store.snapshot
    original_call = node_rpc_handlers.call_local_or_remote

    config_store.export_provider_sync_state = lambda: {"providers": []}  # type: ignore[assignment]
    node_store.snapshot = lambda: [{  # type: ignore[assignment]
        "id": "node-a",
        "role": "worker_node",
        "state": "connected",
    }]

    async def fail_call(*_args, **_kwargs):
        raise RuntimeError("node sync failed")

    node_rpc_handlers.call_local_or_remote = fail_call  # type: ignore[assignment]
    try:
        response = await extension_api._dispatch_machine_nodes_core_backend(
            "nodes/sync-providers",
            _Request("POST"),
        )
    finally:
        config_store.export_provider_sync_state = original_export  # type: ignore[assignment]
        node_store.snapshot = original_snapshot  # type: ignore[assignment]
        node_rpc_handlers.call_local_or_remote = original_call  # type: ignore[assignment]

    assert response is not None
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["results"] == [
        {"node_id": "node-a", "ok": False, "error": "node sync failed"}
    ]


async def test_first_approval_uses_topology_cwd_roots() -> None:
    import node_registry_store

    topology_spec = node_link.NodeSpec(
        id="node-a",
        role="worker_node",
        address="ws://node-a",
        cwd_roots=("/operator-root",),
    )
    self_reported_spec = node_link.NodeSpec(
        id="node-a",
        role="worker_node",
        address="ws://node-a",
        cwd_roots=("/self-reported",),
    )
    topology = SimpleNamespace(all_nodes=lambda: {"node-a": topology_spec})
    original_approve = node_link.pending_node_registrations.approve
    original_add = node_registry_store.add
    original_to_spec = node_registry_store.to_spec
    original_load_topology = node_link.load_topology
    original_resolve_waiter = node_link._resolve_waiter
    resolved: list[node_link.NodeSpec | None] = []

    node_link.pending_node_registrations.approve = lambda _node_id: ({
        "node_id": "node-a",
        "address": "ws://node-a",
        "cwd_roots": ["/self-reported"],
        "secret_hash": "hash",
    }, "ok")  # type: ignore[assignment]
    node_registry_store.add = lambda **kwargs: {  # type: ignore[assignment]
        "node_id": kwargs["node_id"],
        "address": kwargs["address"],
        "cwd_roots": kwargs["cwd_roots"],
    }
    node_registry_store.to_spec = lambda _node_id: self_reported_spec  # type: ignore[assignment]
    node_link.load_topology = lambda: topology  # type: ignore[assignment]
    node_link._resolve_waiter = lambda _node_id, spec: resolved.append(spec) or True  # type: ignore[assignment]
    try:
        _rec, reason = await node_link.approve_registration("node-a")
    finally:
        node_link.pending_node_registrations.approve = original_approve  # type: ignore[assignment]
        node_registry_store.add = original_add  # type: ignore[assignment]
        node_registry_store.to_spec = original_to_spec  # type: ignore[assignment]
        node_link.load_topology = original_load_topology  # type: ignore[assignment]
        node_link._resolve_waiter = original_resolve_waiter  # type: ignore[assignment]

    assert reason == "ok"
    assert resolved == [topology_spec]


async def test_dynamic_first_approval_uses_registry_record() -> None:
    import node_registry_store

    registry_rec = {
        "node_id": "dynamic",
        "address": "ws://dynamic",
        "cwd_roots": ["/dynamic-root"],
        "secret_hash": "hash",
    }
    original_approve = node_link.pending_node_registrations.approve
    original_add = node_registry_store.add
    original_load_topology = node_link.load_topology
    original_resolve_waiter = node_link._resolve_waiter
    resolved: list[node_link.NodeSpec | None] = []

    node_link.pending_node_registrations.approve = lambda _node_id: (registry_rec, "ok")  # type: ignore[assignment]
    node_registry_store.add = lambda **_kwargs: registry_rec  # type: ignore[assignment]
    node_link.load_topology = lambda: (_ for _ in ()).throw(RuntimeError("no topology"))  # type: ignore[assignment]
    node_link._resolve_waiter = lambda _node_id, spec: resolved.append(spec) or True  # type: ignore[assignment]
    try:
        _rec, reason = await node_link.approve_registration("dynamic")
    finally:
        node_link.pending_node_registrations.approve = original_approve  # type: ignore[assignment]
        node_registry_store.add = original_add  # type: ignore[assignment]
        node_link.load_topology = original_load_topology  # type: ignore[assignment]
        node_link._resolve_waiter = original_resolve_waiter  # type: ignore[assignment]

    assert reason == "ok"
    assert resolved[0].cwd_roots == ("/dynamic-root",)


async def test_bulk_provider_sync_rejects_credentials() -> None:
    try:
        await extension_api._dispatch_machine_nodes_core_backend(
            "nodes/sync-providers",
            _Request("POST", {
                "include_secrets": True,
                "provider_ids": ["api-provider"],
            }),
        )
    except extension_api.HTTPException as exc:
        assert exc.status_code == 400
        assert "only be synced to one selected node" in str(exc.detail)
        return
    raise AssertionError("bulk provider sync accepted credentials")


async def test_provider_secret_sync_requires_secure_node_transport() -> None:
    import node_store

    original_get_connection = node_store.get_connection
    node_store.get_connection = lambda _node_id: SimpleNamespace(  # type: ignore[assignment]
        ws=SimpleNamespace(
            url=SimpleNamespace(scheme="ws"),
            client=SimpleNamespace(host="10.0.0.9"),
        )
    )
    try:
        await extension_api._dispatch_machine_nodes_core_backend(
            "nodes/node-a/sync-providers",
            _Request("POST", {
                "include_secrets": True,
                "provider_ids": ["api-provider"],
            }),
        )
    except extension_api.HTTPException as exc:
        assert exc.status_code == 409
        assert "requires a WSS or loopback" in str(exc.detail)
        return
    finally:
        node_store.get_connection = original_get_connection  # type: ignore[assignment]
    raise AssertionError("provider secret sync accepted an insecure node connection")


async def test_provider_secret_sync_passes_explicit_ids_on_loopback() -> None:
    import node_store

    original_export = config_store.export_provider_sync_state
    original_get_connection = node_store.get_connection
    original_call = node_rpc_handlers.call_local_or_remote
    original_get_provider = config_store.get_provider
    original_registry_get = node_registry_store.get
    seen: dict[str, object] = {}
    provider = {
        "id": "api-provider",
        "name": "API Provider",
        "mode": "api_key",
        "generation": "provider-generation",
        "execution_revision": 1,
    }
    authority = {"generation": "state-generation", "revision": 1, "digest": "digest"}

    def export(provider_api_key_ids=None):
        seen.setdefault("provider_api_key_calls", []).append(provider_api_key_ids)
        return {
            "provider_state_authority": authority,
            "default_provider_id": "api-provider",
            "providers": [provider],
            "provider_api_keys": [
                {"provider_id": "api-provider", "api_key": "selected-secret"},
            ],
        }

    async def call(node_id, method, params, **kwargs):
        seen["node_id"] = node_id
        seen["method"] = method
        seen["params"] = params
        seen["secure_transport_required"] = kwargs.get("secure_transport_required")
        seen["version_ready_required"] = kwargs.get("version_ready_required")
        return {
            "provider_count": 1,
            "provider_api_key_count": 1,
            "provider_api_key_ids": ["api-provider"],
            "provider_state_authority": authority,
            "sync_status": "credentials_applied",
        }

    config_store.export_provider_sync_state = export  # type: ignore[assignment]
    config_store.get_provider = lambda _provider_id: provider  # type: ignore[assignment]
    node_registry_store.get = lambda node_id: {  # type: ignore[assignment]
        "node_id": node_id,
        "secret_hash": "$argon2id$node-authority",
    }
    connection = SimpleNamespace(
        ws=SimpleNamespace(
            url=SimpleNamespace(scheme="ws"),
            client=SimpleNamespace(host="127.0.0.1"),
        )
    )
    node_store.get_connection = lambda _node_id: connection  # type: ignore[assignment]
    node_rpc_handlers.call_local_or_remote = call  # type: ignore[assignment]
    try:
        response = await extension_api._dispatch_machine_nodes_core_backend(
            "nodes/node-a/sync-providers",
            _Request("POST", {
                "include_secrets": True,
                "provider_ids": ["api-provider"],
            }),
        )
    finally:
        config_store.export_provider_sync_state = original_export  # type: ignore[assignment]
        config_store.get_provider = original_get_provider  # type: ignore[assignment]
        node_registry_store.get = original_registry_get  # type: ignore[assignment]
        node_store.get_connection = original_get_connection  # type: ignore[assignment]
        node_rpc_handlers.call_local_or_remote = original_call  # type: ignore[assignment]

    assert response is not None
    assert response.status_code == 200
    assert ["api-provider"] in seen["provider_api_key_calls"]
    assert seen["node_id"] == "node-a"
    assert seen["method"] == "sync_provider_config"
    assert seen["secure_transport_required"] is True
    assert seen["version_ready_required"] is True
    assert seen["params"] == {
        "provider_state": {
            "provider_state_authority": authority,
            "default_provider_id": "api-provider",
            "providers": [provider],
            "provider_api_keys": [
                {"provider_id": "api-provider", "api_key": "selected-secret"},
            ],
        },
    }


async def test_provider_sync_requires_version_ready() -> None:
    original_export = config_store.export_provider_sync_state
    original_get_connection = node_store.get_connection
    original_call = node_rpc_handlers.call_local_or_remote
    seen: dict[str, object] = {}

    config_store.export_provider_sync_state = lambda _ids=None: {"providers": []}  # type: ignore[assignment]
    node_store.get_connection = lambda _node_id: SimpleNamespace(  # type: ignore[assignment]
        ws=SimpleNamespace(
            url=SimpleNamespace(scheme="ws"),
            client=SimpleNamespace(host="127.0.0.1"),
        )
    )

    async def call(_node_id, _method, _params, **kwargs):
        seen.update(kwargs)
        return {"provider_count": 0}

    node_rpc_handlers.call_local_or_remote = call  # type: ignore[assignment]
    try:
        response = await extension_api._dispatch_machine_nodes_core_backend(
            "nodes/node-a/sync-providers",
            _Request("POST"),
        )
    finally:
        config_store.export_provider_sync_state = original_export  # type: ignore[assignment]
        node_store.get_connection = original_get_connection  # type: ignore[assignment]
        node_rpc_handlers.call_local_or_remote = original_call  # type: ignore[assignment]

    assert response is not None
    assert response.status_code == 200
    assert seen["version_ready_required"] is True


async def test_provider_secret_sync_rechecks_transport_on_rpc_send() -> None:
    import node_store

    original_export = config_store.export_provider_sync_state
    original_get_connection = node_store.get_connection
    original_get_provider = config_store.get_provider
    original_registry_get = node_registry_store.get
    provider = {
        "id": "api-provider",
        "name": "API Provider",
        "mode": "api_key",
        "generation": "provider-generation",
        "execution_revision": 1,
    }
    connections = iter([
        SimpleNamespace(
            ws=SimpleNamespace(
                url=SimpleNamespace(scheme="ws"),
                client=SimpleNamespace(host="127.0.0.1"),
            )
        ),
        SimpleNamespace(
            ws=SimpleNamespace(
                url=SimpleNamespace(scheme="ws"),
                client=SimpleNamespace(host="10.0.0.9"),
            ),
            pending_rpcs={},
        ),
    ])

    config_store.export_provider_sync_state = lambda _ids=None: {  # type: ignore[assignment]
        "provider_state_authority": {},
        "providers": [provider],
        "provider_api_keys": [
            {"provider_id": "api-provider", "api_key": "selected-secret"},
        ],
    }
    config_store.get_provider = lambda _provider_id: provider  # type: ignore[assignment]
    node_registry_store.get = lambda node_id: {  # type: ignore[assignment]
        "node_id": node_id,
        "secret_hash": "$argon2id$node-authority",
    }
    node_store.get_connection = lambda _node_id: next(connections)  # type: ignore[assignment]
    try:
        await extension_api._dispatch_machine_nodes_core_backend(
            "nodes/node-a/sync-providers",
            _Request("POST", {
                "include_secrets": True,
                "provider_ids": ["api-provider"],
            }),
        )
    except extension_api.HTTPException as exc:
        assert exc.status_code == 409
        assert "connection changed" in str(exc.detail)
        return
    finally:
        config_store.export_provider_sync_state = original_export  # type: ignore[assignment]
        config_store.get_provider = original_get_provider  # type: ignore[assignment]
        node_registry_store.get = original_registry_get  # type: ignore[assignment]
        node_store.get_connection = original_get_connection  # type: ignore[assignment]
    raise AssertionError("provider secret sync did not recheck transport at RPC send")


async def test_version_ready_rpc_rejects_mismatched_node_before_send() -> None:
    original_get_connection = node_store.get_connection
    original_commit = node_store.app_version.current_commit_sha
    sent: list[dict] = []

    class Ws:
        async def send_json(self, payload):
            sent.append(payload)

    conn = SimpleNamespace(
        ws=Ws(),
        pending_rpcs={},
        app_commit_sha="b" * 40,
        app_dirty=False,
    )
    node_store.get_connection = lambda _node_id: conn  # type: ignore[assignment]
    node_store.app_version.current_commit_sha = lambda: "a" * 40
    try:
        try:
            await node_link.rpc_call(
                "node-a",
                "sync_provider_config",
                {},
                version_ready_required=True,
            )
        except RuntimeError as exc:
            assert "primary is running" in str(exc)
            assert sent == []
            return
    finally:
        node_store.get_connection = original_get_connection  # type: ignore[assignment]
        node_store.app_version.current_commit_sha = original_commit
    raise AssertionError("version mismatch did not reject RPC before send")


async def test_spawn_run_rejects_mismatched_node_before_send() -> None:
    original_get_connection = node_store.get_connection
    original_commit = node_store.app_version.current_commit_sha
    sent: list[dict] = []

    class Ws:
        async def send_json(self, payload):
            sent.append(payload)

    conn = SimpleNamespace(
        ws=Ws(),
        pending_rpcs={},
        app_commit_sha="b" * 40,
        app_dirty=False,
    )
    node_store.get_connection = lambda _node_id: conn  # type: ignore[assignment]
    node_store.app_version.current_commit_sha = lambda: "a" * 40
    try:
        try:
            await node_link.send_spawn_run("node-a", {"run_id": "run-a"})
        except RuntimeError as exc:
            assert "primary is running" in str(exc)
            assert sent == []
            return
    finally:
        node_store.get_connection = original_get_connection  # type: ignore[assignment]
        node_store.app_version.current_commit_sha = original_commit
    raise AssertionError("version mismatch did not reject spawn_run before send")


async def test_run_headless_requires_version_ready() -> None:
    original_rpc_call = node_link.rpc_call
    seen: dict[str, object] = {}

    async def rpc_call(_node_id, _method, _params, **kwargs):
        seen.update(kwargs)
        seen["params"] = _params
        return {"result": {"result": "ok"}}

    node_link.rpc_call = rpc_call  # type: ignore[assignment]
    try:
        provider = _provider_record("provider-a", "Provider A")
        request = HeadlessRequest.from_dict({
            "schema": 1,
            "prompt": "test remote boundary",
            "owner": {"kind": "session", "id": "session-a"},
            "fork": False,
            "no_tools": False,
            "timeout": 10,
        })
        admitted = admit_headless_request(
            request,
            HeadlessAuthority.create(
                owner_kind="session",
                owner_id="session-a",
                provider={
                    key: provider[key]
                    for key in (
                        "id",
                        "kind",
                        "generation",
                        "execution_revision",
                    )
                },
                model="opus",
                reasoning_effort="",
                runner="native",
                permission_scope="spawn_runs",
                routing={"node_id": "node-a"},
                cwd="/tmp",
                resume_sid=None,
                supports_fork=True,
                supports_no_tools=True,
            ),
        )
        await provider_remote.RemoteProviderProxy(
            "node-a",
        ).run_admitted_headless(admitted)
    finally:
        node_link.rpc_call = original_rpc_call  # type: ignore[assignment]

    assert seen["version_ready_required"] is True
    wire = seen["params"]["admitted"]
    assert wire["schema"] == 3
    assert "fingerprint" not in wire


async def test_node_runtime_readiness_waits_across_reconnect() -> None:
    spec = node_store.NodeSpec(
        id="runtime-ready-node",
        role="worker_node",
        address="ws://node",
        cwd_roots=("/tmp",),
    )
    first = await node_store.register(spec, SimpleNamespace())
    waiter = asyncio.create_task(
        node_store.wait_for_runtime_ready("runtime-ready-node"),
    )
    await asyncio.sleep(0)
    assert not waiter.done()

    await node_store.unregister("runtime-ready-node")
    second = await node_store.register(spec, SimpleNamespace())
    assert node_store.mark_runtime_ready("runtime-ready-node", first) is False
    assert not waiter.done()
    assert node_store.mark_runtime_ready("runtime-ready-node", second) is True
    assert await asyncio.wait_for(waiter, timeout=1) is second
    await node_store.unregister("runtime-ready-node")


async def test_remote_admission_starts_only_after_runtime_ready() -> None:
    original_wait = node_store.wait_for_runtime_ready
    original_send = node_link.send_spawn_run
    original_get_connection = node_store.get_connection
    original_ensure = node_provider_credential_sync.ensure_for_admission
    gate = asyncio.Event()
    calls: list[str] = []
    conn = SimpleNamespace(runs={})
    proxy = provider_remote.RemoteProviderProxy("node-a")
    state = SimpleNamespace(
        run_id="run-a",
        admission=provider_remote.Future(),
    )

    async def wait_for_runtime_ready(_node_id):
        await gate.wait()
        calls.append("ready")
        return conn

    async def ensure(_node_id, _provider_id, *, expected_connection):
        assert expected_connection is conn

    async def send_spawn_run(_node_id, _payload, *, expected_connection):
        assert expected_connection is conn
        calls.append("send")

    proxy._arm_admission_lease = lambda _state, _conn: calls.append("lease")  # type: ignore[method-assign]
    node_store.wait_for_runtime_ready = wait_for_runtime_ready  # type: ignore[assignment]
    node_store.get_connection = lambda _node_id: conn  # type: ignore[assignment]
    node_provider_credential_sync.ensure_for_admission = ensure  # type: ignore[assignment]
    node_link.send_spawn_run = send_spawn_run  # type: ignore[assignment]
    try:
        dispatch = asyncio.create_task(
            proxy._dispatch_admission_when_ready(
                state,
                {
                    "run_id": "run-a",
                    "execution_artifact": {"provider_id": "zai-claude"},
                },
            ),
        )
        await asyncio.sleep(0)
        assert calls == []
        assert conn.runs == {}
        gate.set()
        assert await dispatch is conn
    finally:
        node_store.wait_for_runtime_ready = original_wait  # type: ignore[assignment]
        node_store.get_connection = original_get_connection  # type: ignore[assignment]
        node_provider_credential_sync.ensure_for_admission = original_ensure  # type: ignore[assignment]
        node_link.send_spawn_run = original_send  # type: ignore[assignment]

    assert calls == ["ready", "lease", "send"]
    assert conn.runs == {"run-a": state}


async def test_failed_remote_send_cleans_exact_connection_and_lease() -> None:
    original_wait = node_store.wait_for_runtime_ready
    original_send = node_link.send_spawn_run
    original_ensure = node_provider_credential_sync.ensure_for_admission
    conn_a = SimpleNamespace(runs={})
    conn_b = SimpleNamespace(runs={"other-run": object()})
    proxy = provider_remote.RemoteProviderProxy("node-a")

    class Lease:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    lease = Lease()
    state = SimpleNamespace(
        run_id="run-a",
        admission=provider_remote.Future(),
        admission_lock=threading.Lock(),
        admission_lease_handle=None,
        finished=False,
    )
    proxy._runs["run-a"] = state

    async def wait_for_runtime_ready(_node_id):
        return conn_a

    async def ensure(_node_id, _provider_id, *, expected_connection):
        assert expected_connection is conn_a

    async def send_spawn_run(_node_id, _payload, *, expected_connection):
        assert expected_connection is conn_a
        node_store.get_connection = lambda _node_id: conn_b  # type: ignore[assignment]
        raise ConnectionError("socket closed")

    original_get_connection = node_store.get_connection
    proxy._arm_admission_lease = (  # type: ignore[method-assign]
        lambda target, _conn: setattr(target, "admission_lease_handle", lease)
    )
    node_store.wait_for_runtime_ready = wait_for_runtime_ready  # type: ignore[assignment]
    node_store.get_connection = lambda _node_id: conn_a  # type: ignore[assignment]
    node_provider_credential_sync.ensure_for_admission = ensure  # type: ignore[assignment]
    node_link.send_spawn_run = send_spawn_run  # type: ignore[assignment]
    try:
        await proxy._dispatch_admission_or_fail(
            state,
            {
                "run_id": "run-a",
                "execution_artifact": {"provider_id": "zai-claude"},
            },
            asyncio.Queue(),
        )
    finally:
        node_store.wait_for_runtime_ready = original_wait  # type: ignore[assignment]
        node_link.send_spawn_run = original_send  # type: ignore[assignment]
        node_provider_credential_sync.ensure_for_admission = original_ensure  # type: ignore[assignment]
        node_store.get_connection = original_get_connection  # type: ignore[assignment]

    assert state.admission.done()
    assert isinstance(state.admission.exception(), ConnectionError)
    assert state.finished is True
    assert "run-a" not in proxy._runs
    assert "run-a" not in conn_a.runs
    assert set(conn_b.runs) == {"other-run"}
    assert lease.cancelled is True
    assert state.admission_lease_handle is None


async def test_node_json_iterator_preserves_disconnect_evidence() -> None:
    disconnect = node_link.WebSocketDisconnect(code=1006, reason="transport lost")

    class Ws:
        async def receive_json(self):
            raise disconnect

    iterator = node_link._iter_json(Ws())
    try:
        await anext(iterator)
    except node_link.WebSocketDisconnect as exc:
        assert exc is disconnect
        assert exc.code == 1006
        assert exc.reason == "transport lost"
        return
    raise AssertionError("node JSON iterator erased WebSocket close evidence")


def test_export_provider_sync_state_excludes_api_keys_by_default() -> None:
    original_read = config_store._read_api_key
    providers = [
        _provider_record(
            "api-provider",
            "API Provider",
            mode="api_key",
        )
    ]
    config_store._save_state({
        "default_provider_id": "api-provider",
        "providers": providers,
    })
    config_store._read_api_key = lambda _provider_id: "secret-value"  # type: ignore[assignment]
    try:
        payload = config_store.export_provider_sync_state()
    finally:
        config_store._read_api_key = original_read  # type: ignore[assignment]

    assert "provider_api_keys" not in payload
    assert "secret-value" not in json.dumps(payload)


def test_export_provider_sync_state_includes_only_selected_api_keys() -> None:
    original_read = config_store._read_api_key
    original_read_authoritative = config_store._read_api_key_authoritative
    providers = [
        _provider_record(
            "api-provider",
            "API Provider",
            mode="api_key",
        ),
        _provider_record(
            "other-provider",
            "Other Provider",
            mode="api_key",
        ),
    ]
    config_store._save_state({
        "default_provider_id": "api-provider",
        "providers": providers,
    })
    config_store._read_api_key = lambda provider_id: {  # type: ignore[assignment]
        "api-provider": "selected-secret",
        "other-provider": "other-secret",
    }.get(provider_id, "")
    config_store._read_api_key_authoritative = config_store._read_api_key  # type: ignore[assignment]
    try:
        payload = config_store.export_provider_sync_state(["api-provider"])
    finally:
        config_store._read_api_key = original_read  # type: ignore[assignment]
        config_store._read_api_key_authoritative = original_read_authoritative  # type: ignore[assignment]

    assert payload["provider_api_keys"] == [
        {"provider_id": "api-provider", "api_key": "selected-secret"}
    ]
    assert "other-secret" not in json.dumps(payload)


def test_import_provider_sync_writes_api_key_before_default_selection() -> None:
    original_compare_set = config_store._compare_set_api_key
    original_read = config_store._read_api_key
    original_read_authoritative = config_store._read_api_key_authoritative
    keys: dict[str, str] = {}
    config_store._compare_set_api_key = (
        lambda provider_id, _expected, api_key: keys.__setitem__(
            provider_id,
            api_key,
        )
    )
    config_store._read_api_key = lambda provider_id: keys.get(provider_id, "")  # type: ignore[assignment]
    config_store._read_api_key_authoritative = lambda provider_id: keys.get(provider_id, "")  # type: ignore[assignment]
    providers = [
        _provider_record(
            "api-provider",
            "API Provider",
            mode="api_key",
        )
    ]
    try:
        result = _fresh_provider_sync(_provider_sync_payload(
            "api-provider",
            providers,
            provider_api_keys=[
                {"provider_id": "api-provider", "api_key": "selected-secret"},
            ],
        ))
    finally:
        config_store._compare_set_api_key = original_compare_set  # type: ignore[assignment]
        config_store._read_api_key = original_read  # type: ignore[assignment]
        config_store._read_api_key_authoritative = original_read_authoritative  # type: ignore[assignment]

    assert keys == {"api-provider": "selected-secret"}
    assert result["provider_api_key_count"] == 1
    assert result["default_provider_id"] == "api-provider"
    assert result["providers"][0].get("suspended") is False


def test_provider_sync_rpc_acknowledges_idempotent_credentials() -> None:
    original_compare_set = config_store._compare_set_api_key
    original_read = config_store._read_api_key
    original_read_authoritative = config_store._read_api_key_authoritative
    keys: dict[str, str] = {}
    config_store._compare_set_api_key = (
        lambda provider_id, _expected, api_key: keys.__setitem__(provider_id, api_key)
    )
    config_store._read_api_key = lambda provider_id: keys.get(provider_id, "")  # type: ignore[assignment]
    config_store._read_api_key_authoritative = lambda provider_id: keys.get(provider_id, "")  # type: ignore[assignment]
    providers = [_provider_record("api-provider", "API Provider", mode="api_key")]
    provider_state = _provider_sync_payload(
        "api-provider",
        providers,
        provider_api_keys=[
            {"provider_id": "api-provider", "api_key": "selected-secret"},
        ],
    )
    try:
        config_store._config_path().unlink(missing_ok=True)
        with config_store._state_cache_lock:
            config_store._state_cache = None
        first = node_rpc_handlers._rpc_sync_provider_config(
            {"provider_state": provider_state}
        )
        second = node_rpc_handlers._rpc_sync_provider_config(
            {"provider_state": provider_state}
        )
    finally:
        config_store._compare_set_api_key = original_compare_set  # type: ignore[assignment]
        config_store._read_api_key = original_read  # type: ignore[assignment]
        config_store._read_api_key_authoritative = original_read_authoritative  # type: ignore[assignment]

    assert first["provider_api_key_ids"] == ["api-provider"]
    assert second["provider_api_key_ids"] == ["api-provider"]
    assert first["sync_status"] == "applied"
    assert second["sync_status"] == "unchanged"
    assert second["provider_api_key_count"] == 0


def test_import_provider_sync_fails_when_api_key_cannot_be_stored() -> None:
    original_compare_set = config_store._compare_set_api_key
    original_read = config_store._read_api_key
    original_read_authoritative = config_store._read_api_key_authoritative

    def fail_store(_provider_id: str, api_key: str) -> None:
        if api_key:
            raise RuntimeError("credential store failed")

    config_store._compare_set_api_key = (
        lambda provider_id, _expected, api_key: fail_store(
            provider_id,
            api_key,
        )
    )
    config_store._read_api_key = lambda _provider_id: ""  # type: ignore[assignment]
    config_store._read_api_key_authoritative = lambda _provider_id: ""  # type: ignore[assignment]
    providers = [
        _provider_record(
            "api-provider",
            "API Provider",
            mode="api_key",
        )
    ]
    try:
        try:
            _fresh_provider_sync(_provider_sync_payload(
                "api-provider",
                providers,
                provider_api_keys=[
                    {"provider_id": "api-provider", "api_key": "selected-secret"},
                ],
            ))
        except RuntimeError as exc:
            assert "credential store failed" in str(exc)
            assert "selected-secret" not in str(exc)
            return
    finally:
        config_store._compare_set_api_key = original_compare_set  # type: ignore[assignment]
        config_store._read_api_key = original_read  # type: ignore[assignment]
        config_store._read_api_key_authoritative = original_read_authoritative  # type: ignore[assignment]
    raise AssertionError("provider sync succeeded without stored credentials")


def test_import_provider_sync_skips_keyless_api_provider_as_default() -> None:
    providers = [
        _provider_record(
            "api-provider",
            "API Provider",
            mode="api_key",
        ),
        _provider_record(
            "subscription-provider",
            "Subscription Provider",
        ),
    ]
    payload = _provider_sync_payload("api-provider", providers)

    result = _fresh_provider_sync(payload)

    assert result["default_provider_id"] == "subscription-provider"


def test_import_provider_sync_clears_default_when_every_provider_needs_missing_key() -> None:
    providers = [
        _provider_record(
            "api-provider",
            "API Provider",
            mode="api_key",
        )
    ]
    payload = _provider_sync_payload("api-provider", providers)

    result = _fresh_provider_sync(payload)

    assert result["default_provider_id"] is None


async def test_node_cwd_is_validated_against_the_node() -> None:
    """A cwd the node cannot use must be rejected before the turn starts.

    A node session inherits the primary's cwd unless one is given. On a
    Windows node under a POSIX primary that reached the runner and failed
    as a bare `NotADirectoryError: [WinError 267]` mid-turn.
    """
    import session_detail_api
    import node_rpc_handlers

    calls: list[dict] = []

    async def fake_rpc(node_id, method, params, **kwargs):
        calls.append({"node_id": node_id, "method": method, "params": params})
        if params.get("path") == "/does/not/exist":
            raise RuntimeError("FileNotFoundError: not a directory: /does/not/exist")
        if params.get("path") == "/transport/flake":
            raise RuntimeError("node went away mid-call")
        return {"path": params.get("path"), "entries": []}

    original = node_rpc_handlers.call_local_or_remote
    node_rpc_handlers.call_local_or_remote = fake_rpc
    session_detail_api._node_cwd_verified.clear()
    try:
        assert await session_detail_api._node_cwd_error("n1", "") is None, "empty cwd should not be probed"
        assert not calls

        bad = await session_detail_api._node_cwd_error("n1", "/does/not/exist")
        assert bad and "/does/not/exist" in bad and "n1" in bad, bad

        # A transport failure is the offline/version checks' job, not this one.
        assert await session_detail_api._node_cwd_error("n1", "/transport/flake") is None

        assert await session_detail_api._node_cwd_error("n1", "/good") is None
        before = len(calls)
        assert await session_detail_api._node_cwd_error("n1", "/good") is None
        assert len(calls) == before, "a verified cwd should not be re-probed"
    finally:
        node_rpc_handlers.call_local_or_remote = original
        session_detail_api._node_cwd_verified.clear()


def _write_package(root, name: str):
    package = root / name
    (package / "src").mkdir(parents=True)
    (package / "better-agent-extension.json").write_text("{}")
    (package / "src" / "main.py").write_text("print('hi')\n")
    return package


def test_package_artifact_prunes_runtime_trees_with_links() -> None:
    """A built .venv/node_modules must not make a package unshippable."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        package = _write_package(Path(tmp), "ext-with-venv")
        venv_bin = package / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python3.12").write_text("#!/bin/sh\n")
        (venv_bin / "python").symlink_to("python3.12")
        (package / "node_modules").mkdir()
        (package / "node_modules" / "dep").symlink_to("../src")

        archive = extension_store._build_package_artifact(package)
        assert archive
        shipped = {p.relative_to(package).as_posix() for p in extension_store._package_artifact_paths(package)}
        assert shipped == {"better-agent-extension.json", "src/main.py"}


def test_package_artifact_still_rejects_links_in_real_content() -> None:
    """The symlink guard is a path-escape control; pruning must not weaken it."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        package = _write_package(Path(tmp), "ext-with-link")
        (package / "src" / "escape.py").symlink_to("/etc/passwd")
        try:
            extension_store._build_package_artifact(package)
        except extension_store.ExtensionError:
            return
        raise AssertionError("symlink in package content was accepted")


def test_export_skips_bad_package_instead_of_aborting() -> None:
    """One unshippable package must not deny every node all the others."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        good = _write_package(root, "good")
        bad = _write_package(root, "bad")
        (bad / "src" / "escape.py").symlink_to("/etc/passwd")

        original = extension_store._load_with_changes
        record = lambda path: {"source": {"install_path": str(path), "commit_sha": ""}}  # noqa: E731
        extension_store._load_with_changes = lambda: (  # type: ignore[assignment]
            {
                "extensions": {
                    "ok.ext": record(good),
                    "bad.ext": record(bad),
                    # Corrupt record: a relative path would resolve against the
                    # process cwd and try to ship that whole tree.
                    "relative.ext": record(Path(".")),
                },
                "deleted_extensions": {},
            },
            False,
            False,
        )
        try:
            payload = extension_store.export_extension_sync_state()
        finally:
            extension_store._load_with_changes = original  # type: ignore[assignment]

    shipped = {a["extension_id"] for a in payload["artifacts"]}
    assert shipped == {"ok.ext"}, shipped


def test_harness_sync_state_round_trips() -> None:
    import harness_profile_store

    exported = harness_profile_store.export_harness_sync_state()
    assert exported["schema_version"] == harness_profile_store.SCHEMA_VERSION
    assert isinstance(exported["profiles"], dict)

    result = harness_profile_store.import_harness_sync_state(exported)
    assert result["profiles"] == len(exported["profiles"])
    assert harness_profile_store.export_harness_sync_state() == exported


def test_harness_sync_state_refuses_foreign_schema() -> None:
    import harness_profile_store

    for bad in ({"schema_version": 999, "profiles": {}}, {"profiles": {}}, "nope"):
        try:
            harness_profile_store.import_harness_sync_state(bad)
        except harness_profile_store.HarnessProfileError:
            continue
        raise AssertionError(f"accepted incompatible harness sync state: {bad!r}")


def test_harness_surface_is_registered_for_node_sync() -> None:
    import node_config_sync

    names = {surface.name for surface in node_config_sync.SURFACES}
    assert names == {"extensions", "providers", "harness"}, names
    assert "sync_harness_profile" in node_rpc_handlers._HANDLERS


async def test_projection_generation_orders_old_slow_before_new_fast() -> None:
    surfaces = (
        ("sync_provider_config", "provider_state"),
        ("sync_extension_config", "extension_state"),
        ("sync_harness_profile", "harness_state"),
    )
    for method, param in surfaces:
        applied: list[int] = []
        old_started = asyncio.Event()
        release_old = asyncio.Event()
        new_entered_dispatch = asyncio.Event()
        current_generation = 1
        original_handler = node_rpc_handlers._HANDLERS[method]

        async def apply_state(params: dict) -> dict:
            version = params[param]["version"]
            if version == 1:
                old_started.set()
                await release_old.wait()
            applied.append(version)
            return {"ok": True}

        def guard(generation: int) -> None:
            if generation != current_generation:
                raise node_rpc_handlers.StaleProjectionGeneration()

        async def dispatch_new():
            new_entered_dispatch.set()
            return await node_rpc_handlers.dispatch_rpc(
                method,
                {param: {"version": 2}},
                assert_projection_current=lambda: guard(2),
            )

        node_rpc_handlers._HANDLERS[method] = apply_state
        try:
            old = asyncio.create_task(
                node_rpc_handlers.dispatch_rpc(
                    method,
                    {param: {"version": 1}},
                    assert_projection_current=lambda: guard(1),
                )
            )
            await old_started.wait()
            current_generation = 2
            new = asyncio.create_task(dispatch_new())
            await new_entered_dispatch.wait()
            assert applied == [], (method, applied)
            release_old.set()
            await asyncio.gather(old, new)
        finally:
            release_old.set()
            node_rpc_handlers._HANDLERS[method] = original_handler
        assert applied == [1, 2], (method, applied)
        try:
            await node_rpc_handlers.dispatch_rpc(
                method,
                {param: {"version": 1}},
                assert_projection_current=lambda: guard(1),
            )
        except node_rpc_handlers.StaleProjectionGeneration:
            pass
        else:
            raise AssertionError(f"{method} accepted a stale queued projection")
        assert applied == [1, 2], (method, applied)


async def test_node_client_tracks_and_rejects_stale_rpc_tasks() -> None:
    stale_client = node_client.NodeClient()
    stale_client._connection_generation = 2
    await stale_client._handle_rpc_request(
        {
            "request_id": "stale",
            "method": "sync_harness_profile",
            "params": {"harness_state": {}},
        },
        1,
    )
    assert stale_client._send_queue.empty()

    active_client = node_client.NodeClient()
    active_client._connection_generation = 1
    started = asyncio.Event()
    release = asyncio.Event()
    original_dispatch = node_rpc_handlers.dispatch_rpc

    async def blocked_dispatch(*_args, **_kwargs):
        started.set()
        await release.wait()
        return {"ok": True}

    node_rpc_handlers.dispatch_rpc = blocked_dispatch  # type: ignore[assignment]
    try:
        await active_client._route_inbound(
            {
                "type": "rpc_request",
                "request_id": "active",
                "method": "sync_harness_profile",
                "params": {"harness_state": {}},
            },
            1,
        )
        await started.wait()
        assert len(active_client._rpc_tasks) == 1
        await active_client.stop()
        assert not active_client._rpc_tasks
    finally:
        release.set()
        node_rpc_handlers.dispatch_rpc = original_dispatch  # type: ignore[assignment]


async def test_node_client_stop_drains_projection_handlers() -> None:
    surfaces = (
        ("sync_provider_config", "provider_state"),
        ("sync_extension_config", "extension_state"),
        ("sync_harness_profile", "harness_state"),
    )
    for method, param in surfaces:
        applied: list[int] = []
        started = threading.Event()
        release = threading.Event()
        original_handler = node_rpc_handlers._HANDLERS[method]

        def blocked_handler(params: dict) -> dict:
            started.set()
            release.wait()
            applied.append(params[param]["version"])
            return {"ok": True}

        client = node_client.NodeClient()
        client._connection_generation = 1
        node_rpc_handlers._HANDLERS[method] = blocked_handler
        stop_task: asyncio.Task[None] | None = None
        try:
            await client._route_inbound(
                {
                    "type": "rpc_request",
                    "request_id": method,
                    "method": method,
                    "params": {param: {"version": 1}},
                },
                1,
            )
            assert await asyncio.to_thread(started.wait, 5), method
            rpc_task = next(iter(client._rpc_tasks))
            stop_task = asyncio.create_task(client.stop())
            while not rpc_task.cancelling():
                await asyncio.sleep(0)
            cancellation_delivered = asyncio.Event()
            asyncio.get_running_loop().call_soon(cancellation_delivered.set)
            await cancellation_delivered.wait()
            assert not stop_task.done(), method
            assert applied == [], (method, applied)
        finally:
            release.set()
            if stop_task is not None:
                await stop_task
            node_rpc_handlers._HANDLERS[method] = original_handler
        assert applied == [1], (method, applied)
        assert not client._rpc_tasks, method


async def test_extension_incident_replay_lifecycle_is_drained() -> None:
    client = node_client.NodeClient()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def blocked_replay() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    client._replay_extension_incidents = blocked_replay  # type: ignore[method-assign]
    reconnect_replay = client._start_extension_incident_replay()
    await started.wait()
    await client._cancel_extension_incident_replay(reconnect_replay)
    assert finished.is_set()
    assert not client._incident_replay_tasks

    started.clear()
    finished.clear()
    client._start_extension_incident_replay()
    await started.wait()
    await client.stop()
    assert finished.is_set()
    assert not client._incident_replay_tasks


async def _main() -> None:
    await test_sync_providers_all_nodes_reports_node_failures()
    await test_first_approval_uses_topology_cwd_roots()
    await test_dynamic_first_approval_uses_registry_record()
    await test_bulk_provider_sync_rejects_credentials()
    await test_provider_secret_sync_requires_secure_node_transport()
    await test_provider_secret_sync_passes_explicit_ids_on_loopback()
    await test_provider_sync_requires_version_ready()
    await test_provider_secret_sync_rechecks_transport_on_rpc_send()
    await test_version_ready_rpc_rejects_mismatched_node_before_send()
    await test_spawn_run_rejects_mismatched_node_before_send()
    await test_run_headless_requires_version_ready()
    await test_node_runtime_readiness_waits_across_reconnect()
    await test_remote_admission_starts_only_after_runtime_ready()
    await test_failed_remote_send_cleans_exact_connection_and_lease()
    await test_node_json_iterator_preserves_disconnect_evidence()
    test_export_provider_sync_state_excludes_api_keys_by_default()
    test_export_provider_sync_state_includes_only_selected_api_keys()
    test_import_provider_sync_writes_api_key_before_default_selection()
    test_import_provider_sync_fails_when_api_key_cannot_be_stored()
    test_import_provider_sync_skips_keyless_api_provider_as_default()
    test_import_provider_sync_clears_default_when_every_provider_needs_missing_key()
    test_node_projection_replaces_foreign_local_authority()
    await test_node_cwd_is_validated_against_the_node()
    test_package_artifact_prunes_runtime_trees_with_links()
    test_package_artifact_still_rejects_links_in_real_content()
    test_export_skips_bad_package_instead_of_aborting()
    test_harness_sync_state_round_trips()
    test_harness_sync_state_refuses_foreign_schema()
    test_harness_surface_is_registered_for_node_sync()
    await test_projection_generation_orders_old_slow_before_new_fast()
    await test_node_client_tracks_and_rejects_stale_rpc_tasks()
    await test_node_client_stop_drains_projection_handlers()
    await test_extension_incident_replay_lifecycle_is_drained()


if __name__ == "__main__":
    asyncio.run(_main())
    print("PASS test_machine_node_sync")

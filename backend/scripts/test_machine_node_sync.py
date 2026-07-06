from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-machine-node-sync-")
_BACKEND = Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import config_store  # noqa: E402
import extension_api  # noqa: E402
import node_link  # noqa: E402
import node_rpc_handlers  # noqa: E402
import node_store  # noqa: E402
import provider_remote  # noqa: E402

logging.disable(logging.CRITICAL)


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
    seen: dict[str, object] = {}

    def export(provider_api_key_ids=None):
        seen["provider_api_key_ids"] = provider_api_key_ids
        return {"providers": [], "provider_api_keys": []}

    async def call(node_id, method, params, **kwargs):
        seen["node_id"] = node_id
        seen["method"] = method
        seen["params"] = params
        seen["secure_transport_required"] = kwargs.get("secure_transport_required")
        seen["version_ready_required"] = kwargs.get("version_ready_required")
        return {"provider_count": 0, "provider_api_key_count": 0}

    config_store.export_provider_sync_state = export  # type: ignore[assignment]
    node_store.get_connection = lambda _node_id: SimpleNamespace(  # type: ignore[assignment]
        ws=SimpleNamespace(
            url=SimpleNamespace(scheme="ws"),
            client=SimpleNamespace(host="127.0.0.1"),
        )
    )
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
        node_store.get_connection = original_get_connection  # type: ignore[assignment]
        node_rpc_handlers.call_local_or_remote = original_call  # type: ignore[assignment]

    assert response is not None
    assert response.status_code == 200
    assert seen["provider_api_key_ids"] == ["api-provider"]
    assert seen["node_id"] == "node-a"
    assert seen["method"] == "sync_provider_config"
    assert seen["secure_transport_required"] is True
    assert seen["version_ready_required"] is True
    assert seen["params"] == {"provider_state": {"providers": [], "provider_api_keys": []}}


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
        "providers": [],
        "provider_api_keys": [],
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
        assert "requires a WSS or loopback" in str(exc.detail)
        return
    finally:
        config_store.export_provider_sync_state = original_export  # type: ignore[assignment]
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
        return {}

    node_link.rpc_call = rpc_call  # type: ignore[assignment]
    try:
        await provider_remote.RemoteProviderProxy("node-a").run_headless(prompt="x")
    finally:
        node_link.rpc_call = original_rpc_call  # type: ignore[assignment]

    assert seen["version_ready_required"] is True


def test_export_provider_sync_state_excludes_api_keys_by_default() -> None:
    original_read = config_store._read_api_key
    config_store._save_state({
        "default_provider_id": "api-provider",
        "providers": [
            {
                "id": "api-provider",
                "name": "API Provider",
                "kind": "claude",
                "mode": "api_key",
                "default_model": "opus",
            },
        ],
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
    config_store._save_state({
        "default_provider_id": "api-provider",
        "providers": [
            {
                "id": "api-provider",
                "name": "API Provider",
                "kind": "claude",
                "mode": "api_key",
                "default_model": "opus",
            },
            {
                "id": "other-provider",
                "name": "Other Provider",
                "kind": "claude",
                "mode": "api_key",
                "default_model": "opus",
            },
        ],
    })
    config_store._read_api_key = lambda provider_id: {  # type: ignore[assignment]
        "api-provider": "selected-secret",
        "other-provider": "other-secret",
    }.get(provider_id, "")
    try:
        payload = config_store.export_provider_sync_state(["api-provider"])
    finally:
        config_store._read_api_key = original_read  # type: ignore[assignment]

    assert payload["provider_api_keys"] == [
        {"provider_id": "api-provider", "api_key": "selected-secret"}
    ]
    assert "other-secret" not in json.dumps(payload)


def test_import_provider_sync_writes_api_key_before_default_selection() -> None:
    original_write = config_store._write_api_key
    original_read = config_store._read_api_key
    original_uncached = config_store._read_api_key_uncached
    keys: dict[str, str] = {}
    config_store._write_api_key = lambda provider_id, api_key: keys.__setitem__(provider_id, api_key)  # type: ignore[assignment]
    config_store._read_api_key = lambda provider_id: keys.get(provider_id, "")  # type: ignore[assignment]
    config_store._read_api_key_uncached = lambda provider_id: keys.get(provider_id, "")  # type: ignore[assignment]
    try:
        result = config_store.import_provider_sync_state({
            "default_provider_id": "api-provider",
            "providers": [
                {
                    "id": "api-provider",
                    "name": "API Provider",
                    "kind": "claude",
                    "mode": "api_key",
                    "default_model": "opus",
                },
            ],
            "provider_api_keys": [
                {"provider_id": "api-provider", "api_key": "selected-secret"},
            ],
        })
    finally:
        config_store._write_api_key = original_write  # type: ignore[assignment]
        config_store._read_api_key = original_read  # type: ignore[assignment]
        config_store._read_api_key_uncached = original_uncached  # type: ignore[assignment]

    assert keys == {"api-provider": "selected-secret"}
    assert result["provider_api_key_count"] == 1
    assert result["default_provider_id"] == "api-provider"
    assert result["providers"][0].get("suspended") is False


def test_import_provider_sync_fails_when_api_key_cannot_be_stored() -> None:
    original_write = config_store._write_api_key
    original_uncached = config_store._read_api_key_uncached
    config_store._write_api_key = lambda _provider_id, _api_key: None  # type: ignore[assignment]
    config_store._read_api_key_uncached = lambda _provider_id: ""  # type: ignore[assignment]
    try:
        try:
            config_store.import_provider_sync_state({
                "default_provider_id": "api-provider",
                "providers": [
                    {
                        "id": "api-provider",
                        "name": "API Provider",
                        "kind": "claude",
                        "mode": "api_key",
                        "default_model": "opus",
                    },
                ],
                "provider_api_keys": [
                    {"provider_id": "api-provider", "api_key": "selected-secret"},
                ],
            })
        except ValueError as exc:
            assert "could not be stored" in str(exc)
            assert "selected-secret" not in str(exc)
            return
    finally:
        config_store._write_api_key = original_write  # type: ignore[assignment]
        config_store._read_api_key_uncached = original_uncached  # type: ignore[assignment]
    raise AssertionError("provider sync succeeded without stored credentials")


def test_import_provider_sync_skips_keyless_api_provider_as_default() -> None:
    payload = {
        "default_provider_id": "api-provider",
        "providers": [
            {
                "id": "api-provider",
                "name": "API Provider",
                "kind": "claude",
                "mode": "api_key",
                "default_model": "opus",
            },
            {
                "id": "subscription-provider",
                "name": "Subscription Provider",
                "kind": "claude",
                "mode": "subscription",
                "default_model": "opus",
            },
        ],
    }

    result = config_store.import_provider_sync_state(payload)

    assert result["default_provider_id"] == "subscription-provider"


def test_import_provider_sync_clears_default_when_every_provider_needs_missing_key() -> None:
    payload = {
        "default_provider_id": "api-provider",
        "providers": [
            {
                "id": "api-provider",
                "name": "API Provider",
                "kind": "claude",
                "mode": "api_key",
                "default_model": "opus",
            },
        ],
    }

    result = config_store.import_provider_sync_state(payload)

    assert result["default_provider_id"] is None


def test_machine_page_uses_sync_callbacks() -> None:
    ui_path = (
        _ROOT
        / "better-agent-private"
        / "extensions"
        / "machine-nodes"
        / "ui"
        / "machine-nodes.entry.js"
    )
    if not ui_path.is_file():
        return
    ui = ui_path.read_text(encoding="utf-8")

    assert "context.syncProvidersToNode" in ui
    assert "includeSecrets" in ui
    assert "Type ${machine.id}" in ui
    assert "version_status" in ui
    assert "Version mismatch" in ui
    assert "context.syncExtensionsToNode" in ui
    assert "Sync providers" in ui
    assert "Sync extensions" in ui


async def _main() -> None:
    await test_sync_providers_all_nodes_reports_node_failures()
    await test_bulk_provider_sync_rejects_credentials()
    await test_provider_secret_sync_requires_secure_node_transport()
    await test_provider_secret_sync_passes_explicit_ids_on_loopback()
    await test_provider_sync_requires_version_ready()
    await test_provider_secret_sync_rechecks_transport_on_rpc_send()
    await test_version_ready_rpc_rejects_mismatched_node_before_send()
    await test_spawn_run_rejects_mismatched_node_before_send()
    await test_run_headless_requires_version_ready()
    test_export_provider_sync_state_excludes_api_keys_by_default()
    test_export_provider_sync_state_includes_only_selected_api_keys()
    test_import_provider_sync_writes_api_key_before_default_selection()
    test_import_provider_sync_fails_when_api_key_cannot_be_stored()
    test_import_provider_sync_skips_keyless_api_provider_as_default()
    test_import_provider_sync_clears_default_when_every_provider_needs_missing_key()
    test_machine_page_uses_sync_callbacks()


if __name__ == "__main__":
    asyncio.run(_main())
    print("PASS test_machine_node_sync")

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-machine-node-sync-")
_BACKEND = Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import config_store  # noqa: E402
import extension_api  # noqa: E402
import node_rpc_handlers  # noqa: E402
import node_store  # noqa: E402

logging.disable(logging.CRITICAL)


class _Request:
    def __init__(self, method: str) -> None:
        self.method = method
        self.query_params = {}


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
    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["results"] == [
        {"node_id": "node-a", "ok": False, "error": "node sync failed"}
    ]


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
    assert "context.syncExtensionsToNode" in ui
    assert "Sync providers" in ui
    assert "Sync extensions" in ui


async def _main() -> None:
    await test_sync_providers_all_nodes_reports_node_failures()
    test_import_provider_sync_skips_keyless_api_provider_as_default()
    test_import_provider_sync_clears_default_when_every_provider_needs_missing_key()
    test_machine_page_uses_sync_callbacks()


if __name__ == "__main__":
    asyncio.run(_main())
    print("PASS test_machine_node_sync")

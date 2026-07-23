#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))

from better_agent_sdk.surfaces import (
    OperationSpec,
    _schema_shape,
    build_client,
    build_registry,
    specs_from_fastmcp,
)
from runtime_broker import RuntimeBroker


def greet(name: str, count: int = 1) -> dict[str, object]:
    return {"message": name * count}


def main() -> None:
    specs = (OperationSpec("greet", greet, "Greet repeatedly.", operation="example_greet"),)
    client = build_client(specs, local=True)
    assert client.operation_names == ("greet",)
    result = asyncio.run(client.run("greet", {"name": "a", "count": 2}))
    assert result.root == {"message": "aa"}
    schema = client.schema()
    encoded = json.dumps(schema, sort_keys=True)
    assert "Greet repeatedly." in encoded
    assert '"additionalProperties": false' in encoded
    try:
        asyncio.run(client.run("greet", {"name": "a", "unexpected": True}))
    except Exception:
        pass
    else:
        raise AssertionError("unknown generated operation input was accepted")
    registry = build_registry(specs)
    received = []

    def handle(request):
        received.append(request)
        if request.kind == "catalog":
            return {
                "success": True,
                "generation": "generation-one",
                "schema": {
                    "example_greet": {
                        "request": registry.snapshot().get("greet").request_schema(),
                        "response": {},
                    }
                },
            }
        assert request.generation == "generation-one"
        return {"success": True, "result": {"message": "brokered"}}

    with tempfile.TemporaryDirectory() as raw:
        broker = RuntimeBroker(Path(raw), handle)
        os.environ["BETTER_AGENT_RUNTIME_BROKER"] = broker.start()
        try:
            brokered = build_client(specs)
            result = asyncio.run(
                brokered.run("greet", {"name": "a", "count": 2})
            )
            assert result.root == {"message": "brokered"}
            assert [item.kind for item in received] == ["catalog", "invoke"]
        finally:
            broker.stop()
            os.environ.pop("BETTER_AGENT_RUNTIME_BROKER", None)
    _assert_maintained_catalog_alignment()
    print("generated operation surface tests passed")


def _assert_maintained_catalog_alignment() -> None:
    import capabilities_mcp
    import communicate_mcp
    import open_config_panel_mcp
    import open_file_panel_mcp
    import capability_api
    import operation_catalog
    from runtime_operations import _load_bundled_server
    from provider_config_sync_backend.mcp_server import create_server

    previous_file_editing = os.environ.get("BETTER_CLAUDE_FILE_EDITING")
    os.environ["BETTER_CLAUDE_FILE_EDITING"] = "1"
    try:
        bundled_groups = (
            _load_bundled_server("coordination")._specs(),
            _load_bundled_server("marketplace")._specs(),
            _load_bundled_server("session-bridge")._specs(),
            _load_bundled_server("session-control")._specs(),
        )
        groups = (
            capabilities_mcp._specs(),
            communicate_mcp._specs(),
            open_config_panel_mcp._specs(),
            open_file_panel_mcp._specs(),
            *bundled_groups,
            specs_from_fastmcp(
                create_server(),
                operations={
                    name: "provider_config_sync_tools_" + name
                    for name in create_server()._tool_manager._tools
                },
            ),
        )
        specs = tuple(spec for group in groups for spec in group)
    finally:
        if previous_file_editing is None:
            os.environ.pop("BETTER_CLAUDE_FILE_EDITING", None)
        else:
            os.environ["BETTER_CLAUDE_FILE_EDITING"] = previous_file_editing
    assert len(specs) == 54
    catalog = operation_catalog.current()
    assert capability_api
    for spec in (item for group in bundled_groups for item in group):
        generated = build_registry((spec,)).snapshot().get(spec.name)
        assert generated.summary == inspect.getdoc(spec.handler), spec.name
    for spec in specs:
        local = build_registry((spec,)).snapshot().get(spec.name).request_schema()
        remote = catalog.descriptor(spec.operation).request_model.model_json_schema()
        assert _schema_shape(local) == _schema_shape(remote), spec.operation


if __name__ == "__main__":
    main()

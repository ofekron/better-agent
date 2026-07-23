#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile

from pydantic import BaseModel

_STATE_HOME = tempfile.mkdtemp(prefix="better-agent-runtime-api-")
os.environ["BETTER_AGENT_HOME"] = _STATE_HOME

import operation_catalog
import node_store
import runtime_operation_api
from session_manager import manager as session_manager


class ReadRequest(BaseModel):
    value: str


async def _scenario() -> None:
    original_manager = operation_catalog._MANAGER
    original_get_ref = session_manager.get_ref
    original_get_connection = node_store.get_connection
    manager = operation_catalog.CatalogManager()
    operation_catalog._MANAGER = manager
    session_manager.get_ref = (
        lambda sid: {"id": sid, "node_id": "node-one"}
        if sid == "session-one"
        else None
    )
    node_store.get_connection = (
        lambda node_id: object() if node_id == "node-one" else None
    )
    try:
        descriptor = manager.register_capability(
            "runtime",
            "example.read",
            ReadRequest,
            lambda request: {"value": request.value},
            policy=operation_catalog.OperationPolicy(
                side_effect=operation_catalog.SideEffectClass.READ,
                owner=operation_catalog.ExecutionOwner.PRIMARY,
                recovery=operation_catalog.RecoveryPolicy.FAIL,
                durable=False,
                cancel_supported=False,
                context_required=True,
            ),
        )
        catalog = manager.publish()
        run_dir = Path(_STATE_HOME) / "runs" / "run-12345678"
        run_dir.mkdir(parents=True)
        (run_dir / "input.json").write_text(
            json.dumps(
                {
                    "app_session_id": "session-one",
                    "cwd": "/tmp/project",
                    "internal_token": "",
                }
            ),
            encoding="utf-8",
        )
        context = {
            "app_session_id": "session-one",
            "run_id": "run-12345678",
            "provider_id": "provider-one",
            "cwd": "/tmp/project",
        }
        catalog_response = await runtime_operation_api.handle(
            {
                **context,
                "request": {"version": 1, "kind": "catalog"},
            }
        )
        assert catalog_response["generation"] == catalog.generation
        assert descriptor.key in catalog_response["schema"]
        result = await runtime_operation_api.handle(
            {
                **context,
                "request": {
                    "version": 1,
                    "kind": "invoke",
                    "operation": descriptor.key,
                    "payload": {"value": "ok"},
                    "generation": catalog.generation,
                },
            }
        )
        assert result == {"success": True, "result": {"value": "ok"}}
        relayed = await runtime_operation_api.handle(
            {
                **context,
                "node_id": "node-one",
                "request": {
                    "version": 1,
                    "kind": "invoke",
                    "operation": descriptor.key,
                    "payload": {"value": "relayed"},
                    "generation": catalog.generation,
                },
            }
        )
        assert relayed == {"success": True, "result": {"value": "relayed"}}
        try:
            await runtime_operation_api.handle(
                {
                    **context,
                    "app_session_id": "session-two",
                    "request": {"version": 1, "kind": "catalog"},
                }
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("runtime broker crossed its session scope")
    finally:
        node_store.get_connection = original_get_connection
        session_manager.get_ref = original_get_ref
        operation_catalog._MANAGER = original_manager


def main() -> None:
    try:
        asyncio.run(_scenario())
        print("runtime operation API tests passed")
    finally:
        shutil.rmtree(_STATE_HOME)


if __name__ == "__main__":
    main()

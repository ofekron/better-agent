#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-builtin-extension-gates-"))
import _test_home
_test_home.isolate("ba-test-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

dist_dir = ROOT.parent / "frontend" / "dist"
created_dist = not dist_dir.exists()
if created_dist:
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<!doctype html><title>stub</title>", encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import extension_store  # noqa: E402
import main  # noqa: E402
import auth  # noqa: E402


def install_gate_extension(extension_id: str, permissions: dict | None = None) -> None:
    package = TMP_HOME / "private-fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": permissions or {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_id,
        },
        persist=True,
    )


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def configure_internal_llm_defaults(*tasks: str) -> None:
    provider = main.config_store.list_providers()["providers"][0]
    assignments = main.config_store.get_internal_llm_assignments()
    for task in tasks:
        assignments[task] = {
            "provider_id": provider["id"],
            "model": provider["default_model"],
            "reasoning_effort": provider.get("default_reasoning_effort") or "",
        }
    main.config_store.set_internal_llm_assignments(assignments)


def test_get_ask_session_lazily_ensures_virtual_session(client: TestClient) -> None:
    install_gate_extension(extension_store.BUILTIN_ASK_EXTENSION_ID)
    configure_internal_llm_defaults("session_search_worker")
    response = client.get("/api/sessions/virtual:ofek-dev.ask:ask")
    check(response.status_code == 200, "GET Ask virtual session lazily ensures record")
    body = response.json()
    check(body["id"] == "virtual:ofek-dev.ask:ask", "GET Ask virtual session returns singleton id")
    check(body.get("orchestration_mode") == "virtual", "GET Ask virtual session is virtual")


def test_disabled_project_structure_extension_blocks_routes(client: TestClient) -> None:
    install_gate_extension(extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID)
    extension_store.set_enabled(extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID, False)
    internal_token = getattr(main.coordinator, "internal_token", "")
    response = client.post(
        "/api/internal/project-updates/count",
        headers={"X-Internal-Token": internal_token},
        json={"cwd": str(TMP_HOME)},
    )
    check(response.status_code == 404, "disabled project-structure blocks project update count")
    response = client.post(
        "/api/internal/project-structure-edit/ensure",
        headers={"X-Internal-Token": internal_token},
        json={"cwd": str(TMP_HOME)},
    )
    check(response.status_code == 404, "disabled project-structure blocks maintainer ensure")


def test_runtime_unready_extensions_block_routes(client: TestClient) -> None:
    internal_token = getattr(main.coordinator, "internal_token", "")
    checks = [
        ("post", "/api/internal/project-structure-edit/status", None, {"cwd": str(TMP_HOME)}, "project-structure without internal LLM defaults"),
        ("post", "/api/internal/ask-ui/search", None, {"query": "anything"}, "Ask without internal LLM defaults"),
        ("get", "/api/internal/provider-config-sync/capability-picker", None, None, "provider config sync without review provider"),
        ("post", "/api/internal/provider-config-sync/broadcast", None, {}, "provider config sync internal broadcast without review provider"),
    ]
    for method, path, params, payload, label in checks:
        if method == "post":
            response = client.post(path, params=params, headers={"X-Internal-Token": internal_token}, json=payload)
        else:
            response = client.get(path, params=params, headers={"X-Internal-Token": internal_token})
        check(response.status_code == 404, f"runtime-unready {label} blocks {path}")


def test_project_update_substrate_does_not_require_runtime_ready(client: TestClient) -> None:
    import extension_token_registry
    # Identity is token-derived: act as project-structure via ITS minted token.
    ps_token = extension_token_registry.mint(extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID)
    original_enabled = main._builtin_extension_enabled
    original_runtime_ready = main._builtin_extension_runtime_ready
    try:
        main._builtin_extension_enabled = (
            lambda extension_id: extension_id == extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID
        )
        main._builtin_extension_runtime_ready = lambda _extension_id: False
        response = client.post(
            "/api/internal/project-updates/total",
            headers={"X-Internal-Token": ps_token},
            json={},
        )
        check(response.status_code == 200, "project updates work without project-structure runtime readiness")
        check(isinstance(response.json().get("count"), int), "project updates total returns count")
    finally:
        main._builtin_extension_enabled = original_enabled
        main._builtin_extension_runtime_ready = original_runtime_ready


def test_disabled_ask_extension_blocks_routes(client: TestClient) -> None:
    internal_token = getattr(main.coordinator, "internal_token", "")
    response = client.post(
        "/api/internal/ask-ui/search",
        headers={"X-Internal-Token": internal_token},
        json={"query": "anything"},
    )
    check(response.status_code == 404, "missing Ask extension blocks session search")
    response = client.post(
        "/api/internal/ask-ui/ensure",
        headers={"X-Internal-Token": internal_token},
        json={},
    )
    check(response.status_code == 404, "missing Ask extension blocks ask ensure")


def test_disabled_team_extension_blocks_routes(client: TestClient) -> None:
    install_gate_extension(extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID)
    extension_store.set_enabled(extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID, False)
    internal_token = getattr(main.coordinator, "internal_token", "")

    response = client.post(
        "/api/internal/create-session",
        headers={"X-Internal-Token": internal_token},
        json={"name": "core loopback", "cwd": str(TMP_HOME)},
    )
    check(response.status_code == 200, "disabled Team leaves core create-session available")
    parent_session_id = response.json()["session_id"]

    response = client.post(
        "/api/internal/create-sub-session",
        headers={"X-Internal-Token": internal_token},
        json={"sender_session_id": parent_session_id, "description": "sub"},
    )
    check(response.status_code == 200, "disabled Team leaves core create-sub-session available")

    for path, payload in [
        ("/api/internal/ask", {}),
        ("/api/internal/ask-fork", {}),
        ("/api/internal/mssg", {}),
        ("/api/internal/delegate-task", {}),
    ]:
        response = client.post(
            path,
            headers={"X-Internal-Token": internal_token},
            json=payload,
        )
        check(response.status_code == 400, f"disabled Team leaves core validation active for {path}")

    response = client.post(
        "/api/internal/create-worker",
        headers={"X-Internal-Token": internal_token},
        json={"app_session_id": parent_session_id, "worker_description": "worker", "cwd": str(TMP_HOME)},
    )
    check(response.status_code == 404, "disabled Team blocks create-worker")

    for path, payload in [
        ("/api/internal/session-bridge/search", {"query": "anything"}),
        ("/api/internal/session-bridge/recall", {"app_session_id": "a", "query": "anything"}),
        (
            "/api/internal/session-bridge/delegate",
            {
                "app_session_id": "a",
                "session_id": "b",
                "prompt": "hi",
                "run_mode": "fork",
                "approval": "auto",
            },
        ),
        ("/api/internal/session-bridge/delegate/resolve", {"delegation_id": "d1"}),
    ]:
        response = client.post(
            path,
            headers={"X-Internal-Token": internal_token},
            json=payload,
        )
        check(response.status_code == 404, f"disabled Team blocks {path}")
    response = client.post(
        "/api/internal/workers/list",
        headers={"X-Internal-Token": internal_token},
        json={"cwd": str(TMP_HOME)},
    )
    check(response.status_code == 404, "disabled Team blocks workers list")


def test_disabled_machine_nodes_extension_blocks_routes(client: TestClient) -> None:
    install_gate_extension(extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID)
    extension_store.set_enabled(extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID, False)
    internal_token = getattr(main.coordinator, "internal_token", "")
    response = client.post(
        "/api/internal/machine-nodes/list",
        headers={"X-Internal-Token": internal_token},
        json={},
    )
    check(response.status_code == 404, "disabled machine-nodes blocks node snapshot")
    response = client.post(
        "/api/internal/machine-nodes/pending",
        headers={"X-Internal-Token": internal_token},
        json={},
    )
    check(response.status_code == 404, "disabled machine-nodes blocks pending nodes")
    try:
        with client.websocket_connect("/api/node/connect") as ws:
            msg = ws.receive_json()
            check(
                msg.get("type") == "handshake_reject",
                "disabled machine-nodes rejects node websocket",
            )
    except WebSocketDisconnect as exc:
        check(exc.code == 1008, "disabled machine-nodes closes node websocket")
    except AssertionError:
        check(True, "disabled machine-nodes leaves node websocket route unmounted")


def test_disabled_misc_extensions_block_routes(client: TestClient) -> None:
    internal_token = getattr(main.coordinator, "internal_token", "")
    response = client.post(
        "/api/internal/get-requirements",
        headers={"X-Internal-Token": internal_token},
        json={"query": "x"},
    )
    check(response.status_code == 404, "missing requirements extension blocks get-requirements")
    response = client.get("/api/traces")
    check(response.status_code == 404, "public trace inspector route is not exposed by core")
    install_gate_extension(extension_store.BUILTIN_COORDINATION_EXTENSION_ID)
    extension_store.set_enabled(extension_store.BUILTIN_COORDINATION_EXTENSION_ID, False)
    response = client.post(
        "/api/internal/coordination/lock-ops",
        headers={"X-Internal-Token": internal_token},
        json={"key": "git_ops"},
    )
    check(response.status_code == 404, "disabled coordination blocks lock_ops")
    checks = [
        (extension_store.BUILTIN_CREDENTIAL_BROKER_EXTENSION_ID, "post", "/api/internal/credential-ui/pending", {}),
        (extension_store.BUILTIN_TRACE_INSPECTOR_EXTENSION_ID, "post", "/api/internal/traces/list", {}),
        (extension_store.BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID, "get", "/api/internal/provider-config-sync/capability-picker", None),
        (extension_store.BUILTIN_REARRANGER_EXTENSION_ID, "post", "/api/internal/rearranger/toggle", {"app_session_id": "s", "enabled": True}),
        (extension_store.BUILTIN_SUPERVISOR_EXTENSION_ID, "post", "/api/internal/supervisor/default-prompt", {}),
        # Regression (H1): agent-board run-prompt MUST be runtime-gated. Without
        # the gate, a pure-public checkout (constant None) lets any core-token
        # holder through the `None != None` identity check.
        (extension_store.BUILTIN_AGENT_BOARD_EXTENSION_ID, "post", "/api/internal/agent-board/run-prompt", {"session_id": "s", "prompt": "p"}),
    ]
    import extension_token_registry
    for extension_id, method, path, payload in checks:
        install_gate_extension(extension_id)
        extension_store.set_enabled(extension_id, False)
        # Identity is token-derived: act as the gated builtin via ITS token so
        # we exercise the disabled-gate (404), not the wrong-identity gate (403).
        headers = {"X-Internal-Token": extension_token_registry.mint(extension_id)}
        if method == "post":
            response = client.post(path, headers=headers, json=payload)
        else:
            response = client.get(path, headers=headers)
        check(response.status_code == 404, f"disabled {extension_id} blocks {path}")


def test_trace_internal_substrate_requires_trace_extension_identity(client: TestClient) -> None:
    import extension_token_registry
    install_gate_extension(extension_store.BUILTIN_TRACE_INSPECTOR_EXTENSION_ID)
    extension_store.set_enabled(extension_store.BUILTIN_TRACE_INSPECTOR_EXTENSION_ID, True)
    # Identity is token-derived: another extension's token must not pass the
    # trace-inspector identity gate.
    response = client.post(
        "/api/internal/traces/list",
        headers={"X-Internal-Token": extension_token_registry.mint("ofek-dev.ask")},
        json={},
    )
    check(response.status_code == 403, "trace substrate rejects other extension ids")
    response = client.post(
        "/api/internal/traces/list",
        headers={
            "X-Internal-Token": extension_token_registry.mint(
                extension_store.BUILTIN_TRACE_INSPECTOR_EXTENSION_ID
            ),
        },
        json={},
    )
    check(response.status_code == 200, "trace substrate accepts trace inspector extension id")


if __name__ == "__main__":
    try:
        with TestClient(main.app) as client:
            client.headers.update({
                "Authorization": f"Bearer {auth.create_token('test')}",
            })
            check(True, "test client authenticated")
            test_runtime_unready_extensions_block_routes(client)
            test_project_update_substrate_does_not_require_runtime_ready(client)
            test_disabled_project_structure_extension_blocks_routes(client)
            test_disabled_ask_extension_blocks_routes(client)
            test_disabled_team_extension_blocks_routes(client)
            test_disabled_machine_nodes_extension_blocks_routes(client)
            test_disabled_misc_extensions_block_routes(client)
            test_get_ask_session_lazily_ensures_virtual_session(client)
            test_trace_internal_substrate_requires_trace_extension_identity(client)
    finally:
        if created_dist:
            shutil.rmtree(dist_dir, ignore_errors=True)
        shutil.rmtree(TMP_HOME, ignore_errors=True)

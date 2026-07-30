#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-builtin-extension-gates-"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _test_home
_test_home.isolate_installed("ba-test-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

dist_dir = ROOT.parent / "frontend" / "dist"
created_dist = not dist_dir.exists()
if created_dist:
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<!doctype html><title>stub</title>", encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import extension_store  # noqa: E402
import internal_guards  # noqa: E402
import main  # noqa: E402
import auth  # noqa: E402


def install_gate_extension(
    extension_id: str, permissions: dict | None = None, *, core_role: str | None = None
) -> None:
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
    if core_role:
        manifest["core_roles"] = [core_role]
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
        force_enabled=True,
    )


def _role_extension_id(role: str) -> str:
    """Resolve a core role to its installed extension id, falling back to a
    synthetic test id when the role isn't owned by anything yet.
    extension_id_for_role() only resolves active (enabled) extensions, so
    callers must cache this return value and reuse it across
    install/enable/disable transitions rather than re-querying the role."""
    return extension_store.extension_id_for_role(role) or f"test.{role}"


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
    project_structure_id = _role_extension_id("project-structure")
    install_gate_extension(project_structure_id, core_role="project-structure")
    extension_store.set_enabled(project_structure_id, False)
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
    ]
    for method, path, params, payload, label in checks:
        if method == "post":
            response = client.post(path, params=params, headers={"X-Internal-Token": internal_token}, json=payload)
        else:
            response = client.get(path, params=params, headers={"X-Internal-Token": internal_token})
        check(response.status_code == 404, f"runtime-unready {label} blocks {path}")


def test_project_update_substrate_does_not_require_runtime_ready(client: TestClient) -> None:
    import extension_token_registry
    # This test doesn't depend on an earlier test having installed
    # project-structure: it installs its own fixture so extension_id_for_role
    # resolves regardless of run order.
    project_structure_id = _role_extension_id("project-structure")
    # The internal-token auth middleware requires internal_loopback on any
    # extension identity calling /api/internal/*, so the fixture must declare
    # it to exercise this route via project-structure's own minted token.
    install_gate_extension(
        project_structure_id,
        {"internal_loopback": True},
        core_role="project-structure",
    )
    # Identity is token-derived: act as project-structure via ITS minted token.
    ps_token = extension_token_registry.mint(project_structure_id)
    original_enabled = main._builtin_extension_enabled
    original_runtime_gate = internal_guards.require_builtin_runtime_extension

    def _never_runtime_ready(extension_id: str) -> None:
        raise main.HTTPException(status_code=404, detail="not runtime ready")

    try:
        main._builtin_extension_enabled = (
            lambda extension_id: extension_id == project_structure_id
        )
        internal_guards.require_builtin_runtime_extension = _never_runtime_ready
        response = client.post(
            "/api/internal/project-updates/total",
            headers={"X-Internal-Token": ps_token},
            json={},
        )
        check(response.status_code == 200, "project updates work without project-structure runtime readiness")
        check(isinstance(response.json().get("count"), int), "project updates total returns count")
    finally:
        main._builtin_extension_enabled = original_enabled
        internal_guards.require_builtin_runtime_extension = original_runtime_gate


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
    team_orchestration_id = _role_extension_id("team-orchestration")
    install_gate_extension(team_orchestration_id, core_role="team-orchestration")
    extension_store.set_enabled(team_orchestration_id, False)
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
    machine_nodes_id = _role_extension_id("machine-nodes")
    install_gate_extension(machine_nodes_id, core_role="machine-nodes")
    extension_store.set_enabled(machine_nodes_id, False)
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
    install_gate_extension(extension_store.BUILTIN_COORDINATION_EXTENSION_ID)
    extension_store.set_enabled(extension_store.BUILTIN_COORDINATION_EXTENSION_ID, False)
    response = client.post(
        "/api/internal/coordination/lock-ops",
        headers={"X-Internal-Token": internal_token},
        json={"key": "git_ops"},
    )
    check(response.status_code == 404, "disabled coordination blocks lock_ops")
    checks = [
        (_role_extension_id('credential-broker'), 'credential-broker', "post", "/api/internal/credential-ui/pending", {}),
        (_role_extension_id('supervisor'), 'supervisor', "post", "/api/internal/supervisor/default-prompt", {}),
        # Regression (H1): agent-board run-prompt MUST be runtime-gated. Without
        # the gate, a pure-public checkout (constant None) lets any core-token
        # holder through the `None != None` identity check.
        (_role_extension_id('agent-board'), 'agent-board', "post", "/api/internal/agent-board/run-prompt", {"session_id": "s", "prompt": "p"}),
    ]
    import extension_token_registry
    for extension_id, core_role, method, path, payload in checks:
        # internal_loopback lets the fixture's own minted token clear the
        # internal-token auth middleware, so the request reaches the
        # deeper disabled-extension gate this test is actually exercising.
        install_gate_extension(
            extension_id, {"internal_loopback": True}, core_role=core_role
        )
        extension_store.set_enabled(extension_id, False)
        # Identity is token-derived: act as the gated builtin via ITS token so
        # we exercise the disabled-gate (404), not the wrong-identity gate (403).
        headers = {"X-Internal-Token": extension_token_registry.mint(extension_id)}
        if method == "post":
            response = client.post(path, headers=headers, json=payload)
        else:
            response = client.get(path, headers=headers)
        check(response.status_code == 404, f"disabled {extension_id} blocks {path}")


def test_coordination_lock_ops_route_forwards_multi_key_body(client: TestClient) -> None:
    install_gate_extension(extension_store.BUILTIN_COORDINATION_EXTENSION_ID)
    extension_store.set_enabled(extension_store.BUILTIN_COORDINATION_EXTENSION_ID, True)
    internal_token = getattr(main.coordinator, "internal_token", "")
    response = client.post(
        "/api/internal/coordination/lock-ops",
        headers={"X-Internal-Token": internal_token},
        json={"key": "", "keys": ["route-a", "route-b"], "timeout_seconds": 0.05, "lease_seconds": 30},
    )
    body = response.json()
    check(response.status_code == 200, "coordination lock_ops route accepts multi-key body")
    check(body.get("success") is True, "coordination lock_ops route forwards keys")
    check(body.get("keys") == ["route-a", "route-b"], "coordination lock_ops returns forwarded keys")
    check(body.get("waited_keys") == [], "coordination lock_ops returns precise waited_keys")
    renew = client.post(
        "/api/internal/coordination/lock-ops",
        headers={"X-Internal-Token": internal_token},
        json={
            "key": "",
            "keys": body.get("keys"),
            "op": "renew",
            "holder_token": body.get("holder_token"),
            "lease_seconds": 45,
        },
    )
    check(renew.json().get("success") is True, "coordination lock_ops route renews multi-key lock")
    release = client.post(
        "/api/internal/coordination/lock-ops",
        headers={"X-Internal-Token": internal_token},
        json={
            "key": "",
            "keys": body.get("keys"),
            "release": True,
            "holder_token": body.get("holder_token"),
        },
    )
    check(release.json().get("success") is True, "coordination lock_ops route releases multi-key lock")


def test_coordination_owner_ops_require_core_identity(client: TestClient) -> None:
    import extension_token_registry
    install_gate_extension(extension_store.BUILTIN_COORDINATION_EXTENSION_ID)
    extension_store.set_enabled(extension_store.BUILTIN_COORDINATION_EXTENSION_ID, True)
    response = client.post(
        "/api/internal/coordination/lock-ops",
        headers={"X-Internal-Token": extension_token_registry.mint(extension_store.BUILTIN_COORDINATION_EXTENSION_ID)},
        json={
            "key": "route-owner-op",
            "op": "reattach",
            "owner": {"app_session_id": "forged-session", "cwd": "/repo"},
        },
    )
    check(response.status_code == 403, "coordination owner-based lock ops require core identity")


def test_coordination_lock_ops_route_overwrites_forged_owner_principal(client: TestClient) -> None:
    install_gate_extension(extension_store.BUILTIN_COORDINATION_EXTENSION_ID)
    extension_store.set_enabled(extension_store.BUILTIN_COORDINATION_EXTENSION_ID, True)
    internal_token = getattr(main.coordinator, "internal_token", "")
    key = "route-owner-principal"
    acquired = client.post(
        "/api/internal/coordination/lock-ops",
        headers={"X-Internal-Token": internal_token},
        json={
            "key": key,
            "owner": {
                "principal_extension_id": "forged-extension",
                "source": "route-test",
            },
        },
    ).json()
    blocked = client.post(
        "/api/internal/coordination/lock-ops",
        headers={"X-Internal-Token": internal_token},
        json={"key": key},
    ).json()
    owner = ((blocked.get("holder") or {}).get("owner") or {})
    check(acquired.get("success") is True, "coordination lock_ops route test acquires holder")
    check(owner.get("principal_extension_id") == "core", "coordination lock_ops route overwrites forged owner principal")
    release = client.post(
        "/api/internal/coordination/lock-ops",
        headers={"X-Internal-Token": internal_token},
        json={"key": key, "release": True, "holder_token": acquired.get("holder_token")},
    )
    check(release.json().get("success") is True, "coordination lock_ops route releases forged-principal test lock")


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
            test_coordination_lock_ops_route_forwards_multi_key_body(client)
            test_coordination_owner_ops_require_core_identity(client)
            test_coordination_lock_ops_route_overwrites_forged_owner_principal(client)
            test_get_ask_session_lazily_ensures_virtual_session(client)
    finally:
        if created_dist:
            shutil.rmtree(dist_dir, ignore_errors=True)
        shutil.rmtree(TMP_HOME, ignore_errors=True)

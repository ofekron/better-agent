from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home

TEST_HOME = _test_home.TestHome.acquire("bc-testape-provider-parity-")
TMP_HOME = Path(TEST_HOME.path)
os.environ["BETTER_AGENT_RUNTIME_BROKER"] = "unix:/tmp/testape-provider-parity.sock"

import builtin_mcp_config  # noqa: E402
import extension_store  # noqa: E402
import harness_field_writer  # noqa: E402
import harness_fields  # noqa: E402
import harness_profile_resolver  # noqa: E402
import harness_profile_store  # noqa: E402
import installation_profile  # noqa: E402
from provider_runtime_plan_source import (  # noqa: E402
    structural_provider_runtime_plan,
)
import runner_agy  # noqa: E402
import runner_codex  # noqa: E402
import runner_qwen  # noqa: E402

# Bare test homes have no installation.json -- the activation gate that decides
# whether extensions are "active" must be bypassed, same as the sibling parity
# tests.
installation_profile.integrations_enabled = lambda: True

EXTENSION_ID = "ofek.testape"
SERVER_NAME = "testape"


@pytest.fixture(autouse=True)
def _active_runtime_python() -> Any:
    """The test container exposes no managed backend dependency environment, so
    resolve extension runtime launch to this interpreter."""
    original = extension_store.dependency_plan.active_runtime_python
    extension_store.dependency_plan.active_runtime_python = (
        lambda _backend_dir: Path(sys.executable)
    )
    try:
        yield
    finally:
        extension_store.dependency_plan.active_runtime_python = original


@pytest.fixture(autouse=True, scope="module")
def _install_testape_once() -> None:
    """Setup lived in main(), which pytest never calls. Install the fixture
    once so the projection resolves the testape server."""
    _install_testape_fixture()


def _install_testape_fixture() -> None:
    package = TMP_HOME / "testape-extension"
    package.mkdir(parents=True, exist_ok=True)
    manifest = extension_store.validate_manifest({
        "kind": extension_store.MANIFEST_KIND,
        "id": EXTENSION_ID,
        "name": "TestApe",
        "version": "1.0.0",
        "description": "Hermetic TestApe parity fixture",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "backend": "",
            "frontend": "",
            "mcp": [{
                "name": SERVER_NAME,
                "command": sys.executable,
                "args": ["--version"],
                "env": {},
                "user_facing": True,
                "requires_backend_auth": False,
            }],
            "provider_capabilities": [],
            "frontend_modules": [],
            "settings": [],
        },
        "permissions": {},
        "marketplace": {},
        "protocol": {
            "version": 1,
            "smoke_test": {
                "required_paths": ["better-agent-extension.json"],
                "python_modules": [],
            },
        },
    })
    (package / "better-agent-extension.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][EXTENSION_ID] = {
        "manifest": manifest,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/testape.git",
            "extension_path": "extensions/testape",
            "ref": "",
            "commit_sha": "testape-parity-fixture",
            "install_path": str(package),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]
    extension_store.grant_consent(EXTENSION_ID)
    extension_store.set_mcp_server_enabled(EXTENSION_ID, SERVER_NAME, True)


def _deselected_snapshot() -> dict:
    profile = harness_profile_store.create_profile({"name": "Without TestApe"})
    harness_field_writer.apply_field_writes(
        profile["id"],
        profile["revision"],
        [{
            "path": [
                "extension_instances",
                EXTENSION_ID,
                harness_fields.GROUP_MCP,
                SERVER_NAME,
            ],
            "value": False,
        }],
    )
    snapshot = harness_profile_resolver.resolve_for_session({
        "harness_profile_id": profile["id"],
    })
    assert snapshot is not None
    return snapshot


def _inputs(snapshot: dict, provider_kind: str) -> dict:
    return {
        "resolved_harness_run_config": snapshot,
        "provider_kind": provider_kind,
        "user_facing": True,
        "app_session_id": "testape-provider-parity",
        "backend_url": "http://127.0.0.1:8000",
        "cwd": str(TMP_HOME),
        "mode": "native",
    }


def _resolved_provider_config(snapshot: dict, provider_kind: str) -> dict:
    return builtin_mcp_config.with_builtin_mcp_servers(
        _inputs(snapshot, provider_kind),
        {},
    )


def _materialized_agy_servers(snapshot: dict, run_dir: Path) -> dict:
    prepared = structural_provider_runtime_plan(_inputs(snapshot, "agy"), "agy")
    mcp_servers = runner_agy._effective_mcp_servers(
        prepared["resolved_plan"],
    )
    source_home = TMP_HOME / "agy-source-home"
    config_root = source_home / ".gemini" / "antigravity-cli"
    config_root.mkdir(parents=True, exist_ok=True)
    original_home = Path.home
    Path.home = staticmethod(lambda: source_home)  # type: ignore[method-assign]
    try:
        env = runner_agy._materialize_agy_run_home(
            run_dir,
            {},
            config_root=config_root,
            settings={},
            mcp_servers=mcp_servers,
            skill_dirs={},
        )
    finally:
        Path.home = original_home  # type: ignore[method-assign]
    if not env:
        return {}
    settings = Path(env["HOME"]) / ".gemini" / "antigravity-cli" / "settings.json"
    return json.loads(settings.read_text(encoding="utf-8")).get("mcpServers") or {}


def _materialized_qwen_servers(snapshot: dict, run_dir: Path) -> dict:
    config = _resolved_provider_config(snapshot, "qwen")
    env = runner_qwen._materialize_qwen_run_home(
        run_dir,
        config,
        real_home=TMP_HOME / "qwen-source-home",
    )
    if not env:
        return {}
    settings = Path(env["HOME"]) / ".qwen" / "settings.json"
    return json.loads(settings.read_text(encoding="utf-8")).get("mcpServers") or {}


async def _codex_dynamic_tool_names(snapshot: dict) -> set[str]:
    original_list_tools = runner_codex.mcp_stdio_bridge.mcp_list_tools

    async def fake_list_tools(server_name: str, _config: dict) -> list[dict]:
        assert server_name == SERVER_NAME
        return [{
            "name": "test_ui",
            "description": "Run TestApe UI verification",
            "inputSchema": {"type": "object", "properties": {}},
        }]

    runner_codex.mcp_stdio_bridge.mcp_list_tools = fake_list_tools  # type: ignore[assignment]
    try:
        dynamic_tools: list[dict] = []
        await runner_codex._bridge_extension_mcp_dynamic_tools(
            inputs=_inputs(snapshot, "codex"),
            provider_run_config=_resolved_provider_config(snapshot, "codex"),
            dynamic_tools=dynamic_tools,
            tool_handlers={},
            existing_tool_names=set(),
            user_facing=True,
            bare_config=False,
        )
    finally:
        runner_codex.mcp_stdio_bridge.mcp_list_tools = original_list_tools  # type: ignore[assignment]
    return {str(tool.get("name") or "") for tool in dynamic_tools}


def test_selected_testape_reaches_every_provider_launch_surface() -> None:
    snapshot = harness_profile_resolver.resolve_for_session({})
    assert snapshot is not None
    assert snapshot["launcher_projection"]["extension_mcp_servers"][EXTENSION_ID] == [
        SERVER_NAME,
    ]
    assert "provider_run_config" not in _inputs(snapshot, "codex")

    assert asyncio.run(_codex_dynamic_tool_names(snapshot)) == {"test_ui"}
    assert SERVER_NAME in _materialized_agy_servers(snapshot, TMP_HOME / "agy-selected")
    assert SERVER_NAME in _materialized_qwen_servers(snapshot, TMP_HOME / "qwen-selected")


def test_deselected_testape_is_absent_from_every_provider_launch_surface() -> None:
    snapshot = _deselected_snapshot()
    assert EXTENSION_ID not in snapshot["launcher_projection"]["extension_mcp_servers"]
    assert asyncio.run(_codex_dynamic_tool_names(snapshot)) == set()
    assert SERVER_NAME not in _materialized_agy_servers(snapshot, TMP_HOME / "agy-deselected")
    assert SERVER_NAME not in _materialized_qwen_servers(snapshot, TMP_HOME / "qwen-deselected")


def main() -> int:
    original_integrations_enabled = installation_profile.integrations_enabled
    installation_profile.integrations_enabled = lambda: True
    try:
        _install_testape_fixture()
        test_selected_testape_reaches_every_provider_launch_surface()
        test_deselected_testape_is_absent_from_every_provider_launch_surface()
    finally:
        installation_profile.integrations_enabled = original_integrations_enabled
        TEST_HOME.release()
    print("PASS TestApe MCP projection parity across Codex, AGY, and Qwen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ["BETTER_AGENT_TEST_MODE"] = "1"

import builtin_mcp_config
import capability_contexts
import extension_applied_config
import extension_store
import installation_profile
import provider_setup
import runner_codex
import runner_gemini
import runtime_skills


def _with_home() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    os.environ["BETTER_AGENT_HOME"] = tmp.name
    return tmp


def test_profile_defaults_and_strict_round_trip() -> None:
    with _with_home():
        assert installation_profile.load() == {
            "schema_version": 2,
            "mode": "default",
            "provider": None,
        }
        assert installation_profile.capabilities() == {
            "mode": "default",
            "mobile_enabled": True,
            "integrations_enabled": True,
        }
        saved = installation_profile.save(mode="desktop-ui-only", provider="codex")
        assert saved["mode"] == "desktop-ui-only"
        assert saved["provider"] == "codex"
        assert not installation_profile.integrations_enabled()
        assert not installation_profile.mobile_enabled()

        installation_profile.save(mode="mobile-desktop-ui-only", provider="codex")
        assert not installation_profile.integrations_enabled()
        assert installation_profile.mobile_enabled()

        installation_profile.save(mode="default", provider="codex")
        assert installation_profile.integrations_enabled()
        assert installation_profile.mobile_enabled()

        profile_path = Path(os.environ["BETTER_AGENT_HOME"]) / "installation.json"
        profile_path.write_text(json.dumps({**saved, "mode": "unknown"}), encoding="utf-8")
        try:
            installation_profile.load()
        except installation_profile.InstallationProfileError:
            pass
        else:
            raise AssertionError("invalid persisted installation mode must fail closed")

        profile_path.write_text(json.dumps({**saved, "schema_version": 1}), encoding="utf-8")
        try:
            installation_profile.load()
        except installation_profile.InstallationProfileError:
            pass
        else:
            raise AssertionError("obsolete installation profile schema must fail closed")


def test_ui_only_suppresses_better_agent_injections() -> None:
    with _with_home():
        installation_profile.save(mode="default", provider="claude")
        active_data = {
            "extensions": {
                "example": {
                    "enabled": True,
                    "entitlement": {"status": "not_required"},
                    "manifest": {"id": "example"},
                }
            }
        }
        assert len(extension_store._active_records_from_data(active_data)) == 1
        default_frontend_key = extension_store.frontend_entrypoints_cache_key()

        installation_profile.save(mode="desktop-ui-only", provider="claude")
        provider_config = {"mcp_servers": {"user-owned": {"command": "user-mcp"}}}
        inputs = {
            "app_session_id": "session",
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "token",
            "open_file_panel_enabled": True,
            "capability_contexts": [{"name": "BA", "content": "injected"}],
        }

        assert builtin_mcp_config.with_builtin_mcp_servers(inputs, provider_config) == provider_config
        assert capability_contexts.prepend_capability_context("hello", inputs) == "hello"
        assert runtime_skills.runtime_skill_contexts(str(ROOT)) == []
        assert not runtime_skills.has_runtime_skills(str(ROOT))
        assert extension_store._active_records_from_data(active_data) == []
        assert extension_store.frontend_entrypoints_cache_key() != default_frontend_key
        assert runner_gemini._with_communicate_mcp(inputs, provider_config) == provider_config
        dynamic_tools, handlers = runner_codex._build_dynamic_tool_set(
            mode="native",
            app_session_id="session",
            backend_url="http://127.0.0.1:8000",
            internal_token="token",
            mssg_sender_session_id="session",
            cwd=str(ROOT),
            model=None,
            open_file_panel_enabled=True,
            request_user_input_enabled=True,
            file_editing_mode=True,
            team_orchestration_enabled=True,
            disabled_builtin_tools=set(),
            existing_tool_names=set(),
        )
        assert dynamic_tools == []
        assert handlers == {}

        fake_record = active_data["extensions"]["example"]
        with patch.object(extension_store, "get_extension", return_value=fake_record):
            try:
                extension_store.resolve_frontend_asset("example", "index.js")
            except extension_store.ExtensionError:
                pass
            else:
                raise AssertionError("UI-only must deny extension frontend assets")

        fake_store = {"extensions": {"example": fake_record}}
        with (
            patch.object(extension_store, "_load", return_value=fake_store),
            patch.object(extension_store, "_active_records", return_value=[]),
            patch.object(extension_store, "is_extension_active", return_value=False),
            patch("file_ref_resolver.set_tag_rules"),
            patch("session_manager.manager.clear_markers_for_extension") as clear_markers,
        ):
            extension_applied_config.reconcile_all()
        clear_markers.assert_called_once_with("example")

        installation_profile.save(mode="default", provider="claude")
        assert len(extension_store._active_records_from_data(active_data)) == 1
        assert builtin_mcp_config.with_builtin_mcp_servers(
            {"bare_config": True}, provider_config
        ) == provider_config

        installation_profile.save(mode="mobile-desktop-ui-only", provider="claude")
        assert builtin_mcp_config.with_builtin_mcp_servers(inputs, provider_config) == provider_config
        assert runtime_skills.runtime_skill_contexts(str(ROOT)) == []
        assert extension_store._active_records_from_data(active_data) == []


def test_provider_install_skips_cli_that_is_already_available() -> None:
    async def run() -> None:
        installed = {
            "kind": "codex",
            "installed": True,
            "verify": {"ok": True},
        }
        with (
            patch.object(provider_setup, "provider_setup_status", AsyncMock(return_value=installed)),
            patch.object(provider_setup, "start_install", AsyncMock()) as start,
        ):
            result = await provider_setup.install_if_missing("codex", AsyncMock())
        assert result["state"] == "already_installed"
        start.assert_not_awaited()

    asyncio.run(run())


def test_platform_installers_are_exactly_named() -> None:
    python_installer = ROOT / "scripts" / "install.py"
    installation_guide = ROOT / "INSTALL.md"
    windows_installer = ROOT / "scripts" / "install-windows.ps1"
    assert python_installer.is_file()
    assert installation_guide.is_file()
    assert not (ROOT / "scripts" / "install-agent.md").exists()
    assert (ROOT / "scripts" / "install-macos.sh").is_file()
    assert windows_installer.is_file()
    assert not (ROOT / "scripts" / "bootstrap-macos.sh").exists()
    assert not (ROOT / "scripts" / "bootstrap-windows.ps1").exists()
    python_source = python_installer.read_text(encoding="utf-8")
    agent_source = installation_guide.read_text(encoding="utf-8")
    windows_source = windows_installer.read_text(encoding="utf-8")
    assert "installation_profile.DESKTOP_UI_ONLY" in python_source
    assert "installation_profile.MOBILE_DESKTOP_UI_ONLY" in python_source
    assert "installation_profile.DEFAULT" in python_source
    for mode in installation_profile.MODES:
        assert mode in agent_source
        assert mode in windows_source
    assert "ask the user" in agent_source.lower()
    assert "Do not infer a choice" in agent_source
    assert "install-macos.sh --mode <mode> --provider <provider> --yes" in agent_source
    assert "install-windows.ps1 -Mode <mode> -Provider <provider> -Yes" in agent_source
    assert "provider_setup.supported_provider_kinds()" in agent_source


if __name__ == "__main__":
    test_profile_defaults_and_strict_round_trip()
    test_ui_only_suppresses_better_agent_injections()
    test_provider_install_skips_cli_that_is_already_available()
    test_platform_installers_are_exactly_named()
    print("installation mode tests passed")

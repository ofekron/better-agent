from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
SDK = ROOT / "sdk"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

os.environ["BETTER_AGENT_TEST_MODE"] = "1"

import _test_installation
import builtin_mcp_config
import capability_contexts
import extension_applied_config
import extension_store
import installation_admission
import installation_profile
import provider_setup
import runner_codex
import runner_gemini
import runtime_skills


@contextmanager
def _with_home():
    previous_home = os.environ.get("BETTER_AGENT_HOME")
    previous_backend = installation_profile.BACKEND_ROOT
    with tempfile.TemporaryDirectory(prefix="ba-install-modes-") as tmp:
        root = Path(tmp)
        os.environ["BETTER_AGENT_HOME"] = tmp
        installation_profile.BACKEND_ROOT = root / "backend"
        try:
            yield root
        finally:
            installation_profile.BACKEND_ROOT = previous_backend
            if previous_home is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = previous_home


def _activate(root: Path, mode: str, provider: str = "codex") -> dict:
    return _test_installation.activate(root, mode=mode, provider=provider)


def test_missing_legacy_malformed_and_interrupted_profiles_require_setup() -> None:
    legacy_profiles = [
        {
            "schema_version": 2,
            "mode": "desktop-ui-only",
            "provider": "codex",
        },
        {
            "schema_version": 2,
            "mode": "default",
            "provider": "codex",
        },
    ]
    with _with_home() as root:
        profile_path = root / "installation.json"

        assert installation_profile.load()["status"] == "setup_required"
        assert installation_profile.capabilities()["setup_required"] is True

        for value in legacy_profiles:
            profile_path.write_text(json.dumps(value), encoding="utf-8")
            assert installation_profile.load()["status"] == "setup_required"
            assert not installation_profile.provider_conversations_enabled()

        for raw in ("{", "[]", json.dumps({"schema_version": 3})):
            profile_path.write_text(raw, encoding="utf-8")
            assert installation_profile.load()["status"] == "setup_required"
            assert not installation_profile.integrations_enabled()

        profile_path.unlink()
        (root / ".installation.json.interrupted.tmp").write_text(
            json.dumps(legacy_profiles[1]),
            encoding="utf-8",
        )
        assert installation_profile.load()["status"] == "setup_required"


def test_activation_receipt_binds_profile_environment_and_selection() -> None:
    with _with_home() as root:
        profile = _activate(root, installation_profile.DEFAULT)
        assert installation_profile.capabilities() == {
            "status": "active",
            "setup_required": False,
            "mode": "default",
            "provider_conversations_enabled": True,
            "mobile_enabled": True,
            "integrations_enabled": True,
        }

        receipt_path = root / "installation-activation.json"
        original = receipt_path.read_text(encoding="utf-8")
        receipt = json.loads(original)

        stripped = {
            key: value
            for key, value in receipt.items()
            if key != "provider_selection_sha256"
        }
        receipt_path.write_text(json.dumps(stripped), encoding="utf-8")
        assert installation_profile.capabilities()["setup_required"] is True

        receipt_path.write_text(
            json.dumps({**receipt, "provider_selection_sha256": "not-a-hash"}),
            encoding="utf-8",
        )
        assert installation_profile.capabilities()["setup_required"] is True

        receipt_path.write_text(original, encoding="utf-8")
        assert installation_profile.capabilities()["setup_required"] is False

        receipt_path.write_text(
            json.dumps({**receipt, "generation": "different"}),
            encoding="utf-8",
        )
        assert installation_profile.capabilities()["setup_required"] is True

        installation_profile.stage_activation(profile)
        assert installation_profile.capabilities()["setup_required"] is True


def test_provider_config_changes_do_not_invalidate_activation() -> None:
    with _with_home() as root:
        _activate(root, installation_profile.DEFAULT, provider="codex")
        active = {
            "status": "active",
            "setup_required": False,
            "mode": "default",
            "provider_conversations_enabled": True,
            "mobile_enabled": True,
            "integrations_enabled": True,
        }
        config_path = root / "config.json"
        state = json.loads(config_path.read_text(encoding="utf-8"))
        state["providers"][0]["suspended"] = True
        state["providers"].append({
            "id": "claude-id",
            "kind": "claude",
            "suspended": False,
        })
        state["default_provider_id"] = "claude-id"
        config_path.write_text(json.dumps(state), encoding="utf-8")

        assert installation_profile.capabilities() == active
        assert not installation_profile.selection_pending()


async def _admit(
    path: str,
    *,
    scope_type: str = "http",
) -> tuple[bool, list[dict]]:
    called = False
    sent: list[dict] = []

    async def app(_scope, _receive, _send):
        nonlocal called
        called = True

    async def receive():
        return {"type": f"{scope_type}.request"}

    async def send(message):
        sent.append(message)

    middleware = installation_admission.InstallationAdmissionMiddleware(app)
    await middleware(
        {"type": scope_type, "path": path, "method": "GET"},
        receive,
        send,
    )
    return called, sent


def test_authoritative_admission_rejects_before_side_effects() -> None:
    async def run() -> None:
        with _with_home():
            called, messages = await _admit("/api/sessions")
            assert not called
            assert messages[0]["status"] == 503

            called, _ = await _admit("/api/installation-profile")
            assert called
            called, _ = await _admit("/assets/index.js")
            assert called
            called, messages = await _admit("/api/provider-setup/install")
            assert not called
            assert messages[0]["status"] == 503

            for prefix in installation_admission._INTEGRATION_PREFIXES:
                called, messages = await _admit(f"{prefix}/probe")
                assert not called, prefix
                assert messages[0]["status"] == 404
            called, messages = await _admit(
                "/api/internal/sessions/example/capabilities"
            )
            assert not called
            assert messages[0]["status"] == 404

            for prefix in installation_admission._MOBILE_PREFIXES:
                called, messages = await _admit(f"{prefix}/probe")
                assert not called, prefix
                assert messages[0]["status"] == 404

            called, messages = await _admit("/ws/chat", scope_type="websocket")
            assert not called
            assert messages == [{
                "type": "websocket.close",
                "code": 1008,
                "reason": "installation capability unavailable",
            }]

    asyncio.run(run())


def test_mode_matrix_uses_one_policy_for_discovery_and_authorization() -> None:
    async def run() -> None:
        cases = (
            (installation_profile.DESKTOP_UI_ONLY, True, False, False),
            (installation_profile.MOBILE_DESKTOP_UI_ONLY, True, True, False),
            (installation_profile.DEFAULT, True, True, True),
        )
        for mode, conversations, mobile, integrations in cases:
            with _with_home() as root:
                _activate(root, mode)
                discovery = installation_profile.capabilities()
                assert discovery["provider_conversations_enabled"] is conversations
                assert discovery["mobile_enabled"] is mobile
                assert discovery["integrations_enabled"] is integrations

                called, _ = await _admit("/api/sessions")
                assert called is conversations
                called, _ = await _admit("/api/mobile/status")
                assert called is mobile
                called, _ = await _admit("/api/extensions")
                assert called is integrations
                called, _ = await _admit("/ws/chat", scope_type="websocket")
                assert called is conversations

    asyncio.run(run())


def test_ui_only_suppresses_better_agent_injections() -> None:
    with _with_home() as root:
        _activate(root, installation_profile.DESKTOP_UI_ONLY, "claude")
        active_data = {
            "extensions": {
                "example": {
                    "enabled": True,
                    "entitlement": {"status": "not_required"},
                    "manifest": {"id": "example"},
                }
            }
        }
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


def test_platform_installers_share_transactional_activation() -> None:
    python_installer = ROOT / "scripts" / "install.py"
    installation_guide = ROOT / "INSTALL.md"
    windows_installer = ROOT / "scripts" / "install-windows.ps1"
    macos_installer = ROOT / "scripts" / "install-macos.sh"
    python_source = python_installer.read_text(encoding="utf-8")
    agent_source = installation_guide.read_text(encoding="utf-8")
    windows_source = windows_installer.read_text(encoding="utf-8")
    macos_source = macos_installer.read_text(encoding="utf-8")

    assert python_installer.is_file()
    assert installation_guide.is_file()
    assert macos_installer.is_file()
    assert windows_installer.is_file()
    for mode in installation_profile.MODES:
        assert mode in agent_source
        assert mode in windows_source
    assert "dependency_plan.activation_lock()" in python_source
    assert "verified_provider_identity" in python_source
    assert "prepare_installation" in python_source
    assert "activate_prepared_installation" in python_source
    assert "dependency_plan.py\" active" in macos_source
    assert "dependency_plan.py\") active" in windows_source
    assert "install-bagent.sh" in macos_source
    assert "bagent.cmd" in windows_source


if __name__ == "__main__":
    test_missing_legacy_malformed_and_interrupted_profiles_require_setup()
    test_activation_receipt_binds_profile_environment_and_selection()
    test_provider_config_changes_do_not_invalidate_activation()
    test_authoritative_admission_rejects_before_side_effects()
    test_mode_matrix_uses_one_policy_for_discovery_and_authorization()
    test_ui_only_suppresses_better_agent_injections()
    test_provider_install_skips_cli_that_is_already_available()
    test_platform_installers_share_transactional_activation()
    print("installation mode tests passed")

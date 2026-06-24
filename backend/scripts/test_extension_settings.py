"""Tests for extension settings + per-MCP-server enable/disable: manifest
settings schema, value storage, secret keychain routing + redaction, MCP
injection filtering, and the SDK Setting builder / get_settings surface."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-extension-settings-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))

import extension_store  # noqa: E402
import builtin_mcp_config  # noqa: E402
import better_agent_sdk as sdk  # noqa: E402
import config_store  # noqa: E402


class _FakeKeychain:
    """In-memory stand-in for password_manager so tests never touch the real
    OS keychain."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def store_service_password(self, payload: dict) -> dict:
        self.entries[(payload["service"], payload["account"])] = payload["password"]
        return {"ok": True}

    def has_service_password(self, service: str, account: str) -> bool:
        return (service, account) in self.entries

    def get_service_password(self, service: str, account: str) -> str:
        return self.entries.get((service, account), "")

    def delete_service_password(self, payload: dict) -> dict:
        self.entries.pop((payload["service"], payload["account"]), None)
        return {"ok": True}


_FAKE_RECORD = {
    "manifest": {
        "id": "ofek.demo",
        "name": "Demo",
        "entrypoints": {
            "settings": [
                {"key": "token", "label": "Token", "type": "secret"},
                {"key": "refresh", "label": "Refresh", "type": "number", "default": 60},
                {"key": "mode", "label": "Mode", "type": "string", "default": "auto", "enum": ["auto", "manual"]},
                {"key": "verbose", "label": "Verbose", "type": "boolean", "default": False},
            ]
        },
    },
    "source": {"type": "git"},
}


def _with_fake_extension(keychain: _FakeKeychain):
    """Patch get_extension to a synthetic settings-declaring record and route
    secrets through the in-memory keychain. Returns the restore callable."""
    real_get = extension_store.get_extension
    real_pm = extension_store.password_manager
    extension_store.get_extension = lambda extension_id: _FAKE_RECORD if extension_id == "ofek.demo" else real_get(extension_id)  # type: ignore[assignment]
    extension_store.password_manager = keychain  # type: ignore[assignment]

    def restore() -> None:
        extension_store.get_extension = real_get  # type: ignore[assignment]
        extension_store.password_manager = real_pm  # type: ignore[assignment]

    return restore


def _base_manifest() -> dict:
    return {
        "kind": extension_store.MANIFEST_KIND,
        "id": "ofek.demo",
        "name": "Demo",
        "version": "1.0.0",
        "surfaces": ["frontend_feature"],
        "entrypoints": {},
        "permissions": {},
    }


def test_settings_schema_accept_and_reject() -> None:
    manifest = _base_manifest()
    manifest["entrypoints"] = {
        "settings": [
            {"key": "token", "label": "Token", "type": "secret"},
            {"key": "refresh", "label": "Refresh", "type": "number", "default": 60},
            {"key": "mode", "label": "Mode", "type": "string", "default": "auto", "enum": ["auto", "manual"]},
            {"key": "verbose", "label": "Verbose", "type": "boolean", "default": False},
        ]
    }
    v = extension_store.validate_manifest(manifest)
    by_key = {s["key"]: s for s in v["entrypoints"]["settings"]}
    assert by_key["refresh"]["default"] == 60
    assert by_key["mode"]["enum"] == ["auto", "manual"]

    def expect_err(settings: list, marker: str) -> None:
        m = _base_manifest()
        m["entrypoints"] = {"settings": settings}
        try:
            extension_store.validate_manifest(m)
            raise AssertionError(f"expected rejection for {marker}")
        except extension_store.ExtensionError:
            pass

    expect_err([{"key": "BAD", "label": "x", "type": "string"}], "invalid key")
    expect_err([{"key": "a", "label": "x"}, {"key": "a", "label": "y"}], "duplicate key")
    expect_err([{"key": "a", "label": "x", "type": "widget"}], "bad type")
    expect_err([{"key": "a", "label": "x", "type": "secret", "enum": ["x"]}], "enum on secret")
    expect_err([{"key": "a", "label": "x", "type": "number", "default": "no"}], "wrong default type")


def test_non_secret_settings_stored_with_defaults() -> None:
    restore = _with_fake_extension(_FakeKeychain())
    try:
        result = extension_store.get_extension_settings("ofek.demo")
        assert result["values"]["refresh"] == 60  # default
        assert result["values"]["mode"] == "auto"
        assert result["values"]["verbose"] is False
        extension_store.set_extension_setting("ofek.demo", "refresh", 120)
        extension_store.set_extension_setting("ofek.demo", "mode", "manual")
        extension_store.set_extension_setting("ofek.demo", "verbose", True)
        result = extension_store.get_extension_settings("ofek.demo")
        assert result["values"]["refresh"] == 120
        assert result["values"]["mode"] == "manual"
        assert result["values"]["verbose"] is True
    finally:
        restore()


def test_secret_routed_to_keychain_and_redacted() -> None:
    keychain = _FakeKeychain()
    restore = _with_fake_extension(keychain)
    try:
        # GET before setting: secret absent, value None (never returned)
        result = extension_store.get_extension_settings("ofek.demo")
        assert result["values"]["token"] is None
        assert result["secret_present"]["token"] is False
        # SET routes to the keychain, never to the JSON store
        extension_store.set_extension_setting("ofek.demo", "token", "supersecret")
        assert keychain.entries[(extension_store._SETTING_SECRET_SERVICE, "ofek.demo/token")] == "supersecret"
        # GET still never returns the value, only the presence flag
        result = extension_store.get_extension_settings("ofek.demo")
        assert result["values"]["token"] is None
        assert result["secret_present"]["token"] is True
        # resolve_all_settings (SDK path) DOES resolve the secret server-side
        resolved = extension_store.resolve_all_settings("ofek.demo")
        assert resolved["token"] == "supersecret"
    finally:
        restore()


def test_secret_clear_and_unknown_key_rejected() -> None:
    keychain = _FakeKeychain()
    restore = _with_fake_extension(keychain)
    try:
        extension_store.set_extension_setting("ofek.demo", "token", "abc")
        assert keychain.has_service_password(extension_store._SETTING_SECRET_SERVICE, "ofek.demo/token")
        extension_store.set_extension_setting("ofek.demo", "token", "")  # empty clears
        assert not keychain.has_service_password(extension_store._SETTING_SECRET_SERVICE, "ofek.demo/token")
        try:
            extension_store.set_extension_setting("ofek.demo", "bogus", 1)
            raise AssertionError("expected rejection for unknown key")
        except extension_store.ExtensionError:
            pass
    finally:
        restore()


def test_mcp_toggle_filters_builtin_injection() -> None:
    # Seed store so built-ins (project-structure) are present + the required
    # marketplace check is satisfied without network.
    store_path = Path(_TMP_HOME) / "extensions" / "extensions.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "schema_version": extension_store.STORE_SCHEMA_VERSION,
                "extensions": {
                    extension_store.MARKETPLACE_EXTENSION_ID: {
                        "manifest": {
                            "kind": extension_store.MANIFEST_KIND,
                            "id": extension_store.MARKETPLACE_EXTENSION_ID,
                            "name": "Marketplace",
                            "version": "1.0.0",
                            "surfaces": ["backend_feature"],
                            "entrypoints": {"backend": "", "frontend": "", "mcp": [], "provider_capabilities": []},
                            "permissions": {},
                            "marketplace": {},
                        },
                        "enabled": True,
                        "installed_at": "1970",
                        "updated_at": "1970",
                        "source": {"type": "private_placeholder"},
                        "entitlement": {"status": "not_required"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    package = Path(_TMP_HOME) / "private-fixtures" / "scheduler"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": "ofek.scheduler",
        "name": "Scheduler",
        "version": "1.0.0",
        "description": "Scheduler",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [
                {
                    "name": "scheduler",
                    "python": "mcp/server.py",
                    "user_facing": True,
                    "bare_allowed": False,
                    "requires_backend_auth": True,
                }
            ]
        },
        "permissions": {"internal_loopback": True},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('scheduler')\n", encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": "scheduler",
            "ref": "",
            "commit_sha": "scheduler-private",
        },
        persist=True,
    )
    extension_store.set_harness_delivery_mode("ofek.scheduler", "runtime")
    inputs = {
        "app_session_id": "s1",
        "backend_url": "http://localhost:8000",
        "internal_token": "tok",
        "cwd": "/tmp",
        "mode": "native",
        "open_file_panel_enabled": True,
    }
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["default_session"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)
    cfg_on = builtin_mcp_config.with_builtin_mcp_servers(inputs, {"mcp_servers": {}})
    assert "scheduler" in cfg_on["mcp_servers"]

    extension_store.set_mcp_server_enabled(
        "ofek.scheduler", "scheduler", False
    )
    cfg_off = builtin_mcp_config.with_builtin_mcp_servers(inputs, {"mcp_servers": {}})
    assert "scheduler" not in cfg_off["mcp_servers"]

    # servers listing reflects the toggle
    servers = extension_store.extension_mcp_servers("ofek.scheduler")
    assert next(s for s in servers if s["name"] == "scheduler")["enabled"] is False

    extension_store.set_mcp_server_enabled(
        "ofek.scheduler", "scheduler", True
    )
    cfg_re = builtin_mcp_config.with_builtin_mcp_servers(inputs, {"mcp_servers": {}})
    assert "scheduler" in cfg_re["mcp_servers"]


def test_sdk_setting_builder_and_read_surface() -> None:
    settings = [
        sdk.Setting(key="token", label="Token", type="secret").to_dict(),
        sdk.Setting(key="refresh", label="Refresh", type="number", default=60).to_dict(),
        sdk.Setting(key="mode", label="Mode", type="string", default="auto", enum=("auto", "manual")).to_dict(),
    ]
    manifest = _base_manifest()
    manifest["entrypoints"] = {"settings": settings}
    v = extension_store.validate_manifest(manifest)
    assert {s["key"] for s in v["entrypoints"]["settings"]} == {"token", "refresh", "mode"}
    assert callable(sdk.Client.get_settings)
    assert callable(sdk.Client.get_setting)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all extension-settings tests passed")

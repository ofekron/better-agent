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
import installation_profile  # noqa: E402
import _test_installation  # noqa: E402

# Bare test homes have no installation.json, so _record_active() would gate
# every installed-extension assertion here on integrations being off. Same
# monkeypatch test_native_mcp_grant_wiring.py and
# test_cross_provider_harness_parity.py already use.
installation_profile.integrations_enabled = lambda: True


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

# Derived, not spelled out: the account format is production's to choose (it
# uses ":" because password_manager rejects "/" in an account).
_TOKEN_ACCOUNT = extension_store._setting_secret_account("ofek.demo", "token")


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


def test_ambient_native_mcp_manifest_requires_stateless_opt_in() -> None:
    manifest = _base_manifest()
    manifest["surfaces"] = ["runtime_mcp"]
    manifest["entrypoints"] = {"mcp": [{
        "name": "search-index",
        "command": "search-index",
        "user_facing": False,
        "requires_backend_auth": False,
        "ambient_native": True,
    }]}
    item = extension_store.validate_manifest(manifest)["entrypoints"]["mcp"][0]
    assert item["ambient_native"] is True

    for unsafe in (
        {"user_facing": True, "requires_backend_auth": False},
        {"user_facing": False, "requires_backend_auth": True},
        {"user_facing": False, "requires_backend_auth": False, "predicate": {"nonempty": ["app_session_id"]}},
    ):
        rejected = _base_manifest()
        rejected["surfaces"] = ["runtime_mcp"]
        rejected["entrypoints"] = {"mcp": [{
            "name": "search-index",
            "command": "search-index",
            "ambient_native": True,
            **unsafe,
        }]}
        try:
            extension_store.validate_manifest(rejected)
            raise AssertionError("unsafe ambient-native MCP manifest was accepted")
        except extension_store.ExtensionError:
            pass


def test_ambient_native_mcp_manifest_accepts_launcher_auth_opt_in() -> None:
    manifest = _base_manifest()
    manifest["surfaces"] = ["runtime_mcp"]
    manifest["entrypoints"] = {"mcp": [{
        "name": "coordination",
        "command": "coordination",
        "user_facing": True,
        "requires_backend_auth": True,
        "ambient_native": True,
        "ambient_auth": "launcher",
    }]}
    item = extension_store.validate_manifest(manifest)["entrypoints"]["mcp"][0]
    assert item["ambient_native"] is True
    assert item["ambient_auth"] == "launcher"

    for unsafe in (
        # ambient_auth is meaningless without ambient_native
        {"ambient_native": False, "ambient_auth": "launcher"},
        # unknown auth mode must fail closed, not be coerced
        {"ambient_native": True, "ambient_auth": "none-at-all"},
        # a predicate is never ambient-resolvable, opt-in or not
        {
            "ambient_native": True,
            "ambient_auth": "launcher",
            "predicate": {"nonempty": ["app_session_id"]},
        },
    ):
        rejected = _base_manifest()
        rejected["surfaces"] = ["runtime_mcp"]
        rejected["entrypoints"] = {"mcp": [{
            "name": "coordination",
            "command": "coordination",
            **unsafe,
        }]}
        try:
            extension_store.validate_manifest(rejected)
            raise AssertionError("unsafe ambient_auth manifest was accepted")
        except extension_store.ExtensionError:
            pass


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
        assert keychain.entries[(extension_store._SETTING_SECRET_SERVICE, _TOKEN_ACCOUNT)] == "supersecret"
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
        assert keychain.has_service_password(extension_store._SETTING_SECRET_SERVICE, _TOKEN_ACCOUNT)
        extension_store.set_extension_setting("ofek.demo", "token", "")  # empty clears
        assert not keychain.has_service_password(extension_store._SETTING_SECRET_SERVICE, _TOKEN_ACCOUNT)
        try:
            extension_store.set_extension_setting("ofek.demo", "bogus", 1)
            raise AssertionError("expected rejection for unknown key")
        except extension_store.ExtensionError:
            pass
    finally:
        restore()


def test_schema_one_settings_migrate_without_losing_user_data() -> None:
    settings_path = extension_store._ext_settings_path()  # type: ignore[attr-defined]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({
            "schema_version": 1,
            "extensions": {
                "ofek.demo": {
                    "values": {"refresh": 120, "mode": "manual", "verbose": True},
                    "mcp_disabled": ["demo-server"],
                    "frontend_modules_disabled": ["input-overflow-menu/demo"],
                    "user_instructions": "prefer staging",
                },
            },
        }),
        encoding="utf-8",
    )
    restore = _with_fake_extension(_FakeKeychain())
    try:
        result = extension_store.get_extension_settings("ofek.demo")
        assert result["values"]["refresh"] == 120
        assert result["values"]["mode"] == "manual"
        assert result["values"]["verbose"] is True
        migrated = json.loads(settings_path.read_text(encoding="utf-8"))
        entry = migrated["extensions"]["ofek.demo"]
        assert migrated["schema_version"] == extension_store._EXT_SETTINGS_SCHEMA_VERSION  # type: ignore[attr-defined]
        assert entry["mcp_disabled"] == ["demo-server"]
        assert entry["frontend_modules_disabled"] == ["input-overflow-menu/demo"]
        assert entry["native_harness"] == []
        assert entry["user_instructions"] == "prefer staging"
        second = extension_store._load_ext_settings()  # type: ignore[attr-defined]
        assert second == migrated
        assert not list(settings_path.parent.glob("extension-settings.incompatible-*.json"))
    finally:
        restore()
        settings_path.unlink(missing_ok=True)


def test_builtin_harness_instructions_default_to_native_exposed_on_migration() -> None:
    extension_id = extension_store.BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID
    settings_path = extension_store._ext_settings_path()  # type: ignore[attr-defined]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({
            "schema_version": 2,
            "extensions": {
                extension_id: {
                    "values": {},
                    "mcp_disabled": [],
                    "frontend_modules_disabled": [],
                    "native_harness": [],
                },
                "ofek.demo": {
                    "values": {},
                    "mcp_disabled": [],
                    "frontend_modules_disabled": [],
                    "native_harness": [],
                },
            },
        }),
        encoding="utf-8",
    )
    try:
        migrated = extension_store._load_ext_settings()  # type: ignore[attr-defined]
        assert migrated["schema_version"] == extension_store._EXT_SETTINGS_SCHEMA_VERSION  # type: ignore[attr-defined]
        assert migrated["extensions"][extension_id]["native_harness"] == [
            "instructions:better-agent-harness-behavior"
        ]
        assert migrated["extensions"]["ofek.demo"]["native_harness"] == []
    finally:
        settings_path.unlink(missing_ok=True)


def test_malformed_schema_one_extensions_rejected() -> None:
    settings_path = extension_store._ext_settings_path()  # type: ignore[attr-defined]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"schema_version": 1, "extensions": []}),
        encoding="utf-8",
    )
    try:
        extension_store._load_ext_settings()  # type: ignore[attr-defined]
        raise AssertionError("malformed schema one settings were migrated")
    except extension_store.ExtensionError as exc:
        assert "extensions must be an object" in str(exc)
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "extensions": [],
    }
    settings_path.unlink(missing_ok=True)


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
    package.mkdir(parents=True, exist_ok=True)
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
                    # A command entrypoint keeps this test on the toggle it
                    # actually covers: a `python` entrypoint would resolve
                    # through dependency_plan.assert_active_plan, whose hash
                    # folds in the BETTER_AGENT_HOME-installed requirements
                    # while its marker lives in the shared repo venv -- so an
                    # isolated test home always reads as stale.
                    "name": "scheduler",
                    "command": "scheduler-stub",
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
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": "scheduler",
            "ref": "",
            "commit_sha": "scheduler-private",
        },
        # Installing no longer starts an extension: fresh installs land
        # disabled and the user turns them on. This fixture arrives enabled so
        # the assertions below stay about the per-MCP-server toggle rather
        # than the install-enable policy.
        default_enabled=True,
        persist=True,
    )
    inputs = {
        "app_session_id": "s1",
        "backend_url": "http://localhost:8000",
        "internal_token": "tok",
        "cwd": "/tmp",
        "mode": "native",
        "user_facing": True,
    }
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["default_session"] = _test_installation.default_llm_assignment(
        provider["id"]
    )
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


def test_user_instructions_save_load_clear() -> None:
    """Per-extension user instructions round-trip: empty by default, trimmed on
    save, cleared when set to whitespace/empty."""
    restore = _with_fake_extension(_FakeKeychain())
    try:
        assert extension_store.get_user_instructions("ofek.demo") == ""
        saved = extension_store.set_user_instructions("ofek.demo", "  prefer staging  ")
        assert saved == "prefer staging"  # trimmed
        assert extension_store.get_user_instructions("ofek.demo") == "prefer staging"
        # extension_config surfaces it for the Settings UI.
        assert extension_store.extension_config("ofek.demo")["user_instructions"] == "prefer staging"
        # Whitespace-only clears it.
        assert extension_store.set_user_instructions("ofek.demo", "   ") == ""
        assert extension_store.get_user_instructions("ofek.demo") == ""
    finally:
        restore()


def test_user_instructions_validation() -> None:
    """Over-length text is rejected; non-string is rejected; None coerces to
    an empty (cleared) value."""
    restore = _with_fake_extension(_FakeKeychain())
    try:
        too_long = "x" * (extension_store._USER_INSTRUCTIONS_MAX_CHARS + 1)
        try:
            extension_store.set_user_instructions("ofek.demo", too_long)
            raise AssertionError("expected rejection for over-length instructions")
        except extension_store.ExtensionError:
            pass
        try:
            extension_store.set_user_instructions("ofek.demo", 123)  # type: ignore[arg-type]
            raise AssertionError("expected rejection for non-string instructions")
        except extension_store.ExtensionError:
            pass
        # None is allowed and clears.
        assert extension_store.set_user_instructions("ofek.demo", None) == ""
    finally:
        restore()


def test_user_instruction_contexts_active_filtering_and_shape() -> None:
    """The injected capability-context block carries only ACTIVE +
    runtime-ready extensions with non-empty instructions, in the
    provider-uniform shape; bare_config suppresses it entirely."""
    record = {
        "manifest": {"id": "ofek.demo", "name": "Demo", "entrypoints": {}},
        "source": {"type": "git"},
        "enabled": True,
    }
    real_get = extension_store.get_extension
    real_list = extension_store.list_extensions
    real_active = extension_store._record_active
    real_ready = extension_store._record_runtime_ready
    extension_store.get_extension = lambda eid: record if eid == "ofek.demo" else real_get(eid)  # type: ignore[assignment]
    extension_store.list_extensions = lambda **_kw: [record]  # type: ignore[assignment]
    extension_store._record_active = lambda r: True  # type: ignore[assignment]
    extension_store._record_runtime_ready = lambda r: True  # type: ignore[assignment]
    try:
        # No instructions yet → no block.
        assert extension_store.user_instruction_contexts() == []

        extension_store.set_user_instructions("ofek.demo", "always ask before deleting")
        blocks = extension_store.user_instruction_contexts()
        assert len(blocks) == 1
        block = blocks[0]
        assert block["category"] == "instructions"
        assert block["content_kind"] == "extension_user_instructions"
        assert "Demo (ofek.demo)" in block["content"]
        assert "always ask before deleting" in block["content"]

        # bare_config suppresses entirely.
        assert extension_store.user_instruction_contexts(bare_config=True) == []

        # Inactive extension contributes nothing.
        extension_store._record_active = lambda r: False  # type: ignore[assignment]
        assert extension_store.user_instruction_contexts() == []
        extension_store._record_active = lambda r: True  # type: ignore[assignment]

        # Not-runtime-ready contributes nothing.
        extension_store._record_runtime_ready = lambda r: False  # type: ignore[assignment]
        assert extension_store.user_instruction_contexts() == []
    finally:
        # Clear residue so order-dependent sibling tests start clean (tests
        # share one temp home).
        extension_store._record_active = lambda r: True  # type: ignore[assignment]
        extension_store._record_runtime_ready = lambda r: True  # type: ignore[assignment]
        extension_store.set_user_instructions("ofek.demo", "")
        extension_store.get_extension = real_get  # type: ignore[assignment]
        extension_store.list_extensions = real_list  # type: ignore[assignment]
        extension_store._record_active = real_active  # type: ignore[assignment]
        extension_store._record_runtime_ready = real_ready  # type: ignore[assignment]


def test_native_harness_exposure_is_per_item_and_unsafe_mcp_fails_closed() -> None:
    record = {
        "manifest": {
            "id": "ofek.demo",
            "name": "Demo",
            "entrypoints": {
                "instructions": [{"name": "rules", "path": "instructions/rules.md", "level": "global"}],
                "skills": [{"name": "reviewer", "path": "skills/reviewer"}],
                "mcp": [
                    {
                        "name": "local-search",
                        "command": "local-search",
                        "args": [],
                        "env": {},
                        "user_facing": False,
                        "requires_backend_auth": False,
                        "ambient_native": True,
                        "predicate": {},
                    },
                    {
                        "name": "session-control",
                        "command": "session-control",
                        "args": [],
                        "env": {},
                        "user_facing": False,
                        "requires_backend_auth": True,
                        "ambient_native": False,
                        "predicate": {},
                    },
                ],
            },
        },
        "source": {"type": "git"},
        "enabled": True,
    }
    real_get = extension_store.get_extension
    real_skills = extension_store.reconcile_runtime_skills
    extension_store.get_extension = lambda eid: record if eid == "ofek.demo" else real_get(eid)  # type: ignore[assignment]
    extension_store.reconcile_runtime_skills = lambda: 0  # type: ignore[assignment]
    try:
        for kind, name in (("instructions", "rules"), ("skill", "reviewer")):
            assert extension_store.native_harness_exposed("ofek.demo", kind, name, record=record) is False
            assert extension_store.set_native_harness_exposed("ofek.demo", kind, name, True) is True
            assert extension_store.native_harness_exposed("ofek.demo", kind, name, record=record) is True

        additions = {(item["kind"], item["name"]): item for item in extension_store.extension_harness_additions(record)}
        assert additions[("instructions", "rules")]["native_exposed"] is True
        assert additions[("skill", "reviewer")]["native_exposed"] is True
        assert additions[("mcp", "local-search")]["native_eligible"] is True
        assert additions[("mcp", "local-search")]["native_exposed"] is False
        assert additions[("mcp", "session-control")]["native_eligible"] is False
        assert additions[("mcp", "session-control")]["native_exposed"] is False

        # kind="mcp" routes through grant_native_mcp_server(), which fails
        # closed here for both entrypoints: neither declares
        # permissions.native_mcp (and this record has no resolvable
        # install_path fingerprint), so no grant is ever created.
        for name in ("local-search", "session-control"):
            try:
                extension_store.set_native_harness_exposed("ofek.demo", "mcp", name, True)
                raise AssertionError(f"kind='mcp' exposure must fail closed for undeclared servers ({name})")
            except extension_store.ExtensionError:
                pass
        try:
            extension_store.set_native_harness_exposed("ofek.demo", "skill", "reviewer", 1)  # type: ignore[arg-type]
            raise AssertionError("non-boolean native exposure was accepted")
        except extension_store.ExtensionError:
            pass

        assert extension_store.set_native_harness_exposed("ofek.demo", "skill", "reviewer", False) is False
        assert extension_store.native_harness_exposed("ofek.demo", "instructions", "rules", record=record) is True
        assert extension_store.native_harness_exposed("ofek.demo", "skill", "reviewer", record=record) is False
    finally:
        extension_store.get_extension = real_get  # type: ignore[assignment]
        extension_store.reconcile_runtime_skills = real_skills  # type: ignore[assignment]


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
    client = sdk.Client(
        internal_token="tok",
        backend_url="http://core",
        harness_launcher_projection='{"profile_id":"p"}',
    )
    assert client._settings_body() == {"harness_launcher_projection": '{"profile_id":"p"}'}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all extension-settings tests passed")

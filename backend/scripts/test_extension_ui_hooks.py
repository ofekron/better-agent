"""Tests for extension UI hooks: manifest quick_button/page validation,
ui_hooks() surfacing, per-extension ui-settings toggles, and the SDK
manifest builders round-tripping through core validation."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-extension-ui-hooks-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))

import extension_store  # noqa: E402
import project_update_store  # noqa: E402
import config_store  # noqa: E402
import better_agent_sdk as sdk  # noqa: E402


def _configure_project_structure_runtime() -> None:
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["project_structure_edit"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)


def _configure_ask_runtime() -> None:
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["session_search_worker"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)


def _seed_store_with_marketplace() -> None:
    """Pre-seed extensions.json so the required-marketplace check (which
    otherwise hits the network) sees a present, enabled marketplace record."""
    store_path = Path(_TMP_HOME) / "extensions" / "extensions.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    marketplace_manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_store.MARKETPLACE_EXTENSION_ID,
        "name": "Marketplace",
        "version": "1.0.0",
        "surfaces": ["backend_feature"],
        "entrypoints": {
            "backend": "",
            "frontend": "",
            "mcp": [],
            "provider_capabilities": [],
        },
        "permissions": {},
        "marketplace": {},
    }
    record = {
        "manifest": marketplace_manifest,
        "enabled": True,
        "installed_at": "1970-01-01T00:00:00+00:00",
        "updated_at": "1970-01-01T00:00:00+00:00",
        "source": {
            "type": "private_placeholder",
            "repo_url": "",
            "extension_path": "",
            "ref": "",
            "commit_sha": "unavailable",
            "install_path": "",
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    # Reset ui-settings so every test starts from default (enabled) toggles.
    (Path(_TMP_HOME) / "extensions" / "ui-settings.json").unlink(missing_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "schema_version": extension_store.STORE_SCHEMA_VERSION,
                "extensions": {extension_store.MARKETPLACE_EXTENSION_ID: record},
            }
        ),
        encoding="utf-8",
    )
    # Raw write_text bypasses the store's _write_store_unlocked, which always
    # refreshes the fingerprint cache and drops the projection cache. Mirror it
    # here so the sub-0.5s store_fingerprint() TTL can't serve a stale ui_hooks
    # projection (or stale ui-settings) from a prior test into the next one.
    extension_store._refresh_store_fingerprint_cache()  # type: ignore[attr-defined]
    extension_store._clear_projection_cache()  # type: ignore[attr-defined]
    _configure_project_structure_runtime()
    _configure_ask_runtime()


def _enable_builtin_ui_extensions() -> None:
    _install_ui_hook_extension(
        extension_store.extension_id_for_role('project-structure'),
        {
            "name": "Project structure",
            "surfaces": ["backend_feature", "frontend_feature"],
            "entrypoints": {
                "page": {
                    "id": "main",
                    "label": "Project structure",
                    "icon": "clipboard",
                    "open": {
                        "type": "ensure",
                        "endpoint": f"/api/extensions/{extension_store.extension_id_for_role('project-structure')}/backend/project-structure-edit/ensure",
                        "path_template": "/s/{session_id}",
                        "id_field": "session_id",
                        "include_cwd": True,
                    },
                    "badge": {
                        "endpoint": f"/api/extensions/{extension_store.extension_id_for_role('project-structure')}/backend/project-updates/total"
                    },
                },
            },
            "permissions": {"session_state": True},
        },
    )
    _install_ui_hook_extension(
        extension_store.BUILTIN_ASK_EXTENSION_ID,
        {
            "name": "Ask",
            "surfaces": ["backend_feature", "frontend_feature"],
            "entrypoints": {
                "quick_button": {
                    "label": "Ask",
                    "icon": "sparkles",
                    "action": {
                        "type": "navigate",
                        "path": "/s/virtual:ofek-dev.ask:ask",
                    },
                },
            },
            "permissions": {"session_state": True},
        },
    )


_ASK_QUICK_BUTTON_ACTION = {
    "type": "navigate",
    "path": "/s/virtual:ofek-dev.ask:ask",
}


def _install_active_assistant() -> None:
    """Install + enable the Assistant superseder so its quick button replaces
    Ask's. Active (installed+enabled+entitled) is all the supersede gate needs;
    a configured internal-LLM task additionally makes Assistant's own button
    runtime-ready so it surfaces in ui_hooks()."""
    if extension_store.extension_id_for_role('assistant') is None:
        raise AssertionError("private registry missing Assistant id")
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["assistant"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)
    _install_ui_hook_extension(
        extension_store.extension_id_for_role('assistant'),
        {
            "name": "Assistant",
            "surfaces": ["backend_feature", "frontend_feature"],
            "entrypoints": {
                "quick_button": {
                    "label": "Assistant",
                    "icon": "assistant-start",
                    "action": {
                        "type": "ensure",
                        "endpoint": f"/api/extensions/{extension_store.extension_id_for_role('assistant')}/backend/assistant/ensure",
                        "path_template": "/s/{id}",
                        "id_field": "id",
                    },
                },
            },
            "permissions": {"session_state": True},
        },
    )


def _install_ui_hook_extension(extension_id: str, manifest: dict) -> None:
    package = Path(_TMP_HOME) / "private-fixtures" / extension_id
    if package.exists():
        import shutil
        shutil.rmtree(package)
    package.mkdir(parents=True)
    full_manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": manifest["name"],
        "version": "1.0.0",
        "description": manifest["name"],
        "surfaces": manifest["surfaces"],
        "entrypoints": manifest["entrypoints"],
        "permissions": manifest["permissions"],
        "marketplace": {},
    }
    entrypoints = full_manifest["entrypoints"]
    frontend_path = entrypoints.get("frontend")
    if frontend_path:
        frontend_file = package / frontend_path
        frontend_file.parent.mkdir(parents=True, exist_ok=True)
        frontend_file.write_text("<div></div>", encoding="utf-8")
    quick_button_action = (entrypoints.get("quick_button") or {}).get("action") or {}
    if quick_button_action.get("type") == "module":
        module_path = str(quick_button_action.get("module_url") or "")
        legacy_prefix = f"/api/extensions/{extension_id}/assets/"
        if module_path.startswith(legacy_prefix):
            module_path = module_path[len(legacy_prefix):]
        module_file = package / module_path
        module_file.parent.mkdir(parents=True, exist_ok=True)
        module_file.write_text("export function mount() {}", encoding="utf-8")
    (package / "better-agent-extension.json").write_text(json.dumps(full_manifest), encoding="utf-8")
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


def _set_stored_quick_button_module_url(extension_id: str, module_url: str) -> None:
    with extension_store._store_lock():  # type: ignore[attr-defined]
        data = extension_store._read_store_unlocked()  # type: ignore[attr-defined]
        record = data["extensions"][extension_id]
        record["manifest"]["entrypoints"]["quick_button"]["action"]["module_url"] = module_url
        extension_store._write_store_unlocked(data)  # type: ignore[attr-defined]


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


def test_quick_button_and_page_validation_accepts() -> None:
    manifest = _base_manifest()
    manifest["entrypoints"] = {
        "quick_button": {
            "label": "Ask",
            "icon": "search",
            "action": {
                "type": "ensure",
                "endpoint": "/api/extensions/ofek-dev.ask/backend/ask/ensure",
                "path_template": "/s/{session_id}",
            },
        },
        "page": {
            "label": "Page",
            "icon": "clipboard",
            "open": {"type": "navigate", "path": "/p"},
            "badge": {"endpoint": "/api/demo/count"},
        },
    }
    v = extension_store.validate_manifest(manifest)
    assert v["entrypoints"]["quick_button"]["action"]["include_cwd"] is False
    assert v["entrypoints"]["quick_button"]["action"]["id_field"] == "session_id"
    assert v["entrypoints"]["page"]["open"] == {"type": "navigate", "path": "/p"}
    assert v["entrypoints"]["page"]["badge"] == {"endpoint": "/api/demo/count"}
    assert v["entrypoints"]["page"]["id"] == "main"


def test_quick_button_module_action_accepted() -> None:
    manifest = _base_manifest()
    manifest["entrypoints"] = {
        "frontend": "ui/index.html",
        "quick_button": {
            "label": "Custom",
            "action": {"type": "module", "module_url": "ui/btn.js"},
        }
    }
    v = extension_store.validate_manifest(manifest)
    assert v["entrypoints"]["quick_button"]["action"] == {
        "type": "module",
        "module_url": "/api/extensions/ofek.demo/frontend/ui/btn.js",
    }


def test_ui_hooks_surfaces_normalized_quick_button_module_url() -> None:
    _install_ui_hook_extension(
        "ofek.demo",
        {
            "name": "Demo",
            "surfaces": ["frontend_feature"],
            "entrypoints": {
                "frontend": "ui/index.html",
                "quick_button": {
                    "label": "Custom",
                    "action": {"type": "module", "module_url": "ui/btn.js"},
                },
            },
            "permissions": {},
        },
    )
    hooks = extension_store.ui_hooks()
    quick_buttons = [q for q in hooks["quick_buttons"] if q["extension_id"] == "ofek.demo"]
    assert len(quick_buttons) == 1
    assert quick_buttons[0]["action"] == {
        "type": "module",
        "module_url": "/api/extensions/ofek.demo/frontend/ui/btn.js",
    }


def test_ui_hooks_normalizes_legacy_assets_quick_button_module_url() -> None:
    _install_ui_hook_extension(
        "ofek.demo",
        {
            "name": "Demo",
            "surfaces": ["frontend_feature"],
            "entrypoints": {
                "frontend": "ui/index.html",
                "quick_button": {
                    "label": "Custom",
                    "action": {"type": "module", "module_url": "ui/btn.js"},
                },
            },
            "permissions": {},
        },
    )
    _set_stored_quick_button_module_url(
        "ofek.demo",
        "/api/extensions/ofek.demo/assets/ui/btn.js",
    )
    hooks = extension_store.ui_hooks()
    quick_buttons = [q for q in hooks["quick_buttons"] if q["extension_id"] == "ofek.demo"]
    assert len(quick_buttons) == 1
    assert quick_buttons[0]["action"] == {
        "type": "module",
        "module_url": "/api/extensions/ofek.demo/frontend/ui/btn.js",
    }


def test_ui_hooks_skips_invalid_quick_button_module_url() -> None:
    _install_ui_hook_extension(
        "ofek.demo",
        {
            "name": "Demo",
            "surfaces": ["frontend_feature"],
            "entrypoints": {
                "frontend": "ui/index.html",
                "quick_button": {
                    "label": "Custom",
                    "action": {"type": "module", "module_url": "ui/btn.js"},
                },
            },
            "permissions": {},
        },
    )
    _set_stored_quick_button_module_url("ofek.demo", "/api/sessions/x.js")
    hooks = extension_store.ui_hooks()
    quick_buttons = [q for q in hooks["quick_buttons"] if q["extension_id"] == "ofek.demo"]
    assert quick_buttons == []


def test_quick_button_placements_normalized() -> None:
    manifest = _base_manifest()
    manifest["entrypoints"] = {
        "frontend": "ui/index.html",
        "quick_button": {
            "label": "Custom",
            "placements": ["settings", "settings"],
            "action": {"type": "module", "module_url": "ui/btn.js"},
        },
    }
    v = extension_store.validate_manifest(manifest)
    assert v["entrypoints"]["quick_button"]["placements"] == ["settings"]


def test_quick_button_placements_default_is_all_surfaces() -> None:
    manifest = _base_manifest()
    manifest["entrypoints"] = {
        "frontend": "ui/index.html",
        "quick_button": {
            "label": "Custom",
            "action": {"type": "module", "module_url": "ui/btn.js"},
        },
    }
    v = extension_store.validate_manifest(manifest)
    assert v["entrypoints"]["quick_button"]["placements"] == ["session", "settings"]


def test_quick_button_placements_rejects_unknown_and_empty() -> None:
    for bad in (["sidebar"], [], "settings"):
        manifest = _base_manifest()
        manifest["entrypoints"] = {
            "frontend": "ui/index.html",
            "quick_button": {
                "label": "Custom",
                "placements": bad,
                "action": {"type": "module", "module_url": "ui/btn.js"},
            },
        }
        try:
            extension_store.validate_manifest(manifest)
            raise AssertionError(f"expected rejection for placements={bad!r}")
        except extension_store.ExtensionError:
            pass


def test_ui_hooks_projects_quick_button_placements() -> None:
    _install_ui_hook_extension(
        "ofek.demo",
        {
            "name": "Demo",
            "surfaces": ["frontend_feature"],
            "entrypoints": {
                "frontend": "ui/index.html",
                "quick_button": {
                    "label": "Custom",
                    "placements": ["settings"],
                    "action": {"type": "module", "module_url": "ui/btn.js"},
                },
            },
            "permissions": {},
        },
    )
    hooks = extension_store.ui_hooks()
    quick_buttons = [q for q in hooks["quick_buttons"] if q["extension_id"] == "ofek.demo"]
    assert len(quick_buttons) == 1
    assert quick_buttons[0]["placements"] == ["settings"]


def test_ui_hooks_defaults_placements_for_pre_placements_records() -> None:
    # Installed records validated before placements existed have no
    # placements key; the projection must still surface both surfaces.
    _install_ui_hook_extension(
        "ofek.demo",
        {
            "name": "Demo",
            "surfaces": ["frontend_feature"],
            "entrypoints": {
                "frontend": "ui/index.html",
                "quick_button": {
                    "label": "Custom",
                    "action": {"type": "module", "module_url": "ui/btn.js"},
                },
            },
            "permissions": {},
        },
    )
    store_path = Path(_TMP_HOME) / "extensions" / "extensions.json"
    data = json.loads(store_path.read_text())
    record = data["extensions"]["ofek.demo"]
    record["manifest"]["entrypoints"]["quick_button"].pop("placements", None)
    store_path.write_text(json.dumps(data))
    extension_store._clear_projection_cache()
    hooks = extension_store.ui_hooks()
    quick_buttons = [q for q in hooks["quick_buttons"] if q["extension_id"] == "ofek.demo"]
    assert len(quick_buttons) == 1
    assert quick_buttons[0]["placements"] == ["session", "settings"]


def test_invalid_actions_rejected() -> None:
    def expect_err(entrypoints: dict, marker: str) -> None:
        manifest = _base_manifest()
        manifest["entrypoints"] = entrypoints
        try:
            extension_store.validate_manifest(manifest)
            raise AssertionError(f"expected rejection for {marker}")
        except extension_store.ExtensionError:
            pass

    # protocol-relative module_url (open redirect / SSRF guard)
    expect_err(
        {"quick_button": {"label": "A", "action": {"type": "module", "module_url": "//evil.com/x"}}},
        "module //host",
    )
    expect_err(
        {"quick_button": {"label": "A", "action": {"type": "module", "module_url": "/api/sessions/x.js"}}},
        "module app route",
    )
    expect_err(
        {
            "frontend": "ui/index.html",
            "quick_button": {"label": "A", "action": {"type": "module", "module_url": "../x.js"}},
        },
        "module traversal",
    )
    # unknown action type
    expect_err({"quick_button": {"label": "A", "action": {"type": "teleport"}}}, "bad action type")
    # page.open may not be a module (pages open routes)
    expect_err(
        {"page": {"label": "P", "open": {"type": "module", "module_url": "/x.js"}}},
        "page.open module",
    )
    # page requires a label
    expect_err({"page": {"open": {"type": "navigate", "path": "/p"}}}, "page missing label")
    # badge endpoint must be site-relative
    expect_err(
        {
            "page": {
                "label": "P",
                "open": {"type": "navigate", "path": "/p"},
                "badge": {"endpoint": "https://evil.com"},
            }
        },
        "badge external",
    )
    # navigate requires a path
    expect_err({"quick_button": {"label": "A", "action": {"type": "navigate"}}}, "navigate no path")
    # ensure requires endpoint + path_template
    expect_err(
        {"quick_button": {"label": "A", "action": {"type": "ensure", "endpoint": "/api/x"}}},
        "ensure no path_template",
    )


def test_ui_hooks_surfaces_project_structure_page() -> None:
    _seed_store_with_marketplace()
    _enable_builtin_ui_extensions()
    hooks = extension_store.ui_hooks()
    pages = [p for p in hooks["pages"] if p["extension_id"] == extension_store.extension_id_for_role('project-structure')]
    assert len(pages) == 1
    page = pages[0]
    assert page["open"]["type"] == "ensure"
    assert page["open"]["endpoint"] == f"/api/extensions/{extension_store.extension_id_for_role('project-structure')}/backend/project-structure-edit/ensure"
    assert page["open"]["path_template"] == "/s/{session_id}"
    assert page["open"]["include_cwd"] is True
    assert page["badge"] == {
        "endpoint": f"/api/extensions/{extension_store.extension_id_for_role('project-structure')}/backend/project-updates/total"
    }
    # Assistant is not installed by _enable_builtin_ui_extensions, so Ask's quick
    # button is NOT superseded and surfaces normally.
    quick_buttons = [q for q in hooks["quick_buttons"] if q["extension_id"] == extension_store.BUILTIN_ASK_EXTENSION_ID]
    assert len(quick_buttons) == 1
    assert quick_buttons[0]["action"] == _ASK_QUICK_BUTTON_ACTION


def test_ask_quick_button_superseded_by_active_assistant() -> None:
    # Ask ships a quick button again; it is hidden only while the Assistant
    # superseder is installed+enabled, and returns when Assistant is disabled or
    # uninstalled (as long as Ask itself stays installed+enabled).
    _seed_store_with_marketplace()
    _enable_builtin_ui_extensions()

    def ask_buttons() -> list:
        return [
            q for q in extension_store.ui_hooks()["quick_buttons"]
            if q["extension_id"] == extension_store.BUILTIN_ASK_EXTENSION_ID
        ]

    def assistant_buttons() -> list:
        return [
            q for q in extension_store.ui_hooks()["quick_buttons"]
            if q["extension_id"] == extension_store.extension_id_for_role('assistant')
        ]

    # Assistant absent -> Ask button shows.
    assert len(ask_buttons()) == 1

    # Assistant installed + enabled -> Ask suppressed, Assistant shows.
    _install_active_assistant()
    assert ask_buttons() == []
    assert len(assistant_buttons()) == 1

    # Assistant disabled -> Ask returns, Assistant gone.
    extension_store.set_enabled(extension_store.extension_id_for_role('assistant'), False)
    assert len(ask_buttons()) == 1
    assert assistant_buttons() == []

    # Assistant re-enabled -> Ask suppressed again.
    extension_store.set_enabled(extension_store.extension_id_for_role('assistant'), True)
    assert ask_buttons() == []

    # Assistant uninstalled -> Ask returns.
    import sys as _sys
    import types as _types
    if "assistant_ui" not in _sys.modules:
        _stub = _types.ModuleType("assistant_ui")
        _stub.cleanup_singleton = lambda: None  # type: ignore[attr-defined]
        _sys.modules["assistant_ui"] = _stub
    extension_store.uninstall(extension_store.extension_id_for_role('assistant'))
    assert len(ask_buttons()) == 1


def test_ask_quick_button_hidden_when_ask_ui_toggle_off() -> None:
    # The supersede gate is independent of Ask's own ui-settings toggle: with
    # Assistant absent, disabling Ask's quick_button surface still hides it.
    _seed_store_with_marketplace()
    _enable_builtin_ui_extensions()
    extension_store.set_ui_settings(
        extension_store.BUILTIN_ASK_EXTENSION_ID, quick_button_enabled=False
    )
    assert [
        q for q in extension_store.ui_hooks()["quick_buttons"]
        if q["extension_id"] == extension_store.BUILTIN_ASK_EXTENSION_ID
    ] == []


def test_builtin_ask_ships_quick_button() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "extensions"
        / "ask"
        / "better-agent-extension.json"
    )
    entrypoints = extension_store.validate_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )["entrypoints"]
    assert entrypoints["quick_button"]["label"] == "Ask"
    assert entrypoints["quick_button"]["icon"] == "sparkles"
    assert entrypoints["quick_button"]["action"] == {
        "type": "navigate",
        "path": "/s/virtual:ofek-dev.ask:ask",
    }
    assert [
        module
        for module in entrypoints["frontend_modules"]
        if module["slot"] in {"session-toolbar", "mobile-session-topbar"}
    ] == []


def test_builtin_ask_manifest_declares_backend_routes_permission() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "extensions"
        / "ask"
        / "better-agent-extension.json"
    )
    manifest = extension_store.validate_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest["permissions"]["backend_routes"] is True


def test_builtin_ask_backend_entrypoint_is_mounted() -> None:
    _seed_store_with_marketplace()
    # Reconcile installs the real public ask package from the bundled repo so
    # its backend entrypoint + backend_routes permission are present. Reads via
    # get_extension() are pure and do not seed; list_extensions_with_reconciliation
    # is the explicit seed path (same idiom as test_marketplace_extension_mcp).
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    spec = extension_store.backend_entrypoint_spec(extension_store.BUILTIN_ASK_EXTENSION_ID)
    assert spec is not None
    assert spec["prefix"] == "/api/extensions/ofek-dev.ask/backend"
    assert spec["effective_permissions"]["backend_routes"] is True


def test_fresh_store_surfaces_first_party_builtin_ui_hooks() -> None:
    # First-party builtins surface their UI hooks once seeded + runtime-ready.
    # On a fresh reconcile the Assistant superseder is not active, so Ask's
    # quick button surfaces; it is hidden only while Assistant is installed +
    # enabled (covered by test_ask_quick_button_superseded_by_active_assistant).
    _seed_store_with_marketplace()
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    hooks = extension_store.ui_hooks()
    assert not extension_store.is_extension_active(
        extension_store.extension_id_for_role('assistant')
    ), "Assistant must be inactive on a fresh reconcile for this assertion to hold"
    assert [
        q for q in hooks["quick_buttons"]
        if q["extension_id"] == extension_store.BUILTIN_ASK_EXTENSION_ID
    ]
    assert [
        p for p in hooks["pages"]
        if p["extension_id"] == extension_store.extension_id_for_role('project-structure')
    ]


def test_installed_manifest_is_authoritative_without_public_sync() -> None:
    _seed_store_with_marketplace()
    _enable_builtin_ui_extensions()
    data = extension_store._load()  # type: ignore[attr-defined]
    record = data["extensions"][extension_store.BUILTIN_ASK_EXTENSION_ID]
    record["manifest"]["entrypoints"] = {
        "backend": "",
        "frontend": "",
        "mcp": [],
        "provider_capabilities": [],
    }
    extension_store._save(data)  # type: ignore[attr-defined]
    hooks = extension_store.ui_hooks()
    quick_buttons = [q for q in hooks["quick_buttons"] if q["extension_id"] == extension_store.BUILTIN_ASK_EXTENSION_ID]
    assert quick_buttons == []


def test_ui_settings_toggle_filters_page() -> None:
    _seed_store_with_marketplace()
    _enable_builtin_ui_extensions()
    ext_id = extension_store.extension_id_for_role('project-structure')
    assert extension_store.get_ui_settings(ext_id) == {
        "quick_button_enabled": True,
        "page_enabled": True,
    }
    extension_store.set_ui_settings(ext_id, page_enabled=False)
    assert extension_store.get_ui_settings(ext_id)["page_enabled"] is False
    pages = [
        p for p in extension_store.ui_hooks()["pages"]
        if p["extension_id"] == ext_id
    ]
    assert pages == [], "disabled page must not surface in ui_hooks"
    # re-enabling restores it
    extension_store.set_ui_settings(ext_id, page_enabled=True)
    pages = [
        p for p in extension_store.ui_hooks()["pages"]
        if p["extension_id"] == ext_id
    ]
    assert len(pages) == 1


def test_frontend_entrypoints_reuse_projection_cache() -> None:
    _seed_store_with_marketplace()
    _enable_builtin_ui_extensions()
    extension_store._PROJECTION_CACHE.clear()  # type: ignore[attr-defined]
    first = extension_store.frontend_entrypoints()
    original_load = extension_store._load  # type: ignore[attr-defined]

    def fail_load():
        raise AssertionError("unchanged frontend_entrypoints should not reread extension store")

    extension_store._load = fail_load  # type: ignore[attr-defined]
    try:
        second = extension_store.frontend_entrypoints()
    finally:
        extension_store._load = original_load  # type: ignore[attr-defined]
    assert second == first


def test_ui_hooks_cache_invalidates_on_ui_settings_write() -> None:
    _seed_store_with_marketplace()
    _enable_builtin_ui_extensions()
    extension_store._PROJECTION_CACHE.clear()  # type: ignore[attr-defined]
    ext_id = extension_store.extension_id_for_role('project-structure')
    assert any(
        p["extension_id"] == ext_id
        for p in extension_store.ui_hooks()["pages"]
    )
    extension_store.set_ui_settings(ext_id, page_enabled=False)
    assert not any(
        p["extension_id"] == ext_id
        for p in extension_store.ui_hooks()["pages"]
    )


def test_ui_settings_unknown_extension_rejected() -> None:
    _seed_store_with_marketplace()
    try:
        extension_store.get_ui_settings("does.not.exist")
        raise AssertionError("expected rejection for unknown extension")
    except extension_store.ExtensionError:
        pass


def test_total_unseen_sums_across_projects() -> None:
    assert project_update_store.total_unseen() == 0
    project_update_store.append("proj-a", "change A")
    project_update_store.append("proj-b", "change B")
    # Sums unseen across both project logs, not just one.
    assert project_update_store.total_unseen() == 2


def test_sdk_builders_round_trip_through_validation() -> None:
    quick_button = sdk.QuickButton(
        label="Ask",
        icon="search",
        action=sdk.HookAction.ensure("/api/extensions/ofek-dev.ask/backend/ask/ensure", "/s/{session_id}"),
        placements=("settings",),
    )
    page = sdk.Page(
        label="Project structure",
        icon="clipboard",
        open=sdk.HookAction.ensure(
            f"/api/extensions/{extension_store.extension_id_for_role('project-structure')}/backend/project-structure-edit/ensure",
            "/s/{session_id}",
            include_cwd=True,
        ),
        badge=sdk.Badge(f"/api/extensions/{extension_store.extension_id_for_role('project-structure')}/backend/project-updates/total"),
    )
    manifest = _base_manifest()
    manifest["entrypoints"] = {"quick_button": quick_button.to_dict(), "page": page.to_dict()}
    v = extension_store.validate_manifest(manifest)
    assert v["entrypoints"]["quick_button"]["label"] == "Ask"
    assert v["entrypoints"]["quick_button"]["placements"] == ["settings"]
    assert v["entrypoints"]["page"]["open"]["include_cwd"] is True
    assert v["entrypoints"]["page"]["badge"] == {
        "endpoint": f"/api/extensions/{extension_store.extension_id_for_role('project-structure')}/backend/project-updates/total"
    }


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all ui-hooks tests passed")

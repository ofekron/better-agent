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
    _configure_project_structure_runtime()
    _configure_ask_runtime()


def _enable_builtin_ui_extensions() -> None:
    _install_ui_hook_extension(
        extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID,
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
                        "endpoint": f"/api/extensions/{extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID}/backend/project-structure-edit/ensure",
                        "path_template": "/s/{session_id}",
                        "id_field": "session_id",
                        "include_cwd": True,
                    },
                    "badge": {
                        "endpoint": f"/api/extensions/{extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID}/backend/project-updates/total"
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
            "entrypoints": {},
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
        "quick_button": {
            "label": "Custom",
            "action": {"type": "module", "module_url": "/api/extensions/ofek.demo/frontend/btn.js"},
        }
    }
    v = extension_store.validate_manifest(manifest)
    assert v["entrypoints"]["quick_button"]["action"] == {
        "type": "module",
        "module_url": "/api/extensions/ofek.demo/frontend/btn.js",
    }


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
    pages = [p for p in hooks["pages"] if p["extension_id"] == extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID]
    assert len(pages) == 1
    page = pages[0]
    assert page["open"]["type"] == "ensure"
    assert page["open"]["endpoint"] == f"/api/extensions/{extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID}/backend/project-structure-edit/ensure"
    assert page["open"]["path_template"] == "/s/{session_id}"
    assert page["open"]["include_cwd"] is True
    assert page["badge"] == {
        "endpoint": f"/api/extensions/{extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID}/backend/project-updates/total"
    }
    quick_buttons = [q for q in hooks["quick_buttons"] if q["extension_id"] == extension_store.BUILTIN_ASK_EXTENSION_ID]
    assert quick_buttons == []


def test_builtin_ask_has_no_toolbar_entrypoint() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "extensions"
        / "ask"
        / "better-agent-extension.json"
    )
    entrypoints = extension_store.validate_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )["entrypoints"]
    assert entrypoints["quick_button"] == {}
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
    # First-party builtins surface their UI hooks once seeded + runtime-ready,
    # except Ask's toolbar entry, which is intentionally hidden in favor of the
    # Assistant quick action.
    _seed_store_with_marketplace()
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    hooks = extension_store.ui_hooks()
    assert not [
        q for q in hooks["quick_buttons"]
        if q["extension_id"] == extension_store.BUILTIN_ASK_EXTENSION_ID
    ]
    assert [
        p for p in hooks["pages"]
        if p["extension_id"] == extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID
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
    ext_id = extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID
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
    )
    page = sdk.Page(
        label="Project structure",
        icon="clipboard",
        open=sdk.HookAction.ensure(
            f"/api/extensions/{extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID}/backend/project-structure-edit/ensure",
            "/s/{session_id}",
            include_cwd=True,
        ),
        badge=sdk.Badge(f"/api/extensions/{extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID}/backend/project-updates/total"),
    )
    manifest = _base_manifest()
    manifest["entrypoints"] = {"quick_button": quick_button.to_dict(), "page": page.to_dict()}
    v = extension_store.validate_manifest(manifest)
    assert v["entrypoints"]["quick_button"]["label"] == "Ask"
    assert v["entrypoints"]["page"]["open"]["include_cwd"] is True
    assert v["entrypoints"]["page"]["badge"] == {
        "endpoint": f"/api/extensions/{extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID}/backend/project-updates/total"
    }


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all ui-hooks tests passed")

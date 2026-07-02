from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-run-config-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provider_run_config  # noqa: E402
import provider  # noqa: E402
import runner  # noqa: E402
import runner_better_agent  # noqa: E402
import runner_codex  # noqa: E402
import runner_gemini  # noqa: E402
import runtime_skills  # noqa: E402
import open_file_panel_mcp  # noqa: E402
import builtin_mcp_config  # noqa: E402
import extension_registry  # noqa: E402
import extension_store  # noqa: E402
import extension_mcp_launcher  # noqa: E402
import config_store  # noqa: E402
from paths import ba_home  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        FAILURES.append(msg)


def _configure_internal_llm_defaults(*tasks: str) -> None:
    providers = config_store.list_providers()["providers"]
    provider = providers[0]
    assignments = config_store.get_internal_llm_assignments()
    for task in tasks:
        assignments[task] = {
            "provider_id": provider["id"],
            "model": provider["default_model"],
            "reasoning_effort": provider.get("default_reasoning_effort") or "",
        }
    config_store.set_internal_llm_assignments(assignments)


def _simulate_backend_restart() -> None:
    importlib.reload(extension_store)
    importlib.reload(extension_mcp_launcher)
    importlib.reload(builtin_mcp_config)


def _save_runtime_extension_record(data: dict, extension_id: str) -> None:
    extension_store._save(data)  # type: ignore[attr-defined]
    extension_store.set_harness_delivery_mode(extension_id, "runtime")


def _module_from_python_path(rel_path: str) -> str:
    path = Path(rel_path).with_suffix("")
    parts = list(path.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _default_protocol(entrypoints: dict | None = None) -> dict:
    modules = set()
    entrypoints = entrypoints or {}
    backend_module = entrypoints.get("backend_module")
    if backend_module:
        modules.add(backend_module)
    for item in entrypoints.get("mcp") or []:
        if not isinstance(item, dict):
            continue
        if item.get("module"):
            modules.add(item["module"])
        if item.get("python"):
            modules.add(_module_from_python_path(item["python"]))
    return {
        "version": 1,
        "smoke_test": {
            "required_paths": ["better-agent-extension.json"],
            "python_modules": sorted(modules),
        },
    }


def _write_installed_manifest(package: Path, manifest: dict) -> dict:
    value = dict(manifest)
    value.setdefault("protocol", _default_protocol(value.get("entrypoints")))
    validated = extension_store.validate_manifest(value)
    (package / "better-agent-extension.json").write_text(json.dumps(validated), encoding="utf-8")
    return validated


def _install_requirements_extension_record(
    *,
    delivery: str = "runtime",
    replaces_builtin: bool = False,
) -> None:
    package = Path(_TMP_HOME) / "requirements-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "requirement_analysis").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('requirements')\n", encoding="utf-8")
    (package / "requirement_analysis" / "__init__.py").write_text("", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    mcp_entry = {
        "name": "better-agent-requirements",
        "python": "mcp/server.py",
        "args": [],
        "env": {},
        "user_facing": False,
        "bare_allowed": False,
        "requires_backend_auth": True,
    }
    if replaces_builtin:
        mcp_entry["replaces_builtin"] = "get-requirements"
    data["extensions"][extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID,
            "name": "Requirements",
            "version": "1.0.0",
            "description": "Requirement analysis extension",
            "surfaces": ["backend_feature", "runtime_mcp", "provider_capabilities"],
            "entrypoints": {
                "backend": "",
                "frontend": "",
                "mcp": [mcp_entry],
                "provider_capabilities": [],
                "frontend_modules": [],
            },
            "permissions": {
                "session_state": True,
                "spawn_runs": True,
                "internal_loopback": True,
                "filesystem": True,
                "provider_config": True,
            },
            "marketplace": {
                "product_id": "requirements.pro",
                "subscription_required": True,
                "entitlement_url": "https://marketplace.test/entitlements",
            },
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/requirements",
            "ref": "",
            "commit_sha": "requirements-private",
            "install_path": str(package),
        },
        "entitlement": {
            "status": "active",
            "product_id": "requirements.pro",
            "token_present": True,
            "last_checked_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    }
    record = data["extensions"][extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID]
    record["consent"] = {
        "fingerprint": extension_store.permission_consent_fingerprint(record),
        "granted_at": "2026-01-01T00:00:00+00:00",
    }
    extension_store._save(data)  # type: ignore[attr-defined]
    extension_store.set_harness_delivery_mode(
        extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID,
        delivery,
    )


def _install_feature_extension_record(extension_id: str, permissions: dict | None = None) -> None:
    package = Path(_TMP_HOME) / f"{extension_id}-feature-extension"
    package.mkdir(parents=True, exist_ok=True)
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_id,
            "name": extension_id,
            "version": "1.0.0",
            "description": extension_id,
            "surfaces": ["backend_feature"],
            "entrypoints": {
                "backend": "",
                "frontend": "",
                "mcp": [],
                "provider_capabilities": [],
                "frontend_modules": [],
            },
            "permissions": permissions or {},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": f"extensions/{extension_id}",
            "ref": "",
            "commit_sha": f"{extension_id}-private",
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


def _install_scheduler_extension_record() -> None:
    package = Path(_TMP_HOME) / "scheduler-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('scheduler')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_store.BUILTIN_SCHEDULER_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_store.BUILTIN_SCHEDULER_EXTENSION_ID,
            "name": "Scheduler",
            "version": "1.0.0",
            "description": "Scheduler",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "scheduler",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": True,
                        "bare_allowed": False,
                        "requires_backend_auth": True,
                    }
                ]
            },
            "permissions": {"internal_loopback": True},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/scheduler",
            "ref": "",
            "commit_sha": "scheduler-private",
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
    _save_runtime_extension_record(data, extension_store.BUILTIN_SCHEDULER_EXTENSION_ID)


def _install_core_mcp_gate_extensions() -> None:
    _install_feature_extension_record(
        extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID,
        {"session_state": True, "internal_loopback": True},
    )
    _install_coordination_extension_record()
    _install_session_bridge_extension_record()
    _install_browser_harness_extension_record()
    _install_credential_broker_extension_record()
    _install_provider_config_sync_extension_record()
    _install_canvas_extension_record()
    _install_scheduler_extension_record()


def _install_provider_config_sync_extension_record() -> None:
    package = Path(_TMP_HOME) / "provider-config-sync-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('provider config sync')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_store.BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_store.BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID,
            "name": "Provider Config Sync",
            "version": "1.0.0",
            "description": "Provider Config Sync",
            "surfaces": ["backend_feature", "runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "better-agent-provider-config-sync",
                        "replaces_builtin": "provider-config-sync",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": True,
                        "bare_allowed": False,
                        "requires_backend_auth": True,
                    }
                ]
            },
            "permissions": {"session_state": True, "internal_loopback": True},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/provider-config-sync",
            "ref": "",
            "commit_sha": "provider-config-sync-private",
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
    _save_runtime_extension_record(data, extension_store.BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID)


def _install_browser_harness_extension_record() -> None:
    package = Path(_TMP_HOME) / "browser-harness-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('browser harness')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_store.BUILTIN_BROWSER_HARNESS_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_store.BUILTIN_BROWSER_HARNESS_EXTENSION_ID,
            "name": "Browser Harness",
            "version": "1.0.0",
            "description": "Browser Harness",
            "surfaces": ["backend_feature", "runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "better-agent-browser-harness",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": True,
                        "bare_allowed": False,
                        "requires_backend_auth": True,
                        "predicate": {"equals": {"browser_harness_enabled": True}},
                    }
                ]
            },
            "permissions": {"session_state": True, "spawn_runs": True, "internal_loopback": True},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/browser-harness",
            "ref": "",
            "commit_sha": "browser-harness-private",
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
    _save_runtime_extension_record(data, extension_store.BUILTIN_BROWSER_HARNESS_EXTENSION_ID)


def _install_credential_broker_extension_record() -> None:
    package = Path(_TMP_HOME) / "credential-broker-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('credential broker')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_store.BUILTIN_CREDENTIAL_BROKER_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_store.BUILTIN_CREDENTIAL_BROKER_EXTENSION_ID,
            "name": "Credential Broker",
            "version": "1.0.0",
            "description": "Credential Broker",
            "surfaces": ["backend_feature", "runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "better-agent-credential-broker",
                        "replaces_builtin": "credential-broker",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": True,
                        "bare_allowed": True,
                        "requires_backend_auth": True,
                    }
                ]
            },
            "permissions": {"session_state": True, "internal_loopback": True},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/credential-broker",
            "ref": "",
            "commit_sha": "credential-broker-private",
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
    _save_runtime_extension_record(data, extension_store.BUILTIN_CREDENTIAL_BROKER_EXTENSION_ID)


def _install_session_bridge_extension_record() -> None:
    package = Path(_TMP_HOME) / "session-bridge-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('session bridge')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"]["ofek-dev.session-bridge"] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": "ofek-dev.session-bridge",
            "name": "Session Bridge",
            "version": "1.0.0",
            "description": "Session Bridge",
            "surfaces": ["backend_feature", "runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "better-agent-session-bridge",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": True,
                        "bare_allowed": False,
                        "requires_backend_auth": True,
                        "predicate": {
                            "equals": {"mode": "native"},
                            "not_equals": {
                                "app_session_id": "virtual:ofek-dev.ask:ask",
                                "working_mode": "search_worker",
                            },
                        },
                    }
                ]
            },
            "permissions": {"session_state": True, "internal_loopback": True},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/session-bridge",
            "ref": "",
            "commit_sha": "session-bridge-private",
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
    _save_runtime_extension_record(data, "ofek-dev.session-bridge")


def _install_coordination_extension_record() -> None:
    package = Path(_TMP_HOME) / "coordination-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('coordination')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_store.BUILTIN_COORDINATION_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_store.BUILTIN_COORDINATION_EXTENSION_ID,
            "name": "Coordination",
            "version": "1.0.0",
            "description": "Coordination",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "ofek-dev-coordination",
                        "replaces_builtin": "better-agent-coordination",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": True,
                        "bare_allowed": False,
                        "requires_backend_auth": True,
                    }
                ]
            },
            "permissions": {"internal_loopback": True},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/coordination",
            "ref": "",
            "commit_sha": "coordination-private",
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
    _save_runtime_extension_record(data, extension_store.BUILTIN_COORDINATION_EXTENSION_ID)


def _install_canvas_extension_record() -> None:
    package = Path(_TMP_HOME) / "canvas-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('canvas')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_store.BUILTIN_CANVAS_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_store.BUILTIN_CANVAS_EXTENSION_ID,
            "name": "Canvas",
            "version": "1.0.0",
            "description": "Canvas",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "better-agent-canvas",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": False,
                        "bare_allowed": True,
                        "requires_backend_auth": False,
                    }
                ]
            },
            "permissions": {},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/canvas",
            "ref": "",
            "commit_sha": "canvas-private",
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
    _save_runtime_extension_record(data, extension_store.BUILTIN_CANVAS_EXTENSION_ID)


def _install_testape_extension_record() -> None:
    package = Path(_TMP_HOME) / "testape-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('testape')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_store.BUILTIN_TESTAPE_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_store.BUILTIN_TESTAPE_EXTENSION_ID,
            "name": "Testape",
            "version": "1.0.0",
            "description": "Testape",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "testape",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": True,
                        "bare_allowed": True,
                        "requires_backend_auth": False,
                    }
                ]
            },
            "permissions": {"filesystem": True, "session_state": True},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/testape",
            "ref": "",
            "commit_sha": "testape-private",
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
    extension_store.set_harness_delivery_mode(extension_store.BUILTIN_TESTAPE_EXTENSION_ID, "native")


def _install_bare_matrix_extension_record() -> None:
    package = Path(_TMP_HOME) / "bare-matrix-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('bare matrix')\n", encoding="utf-8")
    extension_id = "ofek.bare-matrix"
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_id,
            "name": "Bare Matrix",
            "version": "1.0.0",
            "description": "Bare Matrix",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "headless-bare",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": False,
                        "bare_allowed": True,
                        "requires_backend_auth": False,
                    },
                    {
                        "name": "visible-bare",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": True,
                        "bare_allowed": True,
                        "requires_backend_auth": False,
                    },
                    {
                        "name": "visible-not-bare",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": True,
                        "bare_allowed": False,
                        "requires_backend_auth": False,
                    },
                ]
            },
            "permissions": {},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/bare-matrix",
            "ref": "",
            "commit_sha": "bare-matrix-private",
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
    extension_store.set_harness_delivery_mode(extension_id, "native")


def t_normalizes_unified_mcp_key() -> None:
    config = provider_run_config.normalize_provider_run_config({
        "mcpServers": {"demo": {"command": "echo"}},
        "skills": {"reviewer": "Review.\n"},
    })
    check(config["mcp_servers"]["demo"]["command"] == "echo", "mcpServers normalizes to mcp_servers")
    check(config["skills"]["reviewer"] == "Review.\n", "skills pass through")


def t_codex_materializes_mcp_and_skills() -> None:
    old_home = os.environ.get("HOME")
    home = Path(tempfile.mkdtemp(dir=_TMP_HOME))
    os.environ["HOME"] = str(home)
    runtime_skills._DISCOVERY_CACHE.clear()
    try:
        runtime_skill = home / ".agents" / "skills" / "runtime-reviewer" / "SKILL.md"
        runtime_skill.parent.mkdir(parents=True)
        runtime_skill.write_text(
            "---\nname: runtime-reviewer\ndescription: Runtime review.\n---\nRuntime review.\n",
            encoding="utf-8",
        )
        (home / ".codex").mkdir()
        run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        env = runner_codex._materialize_codex_run_home(
            run_dir,
            {
                "skills": {"reviewer": {"description": "Review code", "instructions": "Review carefully.\n"}},
            },
            cwd=str(home),
        )
        overrides = runner_codex._codex_config_overrides(run_dir, {
            "mcp_servers": {"demo": {"command": "echo", "args": ["hello"]}},
        })
        bare_run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        bare_env = runner_codex._materialize_codex_run_home(
            bare_run_dir,
            {},
            cwd=str(home),
            bare_config=True,
        )
    finally:
        runtime_skills._DISCOVERY_CACHE.clear()
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home

    check(len(overrides) == 1 and overrides[0].startswith("mcp_servers="), "Codex MCP becomes config override")
    parsed = tomllib.loads(overrides[0])
    check(parsed["mcp_servers"]["demo"]["args"] == ["hello"], "Codex MCP override is valid TOML")
    overlay_home = Path(env["HOME"])
    skill_root = overlay_home / ".agents" / "skills"
    check(overlay_home == run_dir / "codex-home", "Codex HOME points at run-local overlay")
    check(Path(env["CODEX_HOME"]).is_symlink(), "Codex config home is linked into overlay")
    skill = skill_root / "reviewer" / "SKILL.md"
    check(skill.is_file(), "Codex per-run skill file is materialized")
    check("Review carefully." in skill.read_text(encoding="utf-8"), "Codex skill body is written")
    check(
        (skill_root / "runtime-reviewer" / "SKILL.md").is_file(),
        "Codex runtime skill file is materialized",
    )
    check(
        not (Path(bare_env["HOME"]) / ".agents" / "skills" / "runtime-reviewer" / "SKILL.md").exists(),
        "Codex bare config skips runtime skills",
    )


def t_codex_runner_inputs_self_identify_provider_kind() -> None:
    inputs = runner_codex._codex_runner_inputs({"provider_kind": "stale", "cwd": "/tmp/project"})
    check(inputs["provider_kind"] == "codex", "Codex runner self-identifies provider kind")
    check(inputs["cwd"] == "/tmp/project", "Codex runner preserves original inputs")


def t_codex_context_strategy_overrides_auto_compact() -> None:
    overrides = runner_codex._context_strategy_config_overrides({
        "context_strategy": "continuation",
    })
    check(
        "model_auto_compact_token_limit=999999999" in overrides,
        "Codex continuation disables native auto-compact before overflow",
    )
    check(
        'model_auto_compact_token_limit_scope="total"' in overrides,
        "Codex continuation auto-compact override uses supported total scope",
    )
    check(
        runner_codex._context_strategy_config_overrides({
            "context_strategy": "native_compact",
        }) == [],
        "Codex native compact leaves Codex auto-compact config alone",
    )


def t_claude_materializes_runtime_skills_plugin() -> None:
    old_home = os.environ.get("HOME")
    home = Path(tempfile.mkdtemp(dir=_TMP_HOME))
    os.environ["HOME"] = str(home)
    try:
        skill = home / ".agents" / "skills" / "get-requirements" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: get-requirements\ndescription: Req.\n---\n# Req\n", encoding="utf-8")
        run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        plugin = runner._materialize_claude_skill_plugin(
            run_dir,
            str(home),
            {"skills": {"reviewer": "Review carefully.\n"}},
            bare_config=False,
        )
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home

    check(plugin is not None, "Claude runtime skills plugin is created")
    plugin_path = Path(plugin["path"])
    check((plugin_path / ".claude-plugin" / "plugin.json").is_file(), "Claude runtime skills plugin has manifest")
    manifest = json.loads((plugin_path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check(
        manifest["name"] == "better-agent-runtime-skills",
        "Claude runtime skills plugin uses Better Agent name",
    )
    check(
        (plugin_path / "skills" / "get-requirements" / "SKILL.md").is_file(),
        "Claude runtime skills plugin includes discovered skill",
    )
    check(
        "Review carefully." in (plugin_path / "skills" / "reviewer" / "SKILL.md").read_text(encoding="utf-8"),
        "Claude runtime skills plugin includes provider-run skill",
    )


def t_codex_open_file_panel_dynamic_tool() -> None:
    tool = runner_codex._build_open_file_panel_dynamic_tool()
    check(tool["name"] == "open_file_panel", "Codex open-file-panel dynamic tool is named correctly")
    check(
        tool["inputSchema"]["required"] == ["mode", "path"],
        "Codex open-file-panel requires mode and path",
    )


def t_codex_builtin_tool_schemas_do_not_invite_null_defaults() -> None:
    tools = [
        runner_codex._build_create_worker_dynamic_tool(),
        runner_codex._build_ensure_named_worker_dynamic_tool(),
        runner_codex._build_open_file_panel_dynamic_tool(),
        runner_codex._build_delegate_task_dynamic_tool(),
        runner_codex._build_create_session_dynamic_tool(),
        runner_codex._build_create_sub_session_dynamic_tool(),
        runner_codex._build_ask_dynamic_tool(),
    ]

    nullable_fields: list[str] = []
    for tool in tools:
        for field, schema in tool["inputSchema"].get("properties", {}).items():
            schema_type = schema.get("type") if isinstance(schema, dict) else None
            if isinstance(schema_type, list) and "null" in schema_type:
                nullable_fields.append(f"{tool['name']}.{field}")

    check(
        not nullable_fields,
        f"Codex built-in tool schemas omit unset optional args instead of allowing null: {nullable_fields}",
    )


def t_codex_dynamic_tools_respect_existing_tool_owners() -> None:
    owned = runner_codex._codex_existing_tool_names({
        "mcp_servers": {
            "ui": {},
            "custom": {"tool_names": ["custom_owned_tool"]},
        },
    })
    check("request_user_input" in owned, "Codex native request_user_input is owned before dynamic injection")
    check("open_file_panel" in owned, "Codex open-file-panel MCP owns open_file_panel")
    check("custom_owned_tool" in owned, "Codex MCP tool_names metadata contributes owned tools")

    tools: list[dict] = []
    handlers: dict[str, object] = {}
    added_native = runner_codex._add_dynamic_tool(
        tools,
        handlers,
        {"name": "request_user_input", "inputSchema": {"type": "object"}},
        object(),
        existing_tool_names=owned,
    )
    added_mcp = runner_codex._add_dynamic_tool(
        tools,
        handlers,
        runner_codex._build_open_file_panel_dynamic_tool(),
        object(),
        existing_tool_names=owned,
    )
    added_missing = runner_codex._add_dynamic_tool(
        tools,
        handlers,
        runner_codex._build_delegate_task_dynamic_tool(),
        object(),
        existing_tool_names=owned,
    )
    check(added_native is False, "Codex skips dynamic native-owned tool")
    check(added_mcp is False, "Codex skips dynamic MCP-owned tool")
    check(added_missing is True, "Codex adds dynamic tool when no owner exists")
    check([tool["name"] for tool in tools] == ["delegate_task"], "Codex dynamic tools contain only missing tools")

    try:
        runner_codex._add_dynamic_tool(
            tools,
            handlers,
            runner_codex._build_delegate_task_dynamic_tool(),
            object(),
            existing_tool_names=set(),
        )
    except ValueError:
        duplicate_failed = True
    else:
        duplicate_failed = False
    check(duplicate_failed, "Codex duplicate dynamic tool registration fails closed")


def t_gemini_materializes_isolated_home() -> None:
    real_home = Path(tempfile.mkdtemp(dir=_TMP_HOME))
    (real_home / ".gemini").mkdir()
    (real_home / ".gemini" / "google_accounts.json").write_text("{}", encoding="utf-8")
    (real_home / ".gemini" / "settings.json").write_text(
        json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}),
        encoding="utf-8",
    )
    old = os.environ.get("GEMINI_CLI_HOME")
    os.environ["GEMINI_CLI_HOME"] = str(real_home)
    try:
        run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        env = runner_gemini._materialize_gemini_run_home(run_dir, {
            "mcp_servers": {"demo": {"command": "echo"}},
            "skills": {"reviewer": "Review.\n"},
        }, cwd=str(real_home))
    finally:
        if old is None:
            os.environ.pop("GEMINI_CLI_HOME", None)
        else:
            os.environ["GEMINI_CLI_HOME"] = old
    overlay = Path(env["GEMINI_CLI_HOME"])
    settings = json.loads((overlay / "settings.json").read_text(encoding="utf-8"))
    check(settings["mcpServers"]["demo"]["command"] == "echo", "Gemini MCP settings are run-local")
    check(
        settings["security"]["auth"]["selectedType"] == "oauth-personal",
        "Gemini run-local settings preserve auth selection",
    )
    check((overlay / ".gemini" / "google_accounts.json").is_symlink(), "Gemini auth file is linked, not copied")
    skill = overlay / ".gemini" / "skills" / "reviewer" / "SKILL.md"
    check(skill.read_text(encoding="utf-8") == "Review.\n", "Gemini skill is written")


def t_gemini_max_tokens_result_is_context_overflow() -> None:
    err = runner_gemini._gemini_terminal_error({
        "type": "result",
        "status": "error",
        "stopReason": "max_tokens",
    })
    check(err == "context_window_exceeded", "Gemini max_tokens terminal result triggers continuation")
    check(
        runner_gemini._gemini_terminal_error({
            "type": "result",
            "status": "success",
            "stopReason": "max_tokens",
        }) is None,
        "Gemini successful max_tokens result is not treated as overflow",
    )


def t_builtin_user_facing_mcp_servers_injected() -> None:
    _install_requirements_extension_record()
    _install_core_mcp_gate_extensions()
    _configure_internal_llm_defaults(
        "default_session",
        "requirement_analysis",
        "project_structure_edit",
        "provider_config_sync_review",
    )
    config = builtin_mcp_config.with_builtin_mcp_servers({
        "open_file_panel_enabled": True,
        "browser_harness_enabled": True,
        "app_session_id": "bc-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-1",
    }, {
        "mcp_servers": {"demo": {"command": "echo"}},
    })
    servers = config["mcp_servers"]
    for name in (
        "demo",
        "better-agent-browser-harness",
        "better-agent-coordination",
        "better-agent-session-bridge",
        "credential-broker",
        "ui",
        "scheduler",
        "provider-config-sync",
        "better-agent-requirements",
        "better-agent-canvas",
    ):
        check(name in servers, f"built-in MCP config injects {name}")
    check("browser-harness" not in servers, "public browser-harness MCP is not injected")
    check("session-bridge" not in servers, "public session-bridge MCP is not injected")
    check("get-requirements" not in servers, "public requirements MCP is not injected")
    check("canvas" not in servers, "public canvas MCP is not injected")
    check("project-updates" not in servers, "project-updates is no longer injected as a built-in MCP")
    env = servers["scheduler"]["env"]
    check(env["BETTER_CLAUDE_EXTENSION_ID"] == extension_store.BUILTIN_SCHEDULER_EXTENSION_ID, "extension MCP env selects scheduler owner")
    check(env["BETTER_CLAUDE_APP_SESSION_ID"] == "bc-sid", "built-in MCP carries Better Agent session id")
    check(env["BETTER_CLAUDE_PROVIDER_ID"] == "prov-1", "built-in MCP carries provider id")
    check(
        servers["ui"]["args"][-1].endswith("open_file_panel_mcp.py"),
        "built-in MCP config points ui server at its MCP server",
    )


def t_codex_user_facing_mcp_servers_skip_open_file_panel_mcp() -> None:
    _install_requirements_extension_record()
    _install_core_mcp_gate_extensions()
    config = builtin_mcp_config.with_builtin_mcp_servers({
        "provider_kind": "codex",
        "open_file_panel_enabled": True,
        "app_session_id": "bc-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
    }, {})
    servers = config["mcp_servers"]
    check("ui" not in servers, "Codex omits ui MCP to avoid request_user_input collision")
    check("open-config-panel" in servers, "Codex keeps open-config-panel MCP")
    check("better-agent-coordination" in servers, "Codex keeps extension MCP servers")


def t_builtin_manager_mcp_servers_exclude_session_bridge() -> None:
    _install_requirements_extension_record()
    _install_core_mcp_gate_extensions()
    _configure_internal_llm_defaults("default_session", "requirement_analysis")
    config = builtin_mcp_config.with_builtin_mcp_servers({
        "open_file_panel_enabled": True,
        "app_session_id": "bc-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "manager",
    }, {})
    servers = config["mcp_servers"]
    check("better-agent-session-bridge" not in servers, "manager runs do not get session-bridge")
    check("better-agent-coordination" in servers, "manager runs get coordination")
    check("ui" in servers, "manager user-facing runs get ui server")
    check("scheduler" in servers, "manager user-facing runs still get scheduler")
    check("better-agent-requirements" in servers, "manager runs get requirements from private extension")


def t_builtin_mcp_servers_are_extension_owned() -> None:
    _install_requirements_extension_record()
    _install_core_mcp_gate_extensions()
    _configure_internal_llm_defaults(
        "default_session",
        "requirement_analysis",
        "project_structure_edit",
    )
    registry_servers = {item.mcp_server for item in extension_registry.BUILTIN_MCP_EXTENSIONS}
    check(registry_servers == set(), "public registry owns no private MCP fallbacks")
    # requirements is a dissolved private extension: it is disabled via its
    # enabled flag, not the disabled_builtin_extensions builtin override (which
    # only covers path-map builtins).
    extension_store.set_enabled(extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID, False)
    config = builtin_mcp_config.with_builtin_mcp_servers({
        "open_file_panel_enabled": True,
        "app_session_id": "bc-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
    }, {})
    servers = config["mcp_servers"]
    check("better-agent-requirements" not in servers, "disabled requirements extension removes its private MCP server")
    check("better-agent-canvas" in servers, "other private extension MCP servers remain active")
    extension_store.set_enabled(extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID, True)


def t_installed_extension_can_replace_reserved_builtin_mcp_name() -> None:
    _configure_internal_llm_defaults("default_session")
    package = Path(_TMP_HOME) / "project-structure-extension"
    (package / "mcp").mkdir(parents=True)
    script = package / "mcp" / "server.py"
    script.write_text("print('project updates')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID,
            "name": "Project Structure",
            "version": "1.0.0",
            "description": "",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "better-agent-project-updates",
                        "replaces_builtin": "project-updates",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "user_facing": False,
                        "bare_allowed": False,
                        "requires_backend_auth": True,
                    }
                ],
            },
            "permissions": {"internal_loopback": True},
            "marketplace": {},
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/project-structure",
            "ref": "",
            "commit_sha": "project-structure-private",
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
    _save_runtime_extension_record(data, extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID)
    config = builtin_mcp_config.with_builtin_mcp_servers({
        "open_file_panel_enabled": True,
        "app_session_id": "bc-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
    }, {})
    server = config["mcp_servers"].get("project-updates")
    check(server is not None, "installed extension replacement is exposed under reserved MCP name")
    check(Path(server["args"][0]).resolve() == script.resolve(), "replacement MCP points at private package script")


def t_installed_extension_mcp_servers_are_injected() -> None:
    _configure_internal_llm_defaults("default_session")
    package = Path(_TMP_HOME) / "runtime-extension"
    (package / "mcp").mkdir(parents=True)
    script = package / "mcp" / "server.py"
    script.write_text("print('runtime mcp')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"]["ofek.runtime"] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": "ofek.runtime",
            "name": "Runtime",
            "version": "1.0.0",
            "description": "",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "backend": "",
                "frontend": "",
                "mcp": [
                    {
                        "name": "ofek-runtime",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {"OF_RUNTIME": "1"},
                        "user_facing": True,
                        "bare_allowed": False,
                        "requires_backend_auth": True,
                    }
                ],
                "provider_capabilities": [],
            },
            "permissions": {"internal_loopback": True},
            "marketplace": {
                "product_id": "",
                "subscription_required": False,
                "entitlement_url": "",
            },
        }),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/extensions.git",
            "extension_path": "extensions/runtime",
            "ref": "",
            "commit_sha": "abc",
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
    _save_runtime_extension_record(data, "ofek.runtime")
    config = builtin_mcp_config.with_builtin_mcp_servers({
        "open_file_panel_enabled": True,
        "app_session_id": "bc-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
    }, {})
    runtime = config["mcp_servers"].get("ofek-runtime")
    check(runtime is not None, "installed extension MCP server is injected")
    check(Path(runtime["args"][0]).resolve() == script.resolve(), "installed extension MCP points at package script")
    check(runtime["env"]["BETTER_CLAUDE_EXTENSION_ID"] == "ofek.runtime", "installed extension MCP carries extension id")
    check(runtime["env"]["OF_RUNTIME"] == "1", "installed extension MCP carries manifest env")


def t_runtime_mcp_servers_reload_after_backend_restart_simulation() -> None:
    _install_requirements_extension_record()
    _install_core_mcp_gate_extensions()
    _configure_internal_llm_defaults(
        "default_session",
        "requirement_analysis",
        "project_structure_edit",
        "provider_config_sync_review",
    )
    inputs = {
        "open_file_panel_enabled": True,
        "browser_harness_enabled": True,
        "app_session_id": "restart-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-restart",
    }
    before = builtin_mcp_config.with_builtin_mcp_servers(inputs, {})["mcp_servers"]
    _simulate_backend_restart()
    after = builtin_mcp_config.with_builtin_mcp_servers(inputs, {})["mcp_servers"]
    for name in (
        "capabilities",
        "open-config-panel",
        "ui",
        "better-agent-requirements",
        "better-agent-coordination",
        "better-agent-session-bridge",
        "provider-config-sync",
    ):
        check(name in before, f"restart simulation baseline includes {name}")
        check(name in after, f"restart simulation keeps {name}")
    check(
        after["better-agent-requirements"]["env"]["BETTER_CLAUDE_APP_SESSION_ID"] == "restart-sid",
        "restart simulation keeps runtime extension MCP session env",
    )


def t_native_mcp_launcher_reresolves_after_backend_restart_simulation() -> None:
    _install_requirements_extension_record(delivery="native", replaces_builtin=True)
    _configure_internal_llm_defaults("requirement_analysis")
    inputs = {
        "open_file_panel_enabled": True,
        "app_session_id": "native-restart-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-native-restart",
    }
    config = builtin_mcp_config.with_builtin_mcp_servers(inputs, {})
    req = config["mcp_servers"].get("get-requirements")
    check(req is not None, "native restart baseline includes requirements launcher")
    if not req:
        return
    launcher_env = {
        **builtin_mcp_config.native_mcp_runtime_env(inputs),
        **dict(req.get("env") or {}),
    }
    saved = {key: os.environ.get(key) for key in launcher_env}
    try:
        _simulate_backend_restart()
        os.environ.update(launcher_env)
        runtime_inputs = extension_mcp_launcher._runtime_inputs()
        resolved = extension_store.resolve_native_mcp_server_config(
            extension_id=req["env"]["BETTER_CLAUDE_EXTENSION_ID"],
            server_name=req["env"]["BETTER_CLAUDE_EXTENSION_MCP_SERVER"],
            inputs=runtime_inputs,
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check(resolved is not None, "native launcher re-resolves extension MCP after restart")
    if resolved:
        check(
            Path(resolved["args"][0]).name == "server.py",
            "native launcher restart resolution points at extension MCP script",
        )
        check(
            resolved["env"]["BETTER_CLAUDE_APP_SESSION_ID"] == "native-restart-sid",
            "native launcher restart resolution keeps session env",
        )


def t_builtin_mcp_registry_applies_to_all_provider_runners() -> None:
    _install_requirements_extension_record()
    _configure_internal_llm_defaults("default_session", "requirement_analysis")
    runner_src = (Path(_BACKEND) / "runner.py").read_text(encoding="utf-8")
    check(
        "extension_store.runtime_mcp_server_configs(" in runner_src,
        "Claude runner gates extension MCP registration through shared runtime config",
    )
    check(
        '"session-bridge" in _active_builtin_mcp_servers' not in runner_src,
        "Claude session bridge public fallback is absent",
    )
    check(
        "runtime_mcp_server_configs(" in runner_src,
        "Claude runner injects installed extension MCP servers",
    )
    check(
        "extension_store.native_mcp_server_configs(" in runner_src,
        "Claude runner injects native-delivery extension MCP servers",
    )
    check(
        "native_mcp_launcher_server_configs(" in (Path(_BACKEND) / "builtin_mcp_config.py").read_text(encoding="utf-8"),
        "native CLI provider config injects extension launchers instead of resolved native MCP configs",
    )
    check(
        "provider_config_sync_mcp_server_config(" not in runner_src
        and '"provider-config-sync"' not in runner_src,
        "Claude provider-config-sync MCP registration is private runtime-owned",
    )
    supervisor_src = (Path(_BACKEND) / "orchs" / "supervisor" / "__init__.py").read_text(encoding="utf-8")
    orchestrator_src = (Path(_BACKEND) / "orchestrator.py").read_text(encoding="utf-8")
    main_src = (Path(_BACKEND) / "main.py").read_text(encoding="utf-8")
    check(
        "is_extension_runtime_ready(" in supervisor_src
        and "BUILTIN_SUPERVISOR_EXTENSION_ID" in supervisor_src,
        "supervisor loop checks extension runtime readiness",
    )
    check(
        "runtime_not_ready_message(" in orchestrator_src
        and "BUILTIN_SUPERVISOR_EXTENSION_ID" in orchestrator_src,
        "direct supervisor target checks extension runtime readiness",
    )
    main_enabled_only_uses = [
        line
        for line in main_src.splitlines()
        if "_builtin_extension_enabled(" in line
        and "def _builtin_extension_enabled" not in line
        and "if not _builtin_extension_enabled(extension_id)" not in line
    ]
    direct_enabled_only_uses = [
        f"{path.name}: {line}"
        for path, src in (
            (Path(_BACKEND) / "main.py", main_src),
            (Path(_BACKEND) / "orchestrator.py", orchestrator_src),
            (Path(_BACKEND) / "main_node.py", (Path(_BACKEND) / "main_node.py").read_text(encoding="utf-8")),
            (Path(_BACKEND) / "node_link.py", (Path(_BACKEND) / "node_link.py").read_text(encoding="utf-8")),
        )
        for line in src.splitlines()
        if "is_builtin_feature_enabled(" in line
        and "def _builtin_extension_enabled" not in line
        and "return extension_store.is_builtin_feature_enabled(extension_id)" not in line
    ]
    check(
        not main_enabled_only_uses and not direct_enabled_only_uses,
        (
            "runtime paths avoid enabled-only extension gates: "
            f"{main_enabled_only_uses + direct_enabled_only_uses}"
        ),
    )
    for provider_name in ("codex", "gemini"):
        _install_core_mcp_gate_extensions()
        config = builtin_mcp_config.with_builtin_mcp_servers({
            "open_file_panel_enabled": True,
            "app_session_id": f"{provider_name}-sid",
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "secret",
            "mode": "native",
        }, {})
        servers = config["mcp_servers"]
        check("better-agent-requirements" in servers, f"{provider_name} gets requirements through private extension")
        check("better-agent-session-bridge" in servers, f"{provider_name} gets session bridge through private extension")
        check("better-agent-coordination" in servers, f"{provider_name} gets coordination through public extension")


def t_requirements_mcp_uses_private_extension() -> None:
    _install_requirements_extension_record()
    _configure_internal_llm_defaults("requirement_analysis")
    config = builtin_mcp_config.with_builtin_mcp_servers({
        "open_file_panel_enabled": True,
        "app_session_id": "normal-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
    }, {})
    servers = config["mcp_servers"]
    check("get-requirements" not in servers, "normal runs do not use the public requirements MCP")
    check("better-agent-requirements" in servers, "normal runs use private requirements MCP")


def t_better_agent_runner_uses_extension_mcp_configs() -> None:
    _install_requirements_extension_record()
    _configure_internal_llm_defaults("requirement_analysis")
    inputs = {
        "open_file_panel_enabled": True,
        "app_session_id": "ba-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-ba",
    }
    configs = runner_better_agent._extension_mcp_server_configs_for_run(
        inputs, user_facing=True, bare=False,
    )
    check(
        "better-agent-requirements" in configs,
        "Better Agent runner gets requirements through private extension",
    )
    headless = dict(inputs)
    headless["open_file_panel_enabled"] = False
    check(
        "better-agent-requirements" in runner_better_agent._extension_mcp_server_configs_for_run(
            headless, user_facing=False, bare=False,
        ),
        "Better Agent runner keeps requirements MCP for authenticated headless sessions",
    )

    missing_token = dict(inputs)
    missing_token["internal_token"] = ""
    check(
        "better-agent-requirements" not in runner_better_agent._extension_mcp_server_configs_for_run(
            missing_token, user_facing=True, bare=False,
        ),
        "Better Agent runner omits requirements MCP without backend auth",
    )
    check(
        "better-agent-requirements" not in runner_better_agent._extension_mcp_server_configs_for_run(
            inputs, user_facing=True, bare=True,
        ),
        "Better Agent runner omits requirements MCP for bare runs",
    )


def t_native_requirements_mcp_injected_with_run_auth() -> None:
    _install_requirements_extension_record(delivery="native", replaces_builtin=True)
    _configure_internal_llm_defaults("requirement_analysis")
    inputs = {
        "open_file_panel_enabled": True,
        "app_session_id": "bc-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-1",
    }
    config = builtin_mcp_config.with_builtin_mcp_servers(inputs, {})
    servers = config["mcp_servers"]
    req = servers.get("get-requirements")
    check(req is not None, "native requirements MCP is injected per managed run")
    if req:
        env = req["env"]
        check(env["BETTER_CLAUDE_EXTENSION_ID"] == extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID, "native requirements MCP env selects requirements extension")
        check(env["BETTER_CLAUDE_EXTENSION_MCP_SERVER"] == "better-agent-requirements", "native requirements MCP env selects extension server")
        check(env["BETTER_CLAUDE_BACKEND_URL"] == "http://127.0.0.1:8000", "native requirements MCP launcher env carries backend URL")
        check(env["BETTER_CLAUDE_APP_SESSION_ID"] == "bc-sid", "native requirements MCP launcher env carries app session id")
        check(env["BETTER_CLAUDE_CWD"] == "/tmp/project", "native requirements MCP launcher env carries cwd")
        check(env["BETTER_CLAUDE_PROVIDER_ID"] == "prov-1", "native requirements MCP launcher env carries provider id")
        check(env["BETTER_CLAUDE_MODE"] == "native", "native requirements MCP launcher env carries mode")
        check(env["BETTER_CLAUDE_USER_FACING"] == "1", "native requirements MCP launcher env carries user-facing flag")
        check("BETTER_CLAUDE_INTERNAL_TOKEN" not in env, "native requirements MCP config does not carry per-run internal token")
        check(req["args"][0].endswith("extension_mcp_launcher.py"), "native requirements MCP points at extension launcher")
        codex_overrides = runner_codex._codex_config_overrides(Path(tempfile.mkdtemp(dir=_TMP_HOME)), {
            "mcp_servers": {"get-requirements": req},
        })
        check("secret" not in "\n".join(codex_overrides), "Codex native requirements MCP override does not expose token")
        serialized = json.dumps(req, sort_keys=True)
        check("secret" not in serialized, "native requirements MCP provider config does not expose token")
    runtime_env = builtin_mcp_config.native_mcp_runtime_env(inputs)
    check(runtime_env["BETTER_CLAUDE_INTERNAL_TOKEN"] == "secret", "native requirements MCP runtime env carries per-run internal token")
    check(runtime_env["BETTER_CLAUDE_CWD"] == "/tmp/project", "native requirements MCP runtime env carries per-run cwd")
    check(runtime_env["BETTER_CLAUDE_USER_FACING"] == "1", "native requirements MCP runtime env marks user-facing runs")

    missing_token = dict(inputs)
    missing_token["internal_token"] = ""
    check(
        "get-requirements" not in builtin_mcp_config.with_builtin_mcp_servers(missing_token, {})["mcp_servers"],
        "native requirements MCP is omitted without backend auth",
    )
    headless = dict(inputs)
    headless["open_file_panel_enabled"] = False
    check(
        "get-requirements" in builtin_mcp_config.with_builtin_mcp_servers(headless, {})["mcp_servers"],
        "native requirements MCP is kept for authenticated headless runs",
    )
    bare = dict(inputs)
    bare["bare_config"] = True
    check(
        "get-requirements" not in builtin_mcp_config.with_builtin_mcp_servers(bare, {})["mcp_servers"],
        "native requirements MCP is omitted for bare runs",
    )
    # requirements is a dissolved private extension: disabling it via its enabled
    # flag (not the disabled_builtin_extensions builtin override) omits its MCP.
    extension_store.set_enabled(extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID, False)
    check(
        "get-requirements" not in builtin_mcp_config.with_builtin_mcp_servers(inputs, {})["mcp_servers"],
        "native requirements MCP is omitted when the extension is disabled",
    )
    extension_store.set_enabled(extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID, True)


def t_bare_testape_mcp_uses_native_launcher() -> None:
    _install_testape_extension_record()
    inputs = {
        "open_file_panel_enabled": False,
        "app_session_id": "testape-bare-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-testape",
        "bare_config": True,
    }
    config = builtin_mcp_config.with_builtin_mcp_servers(inputs, {})
    server = config["mcp_servers"].get("testape")
    check(server is not None, "bare TestApe MCP is injected")
    if not server:
        return
    check(server["args"][0].endswith("extension_mcp_launcher.py"), "bare TestApe MCP uses native launcher")
    check(server["args"][-2:] == [extension_store.BUILTIN_TESTAPE_EXTENSION_ID, "testape"], "bare TestApe launcher selects TestApe server")
    check(server["env"]["BETTER_CLAUDE_EXTENSION_ID"] == extension_store.BUILTIN_TESTAPE_EXTENSION_ID, "bare TestApe launcher env carries extension id")
    check(server["env"]["BETTER_CLAUDE_BARE_CONFIG"] == "1", "bare TestApe launcher env carries bare flag")
    check(server["env"]["BETTER_CLAUDE_APP_SESSION_ID"] == "testape-bare-sid", "bare TestApe launcher env carries session id")
    check("BETTER_CLAUDE_INTERNAL_TOKEN" not in server["env"], "bare TestApe provider config does not expose internal token")
    raw = extension_store.native_mcp_server_configs(inputs, user_facing=False, bare=True).get("testape")
    check(raw is not None, "bare TestApe raw native config is available for Claude SDK bridge")
    if raw:
        check("BETTER_CLAUDE_INTERNAL_TOKEN" not in raw["env"], "bare TestApe raw native config does not expose internal token")


def t_bare_mcp_availability_matrix() -> None:
    _install_bare_matrix_extension_record()
    inputs = {
        "open_file_panel_enabled": False,
        "app_session_id": "bare-matrix-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-bare-matrix",
        "bare_config": True,
    }
    servers = extension_store.native_mcp_launcher_server_configs(
        inputs,
        user_facing=False,
        bare=True,
    )
    check("headless-bare" in servers, "bare non-user-facing MCP is available when bare_allowed")
    check("visible-bare" in servers, "bare user-facing MCP is available when bare_allowed")
    check("visible-not-bare" not in servers, "bare user-facing MCP is excluded without bare_allowed")


def t_open_file_panel_mcp_validates_required_fields() -> None:
    result = open_file_panel_mcp.open_file_panel_response("panel", "")
    check(result["success"] is False, "open-file-panel MCP rejects missing path before HTTP")


def t_request_user_input_mcp_validates_required_fields() -> None:
    result = open_file_panel_mcp.request_user_input_response([])
    check(result["success"] is False, "request-user-input MCP rejects missing questions before HTTP")


def t_provider_sources_persist_open_file_panel_flag() -> None:
    codex_src = (Path(_BACKEND) / "provider_codex.py").read_text(encoding="utf-8")
    gemini_src = (Path(_BACKEND) / "provider_gemini.py").read_text(encoding="utf-8")
    check(
        '"open_file_panel_enabled": bool(open_file_panel_enabled)' in codex_src,
        "Codex provider persists open_file_panel_enabled into runner input",
    )
    check(
        '"provider_kind": self.KIND' in codex_src,
        "Codex provider persists provider_kind into runner input",
    )
    check(
        '"open_file_panel_enabled": bool(open_file_panel_enabled)' in gemini_src,
        "Gemini provider persists open_file_panel_enabled into runner input",
    )
    check(
        '"browser_harness_enabled": bool(browser_harness_enabled)' in codex_src,
        "Codex provider persists browser_harness_enabled into runner input",
    )
    check(
        '"browser_harness_enabled": bool(browser_harness_enabled)' in gemini_src,
        "Gemini provider persists browser_harness_enabled into runner input",
    )
    check(
        '"context_strategy": user_prefs.get_context_strategy()' in codex_src,
        "Codex provider persists context_strategy into runner input",
    )
    check(
        '"context_strategy": user_prefs.get_context_strategy()' in gemini_src,
        "Gemini provider persists context_strategy into runner input",
    )
    check(
        '"disabled_builtin_extensions": (' in codex_src
        and "disabled_builtin_extensions_for_run(" in codex_src,
        "Codex provider persists disabled built-in extensions into runner input",
    )
    check(
        '"worker_working_mode": (_worker_sess_rec or {}).get("working_mode")' in codex_src,
        "Codex provider persists worker working mode into runner input",
    )
    check(
        '"disabled_builtin_extensions": (' in gemini_src
        and "disabled_builtin_extensions_for_run(" in gemini_src,
        "Gemini provider persists disabled built-in extensions into runner input",
    )
    check(
        '"worker_working_mode": (_worker_sess_rec or {}).get("working_mode")' in gemini_src,
        "Gemini provider persists worker working mode into runner input",
    )
    claude_src = (Path(_BACKEND) / "provider_claude.py").read_text(encoding="utf-8")
    check(
        '"provider_run_config": provider_run_config or {}' in claude_src,
        "Claude provider persists provider_run_config into runner input",
    )
    check(
        '"disabled_builtin_extensions": (' in claude_src
        and "disabled_builtin_extensions_for_run(" in claude_src,
        "Claude provider persists disabled built-in extensions into runner input",
    )
    check(
        '"worker_working_mode": (_worker_sess_rec or {}).get("working_mode")' in claude_src,
        "Claude provider persists worker working mode into runner input",
    )
    remote_src = (Path(_BACKEND) / "provider_remote.py").read_text(encoding="utf-8")
    node_handler_src = (Path(_BACKEND) / "node_rpc_handlers.py").read_text(encoding="utf-8")
    node_protocol_src = (Path(_BACKEND) / "node_protocol.py").read_text(encoding="utf-8")
    check(
        '"disabled_builtin_extensions": (' in remote_src,
        "Remote provider ships disabled built-in extensions in spawn_run payload",
    )
    check(
        "disabled_builtin_extensions=msg.get(\"disabled_builtin_extensions\")" in node_handler_src,
        "Worker node forwards disabled built-in extensions into local provider",
    )
    check(
        "disabled_builtin_extensions: Optional[list[str]]" in node_protocol_src,
        "Node protocol types disabled built-in extensions",
    )


def t_provider_runner_env_pins_better_agent_home() -> None:
    env = provider.build_better_agent_run_env(
        backend_url="http://127.0.0.1:8000",
        internal_token="secret",
        app_session_id="session-1",
        cwd="/tmp/project",
        model="model",
        provider_id="provider-1",
        bare_config=True,
        user_facing=False,
        disabled_builtin_extensions=["ofek.testape-internal"],
    )
    home = str(ba_home())
    check(env["BETTER_AGENT_HOME"] == home, "runner env pins primary Better Agent home")
    check(env["BETTER_CLAUDE_HOME"] == home, "runner env pins legacy Better Agent home")
    check(
        env["BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS"] == "ofek.testape-internal",
        "runner env keeps disabled built-in extensions",
    )
    check("CLAUDE_CONFIG_DIR" not in env, "runner env does not override provider Claude config")


def main() -> int:
    for name, fn in [
        ("normalizes unified mcp key", t_normalizes_unified_mcp_key),
        ("codex materializes mcp and skills", t_codex_materializes_mcp_and_skills),
        ("codex runner inputs self-identify provider kind", t_codex_runner_inputs_self_identify_provider_kind),
        ("codex context strategy overrides auto compact", t_codex_context_strategy_overrides_auto_compact),
        ("claude materializes runtime skills plugin", t_claude_materializes_runtime_skills_plugin),
        ("codex open-file-panel dynamic tool", t_codex_open_file_panel_dynamic_tool),
        ("codex built-in tool schemas do not invite null defaults", t_codex_builtin_tool_schemas_do_not_invite_null_defaults),
        ("codex dynamic tools respect existing tool owners", t_codex_dynamic_tools_respect_existing_tool_owners),
        ("gemini materializes isolated home", t_gemini_materializes_isolated_home),
        ("gemini max_tokens result is context overflow", t_gemini_max_tokens_result_is_context_overflow),
        ("built-in user-facing mcp servers injected", t_builtin_user_facing_mcp_servers_injected),
        ("built-in manager mcp servers exclude session bridge", t_builtin_manager_mcp_servers_exclude_session_bridge),
        ("built-in mcp servers are extension owned", t_builtin_mcp_servers_are_extension_owned),
        ("installed extension can replace reserved builtin mcp name", t_installed_extension_can_replace_reserved_builtin_mcp_name),
        ("installed extension mcp servers are injected", t_installed_extension_mcp_servers_are_injected),
        ("runtime mcp servers reload after backend restart simulation", t_runtime_mcp_servers_reload_after_backend_restart_simulation),
        ("native mcp launcher re-resolves after backend restart simulation", t_native_mcp_launcher_reresolves_after_backend_restart_simulation),
        ("built-in mcp registry applies to all provider runners", t_builtin_mcp_registry_applies_to_all_provider_runners),
        ("codex user-facing mcp servers skip open-file-panel mcp", t_codex_user_facing_mcp_servers_skip_open_file_panel_mcp),
        ("requirements mcp uses private extension", t_requirements_mcp_uses_private_extension),
        ("better-agent runner uses extension mcp configs", t_better_agent_runner_uses_extension_mcp_configs),
        ("native requirements mcp injected with run auth", t_native_requirements_mcp_injected_with_run_auth),
        ("bare TestApe mcp uses native launcher", t_bare_testape_mcp_uses_native_launcher),
        ("bare mcp availability matrix", t_bare_mcp_availability_matrix),
        ("open-file-panel mcp validates required fields", t_open_file_panel_mcp_validates_required_fields),
        ("request-user-input mcp validates required fields", t_request_user_input_mcp_validates_required_fields),
        ("providers persist open-file-panel flag", t_provider_sources_persist_open_file_panel_flag),
        ("provider runner env pins Better Agent home", t_provider_runner_env_pins_better_agent_home),
    ]:
        print(f"\n--- {name} ---")
        try:
            fn()
        except Exception as e:
            FAILURES.append(f"{name}: {e!r}")
            import traceback
            traceback.print_exc()
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} assertion(s)")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

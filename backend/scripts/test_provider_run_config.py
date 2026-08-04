from __future__ import annotations

import importlib
import asyncio
import json
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import _test_home
import _test_installation
_TMP_HOME = _test_home.isolate("bc-test-provider-run-config-")
os.environ["BETTER_AGENT_RUNTIME_BROKER"] = "unix:/tmp/better-agent-test.sock"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provider_run_config  # noqa: E402
import provider  # noqa: E402
import runner  # noqa: E402
import runner_better_agent  # noqa: E402
import runner_codex  # noqa: E402
import runner_agy  # noqa: E402
import runtime_skills  # noqa: E402
import open_file_panel_mcp  # noqa: E402
import builtin_mcp_config  # noqa: E402
import extension_registry  # noqa: E402
import extension_store  # noqa: E402
import extension_mcp_launcher  # noqa: E402
import config_store  # noqa: E402
import dependency_plan  # noqa: E402
import installation_profile  # noqa: E402
from codex_execution_contract import build_codex_execution_contract  # noqa: E402
from paths import ba_home  # noqa: E402
from provider_runtime_plan_source import structural_provider_runtime_plan  # noqa: E402
from provider_session_events_runner import effective_mcp_servers  # noqa: E402

_test_installation.activate(Path(_TMP_HOME))
installation_profile.capture_active_capabilities()
dependency_plan.verified_active_python = lambda _backend: Path(sys.executable)

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        FAILURES.append(msg)


def _check_sdk_script_server(server: dict, script: Path, message: str) -> None:
    args = server.get("args") or []
    check(
        Path(server.get("command") or "").resolve() == Path(sys.executable).resolve()
        and args[:2] == ["-m", "better_agent_sdk.script_entrypoint"]
        and len(args) >= 4
        and Path(args[3]).resolve() == script.resolve(),
        message,
    )


def _configure_internal_llm_defaults(*tasks: str) -> None:
    assignments = config_store.get_internal_llm_assignments()
    for task in tasks:
        assignments[task] = _test_installation.default_llm_assignment()
    config_store.set_internal_llm_assignments(assignments)


def _simulate_backend_restart() -> None:
    importlib.reload(extension_store)
    importlib.reload(extension_mcp_launcher)
    importlib.reload(builtin_mcp_config)


def _save_runtime_extension_record(data: dict, extension_id: str) -> None:
    extension_store._save(data)  # type: ignore[attr-defined]


_FIXTURE_BROWSER_HARNESS_EXTENSION_ID = "fixture.browser-harness"
_FIXTURE_CANVAS_EXTENSION_ID = "fixture.canvas"
_FIXTURE_CREDENTIAL_BROKER_EXTENSION_ID = "fixture.credential-broker"
_FIXTURE_PROJECT_STRUCTURE_EXTENSION_ID = "fixture.project-structure"
_FIXTURE_REQUIREMENTS_EXTENSION_ID = "fixture.requirements"
_FIXTURE_SCHEDULER_EXTENSION_ID = "fixture.scheduler"
_FIXTURE_TEAM_ORCHESTRATION_EXTENSION_ID = "fixture.team-orchestration"
_FIXTURE_TESTAPE_EXTENSION_ID = "fixture.testape"


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
        "bare_allowed": True,
        "requires_backend_auth": True,
    }
    if replaces_builtin:
        mcp_entry["replaces_builtin"] = "get-requirements"
    data["extensions"][_FIXTURE_REQUIREMENTS_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": _FIXTURE_REQUIREMENTS_EXTENSION_ID,
            "core_roles": ["requirements"],
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
    record = data["extensions"][_FIXTURE_REQUIREMENTS_EXTENSION_ID]
    record["consent"] = {
        "fingerprint": extension_store.permission_consent_fingerprint(record),
        "granted_at": "2026-01-01T00:00:00+00:00",
    }
    extension_store._save(data)  # type: ignore[attr-defined]


def _install_feature_extension_record(
    extension_id: str,
    permissions: dict | None = None,
    *,
    core_role: str | None = None,
) -> None:
    package = Path(_TMP_HOME) / f"{extension_id}-feature-extension"
    package.mkdir(parents=True, exist_ok=True)
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_id,
            **({"core_roles": [core_role]} if core_role else {}),
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
    data["extensions"][_FIXTURE_SCHEDULER_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": _FIXTURE_SCHEDULER_EXTENSION_ID,
            "core_roles": ["scheduler"],
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
    _save_runtime_extension_record(data, _FIXTURE_SCHEDULER_EXTENSION_ID)


def _install_core_mcp_gate_extensions() -> None:
    _install_feature_extension_record(
        _FIXTURE_TEAM_ORCHESTRATION_EXTENSION_ID,
        {"session_state": True, "internal_loopback": True},
        core_role="team-orchestration",
    )
    _install_coordination_extension_record()
    _install_session_bridge_extension_record()
    _install_browser_harness_extension_record()
    _install_credential_broker_extension_record()
    _install_canvas_extension_record()
    _install_scheduler_extension_record()


def _install_browser_harness_extension_record() -> None:
    package = Path(_TMP_HOME) / "browser-harness-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('browser harness')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][_FIXTURE_BROWSER_HARNESS_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": _FIXTURE_BROWSER_HARNESS_EXTENSION_ID,
            "core_roles": ["browser-harness"],
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
    _save_runtime_extension_record(data, _FIXTURE_BROWSER_HARNESS_EXTENSION_ID)


def _install_credential_broker_extension_record() -> None:
    package = Path(_TMP_HOME) / "credential-broker-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('credential broker')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][_FIXTURE_CREDENTIAL_BROKER_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": _FIXTURE_CREDENTIAL_BROKER_EXTENSION_ID,
            "core_roles": ["credential-broker"],
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
    _save_runtime_extension_record(data, _FIXTURE_CREDENTIAL_BROKER_EXTENSION_ID)


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
    data["extensions"][_FIXTURE_CANVAS_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": _FIXTURE_CANVAS_EXTENSION_ID,
            "core_roles": ["canvas"],
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
    _save_runtime_extension_record(data, _FIXTURE_CANVAS_EXTENSION_ID)


def _install_testape_extension_record() -> None:
    package = Path(_TMP_HOME) / "testape-extension"
    (package / "mcp").mkdir(parents=True, exist_ok=True)
    (package / "mcp" / "server.py").write_text("print('testape')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][_FIXTURE_TESTAPE_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": _FIXTURE_TESTAPE_EXTENSION_ID,
            "core_roles": ["testape"],
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
                        "ambient_native": True,
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
            "permissions": {"native_mcp": {"headless-bare": ["global"]}},
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
    # kind="mcp" is no longer settable via set_native_harness_exposed -- native
    # MCP exposure is granted via grant_native_mcp_server() now.
    extension_store.grant_native_mcp_server(extension_id, "headless-bare", "global")


def t_normalizes_unified_mcp_key() -> None:
    config = provider_run_config.normalize_provider_run_config({
        "mcpServers": {"demo": {"command": "echo"}},
        "skills": {"reviewer": "Review.\n"},
        "fork_parent_line_count": 42,
    })
    check(config["mcp_servers"]["demo"]["command"] == "echo", "mcpServers normalizes to mcp_servers")
    check(config["skills"]["reviewer"] == "Review.\n", "skills pass through")
    check(config["fork_parent_line_count"] == 42, "dynamic run-local fields pass through")


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
        (home / ".zshenv").write_text('. "$HOME/.cargo/env"\n', encoding="utf-8")
        (home / ".bashrc").write_text(". ~/.profile\n", encoding="utf-8")
        (home / ".codex").mkdir()
        (home / ".codex" / "config.toml").write_text("", encoding="utf-8")
        codex_launcher = home / "codex"
        codex_launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codex_launcher.chmod(0o700)
        contract = build_codex_execution_contract(
            {
                "id": "codex-fixture",
                "kind": "codex",
                "generation": "fixture-generation",
                "revision": 1,
                "mode": "subscription",
                "config_dir": str(home / ".codex"),
            },
            launcher_path=str(codex_launcher),
            environment_selectors={"CODEX_HOME": str(home / ".codex")},
        )
        run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        env = runner_codex._materialize_codex_run_home(
            run_dir,
            {
                "skills": {"reviewer": {"description": "Review code", "instructions": "Review carefully.\n"}},
            },
            execution_contract=contract,
            cwd=str(home),
        )
        overrides = runner_codex._codex_config_overrides(run_dir, {
            "mcp_servers": {"demo": {"command": "echo", "args": ["hello"]}},
        })
        bare_run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        bare_env = runner_codex._materialize_codex_run_home(
            bare_run_dir,
            {},
            execution_contract=contract,
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
    codex_home = Path(env["CODEX_HOME"])
    check(
        codex_home.is_dir()
        and not codex_home.is_symlink()
        and (codex_home / "config.toml").read_text(encoding="utf-8") == "",
        "Codex config home is an isolated attested overlay",
    )
    check(not (overlay_home / ".zshenv").exists(), "Codex run home skips zsh startup files")
    check(not (overlay_home / ".bashrc").exists(), "Codex run home skips bash startup files")
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
    try:
        runner_codex._codex_runner_inputs({
            "provider_kind": "stale",
            "cwd": "/tmp/project",
        })
    except ValueError:
        stale_rejected = True
    else:
        stale_rejected = False
    check(stale_rejected, "Codex runner rejects stale provider identity")
    inputs = runner_codex._codex_runner_inputs({
        "provider_kind": "codex",
        "cwd": "/tmp/project",
    })
    check(inputs["provider_kind"] == "codex", "Codex runner preserves attested provider kind")
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
        skill.chmod(0o400)
        run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        plugin = runner._materialize_claude_runtime_plugin(
            run_dir,
            {"skills": {
                "get-requirements": "Override copied skill.\n",
                "reviewer": "Review carefully.\n",
            }},
            bare_config=False,
            skill_dirs={"get-requirements": skill.parent},
            agent_files={},
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
        manifest["name"] == runner.CLAUDE_RUNTIME_SKILLS_PLUGIN_NAME,
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
    copied_skill = plugin_path / "skills" / "get-requirements" / "SKILL.md"
    check(
        "Override copied skill." in copied_skill.read_text(encoding="utf-8"),
        "configured Claude skill atomically overrides read-only copied skill",
    )
    check(
        copied_skill.stat().st_mode & 0o444 == 0o444,
        "Claude runtime skills plugin is readable by the provider subprocess",
    )
    check(
        plugin_path.stat().st_mode & 0o555 == 0o555,
        "Claude runtime skills plugin directories are traversable",
    )


def t_provider_skill_names_and_atomic_replacement_are_safe() -> None:
    root = Path(tempfile.mkdtemp(dir=_TMP_HOME))
    for name in (
        ".",
        "..",
        " padded",
        "padded ",
        "nested/skill",
        r"nested\skill",
        "C:",
        "C:skill",
        "CON",
        "nul.txt",
        "trailing.",
    ):
        try:
            provider_run_config.write_skill_tree(root, {name: "Unsafe.\n"})
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe provider skill name accepted: {name!r}")

    skill_dir = root / "reviewer"
    skill_dir.mkdir(parents=True)
    collision = skill_dir / ".SKILL.md.tmp"
    collision.write_text("owned by another writer\n", encoding="utf-8")
    provider_run_config.write_skill_tree(root, {"reviewer": "Review carefully.\n"})
    check(
        collision.read_text(encoding="utf-8") == "owned by another writer\n",
        "provider skill replacement does not reuse or remove another writer's temp file",
    )
    check(
        (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        == "Review carefully.\n",
        "provider skill replacement remains atomic with a pre-existing temp file",
    )


def t_claude_managed_mcp_config_is_authoritative() -> None:
    regular = runner._claude_cli_extra_args(bare_config=False)
    bare = runner._claude_cli_extra_args(bare_config=True)
    check(
        "strict-mcp-config" in regular and "strict-mcp-config" in bare,
        "Claude rejects project and user MCP servers outside the frozen run artifact",
    )
    check(
        "disable-slash-commands" not in regular
        and "disable-slash-commands" in bare,
        "Claude preserves the bare-only slash-command isolation",
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
        runner_codex._build_request_user_input_dynamic_tool(),
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
    check("request_user_input" in owned, "Codex ui MCP request_user_input is owned before dynamic injection")
    check("open_file_panel" in owned, "Codex open-file-panel MCP owns open_file_panel")
    check("custom_owned_tool" in owned, "Codex MCP tool_names metadata contributes owned tools")

    tools: list[dict] = []
    handlers: dict[str, object] = {}
    added_request_user_input = runner_codex._add_dynamic_tool(
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
    check(added_request_user_input is False, "Codex skips dynamic request_user_input when ui MCP owns it")
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


def t_codex_request_user_input_uses_better_agent_dynamic_tool() -> None:
    owned = runner_codex._codex_existing_tool_names({
        "mcp_servers": {
            "custom": {"tool_names": ["custom_owned_tool"]},
        },
    })
    check(
        "request_user_input" not in owned,
        "Codex does not reserve request_user_input as a native-owned Default-mode tool",
    )

    owned_with_ui_mcp = runner_codex._codex_existing_tool_names({
        "mcp_servers": {
            "ui": {},
        },
    })
    check(
        "request_user_input" in owned_with_ui_mcp,
        "Codex preserves request_user_input ownership when an actual ui MCP is configured",
    )

    dynamic_tools, handlers = runner_codex._build_dynamic_tool_set(
        mode="native",
        app_session_id="session-1",
        backend_url="http://backend",
        internal_token="token-1",
        mssg_sender_session_id="",
        cwd="/tmp/project",
        model="model-1",
        user_facing=True,
        request_user_input_enabled=True,
        file_editing_mode=False,
        team_orchestration_enabled=False,
        disabled_builtin_tools=set(),
        existing_tool_names=set(),
    )
    check(
        "request_user_input" in {tool["name"] for tool in dynamic_tools},
        "Codex injects Better Agent request_user_input when UI loopback tools are enabled",
    )
    check(
        "request_user_input" in handlers,
        "Codex request_user_input dynamic tool has a loopback handler",
    )

    open_file_only_tools, open_file_only_handlers = runner_codex._build_dynamic_tool_set(
        mode="native",
        app_session_id="session-1",
        backend_url="http://backend",
        internal_token="token-1",
        mssg_sender_session_id="",
        cwd="/tmp/project",
        model="model-1",
        user_facing=True,
        request_user_input_enabled=False,
        file_editing_mode=False,
        team_orchestration_enabled=False,
        disabled_builtin_tools=set(),
        existing_tool_names=set(),
    )
    check(
        "open_file_panel" in {tool["name"] for tool in open_file_only_tools},
        "Codex still injects open_file_panel when only open-file-panel is enabled",
    )
    check(
        "request_user_input" not in open_file_only_handlers,
        "Codex open-file-panel enablement does not imply request_user_input",
    )

    calls: list[tuple[dict, dict]] = []
    original_post = runner_codex._post_loopback_sync

    def fake_post(payload: dict, **kwargs: dict) -> dict:
        calls.append((payload, kwargs))
        return {"success": True, "answers": {"q": "answer"}}

    runner_codex._post_loopback_sync = fake_post
    try:
        result = asyncio.run(handlers["request_user_input"]({
            "arguments": {
                "questions": [{"id": "q", "header": "H", "question": "Q"}],
                "timeout_seconds": 5,
            },
        }))
    finally:
        runner_codex._post_loopback_sync = original_post

    check(result.get("success") is True, "Codex request_user_input handler returns success")
    check(len(calls) == 1, "Codex request_user_input handler makes one loopback call")
    if calls:
        payload, kwargs = calls[0]
        check(
            payload == {
                "app_session_id": "session-1",
                "kind": "input",
                "questions": [{"id": "q", "header": "H", "question": "Q"}],
                "timeout_seconds": 5,
            },
            "Codex request_user_input handler sends the expected payload",
        )
        check(
            kwargs.get("url_path") == "/api/internal/user-input/request",
            "Codex request_user_input handler routes to the user-input endpoint",
        )


def t_agy_materializes_isolated_home() -> None:
    real_home = Path(tempfile.mkdtemp(dir=_TMP_HOME))
    real_cli = real_home / ".gemini" / "antigravity-cli"
    real_cli.mkdir(parents=True)
    (real_home / ".gemini" / "google_accounts.json").write_text("{}", encoding="utf-8")
    (real_cli / "settings.json").write_text(
        json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}),
        encoding="utf-8",
    )
    original_home = Path.home
    Path.home = staticmethod(lambda: real_home)  # type: ignore[method-assign]
    try:
        run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        env = runner_agy._materialize_agy_run_home(
            run_dir,
            {
                "mcp_servers": {"demo": {"command": "echo"}},
                "skills": {"reviewer": "Review.\n"},
            },
            config_root=real_cli,
            settings={"security": {"auth": {"selectedType": "oauth-personal"}}},
            mcp_servers={"demo": {"command": "echo"}},
            skill_dirs={},
        )
    finally:
        Path.home = original_home  # type: ignore[method-assign]
    overlay = Path(env["HOME"])
    overlay_cli = overlay / ".gemini" / "antigravity-cli"
    settings = json.loads((overlay_cli / "settings.json").read_text(encoding="utf-8"))
    check(settings["mcpServers"]["demo"]["command"] == "echo", "AGY MCP settings are run-local")
    check(
        settings["security"]["auth"]["selectedType"] == "oauth-personal",
        "AGY run-local settings preserve auth selection",
    )
    check(
        (overlay / ".gemini" / "google_accounts.json").is_symlink(),
        "AGY auth file is linked, not copied",
    )
    skill = overlay_cli / "builtin" / "skills" / "reviewer" / "SKILL.md"
    check(skill.read_text(encoding="utf-8") == "Review.\n", "AGY skill is written")


def t_builtin_user_facing_mcp_servers_injected() -> None:
    _install_requirements_extension_record()
    _install_core_mcp_gate_extensions()
    _configure_internal_llm_defaults(
        "default_session",
        "requirement_analysis",
        "project_structure_edit",
    )
    config = builtin_mcp_config.with_builtin_mcp_servers({
        "user_facing": True,
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
    check(env["BETTER_CLAUDE_EXTENSION_ID"] == _FIXTURE_SCHEDULER_EXTENSION_ID, "extension MCP env selects scheduler owner")
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
        "user_facing": True,
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
        "user_facing": True,
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
    extension_store.set_enabled(_FIXTURE_REQUIREMENTS_EXTENSION_ID, False)
    config = builtin_mcp_config.with_builtin_mcp_servers({
        "user_facing": True,
        "app_session_id": "bc-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
    }, {})
    servers = config["mcp_servers"]
    check("better-agent-requirements" not in servers, "disabled requirements extension removes its private MCP server")
    check("better-agent-canvas" in servers, "other private extension MCP servers remain active")
    extension_store.set_enabled(_FIXTURE_REQUIREMENTS_EXTENSION_ID, True)


def t_installed_extension_can_replace_reserved_builtin_mcp_name() -> None:
    _configure_internal_llm_defaults("default_session")
    package = Path(_TMP_HOME) / "project-structure-extension"
    (package / "mcp").mkdir(parents=True)
    script = package / "mcp" / "server.py"
    script.write_text("print('project updates')\n", encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][_FIXTURE_PROJECT_STRUCTURE_EXTENSION_ID] = {
        "manifest": _write_installed_manifest(package, {
            "kind": extension_store.MANIFEST_KIND,
            "id": _FIXTURE_PROJECT_STRUCTURE_EXTENSION_ID,
            "core_roles": ["project-structure"],
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
    _save_runtime_extension_record(data, _FIXTURE_PROJECT_STRUCTURE_EXTENSION_ID)
    config = builtin_mcp_config.with_builtin_mcp_servers({
        "user_facing": True,
        "app_session_id": "bc-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
    }, {})
    server = config["mcp_servers"].get("project-updates")
    check(server is not None, "installed extension replacement is exposed under reserved MCP name")
    _check_sdk_script_server(
        server,
        script,
        "replacement MCP uses the SDK script entrypoint",
    )


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
        "user_facing": True,
        "app_session_id": "bc-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
    }, {})
    runtime = config["mcp_servers"].get("ofek-runtime")
    check(runtime is not None, "installed extension MCP server is injected")
    _check_sdk_script_server(
        runtime,
        script,
        "installed extension MCP uses the SDK script entrypoint",
    )
    check(runtime["env"]["BETTER_CLAUDE_EXTENSION_ID"] == "ofek.runtime", "installed extension MCP carries extension id")
    check(runtime["env"]["OF_RUNTIME"] == "1", "installed extension MCP carries manifest env")


def t_runtime_mcp_servers_reload_after_backend_restart_simulation() -> None:
    _install_requirements_extension_record()
    _install_core_mcp_gate_extensions()
    _configure_internal_llm_defaults(
        "default_session",
        "requirement_analysis",
        "project_structure_edit",
    )
    inputs = {
        "user_facing": True,
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
    ):
        check(name in before, f"restart simulation baseline includes {name}")
        check(name in after, f"restart simulation keeps {name}")
    check(
        after["better-agent-requirements"]["env"]["BETTER_CLAUDE_APP_SESSION_ID"] == "restart-sid",
        "restart simulation keeps runtime extension MCP session env",
    )


def t_session_bound_mcp_is_not_available_to_ambient_native_tools() -> None:
    _install_requirements_extension_record(replaces_builtin=True)
    _configure_internal_llm_defaults("requirement_analysis")
    inputs = {
        "user_facing": True,
        "app_session_id": "native-restart-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-native-restart",
    }
    configs = extension_store.native_mcp_launcher_server_configs(
        inputs, user_facing=True, bare=False
    )
    check("get-requirements" not in configs, "session-bound requirements MCP is excluded ambiently")


def t_builtin_mcp_registry_applies_to_all_provider_runners() -> None:
    _install_requirements_extension_record()
    _configure_internal_llm_defaults("default_session", "requirement_analysis")
    runner_src = (Path(_BACKEND) / "runner.py").read_text(encoding="utf-8")
    claude_provider_src = (
        Path(_BACKEND) / "provider_claude.py"
    ).read_text(encoding="utf-8")
    check(
        "hydrate_frozen_provider_runtime_plan(" in claude_provider_src
        and "hydrate_runner_operation_broker(" in runner_src,
        "Claude spawn hydrates only volatile frozen-plan references",
    )
    check(
        '"session-bridge" in _active_builtin_mcp_servers' not in runner_src,
        "Claude session bridge public fallback is absent",
    )
    check(
        "structural_provider_runtime_plan(" in claude_provider_src,
        "Claude provider freezes installed extension MCP servers before spawn",
    )
    check(
        "runtime_mcp_server_configs(" not in runner_src
        and "native_mcp_server_configs(" not in runner_src,
        "Claude runner never re-projects mutable extension MCP state",
    )
    check(
        "native_mcp_launcher_server_configs(" in (Path(_BACKEND) / "builtin_mcp_config.py").read_text(encoding="utf-8"),
        "native CLI provider config injects extension launchers instead of resolved native MCP configs",
    )
    supervisor_src = (Path(_BACKEND) / "orchs" / "supervisor" / "__init__.py").read_text(encoding="utf-8")
    orchestrator_src = (Path(_BACKEND) / "orchestrator.py").read_text(encoding="utf-8")
    main_src = (Path(_BACKEND) / "main.py").read_text(encoding="utf-8")
    check(
        "is_extension_runtime_ready(" in supervisor_src
        and "extension_id_for_role('supervisor')" in supervisor_src,
        "supervisor loop checks extension runtime readiness",
    )
    check(
        "runtime_not_ready_message(" in orchestrator_src
        and "extension_id_for_role('supervisor')" in orchestrator_src,
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
    for provider_name in ("codex", "agy"):
        _install_core_mcp_gate_extensions()
        config = builtin_mcp_config.with_builtin_mcp_servers({
            "user_facing": True,
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
        "user_facing": True,
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
        "user_facing": True,
        "app_session_id": "ba-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-ba",
    }
    configs = effective_mcp_servers(
        structural_provider_runtime_plan(
            inputs,
            "openai",
        )["resolved_plan"],
    )
    check(
        "better-agent-requirements" in configs,
        "Better Agent runner gets requirements through private extension",
    )
    headless = dict(inputs)
    headless["user_facing"] = False
    check(
        "better-agent-requirements" in effective_mcp_servers(
            structural_provider_runtime_plan(
                headless,
                "openai",
            )["resolved_plan"],
        ),
        "Better Agent runner keeps requirements MCP for authenticated headless sessions",
    )

    missing_token = dict(inputs)
    missing_token["internal_token"] = ""
    check(
        "better-agent-requirements" not in effective_mcp_servers(
            structural_provider_runtime_plan(
                missing_token,
                "openai",
            )["resolved_plan"],
        ),
        "Better Agent runner omits requirements MCP without backend auth",
    )
    # `internal_token` is always blank at structural-plan-build time for every
    # provider (the runner subprocess mints the real one moments later), so
    # `provider_claude.py`/`provider_agy.py`/`provider_codex.py` all set this
    # flag on their real runner_input -- without it, every
    # `requires_backend_auth` MCP server (session-bridge, coordination,
    # session-control, requirements, ...) would be silently excluded from
    # every real turn regardless of harness-profile selection or predicate
    # match. `launcher_can_mint_token` legitimately substitutes for the
    # not-yet-minted token here since `backend_url`/`app_session_id` are real.
    launcher_context_no_token = dict(missing_token)
    launcher_context_no_token["extension_mcp_launcher_context"] = True
    check(
        "better-agent-requirements" in effective_mcp_servers(
            structural_provider_runtime_plan(
                launcher_context_no_token,
                "openai",
            )["resolved_plan"],
        ),
        "Better Agent runner keeps requirements MCP without a token when "
        "extension_mcp_launcher_context signals a real session/backend_url",
    )
    check(
        "better-agent-requirements" in effective_mcp_servers(
            structural_provider_runtime_plan(
                {**inputs, "bare_config": True},
                "openai",
            )["resolved_plan"],
        ),
        "Better Agent runner keeps requirements MCP for bare runs",
    )


def t_requirements_mcp_stays_on_better_agent_runtime() -> None:
    _install_requirements_extension_record(replaces_builtin=True)
    _configure_internal_llm_defaults("requirement_analysis")
    inputs = {
        "user_facing": True,
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
    check(req is not None, "requirements MCP is injected per managed Better Agent run")
    if req:
        env = req["env"]
        check(env["BETTER_CLAUDE_EXTENSION_ID"] == _FIXTURE_REQUIREMENTS_EXTENSION_ID, "runtime requirements MCP env selects requirements extension")
        check(env["BETTER_CLAUDE_APP_SESSION_ID"] == "bc-sid", "runtime requirements MCP env carries app session id")
        check(env["BETTER_CLAUDE_CWD"] == "/tmp/project", "runtime requirements MCP env carries cwd")
        _check_sdk_script_server(
            req,
            Path(_TMP_HOME) / "requirements-extension" / "mcp" / "server.py",
            "runtime requirements MCP uses the SDK script entrypoint",
        )
    missing_token = dict(inputs)
    missing_token["internal_token"] = ""
    check(
        "get-requirements" not in builtin_mcp_config.with_builtin_mcp_servers(missing_token, {})["mcp_servers"],
        "runtime requirements MCP is omitted without backend auth",
    )
    headless = dict(inputs)
    headless["user_facing"] = False
    check(
        "get-requirements" in builtin_mcp_config.with_builtin_mcp_servers(headless, {})["mcp_servers"],
        "runtime requirements MCP is kept for authenticated headless runs",
    )
    bare = dict(inputs)
    bare["bare_config"] = True
    check(
        "get-requirements" in builtin_mcp_config.with_builtin_mcp_servers(bare, {})["mcp_servers"],
        "runtime requirements MCP is kept for bare runs",
    )
    # requirements is a dissolved private extension: disabling it via its enabled
    # flag (not the disabled_builtin_extensions builtin override) omits its MCP.
    extension_store.set_enabled(_FIXTURE_REQUIREMENTS_EXTENSION_ID, False)
    check(
        "get-requirements" not in builtin_mcp_config.with_builtin_mcp_servers(inputs, {})["mcp_servers"],
        "runtime requirements MCP is omitted when the extension is disabled",
    )
    extension_store.set_enabled(_FIXTURE_REQUIREMENTS_EXTENSION_ID, True)


def t_requirements_processor_profile_marks_requirements_mcp_env() -> None:
    _install_requirements_extension_record(replaces_builtin=True)
    _configure_internal_llm_defaults("requirement_analysis")
    inputs = {
        "user_facing": False,
        "app_session_id": "processor-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-1",
        "provisioned_tool_profile": "requirements_processor",
    }
    direct = extension_store.runtime_mcp_server_configs(
        inputs,
        user_facing=False,
        bare=False,
    ).get("get-requirements")
    check(direct is not None, "processor profile runtime requirements MCP resolves")
    if direct:
        check(
            direct["env"].get("BETTER_CLAUDE_REQUIREMENTS_PROCESSOR") == "1",
            "processor profile runtime requirements MCP enables restricted server mode",
        )

    config = builtin_mcp_config.with_builtin_mcp_servers(inputs, {})
    managed = config["mcp_servers"].get("get-requirements")
    check(managed is not None, "processor profile managed requirements MCP resolves")
    if managed:
        check(
            managed["env"].get("BETTER_CLAUDE_REQUIREMENTS_PROCESSOR") == "1",
            "processor profile managed MCP enables restricted server mode",
        )
    runtime_env = builtin_mcp_config.native_mcp_runtime_env(inputs)
    check(
        runtime_env["BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE"] == "requirements_processor",
        "processor profile native runtime env carries provisioned tool profile",
    )


def _with_runtime_broker_env(*, agent: str = "", legacy: str = ""):
    class BrokerEnv:
        def __enter__(self):
            self.previous_agent = os.environ.get("BETTER_AGENT_RUNTIME_BROKER")
            self.previous_legacy = os.environ.get("BETTER_CLAUDE_RUNTIME_BROKER")
            if agent:
                os.environ["BETTER_AGENT_RUNTIME_BROKER"] = agent
            else:
                os.environ.pop("BETTER_AGENT_RUNTIME_BROKER", None)
            if legacy:
                os.environ["BETTER_CLAUDE_RUNTIME_BROKER"] = legacy
            else:
                os.environ.pop("BETTER_CLAUDE_RUNTIME_BROKER", None)
            return self

        def __exit__(self, *_exc):
            if self.previous_agent is None:
                os.environ.pop("BETTER_AGENT_RUNTIME_BROKER", None)
            else:
                os.environ["BETTER_AGENT_RUNTIME_BROKER"] = self.previous_agent
            if self.previous_legacy is None:
                os.environ.pop("BETTER_CLAUDE_RUNTIME_BROKER", None)
            else:
                os.environ["BETTER_CLAUDE_RUNTIME_BROKER"] = self.previous_legacy

    return BrokerEnv()


def _with_integrations_enabled():
    class IntegrationsEnabled:
        def __enter__(self):
            self.previous = extension_store.installation_profile.integrations_enabled
            extension_store.installation_profile.integrations_enabled = lambda: True
            return self

        def __exit__(self, *_exc):
            extension_store.installation_profile.integrations_enabled = self.previous

    return IntegrationsEnabled()


def _coordination_launcher_inputs() -> dict:
    return {
        "user_facing": True,
        "app_session_id": "coordination-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-coordination",
        "resolved_harness_run_config": {
            "launcher_projection": {
                "extension_revisions": {
                    extension_store.BUILTIN_COORDINATION_EXTENSION_ID: "test",
                },
                "extension_mcp_servers": {
                    extension_store.BUILTIN_COORDINATION_EXTENSION_ID: [
                        "ofek-dev-coordination",
                    ],
                },
            },
        },
    }


def _check_coordination_broker_env(env: dict, expected: str, label: str) -> None:
    check(
        env.get("BETTER_AGENT_RUNTIME_BROKER") == expected,
        f"{label} carries Better Agent runtime broker alias",
    )
    check(
        env.get("BETTER_CLAUDE_RUNTIME_BROKER") == expected,
        f"{label} carries legacy runtime broker alias",
    )


def t_coordination_native_launcher_preserves_runtime_broker_aliases() -> None:
    _install_coordination_extension_record()
    cases = [
        ("agent-only", "unix:/tmp/agent-only.sock", ""),
        ("legacy-only", "", "unix:/tmp/legacy-only.sock"),
        ("both", "unix:/tmp/agent-first.sock", "unix:/tmp/legacy-second.sock"),
    ]
    for label, agent, legacy in cases:
        expected = agent or legacy
        with _with_integrations_enabled(), _with_runtime_broker_env(agent=agent, legacy=legacy):
            configs = extension_store.native_mcp_launcher_server_configs(
                _coordination_launcher_inputs(),
                user_facing=True,
                bare=False,
            )
            launcher = configs.get("better-agent-coordination")
            check(launcher is not None, f"{label} coordination launcher config resolves")
            if launcher:
                _check_coordination_broker_env(launcher.get("env") or {}, expected, f"{label} launcher")


def t_coordination_launcher_second_stage_preserves_runtime_broker() -> None:
    _install_coordination_extension_record()
    broker = "unix:/tmp/coordination-second-stage.sock"
    with _with_integrations_enabled(), _with_runtime_broker_env(agent=broker):
        inputs = {
            **_coordination_launcher_inputs(),
            "extension_mcp_launcher_context": True,
        }
        resolved = extension_store.resolve_native_mcp_server_config(
            extension_id=extension_store.BUILTIN_COORDINATION_EXTENSION_ID,
            server_name="ofek-dev-coordination",
            inputs=inputs,
        )
    check(resolved is not None, "coordination launcher second stage resolves MCP config")
    if resolved:
        _check_coordination_broker_env(resolved.get("env") or {}, broker, "second-stage coordination")
        check(
            "BETTER_AGENT_INTERNAL_TOKEN" not in (resolved.get("env") or {}),
            "brokered coordination MCP does not receive internal token",
        )


def t_bare_testape_mcp_stays_on_better_agent_runtime() -> None:
    _install_testape_extension_record()
    inputs = {
        "user_facing": False,
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
    _check_sdk_script_server(
        server,
        Path(_TMP_HOME) / "testape-extension" / "mcp" / "server.py",
        "bare TestApe MCP uses the SDK script entrypoint",
    )
    check(server["env"]["BETTER_CLAUDE_EXTENSION_ID"] == _FIXTURE_TESTAPE_EXTENSION_ID, "bare TestApe runtime env carries extension id")
    check(server["env"]["BETTER_CLAUDE_APP_SESSION_ID"] == "testape-bare-sid", "bare TestApe runtime env carries session id")
    raw = extension_store.native_mcp_server_configs(inputs, user_facing=False, bare=True).get("testape")
    check(raw is None, "session-aware TestApe MCP is excluded from ambient native tools")


def t_explicit_testape_mcp_opt_in_works_headlessly() -> None:
    _install_testape_extension_record()
    inputs = {
        "user_facing": False,
        "app_session_id": "testape-headless-sid",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "secret",
        "mode": "native",
        "cwd": "/tmp/project",
        "model": "m",
        "provider_id": "prov-testape",
        "bare_config": False,
        "extra_mcp_servers": ["testape"],
    }
    config = builtin_mcp_config.with_builtin_mcp_servers(inputs, {})
    server = config["mcp_servers"].get("testape")
    check(server is not None, "explicit TestApe MCP opt-in is injected headlessly")
    if not server:
        return
    _check_sdk_script_server(
        server,
        Path(_TMP_HOME) / "testape-extension" / "mcp" / "server.py",
        "explicit TestApe opt-in uses the SDK script entrypoint",
    )
    check(server["env"]["BETTER_CLAUDE_APP_SESSION_ID"] == "testape-headless-sid", "explicit TestApe opt-in carries session id")


def t_bare_mcp_availability_matrix() -> None:
    _install_bare_matrix_extension_record()
    inputs = {
        "user_facing": False,
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
    check("visible-bare" not in servers, "user-facing MCP is excluded from ambient native tools")
    check("visible-not-bare" not in servers, "bare user-facing MCP is excluded without bare_allowed")

    explicit = dict(inputs)
    explicit["extra_mcp_servers"] = ["visible-not-bare"]
    explicit_servers = extension_store.native_mcp_launcher_server_configs(
        explicit,
        user_facing=False,
        bare=True,
    )
    check("visible-not-bare" not in explicit_servers, "explicit opt-in does not bypass bare_allowed")
    explicit_runtime_servers = extension_store.runtime_mcp_server_configs(
        explicit,
        user_facing=False,
        bare=True,
    )
    check("visible-not-bare" not in explicit_runtime_servers, "explicit runtime opt-in does not bypass bare_allowed")


def t_open_file_panel_mcp_validates_required_fields() -> None:
    result = open_file_panel_mcp.open_file_panel_response("panel", "")
    check(result["success"] is False, "open-file-panel MCP rejects missing path before HTTP")


def t_request_user_input_mcp_validates_required_fields() -> None:
    result = open_file_panel_mcp.request_user_input_response([])
    check(result["success"] is False, "request-user-input MCP rejects missing questions before HTTP")


def t_request_user_approval_contract_has_provider_parity() -> None:
    result = open_file_panel_mcp.request_user_approval_response("")
    check(result["success"] is False, "request-user-approval MCP rejects missing prompt before HTTP")
    for module, label in (
        (runner, "Claude"),
        (runner_codex, "Codex"),
        (runner_better_agent, "Better Agent"),
    ):
        schema = module._REQUEST_USER_APPROVAL_SCHEMA
        check(schema["required"] == ["prompt"], f"{label} uses the approval-only prompt contract")
        check(
            module._REQUEST_USER_APPROVAL_DESCRIPTION == runner._REQUEST_USER_APPROVAL_DESCRIPTION,
            f"{label} shares the approval tool description",
        )
    mcp_tool_names = {
        tool.name for tool in asyncio.run(open_file_panel_mcp.build_server().list_tools())
    }
    check("request_user_approval" in mcp_tool_names, "native UI MCP registers request_user_approval")
    claude_tool = runner._build_request_user_approval_tool(
        app_session_id="session-1",
        backend_url="http://backend",
        internal_token="token-1",
    )
    check(claude_tool.name == "request_user_approval", "Claude SDK MCP registers request_user_approval")
    codex_tools, codex_handlers = runner_codex._build_dynamic_tool_set(
        mode="native",
        app_session_id="session-1",
        backend_url="http://backend",
        internal_token="token-1",
        mssg_sender_session_id="",
        cwd="/tmp/project",
        model="model-1",
        user_facing=True,
        request_user_input_enabled=True,
        file_editing_mode=False,
        team_orchestration_enabled=False,
        disabled_builtin_tools=set(),
        existing_tool_names=set(),
    )
    check(
        "request_user_approval" in {tool["name"] for tool in codex_tools},
        "Codex dynamic tools register request_user_approval",
    )
    check("request_user_approval" in codex_handlers, "Codex registers the approval loopback handler")
    better_agent_schemas = runner_better_agent._tool_schemas_for_run(
        inputs={},
        capabilities_enabled=False,
        loopback_enabled=True,
        team_manager_enabled=False,
        team_orchestration_enabled=False,
        user_facing=True,
        file_editing_mode=False,
        coordination_enabled=False,
    )
    better_agent_names = {
        schema.get("function", {}).get("name") for schema in better_agent_schemas
    }
    check("request_user_approval" in better_agent_names, "Better Agent runner registers request_user_approval")


def t_provider_sources_persist_open_file_panel_flag() -> None:
    codex_src = (Path(_BACKEND) / "provider_codex.py").read_text(encoding="utf-8")
    agy_src = (Path(_BACKEND) / "provider_agy.py").read_text(encoding="utf-8")
    claude_src = (Path(_BACKEND) / "provider_claude.py").read_text(encoding="utf-8")
    session_events_src = (
        Path(_BACKEND) / "provider_session_events_execution.py"
    ).read_text(encoding="utf-8")
    session_events_strategy_src = (
        Path(_BACKEND) / "provider_session_events_execution_strategy.py"
    ).read_text(encoding="utf-8")
    family_runtime_src = (
        Path(_BACKEND) / "provider_family_execution_runtime.py"
    ).read_text(encoding="utf-8")
    check(
        '"user_facing": bool(user_facing)' in codex_src,
        "Codex provider persists user_facing into runner input",
    )
    check(
        '"request_user_input_enabled": request_user_input_enabled' in codex_src,
        "Codex provider persists request_user_input_enabled into runner input separately",
    )
    check(
        '"provider_kind": self.KIND' in codex_src,
        "Codex provider persists provider_kind into runner input",
    )
    check(
        "runner_input = self._build_runner_input(start_arguments)" in agy_src,
        "AGY provider builds one authoritative runner input",
    )
    check(
        '"browser_harness_enabled": bool(browser_harness_enabled)' in codex_src,
        "Codex provider persists browser_harness_enabled into runner input",
    )
    check(
        'runtime_policy={"runner_input": dict(runner_input)}' in family_runtime_src,
        "AGY family artifact freezes the authoritative runner input",
    )
    check(
        '"context_strategy": user_prefs.get_context_strategy()' in codex_src,
        "Codex provider persists context_strategy into runner input",
    )
    check(
        '"context_strategy": user_prefs.get_context_strategy()' in agy_src,
        "AGY provider persists context_strategy into runner input",
    )
    check(
        "input_payload.update(run_policy)" in codex_src,
        "Codex applies the frozen extension run policy to runner input",
    )
    check(
        '"worker_working_mode": runtime_policy["worker_working_mode"]' in codex_src,
        "Codex consumes frozen worker working mode",
    )
    check(
        '"provisioned_tool_profile": str(provisioned_tool_profile or "").strip()' in codex_src,
        "Codex provider persists provisioned tool profile into runner input",
    )
    check(
        "payload.update(run_policy)" in agy_src,
        "AGY runner input includes the resolved extension run policy",
    )
    check(
        '"worker_working_mode": worker_record.get("working_mode")' in agy_src,
        "AGY runner input freezes worker working mode",
    )
    check(
        '"provider_kind": self.KIND' in agy_src,
        "AGY runner input self-identifies its provider kind",
    )
    check(
        "input_payload.update(run_policy)" in claude_src,
        "Claude applies the frozen extension run policy to runner input",
    )
    check(
        "resolve_extension_run_policy(" in claude_src,
        "Claude resolves extension policy through the shared authority",
    )
    check(
        '"worker_working_mode": (_worker_sess_rec or {}).get("working_mode")' in claude_src,
        "Claude provider persists worker working mode into runner input",
    )
    check(
        '"provisioned_tool_profile": str(provisioned_tool_profile or "").strip()' in claude_src,
        "Claude provider persists provisioned tool profile into runner input",
    )
    remote_src = (Path(_BACKEND) / "provider_remote.py").read_text(encoding="utf-8")
    node_handler_src = (Path(_BACKEND) / "node_rpc_handlers.py").read_text(encoding="utf-8")
    node_protocol_src = (Path(_BACKEND) / "node_protocol.py").read_text(encoding="utf-8")
    execution_template_src = (
        Path(_BACKEND) / "execution_template.py"
    ).read_text(encoding="utf-8")
    check(
        '"disabled_builtin_extensions": None' in execution_template_src
        and "**node_execution.artifact.template.arguments()" in remote_src,
        "Remote execution artifact ships disabled built-in extensions",
    )
    check(
        '"provisioned_tool_profile": ""' in execution_template_src
        and "**node_execution.artifact.template.arguments()" in remote_src,
        "Remote execution artifact ships provisioned tool profile",
    )
    check(
        "restore_prepared_execution(" in node_handler_src
        and "start_prepared_run," in node_handler_src,
        "Worker node restores and forwards the authoritative execution artifact",
    )
    check(
        'artifact.template.arguments()' in node_handler_src,
        "Worker node validates selectors through the artifact template",
    )
    check(
        "disabled_builtin_extensions: Optional[list[str]]" in node_protocol_src,
        "Node protocol types disabled built-in extensions",
    )
    for kind in ("amp", "copilot", "cursor", "kimi", "openai", "opencode", "pi", "qwen"):
        check(
            f'"{kind}": SessionEventsExecutionStrategy('
            in session_events_strategy_src,
            f"{kind} uses the centralized session-events execution strategy",
        )
        check(
            "resolve_extension_run_policy(" in session_events_src
            and "**policy," in session_events_src,
            f"{kind} freezes the effective extension run policy",
        )
    check(
        "provisioned_tool_profile: str" in node_protocol_src,
        "Node protocol types provisioned tool profile",
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
        ("provider skill names and atomic replacement are safe", t_provider_skill_names_and_atomic_replacement_are_safe),
        ("claude managed mcp config is authoritative", t_claude_managed_mcp_config_is_authoritative),
        ("codex open-file-panel dynamic tool", t_codex_open_file_panel_dynamic_tool),
        ("codex built-in tool schemas do not invite null defaults", t_codex_builtin_tool_schemas_do_not_invite_null_defaults),
        ("codex dynamic tools respect existing tool owners", t_codex_dynamic_tools_respect_existing_tool_owners),
        ("codex request_user_input uses Better Agent dynamic tool", t_codex_request_user_input_uses_better_agent_dynamic_tool),
        ("agy materializes isolated home", t_agy_materializes_isolated_home),
        ("built-in user-facing mcp servers injected", t_builtin_user_facing_mcp_servers_injected),
        ("built-in manager mcp servers exclude session bridge", t_builtin_manager_mcp_servers_exclude_session_bridge),
        ("built-in mcp servers are extension owned", t_builtin_mcp_servers_are_extension_owned),
        ("installed extension can replace reserved builtin mcp name", t_installed_extension_can_replace_reserved_builtin_mcp_name),
        ("installed extension mcp servers are injected", t_installed_extension_mcp_servers_are_injected),
        ("runtime mcp servers reload after backend restart simulation", t_runtime_mcp_servers_reload_after_backend_restart_simulation),
        ("session-bound mcp is not available to ambient native tools", t_session_bound_mcp_is_not_available_to_ambient_native_tools),
        ("built-in mcp registry applies to all provider runners", t_builtin_mcp_registry_applies_to_all_provider_runners),
        ("codex user-facing mcp servers skip open-file-panel mcp", t_codex_user_facing_mcp_servers_skip_open_file_panel_mcp),
        ("requirements mcp uses private extension", t_requirements_mcp_uses_private_extension),
        ("better-agent runner uses extension mcp configs", t_better_agent_runner_uses_extension_mcp_configs),
        ("requirements mcp stays on Better Agent runtime", t_requirements_mcp_stays_on_better_agent_runtime),
        ("requirements processor profile marks requirements mcp env", t_requirements_processor_profile_marks_requirements_mcp_env),
        ("coordination native launcher preserves runtime broker aliases", t_coordination_native_launcher_preserves_runtime_broker_aliases),
        ("coordination launcher second stage preserves runtime broker", t_coordination_launcher_second_stage_preserves_runtime_broker),
        ("bare TestApe mcp stays on Better Agent runtime", t_bare_testape_mcp_stays_on_better_agent_runtime),
        ("explicit TestApe mcp opt-in works headlessly", t_explicit_testape_mcp_opt_in_works_headlessly),
        ("bare mcp availability matrix", t_bare_mcp_availability_matrix),
        ("open-file-panel mcp validates required fields", t_open_file_panel_mcp_validates_required_fields),
        ("request-user-input mcp validates required fields", t_request_user_input_mcp_validates_required_fields),
        ("request-user-approval contract has provider parity", t_request_user_approval_contract_has_provider_parity),
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

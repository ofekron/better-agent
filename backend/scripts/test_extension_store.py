from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import base64
import hashlib
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-extensions-home-")
_TMP_OS_HOME = tempfile.mkdtemp(prefix="bc-test-extensions-os-home-")
os.environ["HOME"] = _TMP_OS_HOME
_TRUSTED_TEST_ROOT = Path(tempfile.mkdtemp(prefix="bc-test-trusted-extension-root-"))
os.environ["BETTER_AGENT_TRUSTED_EXTENSION_FILE_ROOTS"] = str(_TRUSTED_TEST_ROOT)
os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = str(_TRUSTED_TEST_ROOT)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402
import extension_backend_loader  # noqa: E402
import personal_harness_extension  # noqa: E402
from json_store import read_json, write_json  # noqa: E402


def _record_testape_internal_runtime_mcp() -> Path:
    package = Path(tempfile.mkdtemp(prefix="bc-test-recorded-testape-mcp-")) / "testape-mcp"
    (package / "mcp").mkdir(parents=True)
    manifest = _validate_manifest({
        "kind": "better-agent-extension",
        "id": "ofek.testape-internal",
        "name": "TestApe Internal MCP",
        "version": "1.0.0",
        "description": "Dynamic runtime MCP fixture.",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [
                {
                    "name": "testape",
                    "python": "mcp/server.py",
                    "interacts_with_user": False,
                    "bare_allowed": True,
                    "requires_backend_auth": False,
                    "native_exposure": {"allowed": True, "permissions": []},
                }
            ],
        },
        "marketplace": {},
    })
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('testape mcp')\n", encoding="utf-8")
    data = extension_store._load()
    data["extensions"][manifest["id"]] = {
        "manifest": manifest,
        "enabled": True,
        "installed_at": "test",
        "updated_at": "test",
        "source": {
            "type": "test-recorded-runtime",
            "repo_url": "",
            "extension_path": "testape-mcp",
            "ref": "",
            "commit_sha": "testape-mcp-test",
            "install_path": str(package),
        },
        "entitlement": {"status": "active"},
    }
    extension_store._save(data, resurrect_extension_ids={manifest["id"]})
    return package


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


def _validate_manifest(raw: dict) -> dict:
    value = dict(raw)
    value.setdefault("protocol", _default_protocol(value.get("entrypoints")))
    return extension_store.validate_manifest(value)


def _configure_internal_llm_defaults(*tasks: str) -> None:
    import config_store

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


def _seed_required_marketplace_package() -> None:
    package = _TRUSTED_TEST_ROOT / "extensions" / "marketplace"
    (package / "backend").mkdir(parents=True, exist_ok=True)
    (package / "ui").mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": extension_store.MARKETPLACE_EXTENSION_ID,
        "name": "Marketplace",
        "version": "1.0.0",
        "description": "Required marketplace",
        "surfaces": ["backend_feature", "frontend_feature"],
        "entrypoints": {
            "backend": "backend/routes.py",
            "frontend": "ui/index.html",
            "mcp": [],
            "instructions": [],
        },
        "permissions": {"backend_routes": True},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": ["mcp.server"]},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "backend" / "routes.py").write_text(
        "from fastapi import APIRouter\n\n"
        "def create_router(context):\n"
        "    return APIRouter()\n",
        encoding="utf-8",
    )
    (package / "ui" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")


def _seed_public_harness_instructions_package() -> None:
    package = _TRUSTED_TEST_ROOT / "extensions" / "harness-instructions"
    (package / "instructions").mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": extension_store.BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID,
        "name": "Harness Instructions",
        "version": "1.0.0",
        "description": "Visible Better Agent harness behavior instructions.",
        "surfaces": ["instructions"],
        "entrypoints": {
            "instructions": [
                {
                    "name": "better-agent-harness-behavior",
                    "path": "instructions/harness_behavior.md",
                    "level": "global",
                }
            ],
        },
        "permissions": {},
        "protocol": {
            "version": 1,
            "smoke_test": {
                "required_paths": [
                    "better-agent-extension.json",
                    "instructions/harness_behavior.md",
                ],
                "python_modules": [],
            },
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "instructions" / "harness_behavior.md").write_text(
        "Better Agent requires a parent-session reply after subagent work. "
        "If you call spawn_agent, multi_agent_v1.spawn_agent, or any other "
        "native subagent tool, then after every wait_agent result you must "
        "write your own final assistant message to the user.\n\n"
        "Better Agent groups action/tool blocks under the assistant text that "
        "immediately precedes them.\n",
        encoding="utf-8",
    )


_seed_required_marketplace_package()
_seed_public_harness_instructions_package()


def _write_private_extension_package(
    extension_id: str,
    extension_path: str,
    manifest: dict,
    files: dict[str, str] | None = None,
) -> Path:
    package = _TRUSTED_TEST_ROOT / extension_path
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    full_manifest = {
        "kind": "better-agent-extension",
        "id": extension_id,
        "name": manifest.get("name") or extension_id,
        "version": manifest.get("version") or "1.0.0",
        "description": manifest.get("description") or extension_id,
        "surfaces": manifest.get("surfaces") or [],
        "entrypoints": manifest.get("entrypoints") or {},
        "permissions": manifest.get("permissions") or {},
        "protocol": manifest.get("protocol") or _default_protocol(manifest.get("entrypoints") or {}),
        "marketplace": manifest.get("marketplace") or {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(full_manifest), encoding="utf-8")
    for rel_path, content in (files or {}).items():
        target = package / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return package


def _private_monorepo_test_work(prefix: str = "bc-test-extension-repo-") -> Path:
    root = _TRUSTED_TEST_ROOT / ".test-repos"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _make_repo(root: Path, extension_id: str = "ofek.requirements") -> tuple[Path, str]:
    repo = root / "extension-repo"
    package = repo / "extensions" / "requirements"
    package.mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": extension_id,
        "name": "Requirements",
        "version": "1.0.0",
        "description": "Requirement analysis extension",
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {
            "session_state": True,
            "spawn_runs": True,
            "provider_config": True,
        },
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": ["mcp.server"]},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "extensions/requirements/better-agent-extension.json")
    _git(repo, "commit", "-m", "add requirements extension")
    commit = _git(repo, "rev-parse", "HEAD")
    return repo, commit


def _make_instructions_repo(root: Path, extension_id: str = "ofek.instructions") -> tuple[Path, str]:
    """Extension shipping a global-level instruction section (for block-lifecycle tests)."""
    repo = root / "instructions-repo"
    package = repo / "extensions" / "instructions"
    package.mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": extension_id,
        "name": "Instructions",
        "version": "1.0.0",
        "description": "Instruction-section extension",
        "surfaces": ["instructions"],
        "entrypoints": {
            "instructions": [
                {"name": "rules", "path": "instructions/rules.md", "level": "global"},
                {"name": "projrules", "path": "instructions/projrules.md", "level": "project"},
            ],
        },
        "permissions": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": ["mcp.server"]},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "instructions").mkdir()
    (package / "instructions" / "rules.md").write_text("Requirement analysis capability\n", encoding="utf-8")
    (package / "instructions" / "projrules.md").write_text("Project-scoped rules\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "extensions")
    _git(repo, "commit", "-m", "add instructions extension")
    return repo, _git(repo, "rev-parse", "HEAD")


def _make_runtime_repo(root: Path) -> tuple[Path, str]:
    repo = root / "private-runtime-extensions"
    package = repo / "extensions" / "scheduler"
    (package / "mcp").mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.scheduler",
        "name": "Scheduler",
        "version": "1.0.0",
        "description": "Runtime MCP extension",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [
                {
                    "name": "ofek-scheduler",
                    "python": "mcp/server.py",
                    "env": {"OF_EXTENSION_TEST": "1"},
                    "interacts_with_user": True,
                    "bare_allowed": False,
                    "requires_backend_auth": True,
                }
            ],
        },
        "permissions": {
            "internal_loopback": True,
        },
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": ["mcp.server"]},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('mcp server')\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "extensions/scheduler/better-agent-extension.json", "extensions/scheduler/mcp/server.py")
    _git(repo, "commit", "-m", "add scheduler runtime extension")
    commit = _git(repo, "rev-parse", "HEAD")
    return repo, commit


def test_extension_package_installs_preserving_requirements_and_exposes_runtime_mcp() -> None:
    """A runtime-MCP extension installs from a package dir, preserves its
    declared python_requirements, and is exposed via runtime_mcp_server_configs
    with its own venv on PATH. Uses a self-contained fixture so it does not
    depend on any extension living inside this repo (extensions live in the
    private extensions repo)."""
    os.environ["BETTER_AGENT_SKIP_EXTENSION_DEPENDENCY_INSTALL"] = "1"
    package = Path(tempfile.mkdtemp(prefix="bc-test-synthetic-ext-")) / "synthetic-runtime-mcp"
    (package / "mcp").mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.synthetic-runtime-mcp",
        "name": "Synthetic runtime MCP",
        "version": "0.1.0",
        "description": "Fixture exercising package install + runtime MCP exposure.",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [
                {
                    "name": "synthetic",
                    "python": "mcp/server.py",
                    "interacts_with_user": True,
                    "bare_allowed": False,
                    "requires_backend_auth": True,
                }
            ],
            "instructions": [],
            "python_requirements": ["some-runtime-dep[mcp]"],
        },
        "permissions": {"internal_loopback": True},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": ["mcp.server"]},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('mcp server')\n", encoding="utf-8")

    record = extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "test",
            "repo_url": "",
            "extension_path": "synthetic-runtime-mcp",
            "ref": "",
            "commit_sha": "synthetic-test",
        },
        persist=True,
    )
    if record["manifest"]["entrypoints"]["python_requirements"] != ["some-runtime-dep[mcp]"]:
        raise AssertionError("python_requirements declaration was not preserved")
    # Resolve to the canonical path: the runtime-MCP builder resolves
    # install_root (Path(...).resolve()), so on macOS (/var -> /private/var)
    # the PATH entry is the resolved form. Match it or the entry-level check
    # compares unresolved vs resolved strings and fails.
    venv_bin = extension_store._venv_bin_dir(Path(record["source"]["install_path"]).resolve() / ".venv")
    venv_bin.mkdir(parents=True)
    _configure_internal_llm_defaults("default_session")

    config = extension_store.runtime_mcp_server_configs(
        {
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "token",
            "app_session_id": "session-1",
            "cwd": "/tmp/project",
            "model": "model",
        },
        interacts_with_user=True,
        bare=False,
    )
    server = config.get("synthetic")
    if not server:
        raise AssertionError("runtime MCP server was not exposed")
    args = server.get("args") or []
    if len(args) != 1 or not str(args[0]).endswith("mcp/server.py"):
        raise AssertionError(f"unexpected MCP args: {args!r}")
    env = server.get("env") or {}
    if env.get("BETTER_CLAUDE_EXTENSION_ID") != "ofek.synthetic-runtime-mcp":
        raise AssertionError("runtime MCP config missing extension id")
    if str(venv_bin) not in str(env.get("PATH") or "").split(os.pathsep):
        raise AssertionError("runtime MCP config does not prefer the extension venv")


def test_internal_runtime_mcp_requires_loopback_auth_but_not_user_facing() -> None:
    package = Path(tempfile.mkdtemp(prefix="bc-test-internal-runtime-mcp-")) / "internal-mcp"
    (package / "mcp").mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.internal-runtime-mcp",
        "name": "Internal Runtime MCP",
        "version": "1.0.0",
        "description": "Internal runtime MCP fixture.",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [
                {
                    "name": "internal-runtime",
                    "python": "mcp/server.py",
                    "interacts_with_user": False,
                    "bare_allowed": False,
                    "requires_backend_auth": True,
                }
            ],
        },
        "permissions": {"internal_loopback": True},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('mcp server')\n", encoding="utf-8")
    record = extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "test",
            "repo_url": "",
            "extension_path": "internal-mcp",
            "ref": "",
            "commit_sha": "internal-mcp-test",
        },
        persist=True,
    )
    venv_bin = extension_store._venv_bin_dir(Path(record["source"]["install_path"]).resolve() / ".venv")
    venv_bin.mkdir(parents=True)

    inputs = {
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "token",
        "app_session_id": "session-1",
    }
    configs = extension_store.runtime_mcp_server_configs(inputs, interacts_with_user=False, bare=False)
    if "internal-runtime" not in configs:
        raise AssertionError("internal runtime MCP unavailable to non-user-facing runner")

    missing_token = dict(inputs)
    missing_token["internal_token"] = ""
    configs = extension_store.runtime_mcp_server_configs(missing_token, interacts_with_user=False, bare=False)
    if "internal-runtime" in configs:
        raise AssertionError("internal runtime MCP available without internal token")


def test_dynamic_runtime_mcp_can_be_disabled_per_run() -> None:
    package = Path(tempfile.mkdtemp(prefix="bc-test-dynamic-disabled-mcp-")) / "testape-mcp"
    (package / "mcp").mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.testape-internal",
        "name": "TestApe Internal MCP",
        "version": "1.0.0",
        "description": "Dynamic runtime MCP fixture.",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [
                {
                    "name": "testape",
                    "python": "mcp/server.py",
                    "interacts_with_user": False,
                    "bare_allowed": True,
                    "requires_backend_auth": False,
                }
            ],
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('testape mcp')\n", encoding="utf-8")
    extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "test",
            "repo_url": "",
            "extension_path": "testape-mcp",
            "ref": "",
            "commit_sha": "testape-mcp-test",
        },
        force_enabled=True,
        persist=True,
    )

    inputs = {
        "app_session_id": "s1",
        "backend_url": "http://127.0.0.1:8000",
        "cwd": "/tmp/project",
        "model": "m",
        "bare_config": True,
    }
    enabled = extension_store.runtime_mcp_server_configs(
        inputs,
        interacts_with_user=False,
        bare=True,
    )
    if "testape" not in enabled:
        raise AssertionError(enabled)
    disabled = extension_store.runtime_mcp_server_configs(
        {**inputs, "disabled_builtin_extensions": ["ofek.testape-internal"]},
        interacts_with_user=False,
        bare=True,
    )
    if "testape" in disabled:
        raise AssertionError(disabled)


def test_recorded_runtime_mcp_outside_builtin_maps_can_be_disabled_per_run() -> None:
    _record_testape_internal_runtime_mcp()

    inputs = {
        "app_session_id": "s1",
        "backend_url": "http://127.0.0.1:8000",
        "cwd": "/tmp/project",
        "model": "m",
        "bare_config": True,
    }
    enabled = extension_store.runtime_mcp_server_configs(
        inputs,
        interacts_with_user=False,
        bare=True,
    )
    if "testape" not in enabled:
        raise AssertionError(enabled)
    disabled = extension_store.runtime_mcp_server_configs(
        {**inputs, "disabled_builtin_extensions": ["ofek.testape-internal"]},
        interacts_with_user=False,
        bare=True,
    )
    if "testape" in disabled:
        raise AssertionError(disabled)


def test_native_mcp_reconcile_omits_disabled_recorded_runtime_mcp() -> None:
    import config_store

    _record_testape_internal_runtime_mcp()
    extension_store.set_native_harness_exposed("ofek.testape-internal", "mcp", "testape", True)
    original_has_permission = extension_store.has_permission
    extension_store.has_permission = lambda _record, permission: permission == "internal_loopback"  # type: ignore[assignment]
    try:
        ambient_config = extension_store.resolve_native_mcp_server_config(
            extension_id="ofek.testape-internal",
            server_name="testape",
            inputs={},
        )
    finally:
        extension_store.has_permission = original_has_permission  # type: ignore[assignment]
    if ambient_config is None:
        raise AssertionError("ambient-native MCP did not resolve")
    if any("INTERNAL_TOKEN" in key for key in ambient_config.get("env", {})):
        raise AssertionError("ambient-native MCP received an internal token")
    captured: list[str] = []
    original_reconcile = extension_store.extension_mcp.reconcile_native_mcp_servers

    def fake_reconcile(records):
        captured.extend(record["manifest"]["id"] for record in records)
        return 0

    extension_store.extension_mcp.reconcile_native_mcp_servers = fake_reconcile
    try:
        config_store.set_disabled_builtin_extensions([])
        extension_store.reconcile_native_mcp_servers()
        if "ofek.testape-internal" not in captured:
            raise AssertionError(captured)
        captured.clear()
        config_store.set_disabled_builtin_extensions(["ofek.testape-internal"])
    finally:
        extension_store.extension_mcp.reconcile_native_mcp_servers = original_reconcile
        config_store.set_disabled_builtin_extensions([])

    if "ofek.testape-internal" in captured:
        raise AssertionError(captured)


def test_extension_store_save_preserves_concurrent_marketplace_mcp_records() -> None:
    import builtin_mcp_config

    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    stale = extension_store._load()
    package = Path(tempfile.mkdtemp(prefix="bc-test-concurrent-marketplace-ext-")) / "headroom-like"
    (package / "mcp").mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.concurrent-headroom",
        "name": "Concurrent Headroom",
        "version": "0.1.0",
        "description": "Marketplace-style MCP extension fixture.",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [
                {
                    "name": "headroom",
                    "python": "mcp/server.py",
                    "interacts_with_user": True,
                    "bare_allowed": False,
                    "requires_backend_auth": True,
                }
            ],
        },
        "permissions": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": ["mcp.server"]},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('headroom')\n", encoding="utf-8")

    extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "artifact",
            "repo_url": "https://example.test/headroom.tar.gz",
            "extension_path": "",
            "ref": "",
            "commit_sha": "concurrent-headroom",
            "artifact_sha256": "0" * 64,
            "artifact_url": "https://example.test/headroom.tar.gz",
        },
        persist=True,
    )

    if extension_store.get_extension("ofek.concurrent-headroom") is None:
        raise AssertionError("fixture extension did not install")

    stale["extensions"][extension_store.MARKETPLACE_EXTENSION_ID]["updated_at"] = "stale-writer"
    extension_store._save(stale)

    if extension_store.get_extension("ofek.concurrent-headroom") is None:
        raise AssertionError("stale registry save dropped an installed marketplace extension")

    config = builtin_mcp_config.with_builtin_mcp_servers(
        {
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "token",
            "app_session_id": "session-1",
            "cwd": "/tmp/project",
            "model": "model",
            "bare_config": False,
            "open_file_panel_enabled": True,
            "disabled_builtin_extensions": [],
        },
        {},
    )
    if "headroom" not in config["mcp_servers"]:
        raise AssertionError("preserved marketplace MCP extension was not exposed")


def test_extension_store_save_does_not_resurrect_concurrently_uninstalled_extension() -> None:
    package = Path(tempfile.mkdtemp(prefix="bc-test-concurrent-uninstall-ext-")) / "uninstall-race"
    package.mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.concurrent-uninstall",
        "name": "Concurrent Uninstall",
        "version": "0.1.0",
        "description": "Uninstall race fixture.",
        "surfaces": [],
        "entrypoints": {},
        "permissions": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "artifact",
            "repo_url": "https://example.test/uninstall.tar.gz",
            "extension_path": "",
            "ref": "",
            "commit_sha": "concurrent-uninstall",
            "artifact_sha256": "1" * 64,
            "artifact_url": "https://example.test/uninstall.tar.gz",
        },
        persist=True,
    )
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    stale = extension_store._load()
    extension_store.uninstall("ofek.concurrent-uninstall")
    stale["extensions"][extension_store.MARKETPLACE_EXTENSION_ID]["updated_at"] = "stale-writer"
    extension_store._save(stale)
    if extension_store.get_extension("ofek.concurrent-uninstall") is not None:
        raise AssertionError("stale registry save resurrected an uninstalled extension")


def test_extension_store_rehydrate_skips_tombstoned_installed_snapshot() -> None:
    package = Path(tempfile.mkdtemp(prefix="bc-test-tombstoned-rehydrate-ext-")) / "tombstoned"
    package.mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.tombstoned-rehydrate",
        "name": "Tombstoned Rehydrate",
        "version": "0.1.0",
        "description": "Tombstoned rehydrate fixture.",
        "surfaces": [],
        "entrypoints": {},
        "permissions": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "artifact",
            "repo_url": "https://example.test/tombstoned.tar.gz",
            "extension_path": "",
            "ref": "",
            "commit_sha": "tombstoned-rehydrate",
            "artifact_sha256": "2" * 64,
            "artifact_url": "https://example.test/tombstoned.tar.gz",
        },
        persist=True,
    )
    data = extension_store._load()
    data["extensions"].pop("ofek.tombstoned-rehydrate", None)
    extension_store._save(data, deleted_extension_ids={"ofek.tombstoned-rehydrate"})
    if extension_store.get_extension("ofek.tombstoned-rehydrate") is not None:
        raise AssertionError("rehydration restored a tombstoned installed snapshot")


def test_extension_store_rehydrates_installed_artifact_snapshot() -> None:
    # An installed artifact snapshot (a version dir under the install root with
    # no registry record) must be rehydrated into the registry on reconcile.
    # Regression for _rehydrate_installed_extension_records: a snapshot left on
    # disk (e.g. after a crash mid-install, or a dissolved extension whose
    # managed-id package no longer exists) must register so its MCP injects.
    # Uses a synthetic non-managed id so the managed-id skip
    # (_managed_extension_package_exists) does not apply and rehydration is the
    # only path that can create the record.
    ext_id = "ofek.rehydrate-fixture"
    install_root = extension_store._install_root()
    # Clean slate: the suite shares one temp home, so drop any fixture registry
    # record and installed snapshot left by earlier runs, otherwise rehydration
    # is skipped and a stale record masks the regression.
    with extension_store._store_lock():
        data = extension_store._read_store_unlocked()
        data["extensions"].pop(ext_id, None)
        extension_store._write_store_unlocked(data)
    existing_snapshot_root = install_root / ext_id
    if existing_snapshot_root.exists():
        shutil.rmtree(existing_snapshot_root)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": ext_id,
        "name": "Rehydrate fixture",
        "version": "0.1.0",
        "description": "Installed-snapshot rehydrate fixture.",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [
                {
                    "name": "fixture-mcp",
                    "python": "mcp/server.py",
                    "args": [],
                    "env": {},
                    "interacts_with_user": True,
                    "bare_allowed": False,
                    "requires_backend_auth": True,
                }
            ],
        },
        "permissions": {"internal_loopback": True},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": ["mcp.server"]},
        },
        "marketplace": {},
    }
    snapshot = (
        extension_store._install_root()
        / ext_id
        / "versions"
        / "rehydrate-fixture"
    )
    (snapshot / "mcp").mkdir(parents=True)
    (snapshot / "mcp" / "server.py").write_text("# stub mcp server\n", encoding="utf-8")
    (snapshot / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Reconcile (not the pure _load read) is what runs rehydration. The fixture
    # is non-managed, so _ensure_public/private_extensions leave it alone and
    # _rehydrate_installed_extension_records is the only path that registers it.
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    record = extension_store.get_extension(ext_id)
    if record is None:
        raise AssertionError("installed artifact snapshot was not rehydrated into the registry")
    if record.get("enabled") is not True:
        raise AssertionError("rehydrated artifact snapshot is not enabled")
    if record.get("source", {}).get("type") != "artifact":
        raise AssertionError("rehydrated artifact snapshot has unexpected source type")


def test_extension_skill_native_install_preserves_edits_and_runtime_mode_skips_native_copy() -> None:
    import runtime_skills

    package = Path(tempfile.mkdtemp(prefix="bc-test-synthetic-skill-ext-")) / "synthetic-skill"
    (package / "skills" / "synthetic-skill").mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.synthetic-skill",
        "name": "Synthetic skill",
        "version": "0.1.0",
        "description": "Fixture exercising extension skill delivery.",
        "surfaces": ["skills"],
        "entrypoints": {
            "skills": [{"name": "synthetic-skill", "path": "skills/synthetic-skill"}],
        },
        "permissions": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "skills" / "synthetic-skill" / "SKILL.md").write_text(
        "---\nname: synthetic-skill\ndescription: From package\n---\n",
        encoding="utf-8",
    )

    extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "test",
            "repo_url": "",
            "extension_path": "synthetic-skill",
            "ref": "",
            "commit_sha": "synthetic-skill-test",
        },
        persist=True,
    )

    extension_store.set_native_harness_exposed(
        "ofek.synthetic-skill", "skill", "synthetic-skill", True
    )

    native_skill = Path.home() / ".agents" / "skills" / "synthetic-skill"
    skill_md = native_skill / "SKILL.md"
    if not skill_md.exists():
        raise AssertionError("extension skill was not installed into native skill root")
    skill_md.write_text("---\nname: synthetic-skill\ndescription: User edit\n---\n", encoding="utf-8")
    extension_store.reconcile_runtime_skills()
    if "User edit" not in skill_md.read_text(encoding="utf-8"):
        raise AssertionError("native extension skill reconcile clobbered user edits")

    extension_store.set_native_harness_exposed(
        "ofek.synthetic-skill", "skill", "synthetic-skill", False
    )
    if native_skill.exists():
        raise AssertionError("runtime mode left the native skill copy installed")
    contexts = runtime_skills.runtime_skill_contexts(str(package))
    content = "\n".join(str(ctx.get("content") or "") for ctx in contexts)
    if "synthetic-skill" not in content or "From package" not in content:
        raise AssertionError("runtime mode did not inject the extension skill per turn")

    extension_store.set_native_harness_exposed(
        "ofek.synthetic-skill", "skill", "synthetic-skill", True
    )
    if not skill_md.exists():
        raise AssertionError("native mode did not reinstall the extension skill")


def test_runtime_skill_replace_is_atomic_and_repairs_gutted_targets() -> None:
    import runtime_skills

    package = Path(tempfile.mkdtemp(prefix="bc-test-atomic-skill-ext-")) / "atomic-skill"
    (package / "skills" / "atomic-skill").mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.atomic-skill",
        "name": "Atomic skill",
        "version": "0.1.0",
        "description": "Fixture exercising crash-safe runtime skill delivery.",
        "surfaces": ["skills"],
        "entrypoints": {
            "skills": [{"name": "atomic-skill", "path": "skills/atomic-skill"}],
        },
        "permissions": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "skills" / "atomic-skill" / "SKILL.md").write_text(
        "---\nname: atomic-skill\ndescription: From package\n---\n",
        encoding="utf-8",
    )
    extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "test",
            "repo_url": "",
            "extension_path": "atomic-skill",
            "ref": "",
            "commit_sha": "atomic-skill-test",
        },
        persist=True,
    )
    extension_store.set_native_harness_exposed("ofek.atomic-skill", "skill", "atomic-skill", True)

    target = Path.home() / ".agents" / "skills" / "atomic-skill"
    skill_md = target / "SKILL.md"
    if not skill_md.is_file():
        raise AssertionError("extension skill was not installed into native skill root")

    # An interrupted replace leaves an owned dir without SKILL.md; reconcile
    # must repair it instead of trusting the owner marker forever.
    skill_md.unlink()
    extension_store.reconcile_runtime_skills()
    if not skill_md.is_file():
        raise AssertionError("reconcile skipped an owned skill dir that lost its SKILL.md")

    # A replace that dies mid-copy must never leave the target gutted: the old
    # tree stays in place until the staged copy is complete.
    source_dir = target  # any valid skill tree works as the copy source
    real_copytree = shutil.copytree

    def _exploding_copytree(src, dst, **kwargs):  # noqa: ANN001, ANN003
        real_copytree(src, dst, **kwargs)
        raise OSError("simulated crash after copy, before swap")

    shutil.copytree = _exploding_copytree
    try:
        try:
            extension_store._replace_runtime_skill_dir(source_dir, target, "ofek.atomic-skill")
        except OSError:
            pass
    finally:
        shutil.copytree = real_copytree
    if not skill_md.is_file():
        raise AssertionError("failed replace gutted the live skill dir")

    # Staged dot-dirs are internal and must never be discovered as skills.
    staged = target.with_name(".atomic-skill.staging-99999")
    staged.mkdir()
    (staged / "SKILL.md").write_text("---\nname: staged\ndescription: internal\n---\n", encoding="utf-8")
    try:
        names = {s["name"] for s in runtime_skills._discover_skills(str(package))}
    finally:
        shutil.rmtree(staged)
    if any(name.startswith(".") for name in names):
        raise AssertionError("dot-prefixed staging dir leaked into skill discovery")


def _removed_source_runtime_skill_test() -> None:
    import runtime_skills

    extension_id = "ofek.private-source-skill"
    extension_path = "extensions/private-source-skill"
    package = _write_private_extension_package(
        extension_id,
        extension_path,
        {
            "name": "Private source skill",
            "surfaces": ["skills"],
            "entrypoints": {
                "skills": [{"name": "private-source-skill", "path": "skills/private-source-skill"}],
            },
        },
        {
            "skills/private-source-skill/SKILL.md": (
                "---\n"
                "name: private-source-skill\n"
                "description: From direct source.\n"
                "---\n"
                "Use direct source.\n"
            ),
        },
    )
    try:
        extension_store._install_from_package_dir(
            package_dir=package,
            source={
                "type": "better_agent_local",
                "repo_url": str(_TRUSTED_TEST_ROOT),
                "extension_path": extension_path,
                "ref": "",
                "commit_sha": "private-source-skill-test",
            },
            persist=True,
        )
        extension_store.set_native_harness_exposed(extension_id, "skill", "private-source-skill", True)
        native_skill = Path.home() / ".agents" / "skills" / "private-source-skill"
        shutil.rmtree(native_skill, ignore_errors=True)

        contexts = runtime_skills.runtime_skill_contexts(str(package))
        content = "\n".join(str(ctx.get("content") or "") for ctx in contexts)
        source_skill = package / "skills" / "private-source-skill" / "SKILL.md"
        if "private-source-skill" not in content:
            raise AssertionError("direct-source private native skill was not injected")
        if str(source_skill) not in content:
            raise AssertionError(f"runtime skill context did not point at source skill: {content}")

        shutil.rmtree(package)
        contexts = runtime_skills.runtime_skill_contexts(str(_TRUSTED_TEST_ROOT))
        content = "\n".join(str(ctx.get("content") or "") for ctx in contexts)
        if "private-source-skill" in content:
            raise AssertionError("missing private source root should not advertise a stale direct skill")
    finally:
        try:
            extension_store.uninstall(extension_id)
        except Exception:
            pass


def _make_team_definition_repo(root: Path) -> tuple[Path, str]:
    repo = root / "private-team-extensions"
    package = repo / "extensions" / "testape"
    (package / "teams").mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "fixture.testape",
        "core_roles": ["testape"],
        "name": "Testape",
        "version": "1.0.0",
        "description": "Team definitions",
        "surfaces": ["backend_feature"],
        "entrypoints": {
            "team_definitions": [
                {
                    "name": "testape-ui-expert",
                    "path": "teams/ui-expert.json",
                }
            ],
        },
        "permissions": {"session_state": True},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
        "marketplace": {},
    }
    definition = {
        "schema_version": 1,
        "name": "testape-ui-expert",
        "manager": {"id": "coordinator"},
        "catalog": {"workers": []},
        "profiles": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "teams" / "ui-expert.json").write_text(json.dumps(definition), encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "extensions/testape/better-agent-extension.json", "extensions/testape/teams/ui-expert.json")
    _git(repo, "commit", "-m", "add testape team definition")
    commit = _git(repo, "rev-parse", "HEAD")
    return repo, commit


def test_manifest_validation_rejects_unknown_permissions() -> None:
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek-dev.bad",
                "name": "Bad",
                "version": "1.0.0",
                "permissions": {"shell_escape": True},
            }
        )
    except extension_store.ExtensionError as exc:
        if "unknown keys" not in str(exc):
            raise
    else:
        raise AssertionError("unknown permission was accepted")


def test_manifest_rejects_string_only_mcp_entrypoints() -> None:
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek-dev.backend",
                "name": "Backend",
                "version": "1.0.0",
                "surfaces": ["runtime_mcp"],
                "entrypoints": {"mcp": ["scheduler"]},
            }
        )
    except extension_store.ExtensionError as exc:
        if "must declare" not in str(exc):
            raise
    else:
        raise AssertionError("string-only MCP entrypoint was accepted")


def test_manifest_rejects_reserved_mcp_server_names() -> None:
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek.shadow",
                "name": "Shadow",
                "version": "1.0.0",
                "surfaces": ["runtime_mcp"],
                "entrypoints": {
                    "mcp": [
                        {
                            "name": "communicate",
                            "python": "mcp/server.py",
                        }
                    ]
                },
            }
        )
    except extension_store.ExtensionError as exc:
        if "reserved" not in str(exc):
            raise
    else:
        raise AssertionError("reserved MCP server name was accepted")


# (extension id, core_roles or None, display name, mcp name, replaces_builtin)
_BUILTIN_MCP_REPLACEMENT_CASES = (
    (
        "fixture.project-structure",
        ["project-structure"],
        "Project Structure",
        "better-agent-project-updates",
        "project-updates",
    ),
    (
        "fixture.requirements",
        ["requirements"],
        "Requirements",
        "better-agent-requirements",
        "get-requirements",
    ),
    (
        extension_store.BUILTIN_COORDINATION_EXTENSION_ID,
        None,
        "Coordination",
        "ofek-dev-coordination",
        "better-agent-coordination",
    ),
)


def test_manifest_allows_builtin_mcp_replacements() -> None:
    if len(_BUILTIN_MCP_REPLACEMENT_CASES) != 3:
        raise AssertionError(_BUILTIN_MCP_REPLACEMENT_CASES)
    for ext_id, core_roles, name, mcp_name, replaces_builtin in _BUILTIN_MCP_REPLACEMENT_CASES:
        manifest_data = {
            "kind": "better-agent-extension",
            "id": ext_id,
            "name": name,
            "version": "1.0.0",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": mcp_name,
                        "replaces_builtin": replaces_builtin,
                        "python": "mcp/server.py",
                    }
                ]
            },
        }
        if core_roles is not None:
            manifest_data["core_roles"] = core_roles
        manifest = _validate_manifest(manifest_data)
        mcp = manifest["entrypoints"]["mcp"][0]
        if mcp["replaces_builtin"] != replaces_builtin:
            raise AssertionError((ext_id, mcp))


def test_manifest_validates_managed_run_env_permission() -> None:
    manifest = _validate_manifest(
        {
            "kind": "better-agent-extension",
            "id": "ofek.managed-run",
            "name": "Managed Run",
            "version": "1.0.0",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {"mcp": [{"name": "managed-run", "python": "mcp/server.py"}]},
            "permissions": {"spawn_runs": True, "managed_run_env": ["BU_CDP_URL"]},
        }
    )
    if manifest["permissions"]["managed_run_env"] != ["BU_CDP_URL"]:
        raise AssertionError(manifest["permissions"])
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek.bad-env",
                "name": "Bad Env",
                "version": "1.0.0",
                "surfaces": ["runtime_mcp"],
                "entrypoints": {"mcp": [{"name": "managed-run", "python": "mcp/server.py"}]},
                "permissions": {"managed_run_env": ["bad-key"]},
            }
        )
    except extension_store.ExtensionError as exc:
        if "managed_run_env" not in str(exc):
            raise
    else:
        raise AssertionError("invalid managed_run_env key was accepted")


def test_manifest_accepts_remote_services_with_network_permission() -> None:
    manifest = _validate_manifest(
        {
            "kind": "better-agent-extension",
            "id": "ofek.remote",
            "name": "Remote",
            "version": "1.0.0",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "remote_services": [
                    {
                        "name": "planner",
                        "base_url": "https://api.example.test/v1/",
                        "purpose": "Runs proprietary planning logic outside the local package.",
                    }
                ],
            },
            "permissions": {"network": True},
        }
    )
    services = manifest["entrypoints"]["remote_services"]
    if services != [
        {
            "name": "planner",
            "base_url": "https://api.example.test/v1",
            "purpose": "Runs proprietary planning logic outside the local package.",
        }
    ]:
        raise AssertionError(services)


def test_manifest_rejects_remote_services_without_network_permission() -> None:
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek.remote",
                "name": "Remote",
                "version": "1.0.0",
                "surfaces": ["runtime_mcp"],
                "entrypoints": {
                    "remote_services": [
                        {
                            "name": "planner",
                            "base_url": "https://api.example.test",
                            "purpose": "Runs proprietary planning logic.",
                        }
                    ],
                },
                "permissions": {},
            }
        )
    except extension_store.ExtensionError as exc:
        if "permissions.network=true" not in str(exc):
            raise
        return
    raise AssertionError("remote services were accepted without network permission")


def test_manifest_rejects_unsafe_remote_service_urls() -> None:
    for base_url in (
        "http://api.example.test",
        "https://user:pass@api.example.test",
        "https://api.example.test/path?token=secret",
        "https://api.example.test/path#fragment",
    ):
        try:
            _validate_manifest(
                {
                    "kind": "better-agent-extension",
                    "id": "ofek.remote",
                    "name": "Remote",
                    "version": "1.0.0",
                    "surfaces": ["runtime_mcp"],
                    "entrypoints": {
                        "remote_services": [
                            {
                                "name": "planner",
                                "base_url": base_url,
                                "purpose": "Runs proprietary planning logic.",
                            }
                        ],
                    },
                    "permissions": {"network": True},
                }
            )
        except extension_store.ExtensionError:
            continue
        raise AssertionError(f"unsafe remote service URL accepted: {base_url}")


def test_manifest_accepts_backend_module_entrypoint() -> None:
    manifest = _validate_manifest(
        {
            "kind": "better-agent-extension",
            "id": "ofek.compiled-backend",
            "name": "Compiled Backend",
            "version": "1.0.0",
            "surfaces": ["backend_feature"],
            "entrypoints": {"backend_module": "compiled_backend.routes"},
            "permissions": {"backend_routes": True},
        }
    )
    if manifest["entrypoints"]["backend_module"] != "compiled_backend.routes":
        raise AssertionError(manifest["entrypoints"])
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek.bad-backend",
                "name": "Bad Backend",
                "version": "1.0.0",
                "surfaces": ["backend_feature"],
                "entrypoints": {
                    "backend": "backend/routes.py",
                    "backend_module": "compiled_backend.routes",
                },
                "permissions": {"backend_routes": True},
            }
        )
    except extension_store.ExtensionError as exc:
        if "either backend or backend_module" not in str(exc):
            raise
    else:
        raise AssertionError("backend and backend_module were accepted together")
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek.bad-module",
                "name": "Bad Module",
                "version": "1.0.0",
                "surfaces": ["backend_feature"],
                "entrypoints": {"backend_module": "../bad"},
                "permissions": {"backend_routes": True},
            }
        )
    except extension_store.ExtensionError as exc:
        if "backend_module" not in str(exc):
            raise
        return
    raise AssertionError("invalid backend_module was accepted")


def test_installed_extension_config_exposes_remote_services() -> None:
    package = _write_private_extension_package(
        "ofek.remote-config",
        "extensions/remote-config",
        {
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "remote_services": [
                    {
                        "name": "planner",
                        "base_url": "https://api.example.test/v1",
                        "purpose": "Runs proprietary planning logic outside the local package.",
                    }
                ],
            },
            "permissions": {"network": True},
        },
    )
    try:
        record = extension_store._install_from_package_dir(
            package_dir=package,
            source={
                "type": "artifact",
                "repo_url": "https://example.test/remote.tar.gz",
                "extension_path": "",
                "ref": "",
                "commit_sha": "remote-config",
                "artifact_sha256": "0" * 64,
                "artifact_url": "https://example.test/remote.tar.gz",
            },
            persist=True,
        )
        if record["manifest"]["entrypoints"]["remote_services"][0]["name"] != "planner":
            raise AssertionError(record)
        cfg = extension_store.extension_config("ofek.remote-config")
        if cfg["remote_services"] != record["manifest"]["entrypoints"]["remote_services"]:
            raise AssertionError(cfg["remote_services"])
    finally:
        try:
            extension_store.uninstall("ofek.remote-config")
        except Exception:
            pass


def test_manifest_rejects_mismatched_builtin_mcp_replacement() -> None:
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek.shadow",
                "name": "Shadow",
                "version": "1.0.0",
                "surfaces": ["runtime_mcp"],
                "entrypoints": {
                    "mcp": [
                        {
                            "name": "shadow-project-updates",
                            "replaces_builtin": "project-updates",
                            "python": "mcp/server.py",
                        }
                    ]
                },
            }
        )
    except extension_store.ExtensionError as exc:
        if "requires core_roles" not in str(exc):
            raise
    else:
        raise AssertionError("mismatched builtin MCP replacement was accepted")


def test_manifest_rejects_root_level_frontend_entrypoint() -> None:
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek.frontend",
                "name": "Frontend",
                "version": "1.0.0",
                "surfaces": ["frontend_feature"],
                "entrypoints": {"frontend": "index.js"},
            }
        )
    except extension_store.ExtensionError as exc:
        if "dedicated asset directory" not in str(exc):
            raise
    else:
        raise AssertionError("root-level frontend entrypoint was accepted")


def test_manifest_rejects_missing_team_definition_file() -> None:
    work = _private_monorepo_test_work()
    try:
        repo = work / "repo"
        package = repo / "extensions" / "testape"
        package.mkdir(parents=True)
        manifest = {
            "kind": "better-agent-extension",
            "id": "fixture.testape",
        "core_roles": ["testape"],
            "name": "Testape",
            "version": "1.0.0",
            "surfaces": ["backend_feature"],
            "entrypoints": {
                "team_definitions": [
                    {
                        "name": "testape-ui-expert",
                        "path": "teams/missing.json",
                    }
                ]
            },
            "protocol": {
                "version": 1,
                "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
            },
            "marketplace": {},
        }
        (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        _git(repo, "init")
        _git(repo, "config", "user.email", "test@example.test")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "add", "extensions/testape/better-agent-extension.json")
        _git(repo, "commit", "-m", "add broken testape extension")
        try:
            extension_store.install_from_repo(
                repo_url=repo.as_uri(),
                extension_path="extensions/testape",
            )
        except extension_store.ExtensionError as exc:
            if "team definition file not found" not in str(exc):
                raise
            return
        raise AssertionError("missing team definition was accepted")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_installed_extension_exports_team_definition_sources() -> None:
    work = _private_monorepo_test_work()
    try:
        repo, _commit = _make_team_definition_repo(work)
        extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/testape",
        )
        sources = extension_store.team_definition_sources()
        source = next(item for item in sources if item["source_id"] == f"extension:{"fixture.testape"}:testape-ui-expert")
        if source["definition"]["manager"]["id"] != "coordinator":
            raise AssertionError(source)
        if source["extension_name"] != "Testape":
            raise AssertionError(source)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_install_from_private_monorepo_path_and_toggle() -> None:
    work = _private_monorepo_test_work()
    try:
        repo, commit = _make_repo(work)
        record = extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/requirements",
        )
        if record["manifest"]["id"] != "ofek.requirements":
            raise AssertionError(record)
        if record["source"]["commit_sha"] != commit:
            raise AssertionError(record["source"])
        if record["entitlement"]["status"] != "not_required":
            raise AssertionError(record["entitlement"])
        install_path = Path(record["source"]["install_path"])
        if not (install_path / "better-agent-extension.json").exists():
            raise AssertionError("installed manifest missing")
        disabled = extension_store.set_enabled("ofek.requirements", False)
        if disabled["enabled"] is not False:
            raise AssertionError("extension did not disable")
        items = extension_store.list_extensions()
        req_items = [item for item in items if item["manifest"]["id"] == "ofek.requirements"]
        if len(req_items) != 1:
            raise AssertionError(items)
        extension_store.uninstall("ofek.requirements")
        if extension_store.get_extension("ofek.requirements") is not None:
            raise AssertionError("extension still listed after uninstall")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_package_install_rejects_symlink_entries() -> None:
    work = _private_monorepo_test_work()
    try:
        repo, _commit = _make_repo(work, extension_id="ofek.linked")
        package = repo / "extensions" / "requirements"
        os.symlink("better-agent-extension.json", package / "manifest-link.json")
        _git(repo, "add", "extensions/requirements/manifest-link.json")
        _git(repo, "commit", "-m", "add linked package file")
        try:
            extension_store.install_from_repo(
                repo_url=repo.as_uri(),
                extension_path="extensions/requirements",
            )
        except extension_store.ExtensionError as exc:
            if "must not contain links" not in str(exc):
                raise
            return
        raise AssertionError("extension package symlink was accepted")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _make_dep_repo(root: Path, extension_id: str, dependencies: list[str]) -> tuple[Path, str]:
    repo = root / f"repo-{extension_id}"
    package = repo / "extensions" / "pkg"
    package.mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
        "permissions": {},
        "dependencies": dependencies,
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "extensions/pkg/better-agent-extension.json")
    _git(repo, "commit", "-m", f"add {extension_id}")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_manifest_dependencies_accepted_and_deduped() -> None:
    manifest = _validate_manifest({
        "kind": "better-agent-extension",
        "id": "ofek-dev.feature",
        "name": "Feature",
        "version": "1.0.0",
        "dependencies": ["ofek-dev.base", "ofek-dev.base", "other.dep"],
    })
    if manifest["dependencies"] != ["ofek-dev.base", "other.dep"]:
        raise AssertionError(manifest["dependencies"])


def test_manifest_dependencies_reject_self_and_bad_id() -> None:
    try:
        _validate_manifest({
            "kind": "better-agent-extension",
            "id": "ofek-dev.feature",
            "name": "Feature",
            "version": "1.0.0",
            "dependencies": ["ofek-dev.feature"],
        })
    except extension_store.ExtensionError as exc:
        if "itself" not in str(exc):
            raise
    else:
        raise AssertionError("self-dependency was accepted")
    try:
        _validate_manifest({
            "kind": "better-agent-extension",
            "id": "ofek-dev.feature",
            "name": "Feature",
            "version": "1.0.0",
            "dependencies": ["Bad ID With Spaces"],
        })
    except extension_store.ExtensionError as exc:
        if "valid extension id" not in str(exc):
            raise
    else:
        raise AssertionError("invalid dependency id was accepted")


def test_manifest_accepts_extension_protocol_smoke_test() -> None:
    defaulted = extension_store.validate_manifest({
        "kind": "better-agent-extension",
        "id": "ofek.missing-protocol",
        "name": "Missing Protocol",
        "version": "1.0.0",
    })
    if defaulted["protocol"]["smoke_test"]["required_paths"] != ["better-agent-extension.json"]:
        raise AssertionError(defaulted["protocol"])
    python_defaulted = extension_store.validate_manifest({
        "kind": "better-agent-extension",
        "id": "ofek.missing-python-protocol",
        "name": "Missing Python Protocol",
        "version": "1.0.0",
        "entrypoints": {
            "mcp": [
                {
                    "name": "ofek-python",
                    "python": "mcp/server.py",
                }
            ],
        },
    })
    if python_defaulted["protocol"]["smoke_test"]["python_modules"] != ["mcp.server"]:
        raise AssertionError(python_defaulted["protocol"])

    manifest = _validate_manifest({
        "kind": "better-agent-extension",
        "id": "ofek.protocol",
        "name": "Protocol",
        "version": "1.0.0",
        "protocol": {
            "version": 1,
            "smoke_test": {
                "required_paths": ["better-agent-extension.json", "mcp/server.py"],
                "python_modules": ["mcp.server"],
            },
        },
    })
    smoke = manifest["protocol"]["smoke_test"]
    if smoke["required_paths"] != ["better-agent-extension.json", "mcp/server.py"]:
        raise AssertionError(smoke)
    if smoke["python_modules"] != ["mcp.server"]:
        raise AssertionError(smoke)
    try:
        _validate_manifest({
            "kind": "better-agent-extension",
            "id": "ofek.bad-protocol",
            "name": "Bad Protocol",
            "version": "1.0.0",
            "protocol": {"version": 2},
        })
    except extension_store.ExtensionError as exc:
        if "protocol.version" not in str(exc):
            raise
    else:
        raise AssertionError("invalid protocol version was accepted")


def test_install_smoke_test_rejects_missing_protocol_files() -> None:
    package = _write_private_extension_package(
        "ofek.protocol-missing",
        "extensions/protocol-missing",
        {
            "surfaces": [],
            "protocol": {
                "version": 1,
                "smoke_test": {"required_paths": ["missing.txt"], "python_modules": []},
            },
        },
    )
    try:
        extension_store._install_from_package_dir(
            package_dir=package,
            source={
                "type": "artifact",
                "repo_url": "https://example.test/protocol.tar.gz",
                "extension_path": "",
                "ref": "",
                "commit_sha": "protocol-missing",
            },
            persist=False,
        )
    except extension_store.ExtensionError as exc:
        if "protocol.smoke_test.required_paths" not in str(exc):
            raise
        return
    raise AssertionError("install accepted a package that failed protocol smoke")


def test_install_smoke_test_rejects_bad_python_module_import() -> None:
    package = _write_private_extension_package(
        "ofek.protocol-bad-import",
        "extensions/protocol-bad-import",
        {
            "surfaces": [],
            "protocol": {
                "version": 1,
                "smoke_test": {
                    "required_paths": ["better-agent-extension.json"],
                    "python_modules": ["bad_extension"],
                },
            },
        },
        {"bad_extension.py": 'raise RuntimeError("bad import smoke")\n'},
    )
    try:
        extension_store._install_from_package_dir(
            package_dir=package,
            source={
                "type": "artifact",
                "repo_url": "https://example.test/protocol.tar.gz",
                "extension_path": "",
                "ref": "",
                "commit_sha": "protocol-bad-import",
            },
            persist=False,
        )
    except extension_store.ExtensionError as exc:
        if "protocol.smoke_test.python_modules failed" not in str(exc):
            raise
        return
    raise AssertionError("install accepted a package with a failing python smoke import")


def test_runtime_ready_requires_protocol_smoke_to_pass() -> None:
    package = _write_private_extension_package(
        "ofek.protocol-ready",
        "extensions/protocol-ready",
        {
            "surfaces": [],
            "protocol": {
                "version": 1,
                "smoke_test": {"required_paths": ["marker.txt"], "python_modules": []},
            },
        },
        {"marker.txt": "ok"},
    )
    record = extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "artifact",
            "repo_url": "https://example.test/protocol.tar.gz",
            "extension_path": "",
            "ref": "",
            "commit_sha": "protocol-ready",
        },
        persist=True,
    )
    try:
        if record["smoke_test"]["status"] != "passed":
            raise AssertionError(record["smoke_test"])
        if not extension_store.is_extension_runtime_ready("ofek.protocol-ready"):
            raise AssertionError("runtime-ready extension failed protocol smoke")
        Path(record["source"]["install_path"], "marker.txt").unlink()
        if extension_store.is_extension_runtime_ready("ofek.protocol-ready"):
            raise AssertionError("runtime-ready extension ignored failing protocol smoke")
    finally:
        extension_store.uninstall("ofek.protocol-ready")


def test_runtime_ready_accepts_persisted_manifest_without_protocol() -> None:
    package = Path(tempfile.mkdtemp(prefix="bc-test-no-protocol-ready-")) / "legacy"
    package.mkdir(parents=True)
    try:
        manifest = {
            "kind": "better-agent-extension",
            "id": "ofek.no-protocol-ready",
            "name": "No Protocol Ready",
            "version": "1.0.0",
            "description": "Legacy persisted fixture.",
            "surfaces": [],
            "entrypoints": {},
            "permissions": {},
            "marketplace": {},
        }
        (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        data = extension_store._load()
        data["extensions"]["ofek.no-protocol-ready"] = {
            "manifest": manifest,
            "enabled": True,
            "installed_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "instructions_enabled": {},
            "permission_grants": {},
            "source": {"type": "artifact", "install_path": str(package)},
            "entitlement": {"status": "not_required"},
        }
        extension_store._save(data)
        if not extension_store.is_extension_runtime_ready("ofek.no-protocol-ready"):
            raise AssertionError("runtime readiness rejected a persisted manifest with default protocol")
    finally:
        try:
            extension_store.uninstall("ofek.no-protocol-ready")
        except extension_store.ExtensionError:
            pass
        shutil.rmtree(package.parent, ignore_errors=True)


def test_create_personal_harness_extension_snapshots_instructions_and_skills() -> None:
    project = Path(tempfile.mkdtemp(prefix="bc-test-personal-harness-project-"))
    home = Path.home()
    global_claude = home / ".claude" / "CLAUDE.md"
    global_codex = home / ".codex" / "AGENTS.md"
    skill_dir = home / ".agents" / "skills" / "personal-skill"
    old_codex_home = os.environ.get("CODEX_HOME")
    try:
        os.environ["CODEX_HOME"] = str(home / ".codex")
        global_claude.parent.mkdir(parents=True, exist_ok=True)
        global_codex.parent.mkdir(parents=True, exist_ok=True)
        global_claude.write_text(
            "global claude\n\n<!-- BEGIN better-agent:extension:old:rules -->\nmanaged\n<!-- END better-agent:extension:old:rules -->\n",
            encoding="utf-8",
        )
        global_codex.write_text("global codex\n", encoding="utf-8")
        (project / "CLAUDE.md").write_text("project claude\n", encoding="utf-8")
        (project / "AGENTS.md").write_text("project codex\n", encoding="utf-8")
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: personal-skill\ndescription: test skill\n---\n\n# Skill\n",
            encoding="utf-8",
        )

        record = personal_harness_extension.create(project_paths=[str(project)])
        root = extension_store.runtime_package_root(record["manifest"]["id"])
        if root is None:
            raise AssertionError("personal harness install path missing")
        global_text = (root / "instructions" / "global.md").read_text(encoding="utf-8")
        project_text = (root / "instructions" / "project.md").read_text(encoding="utf-8")
        if "global claude" not in global_text or "global codex" not in global_text:
            raise AssertionError(global_text)
        if "managed" in global_text or "BEGIN better-agent" in global_text:
            raise AssertionError(global_text)
        if "project claude" not in project_text or "project codex" not in project_text:
            raise AssertionError(project_text)
        if not (root / "skills" / "personal-skill" / "SKILL.md").is_file():
            raise AssertionError("personal skill was not copied")
        state = record.get("instructions_enabled") or {}
        if state.get("global") is not True or state.get("projects", {}).get(str(project.resolve())) is not True:
            raise AssertionError(state)
    finally:
        try:
            extension_store.uninstall(personal_harness_extension.PERSONAL_HARNESS_EXTENSION_ID)
        except Exception:
            pass
        shutil.rmtree(project, ignore_errors=True)
        shutil.rmtree(skill_dir, ignore_errors=True)
        global_claude.unlink(missing_ok=True)
        global_codex.unlink(missing_ok=True)
        if old_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old_codex_home


def test_runtime_ready_only_spawn_runs_requires_default_session_llm() -> None:
    import config_store

    # Internal LLM tasks resolve via inheritance from the default provider, so
    # clearing an assignment no longer makes a task unready. The way to make a
    # task genuinely unready is to remove providers entirely. Save/restore the
    # full provider state around the test.
    old_state = config_store._load_state()
    config_store._save_state({**old_state, "providers": [], "default_provider_id": None})
    loopback = _write_private_extension_package(
        "ofek.loopback-ready",
        "extensions/loopback-ready",
        {
            "surfaces": [],
            "permissions": {"session_state": True, "internal_loopback": True},
        },
    )
    spawn_runs = _write_private_extension_package(
        "ofek.spawn-runs-gated",
        "extensions/spawn-runs-gated",
        {
            "surfaces": [],
            "permissions": {"spawn_runs": True},
        },
    )
    loopback_record = extension_store._install_from_package_dir(
        package_dir=loopback,
        source={
            "type": "artifact",
            "repo_url": "https://example.test/loopback.tar.gz",
            "extension_path": "",
            "ref": "",
            "commit_sha": "loopback-ready",
        },
        persist=True,
    )
    spawn_record = extension_store._install_from_package_dir(
        package_dir=spawn_runs,
        source={
            "type": "artifact",
            "repo_url": "https://example.test/spawn.tar.gz",
            "extension_path": "",
            "ref": "",
            "commit_sha": "spawn-runs-gated",
        },
        persist=True,
    )
    try:
        if not extension_store.is_extension_runtime_ready(loopback_record["manifest"]["id"]):
            raise AssertionError("session_state/internal_loopback-only extension required default_session")
        if extension_store.is_extension_runtime_ready(spawn_record["manifest"]["id"]):
            raise AssertionError("spawn_runs extension should require default_session")
    finally:
        config_store._save_state(old_state)
        extension_store.uninstall(loopback_record["manifest"]["id"])
        extension_store.uninstall(spawn_record["manifest"]["id"])


def test_set_enabled_enforces_dependencies() -> None:
    work = _private_monorepo_test_work()
    try:
        base_repo, _ = _make_dep_repo(work, "ofek.base", [])
        feat_repo, _ = _make_dep_repo(work, "ofek.feature", ["ofek.base"])
        extension_store.install_from_repo(repo_url=base_repo.as_uri(), extension_path="extensions/pkg")
        extension_store.install_from_repo(repo_url=feat_repo.as_uri(), extension_path="extensions/pkg")
        # Both install enabled-by-default; disable both to set up the test.
        extension_store.set_enabled("ofek.feature", False)
        extension_store.set_enabled("ofek.base", False)
        # Enable the dependent while its dep is inactive -> fail closed.
        try:
            extension_store.set_enabled("ofek.feature", True)
        except extension_store.ExtensionError as exc:
            if "not active" not in str(exc):
                raise
        else:
            raise AssertionError("dependent enabled without active dependency")
        # Enable the dep, then the dependent succeeds.
        extension_store.set_enabled("ofek.base", True)
        enabled = extension_store.set_enabled("ofek.feature", True)
        if enabled["enabled"] is not True:
            raise AssertionError(enabled)
        # Disabling the dep while the dependent is active -> fail closed.
        try:
            extension_store.set_enabled("ofek.base", False)
        except extension_store.ExtensionError as exc:
            if "depend on it" not in str(exc):
                raise
        else:
            raise AssertionError("dependency disabled while dependent active")
        # Disable the dependent first, then the dep disables cleanly.
        extension_store.set_enabled("ofek.feature", False)
        disabled = extension_store.set_enabled("ofek.base", False)
        if disabled["enabled"] is not False:
            raise AssertionError(disabled)
    finally:
        for eid in ("ofek.feature", "ofek.base"):
            try:
                extension_store.uninstall(eid)
            except Exception:
                pass
        shutil.rmtree(work, ignore_errors=True)


def test_slow_call_quarantine_respects_per_route_grace_but_not_unboundedly() -> None:
    """A route with a manifest-declared slow_call_grace_seconds (e.g. a
    routine /run action that legitimately takes ~2 min) must not quarantine
    on calls within that grace period, while an undeclared route on the same
    extension still quarantines at the tight default SLA — grace is scoped
    to the calling route via extension_backend_loader, this test locks the
    underlying extension_store contract that record_slow_backend_call
    actually honors an overridden minimum_seconds."""
    work = _private_monorepo_test_work()
    try:
        base_repo, _ = _make_dep_repo(work, "ofek.graced-base", [])
        extension_store.install_from_repo(repo_url=base_repo.as_uri(), extension_path="extensions/pkg")
        activation_id = extension_store.activation_identity("ofek.graced-base")

        # Calls within the declared grace period (130s) never count as
        # incidents, no matter how many happen.
        for _ in range(5):
            if extension_store.record_slow_backend_call(
                "ofek.graced-base", activation_id=activation_id, elapsed_seconds=5.0, minimum_seconds=130.0
            ):
                raise AssertionError("call within declared grace period must not quarantine")

        # The same extension, called through a route with no grace override
        # (minimum_seconds defaults to the tight platform SLA), still
        # quarantines after 3 strikes exactly as before.
        for elapsed in (2.1, 2.2):
            if extension_store.record_slow_backend_call(
                "ofek.graced-base", activation_id=activation_id, elapsed_seconds=elapsed
            ):
                raise AssertionError("quarantined before third strike")
        disabled = extension_store.record_slow_backend_call(
            "ofek.graced-base", activation_id=activation_id, elapsed_seconds=2.3
        )
        if disabled != ["ofek.graced-base"]:
            raise AssertionError(disabled)

        # A call that exceeds even the declared grace period still counts —
        # grace bounds the SLA, it does not disable the guardrail outright.
        extension_store.set_enabled("ofek.graced-base", True)
        activation_id = extension_store.activation_identity("ofek.graced-base")
        for _ in range(2):
            if extension_store.record_slow_backend_call(
                "ofek.graced-base", activation_id=activation_id, elapsed_seconds=131.0, minimum_seconds=130.0
            ):
                raise AssertionError("quarantined before third strike")
        disabled = extension_store.record_slow_backend_call(
            "ofek.graced-base", activation_id=activation_id, elapsed_seconds=131.0, minimum_seconds=130.0
        )
        if disabled != ["ofek.graced-base"]:
            raise AssertionError("a call exceeding even the declared grace period must still quarantine")
    finally:
        try:
            extension_store.uninstall("ofek.graced-base")
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)


def test_slow_call_quarantine_disables_extension_and_dependents_durably() -> None:
    work = _private_monorepo_test_work()
    try:
        base_repo, _ = _make_dep_repo(work, "ofek.laggy-base", [])
        feat_repo, _ = _make_dep_repo(work, "ofek.laggy-dependent", ["ofek.laggy-base"])
        extension_store.install_from_repo(repo_url=base_repo.as_uri(), extension_path="extensions/pkg")
        extension_store.install_from_repo(repo_url=feat_repo.as_uri(), extension_path="extensions/pkg")
        activation_id = extension_store.activation_identity("ofek.laggy-base")

        if extension_store.record_slow_backend_call("ofek.laggy-base", activation_id=activation_id, elapsed_seconds=1.99):
            raise AssertionError("short call counted")
        if extension_store.record_slow_backend_call("ofek.laggy-base", activation_id=activation_id, elapsed_seconds=2.0):
            raise AssertionError("first slow call quarantined")
        if extension_store.record_slow_backend_call("ofek.laggy-base", activation_id=activation_id, elapsed_seconds=4.0):
            raise AssertionError("second slow call quarantined")
        disabled = extension_store.record_slow_backend_call("ofek.laggy-base", activation_id=activation_id, elapsed_seconds=4.25)
        if disabled != ["ofek.laggy-base", "ofek.laggy-dependent"]:
            raise AssertionError(disabled)
        for extension_id in disabled:
            record = extension_store.get_extension(extension_id)
            if not record or record["enabled"] is not False:
                raise AssertionError(record)
            quarantine = record.get("quarantine") or {}
            if quarantine.get("reason") != "repeated_slow_backend_calls":
                raise AssertionError(quarantine)
            if quarantine.get("attributed_extension_id") != "ofek.laggy-base":
                raise AssertionError(quarantine)

        extension_store.set_enabled("ofek.laggy-base", True)
        enabled = extension_store.get_extension("ofek.laggy-base")
        if not enabled or "quarantine" in enabled:
            raise AssertionError(enabled)
    finally:
        for extension_id in ("ofek.laggy-dependent", "ofek.laggy-base"):
            try:
                extension_store.uninstall(extension_id)
            except Exception:
                pass
        shutil.rmtree(work, ignore_errors=True)


def test_new_generation_recovers_exact_auto_quarantine_cohort() -> None:
    work = _private_monorepo_test_work()
    try:
        base_repo, original_sha = _make_dep_repo(work, "ofek.recover-base", [])
        dependent_repo, _ = _make_dep_repo(work, "ofek.recover-dependent", ["ofek.recover-base"])
        extension_store.install_from_repo(repo_url=base_repo.as_uri(), extension_path="extensions/pkg")
        extension_store.install_from_repo(repo_url=dependent_repo.as_uri(), extension_path="extensions/pkg")
        activation_id = extension_store.activation_identity("ofek.recover-base")
        for elapsed in (2.1, 2.2, 2.3):
            disabled = extension_store.record_slow_backend_call("ofek.recover-base", activation_id=activation_id, elapsed_seconds=elapsed)
        if disabled != ["ofek.recover-base", "ofek.recover-dependent"]:
            raise AssertionError(disabled)
        quarantined = extension_store.get_extension("ofek.recover-base") or {}
        quarantine = quarantined.get("quarantine") or {}
        if quarantine.get("attributed_generation") != original_sha:
            raise AssertionError(quarantine)
        if quarantine.get("cohort") != ["ofek.recover-base", "ofek.recover-dependent"]:
            raise AssertionError(quarantine)

        manifest_path = base_repo / "extensions" / "pkg" / "better-agent-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "1.0.1"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        _git(base_repo, "add", "extensions/pkg/better-agent-extension.json")
        _git(base_repo, "commit", "-m", "new generation")
        extension_store.install_from_repo(repo_url=base_repo.as_uri(), extension_path="extensions/pkg")
        recovered_activation = extension_store.activation_identity("ofek.recover-base")
        if not recovered_activation or recovered_activation == activation_id:
            raise AssertionError("new-generation recovery did not rotate activation")
        for elapsed in (2.4, 2.5, 2.6):
            if extension_store.record_slow_backend_call(
                "ofek.recover-base", activation_id=activation_id, elapsed_seconds=elapsed
            ):
                raise AssertionError("old generation quarantined recovered activation")
        history = read_json(extension_store._slow_calls_path(), {"extensions": {}})
        if "ofek.recover-base" in history.get("extensions", {}):
            raise AssertionError("old generation polluted recovered incident history")
        for extension_id in ("ofek.recover-base", "ofek.recover-dependent"):
            record = extension_store.get_extension(extension_id) or {}
            if record.get("enabled") is not True or record.get("quarantine"):
                raise AssertionError(record)
    finally:
        for extension_id in ("ofek.recover-dependent", "ofek.recover-base"):
            try:
                extension_store.uninstall(extension_id)
            except Exception:
                pass
        shutil.rmtree(work, ignore_errors=True)


def test_incidents_are_fenced_to_same_generation_activation() -> None:
    work = _private_monorepo_test_work()
    extension_id = "ofek.activation-fence"
    try:
        repo, _ = _make_dep_repo(work, extension_id, [])
        extension_store.install_from_repo(repo_url=repo.as_uri(), extension_path="extensions/pkg")
        old_activation = extension_store.activation_identity(extension_id)
        extension_store.set_enabled(extension_id, False)
        extension_store.set_enabled(extension_id, True)
        current_activation = extension_store.activation_identity(extension_id)
        if not current_activation or current_activation == old_activation:
            raise AssertionError("same-generation re-enable did not rotate activation")
        with extension_store._store_lock():
            write_json(extension_store._slow_calls_path(), {"extensions": {
                extension_id: {"activation_id": old_activation, "slow_asgi": [time.time()]}
            }})

        results: list[list[str]] = []
        threads = [
            threading.Thread(
                target=lambda: results.append(extension_store.record_slow_backend_call(
                    extension_id, activation_id=old_activation, elapsed_seconds=3.0
                ))
            )
            for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if any(results):
            raise AssertionError(results)
        if extension_store.record_backend_timeout(
            extension_id, activation_id=old_activation, elapsed_seconds=3.0
        ):
            raise AssertionError("old activation timeout quarantined current activation")
        history = read_json(extension_store._slow_calls_path(), {"extensions": {}})
        stale_history = history.get("extensions", {}).get(extension_id, {})
        if stale_history.get("activation_id") != old_activation or len(stale_history.get("slow_asgi", [])) != 1:
            raise AssertionError("old activation completion mutated durable history")

        for _ in range(2):
            if extension_store.record_slow_backend_call(
                extension_id, activation_id=current_activation, elapsed_seconds=3.0
            ):
                raise AssertionError("current activation quarantined before limit")
        disabled = extension_store.record_slow_backend_call(
            extension_id, activation_id=current_activation, elapsed_seconds=3.0
        )
        if disabled != [extension_id]:
            raise AssertionError(disabled)
        record = extension_store.get_extension(extension_id) or {}
        if record.get("enabled") is not False or record.get("activation_id") == current_activation:
            raise AssertionError(record)
        extension_store.set_enabled(extension_id, True)
        timeout_activation = extension_store.activation_identity(extension_id)
        for _ in range(2):
            if extension_store.record_backend_timeout(
                extension_id, activation_id=timeout_activation, elapsed_seconds=3.0
            ):
                raise AssertionError("current timeout activation quarantined before limit")
        if extension_store.record_backend_timeout(
            extension_id, activation_id=timeout_activation, elapsed_seconds=3.0
        ) != [extension_id]:
            raise AssertionError("current timeout activation did not quarantine at limit")
    finally:
        try:
            extension_store.uninstall(extension_id)
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)


def test_legacy_quarantine_is_annotated_without_enabling_then_recovers() -> None:
    work = _private_monorepo_test_work()
    try:
        base_repo, original_sha = _make_dep_repo(work, "ofek.legacy-base", [])
        dependent_repo, _ = _make_dep_repo(
            work, "ofek.legacy-dependent", ["ofek.legacy-base"]
        )
        extension_store.install_from_repo(repo_url=base_repo.as_uri(), extension_path="extensions/pkg")
        extension_store.install_from_repo(
            repo_url=dependent_repo.as_uri(), extension_path="extensions/pkg"
        )
        for elapsed in (2.1, 2.2, 2.3):
            extension_store.record_slow_backend_call(
                "ofek.legacy-base", activation_id=extension_store.activation_identity("ofek.legacy-base"), elapsed_seconds=elapsed
            )
        with extension_store._store_lock():
            data = extension_store._read_store_unlocked()
            for extension_id in ("ofek.legacy-base", "ofek.legacy-dependent"):
                quarantine = data["extensions"][extension_id]["quarantine"]
                quarantine.pop("attributed_generation")
                quarantine.pop("cohort")
            extension_store._write_store_unlocked(data)

        first = extension_store._load()
        second = extension_store._load()
        if first != second:
            raise AssertionError("legacy migration is not restart-idempotent")
        expected_cohort = ["ofek.legacy-base", "ofek.legacy-dependent"]
        for extension_id in expected_cohort:
            record = first["extensions"][extension_id]
            quarantine = record.get("quarantine") or {}
            if record.get("enabled") is not False:
                raise AssertionError("migration implicitly enabled an extension")
            if quarantine.get("attributed_generation") != original_sha:
                raise AssertionError(quarantine)
            if quarantine.get("cohort") != expected_cohort:
                raise AssertionError(quarantine)

        manifest_path = base_repo / "extensions" / "pkg" / "better-agent-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "1.0.1"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        _git(base_repo, "add", "extensions/pkg/better-agent-extension.json")
        _git(base_repo, "commit", "-m", "new generation")
        extension_store.install_from_repo(repo_url=base_repo.as_uri(), extension_path="extensions/pkg")
        for extension_id in expected_cohort:
            record = extension_store.get_extension(extension_id) or {}
            if record.get("enabled") is not True or record.get("quarantine"):
                raise AssertionError(record)
    finally:
        for extension_id in ("ofek.legacy-dependent", "ofek.legacy-base"):
            try:
                extension_store.uninstall(extension_id)
            except Exception:
                pass
        shutil.rmtree(work, ignore_errors=True)


def test_legacy_quarantine_rejects_ambiguous_or_invalid_cohorts() -> None:
    valid_manifest = lambda extension_id, dependencies=(): {
        "kind": "better-agent-extension",
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": "test",
        "surfaces": [],
        "entrypoints": {},
        "permissions": {},
        "dependencies": list(dependencies),
    }

    def record(extension_id, dependencies=(), *, at="same", generation="gen"):
        return {
            "manifest": valid_manifest(extension_id, dependencies),
            "enabled": False,
            "source": {"commit_sha": generation},
            "quarantine": {
                "reason": "repeated_slow_backend_calls",
                "at": at,
                "attributed_extension_id": "ofek.legacy-trigger",
            },
        }

    cases = []
    missing_generation = {
        "ofek.legacy-trigger": record("ofek.legacy-trigger", generation=""),
    }
    cases.append(missing_generation)
    extra = {
        "ofek.legacy-trigger": record("ofek.legacy-trigger"),
        "ofek.legacy-extra": record("ofek.legacy-extra"),
    }
    cases.append(extra)
    mismatched_time = {
        "ofek.legacy-trigger": record("ofek.legacy-trigger"),
        "ofek.legacy-dependent": record(
            "ofek.legacy-dependent", ["ofek.legacy-trigger"], at="other"
        ),
    }
    cases.append(mismatched_time)
    cycle = {
        "ofek.legacy-trigger": record("ofek.legacy-trigger", ["ofek.legacy-dependent"]),
        "ofek.legacy-dependent": record(
            "ofek.legacy-dependent", ["ofek.legacy-trigger"]
        ),
    }
    cases.append(cycle)
    manually_enabled = {"ofek.legacy-trigger": record("ofek.legacy-trigger")}
    manually_enabled["ofek.legacy-trigger"]["enabled"] = True
    cases.append(manually_enabled)

    for extensions in cases:
        data = {
            "schema_version": extension_store.STORE_SCHEMA_VERSION,
            "extensions": extensions,
            "deleted_extensions": {},
        }
        before = json.loads(json.dumps(data))
        if extension_store._annotate_legacy_quarantine_cohorts(data):
            raise AssertionError(data)
        if data != before:
            raise AssertionError("rejected legacy cohort was mutated")


def test_legacy_quarantine_retains_then_exactly_once_drains_lag_spool() -> None:
    import lag_incident_queue

    work = _private_monorepo_test_work()
    receipt_path = work / "receipts.jsonl"
    old_receipt_path = os.environ.get("LEGACY_LAG_RECEIPT_PATH")
    os.environ["LEGACY_LAG_RECEIPT_PATH"] = str(receipt_path)
    try:
        board_repo, _ = _make_dep_repo(work, "ofek-dev.agent-board", [])
        assistant_repo, _ = _make_dep_repo(
            work, "ofek-dev.assistant", ["ofek-dev.agent-board"]
        )
        package = assistant_repo / "extensions" / "pkg"
        manifest_path = package / "better-agent-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["entrypoints"]["backend"] = "backend/routes.py"
        manifest["permissions"]["backend_routes"] = True
        (package / "backend").mkdir()
        (package / "backend" / "routes.py").write_text(
            "\n".join([
                "import json",
                "from pathlib import Path",
                "from fastapi import APIRouter, Request",
                "def create_router(context):",
                "    router = APIRouter()",
                "    @router.post('/assistant/bug-report')",
                "    async def receive(request: Request):",
                "        body = await request.json()",
                f"        path = Path({str(receipt_path)!r})",
                "        with path.open('a', encoding='utf-8') as stream:",
                "            stream.write(json.dumps(body, sort_keys=True) + '\\n')",
                "        return {'ok': True}",
                "    return router",
            ]),
            encoding="utf-8",
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        _git(assistant_repo, "add", "extensions/pkg")
        _git(assistant_repo, "commit", "-m", "add lag receipt route")
        extension_store.install_from_repo(
            repo_url=board_repo.as_uri(), extension_path="extensions/pkg"
        )
        extension_store.install_from_repo(
            repo_url=assistant_repo.as_uri(), extension_path="extensions/pkg"
        )
        for elapsed in (2.1, 2.2, 2.3):
            extension_store.record_slow_backend_call(
                "ofek-dev.agent-board", activation_id=extension_store.activation_identity("ofek-dev.agent-board"), elapsed_seconds=elapsed
            )
        with extension_store._store_lock():
            data = extension_store._read_store_unlocked()
            for extension_id in ("ofek-dev.agent-board", "ofek-dev.assistant"):
                quarantine = data["extensions"][extension_id]["quarantine"]
                quarantine.pop("attributed_generation")
                quarantine.pop("cohort")
            extension_store._write_store_unlocked(data)
        spool = Path(_TMP_HOME) / "lag-incidents"
        shutil.rmtree(spool, ignore_errors=True)
        body = json.dumps({
            "requirement_ref": "bug:lag-watchdog:1234567890abcdef",
            "summary": "legacy migration e2e",
            "source": "lag_watchdog",
            "severity": "high",
        }, separators=(",", ":")).encode()
        lag_incident_queue.enqueue(body)
        extension_store._load()
        status, _ = extension_backend_loader.invoke_named_core_destination_sync(
            "assistant.lag-report", body_bytes=body
        )
        if status != 503 or lag_incident_queue.depth() != 1 or receipt_path.exists():
            raise AssertionError((status, lag_incident_queue.depth(), receipt_path.exists()))
        for extension_id in ("ofek-dev.agent-board", "ofek-dev.assistant"):
            failed_record = extension_store.get_extension(extension_id) or {}
            if failed_record.get("enabled") is not False or not failed_record.get("quarantine"):
                raise AssertionError("failed delivery changed quarantine state")

        board_manifest_path = board_repo / "extensions" / "pkg" / "better-agent-extension.json"
        board_manifest = json.loads(board_manifest_path.read_text(encoding="utf-8"))
        board_manifest["version"] = "1.0.1"
        board_manifest_path.write_text(json.dumps(board_manifest), encoding="utf-8")
        _git(board_repo, "add", "extensions/pkg/better-agent-extension.json")
        _git(board_repo, "commit", "-m", "new board generation")
        extension_store.install_from_repo(
            repo_url=board_repo.as_uri(), extension_path="extensions/pkg"
        )
        if extension_store.backend_surface_status("ofek-dev.assistant") != "ready":
            raise AssertionError({
                extension_id: extension_store.get_extension(extension_id)
                for extension_id in ("ofek-dev.agent-board", "ofek-dev.assistant")
            })

        async def dispatch(payload: bytes) -> lag_incident_queue.DispatchOutcome:
            status, _ = extension_backend_loader.invoke_named_core_destination_sync(
                "assistant.lag-report", body_bytes=payload
            )
            return lag_incident_queue.DispatchOutcome(status < 400)

        async def drain() -> None:
            lag_incident_queue.start(dispatch)
            try:
                for _ in range(200):
                    if lag_incident_queue.depth() == 0:
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("lag spool did not drain")
            finally:
                await lag_incident_queue.stop()

        asyncio.run(drain())
        receipts = receipt_path.read_text(encoding="utf-8").splitlines()
        if len(receipts) != 1 or lag_incident_queue.depth() != 0:
            raise AssertionError((receipts, lag_incident_queue.depth()))
        asyncio.run(drain())
        if len(receipt_path.read_text(encoding="utf-8").splitlines()) != 1:
            raise AssertionError("acknowledged lag incident was delivered twice")
    finally:
        extension_backend_loader.evict_persistent_backend("ofek-dev.assistant")
        for extension_id in ("ofek-dev.assistant", "ofek-dev.agent-board"):
            try:
                extension_store.uninstall(extension_id)
            except Exception:
                pass
        shutil.rmtree(Path(_TMP_HOME) / "lag-incidents", ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
        if old_receipt_path is None:
            os.environ.pop("LEGACY_LAG_RECEIPT_PATH", None)
        else:
            os.environ["LEGACY_LAG_RECEIPT_PATH"] = old_receipt_path


def test_user_disabled_quarantine_member_blocks_auto_recovery() -> None:
    work = _private_monorepo_test_work()
    try:
        base_repo, _ = _make_dep_repo(work, "ofek.user-base", [])
        dependent_repo, _ = _make_dep_repo(work, "ofek.user-dependent", ["ofek.user-base"])
        extension_store.install_from_repo(repo_url=base_repo.as_uri(), extension_path="extensions/pkg")
        extension_store.install_from_repo(repo_url=dependent_repo.as_uri(), extension_path="extensions/pkg")
        for elapsed in (2.1, 2.2, 2.3):
            extension_store.record_backend_timeout("ofek.user-base", activation_id=extension_store.activation_identity("ofek.user-base"), elapsed_seconds=elapsed)
        extension_store.set_enabled("ofek.user-dependent", False)
        manifest_path = base_repo / "extensions" / "pkg" / "better-agent-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "1.0.1"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        _git(base_repo, "add", "extensions/pkg/better-agent-extension.json")
        _git(base_repo, "commit", "-m", "new generation")
        extension_store.install_from_repo(repo_url=base_repo.as_uri(), extension_path="extensions/pkg")
        base = extension_store.get_extension("ofek.user-base") or {}
        dependent = extension_store.get_extension("ofek.user-dependent") or {}
        if base.get("enabled") is not False or not base.get("quarantine"):
            raise AssertionError(base)
        if dependent.get("enabled") is not False or dependent.get("quarantine"):
            raise AssertionError(dependent)
    finally:
        for extension_id in ("ofek.user-dependent", "ofek.user-base"):
            try:
                extension_store.uninstall(extension_id)
            except Exception:
                pass
        shutil.rmtree(work, ignore_errors=True)


def test_required_runtime_path_extensions_are_managed_builtins() -> None:
    # An extension that gates runtime-readiness on a required path it ships
    # (_BUILTIN_RUNTIME_REQUIRED_PATHS) can only ever satisfy that gate if the
    # backend seeds/refreshes it from local source. That happens exclusively
    # for ids registered as managed builtins. If such an id is absent from both
    # registries it installs once as a stale artifact, never gets the required
    # path, fails the runtime-ready gate, and its MCP never launches.
    managed = set(extension_store._PUBLIC_EXTENSION_PATHS)
    unmanaged = [
        eid
        for eid in extension_store._BUILTIN_RUNTIME_REQUIRED_PATHS
        if eid not in managed
    ]
    if unmanaged:
        raise AssertionError(
            f"required-runtime-path extensions are not managed builtins: {unmanaged}"
        )
    missing_names = [
        eid
        for eid in extension_store._PUBLIC_EXTENSION_PATHS
        if eid not in extension_store._EXTENSION_DISPLAY_NAMES
    ]
    if missing_names:
        raise AssertionError(f"managed builtins missing display names: {missing_names}")


def test_prune_extension_versions_keeps_active_and_newest_fallbacks() -> None:
    """Version snapshots accumulate one per private-repo HEAD / public hash and
    are never pruned elsewhere. _prune_extension_versions must keep the active
    install_path plus the N newest fallbacks and delete the rest, without
    touching anything outside the extension's versions/ dir."""
    import time

    ext_id = "ofek.prune-fixture"
    versions_dir = extension_store._install_root() / ext_id / "versions"
    versions_dir.mkdir(parents=True)
    active = versions_dir / "active-sha"
    active.mkdir()
    (active / "marker").write_text("active", encoding="utf-8")
    base = time.time()
    created = []
    for i, name in enumerate(["v1", "v2", "v3", "v4", "v5", "v6"]):
        d = versions_dir / name
        d.mkdir()
        (d / "marker").write_text(name, encoding="utf-8")
        os.utime(d, (base + i, base + i))  # v1 oldest .. v6 newest
        created.append(d)
    data = {"extensions": {ext_id: {"source": {"install_path": str(active)}}}}

    extension_store._prune_extension_versions(data)

    keep = extension_store._MAX_FALLBACK_VERSIONS
    remaining = {p.name for p in versions_dir.iterdir()}
    expected = {"active-sha", "v6", "v5", "v4"}  # active + 3 newest fallbacks
    if remaining != expected:
        raise AssertionError(
            f"pruning retained {sorted(remaining)}, expected {sorted(expected)}"
        )


def test_prune_extension_versions_tolerates_vanishing_dir() -> None:
    """If a version dir is removed between iterdir() and the sort's stat()
    (a concurrent install/GC), _prune_extension_versions must fail open
    per-dir instead of raising FileNotFoundError and 500-ing /api/extensions.
    See docstring: "Fails open per-dir so one broken entry never blocks
    reconcile."

    Reproduction: on Python 3.14 is_dir() does NOT route through Path.stat()
    (it uses os.scandir/os.stat directly), while the sort key calls the
    overridable Path.stat() explicitly. So a Path subclass whose stat()
    raises FileNotFoundError for one entry leaves that entry present in
    `fallbacks` (is_dir saw it as a dir) while making the sort key blow up —
    exactly the observed production race."""
    import time

    ext_id = "ofek.prune-race"
    versions_dir = extension_store._install_root() / ext_id / "versions"
    versions_dir.mkdir(parents=True)
    active = versions_dir / "active-sha"
    active.mkdir()
    (active / "marker").write_text("active", encoding="utf-8")
    base = time.time()
    # More than _MAX_FALLBACK_VERSIONS fallbacks so the sort path runs.
    names = ["r1", "r2", "r3", "r4", "r5"]
    for i, name in enumerate(names):
        d = versions_dir / name
        d.mkdir()
        (d / "marker").write_text(name, encoding="utf-8")
        os.utime(d, (base + i, base + i))
    data = {"extensions": {ext_id: {"source": {"install_path": str(active)}}}}

    class _VanishingStatPath(extension_store.Path):
        """Explicit .stat() pretends r1 vanished (FileNotFoundError). is_dir()
        /resolve() bypass Path.stat() on py3.14, so r1 is still admitted to
        `fallbacks`; only the sort's explicit stat() raises."""

        def stat(self, *args, **kwargs):  # type: ignore[override]
            if self.name == "r1":
                raise FileNotFoundError(2, "No such file or directory", str(self))
            return super().stat(*args, **kwargs)

    # Make _install_root() yield our subclass so paths built inside the
    # function (root / id / "versions", iterdir() children, and the `active`
    # Path at line 565) are all _VanishingStatPath instances.
    original_install_root = extension_store._install_root
    extension_store._install_root = lambda: _VanishingStatPath(str(original_install_root()))
    original_path_attr = extension_store.Path
    extension_store.Path = _VanishingStatPath
    try:
        # Before the fix this raised FileNotFoundError and 500'd /api/extensions.
        extension_store._prune_extension_versions(data)
    finally:
        extension_store._install_root = original_install_root
        extension_store.Path = original_path_attr

    remaining = {p.name for p in versions_dir.iterdir()}
    # The contract from the docstring is "fail open per-dir so one broken
    # entry never blocks reconcile": no exception is raised and the active
    # install_path always survives. The flaky entry (r1) sorts oldest (mtime
    # floored to 0) and is pruned alongside the oldest real fallback; the
    # newest real fallbacks must remain.
    if "active-sha" not in remaining:
        raise AssertionError(f"active version was pruned: {sorted(remaining)}")
    if "r5" not in remaining or "r4" not in remaining:
        raise AssertionError(f"newest fallbacks dropped unexpectedly: {sorted(remaining)}")


def test_install_from_signed_marketplace_artifact() -> None:
    work = _private_monorepo_test_work()
    old_public_key = os.environ.get("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY")
    old_insecure = os.environ.get("BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS")
    try:
        repo, _ = _make_repo(work, extension_id="ofek.signed")
        package = repo / "extensions" / "requirements"
        artifact = work / "ofek.signed.tar.gz"
        with tarfile.open(artifact, "w:gz") as archive:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(package).as_posix(), recursive=False)
        artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
        key = Ed25519PrivateKey.generate()
        public_key = key.public_key().public_bytes_raw().hex()
        signature = base64.b64encode(
            key.sign(
                json.dumps(
                    {
                        "artifact_sha256": artifact_sha256,
                        "extension_id": "ofek.signed",
                        "version": "1.0.0",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        ).decode("ascii")
        os.environ["BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"] = public_key
        os.environ["BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"] = "1"

        record = extension_store.install_from_artifact(
            artifact_url=artifact.as_uri(),
            artifact_sha256=artifact_sha256,
            artifact_signature=signature,
        )

        if record["manifest"]["id"] != "ofek.signed":
            raise AssertionError(record)
        if record["source"]["type"] != "artifact":
            raise AssertionError(record["source"])
    finally:
        if old_public_key is None:
            os.environ.pop("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY", None)
        else:
            os.environ["BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"] = old_public_key
        if old_insecure is None:
            os.environ.pop("BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS", None)
        else:
            os.environ["BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"] = old_insecure
        shutil.rmtree(work, ignore_errors=True)


def test_install_from_marketplace_metadata_installs_artifact() -> None:
    work = _private_monorepo_test_work()
    old_public_key = os.environ.get("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY")
    old_insecure = os.environ.get("BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS")
    try:
        repo, _ = _make_repo(work, extension_id="ofek.marketplace-metadata")
        package = repo / "extensions" / "requirements"
        key = Ed25519PrivateKey.generate()
        public_key = key.public_key().public_bytes_raw().hex()
        metadata = _write_signed_artifact(
            package,
            work / "ofek.marketplace-metadata.tar.gz",
            "ofek.marketplace-metadata",
            "1.0.0",
            key,
        )
        metadata["extension_id"] = "ofek.marketplace-metadata"
        metadata["public_key"] = public_key
        os.environ["BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"] = public_key
        os.environ["BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"] = "1"

        record = extension_store.install_from_marketplace_metadata(metadata=metadata)

        if record["manifest"]["id"] != "ofek.marketplace-metadata":
            raise AssertionError(record)
        if record["source"]["type"] != "marketplace":
            raise AssertionError(record["source"])
        if extension_store.get_extension("ofek.marketplace-metadata") is None:
            raise AssertionError("marketplace metadata install did not persist the extension")
    finally:
        if old_public_key is None:
            os.environ.pop("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY", None)
        else:
            os.environ["BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"] = old_public_key
        if old_insecure is None:
            os.environ.pop("BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS", None)
        else:
            os.environ["BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"] = old_insecure
        shutil.rmtree(work, ignore_errors=True)


def test_update_installed_extensions_updates_marketplace_metadata_record() -> None:
    work = _private_monorepo_test_work()
    old_public_key = os.environ.get("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY")
    old_insecure = os.environ.get("BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS")
    try:
        extension_id = "ofek.auto-market"
        repo, _ = _make_repo(work, extension_id=extension_id)
        package = repo / "extensions" / "requirements"
        key = Ed25519PrivateKey.generate()
        public_key = key.public_key().public_bytes_raw().hex()
        metadata_path = work / "api" / "marketplace" / "extensions" / extension_id / "metadata"
        metadata_path.parent.mkdir(parents=True)

        metadata_v1 = _write_signed_artifact(
            package,
            work / "ofek.auto-market-v1.tar.gz",
            extension_id,
            "1.0.0",
            key,
        )
        metadata_v1["extension_id"] = extension_id
        metadata_v1["public_key"] = public_key
        metadata_path.write_text(json.dumps(metadata_v1), encoding="utf-8")
        os.environ["BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"] = public_key
        os.environ["BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"] = "1"

        record = extension_store.install_from_marketplace_metadata(
            metadata_url=metadata_path.as_uri()
        )
        if record["source"].get("metadata_url") != metadata_path.as_uri():
            raise AssertionError("marketplace metadata_url was not persisted")

        manifest_path = package / "better-agent-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "2.0.0"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        metadata_v2 = _write_signed_artifact(
            package,
            work / "ofek.auto-market-v2.tar.gz",
            extension_id,
            "2.0.0",
            key,
        )
        metadata_v2["extension_id"] = extension_id
        metadata_v2["public_key"] = public_key
        metadata_path.write_text(json.dumps(metadata_v2), encoding="utf-8")

        result = extension_store.update_installed_extensions()

        updated = extension_store.get_extension(extension_id)
        row = next((item for item in result["results"] if item["extension_id"] == extension_id), None)
        if not row or row.get("updated") is not True:
            raise AssertionError(result)
        if updated["manifest"]["version"] != "2.0.0":
            raise AssertionError(updated["manifest"])
        if updated["source"]["artifact_sha256"] != metadata_v2["artifact_sha256"]:
            raise AssertionError(updated["source"])
    finally:
        try:
            extension_store.uninstall("ofek.auto-market")
        except extension_store.ExtensionError:
            pass
        if old_public_key is None:
            os.environ.pop("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY", None)
        else:
            os.environ["BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"] = old_public_key
        if old_insecure is None:
            os.environ.pop("BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS", None)
        else:
            os.environ["BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"] = old_insecure
        shutil.rmtree(work, ignore_errors=True)


def test_update_installed_extensions_updates_git_record_and_preserves_enabled() -> None:
    work = _private_monorepo_test_work()
    try:
        repo, first_commit = _make_repo(work, extension_id="ofek.auto-git")
        record = extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/requirements",
        )
        if record["source"]["commit_sha"] != first_commit:
            raise AssertionError(record["source"])
        extension_store.set_enabled("ofek.auto-git", False)

        manifest_path = repo / "extensions" / "requirements" / "better-agent-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "2.0.0"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        _git(repo, "add", "extensions/requirements/better-agent-extension.json")
        _git(repo, "commit", "-m", "update extension")
        second_commit = _git(repo, "rev-parse", "HEAD")

        result = extension_store.update_installed_extensions()

        updated = extension_store.get_extension("ofek.auto-git")
        row = next((item for item in result["results"] if item["extension_id"] == "ofek.auto-git"), None)
        if not row or row.get("updated") is not True:
            raise AssertionError(result)
        if updated["manifest"]["version"] != "2.0.0":
            raise AssertionError(updated["manifest"])
        if updated["source"]["commit_sha"] != second_commit:
            raise AssertionError(updated["source"])
        if updated["enabled"] is not False:
            raise AssertionError("disabled state was not preserved")
    finally:
        try:
            extension_store.uninstall("ofek.auto-git")
        except extension_store.ExtensionError:
            pass
        shutil.rmtree(work, ignore_errors=True)


def _write_signed_artifact(package: Path, artifact: Path, extension_id: str, version: str, key: Ed25519PrivateKey) -> dict[str, str]:
    with tarfile.open(artifact, "w:gz") as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(package).as_posix(), recursive=False)
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    signature = base64.b64encode(
        key.sign(
            json.dumps(
                {
                    "artifact_sha256": artifact_sha256,
                    "extension_id": extension_id,
                    "version": version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    ).decode("ascii")
    return {
        "artifact_url": artifact.as_uri(),
        "artifact_sha256": artifact_sha256,
        "signature": signature,
        "version": version,
    }


def _marketplace_artifact_fixture(work: Path) -> tuple[Path, dict[str, str], str]:
    package = work / "marketplace-package"
    (package / "backend").mkdir(parents=True)
    (package / "ui").mkdir()
    manifest = {
        "kind": "better-agent-extension",
        "id": extension_store.MARKETPLACE_EXTENSION_ID,
        "name": "Marketplace",
        "version": "1.2.3",
        "description": "Required marketplace",
        "surfaces": ["backend_feature", "frontend_feature"],
        "entrypoints": {
            "backend": "backend/routes.py",
            "frontend": "ui/index.html",
            "mcp": [],
            "instructions": [],
        },
        "permissions": {"backend_routes": True},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "backend" / "routes.py").write_text(
        "from fastapi import APIRouter\n\n"
        "def create_router(context):\n"
        "    return APIRouter()\n",
        encoding="utf-8",
    )
    (package / "ui" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    key = Ed25519PrivateKey.generate()
    metadata = _write_signed_artifact(
        package,
        work / "marketplace.tar.gz",
        extension_store.MARKETPLACE_EXTENSION_ID,
        "1.2.3",
        key,
    )
    public_key = key.public_key().public_bytes_raw().hex()
    return package, metadata, public_key


def _with_marketplace_bootstrap_env(work: Path, metadata: dict[str, str], public_key: str | None):
    old = {
        "BETTER_AGENT_HOME": os.environ.get("BETTER_AGENT_HOME"),
        "BETTER_CLAUDE_HOME": os.environ.get("BETTER_CLAUDE_HOME"),
        "BETTER_AGENT_MARKETPLACE_BASE_URL": os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL"),
        "BETTER_AGENT_MARKETPLACE_PUBLIC_KEY": os.environ.get("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"),
        "BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS": os.environ.get("BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"),
        "BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH": os.environ.get("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"),
        "BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE": os.environ.get("BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE"),
    }
    metadata_file = work / "api" / "marketplace" / "extensions" / extension_store.MARKETPLACE_EXTENSION_ID / "metadata"
    metadata_file.parent.mkdir(parents=True)
    metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
    test_home = str(work / "home")
    os.environ["BETTER_AGENT_HOME"] = test_home
    os.environ["BETTER_CLAUDE_HOME"] = test_home
    os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = (work / "api" / "marketplace").as_uri()
    os.environ["BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"] = "1"
    os.environ["BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE"] = "1"
    os.environ.pop("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH", None)
    if public_key is None:
        os.environ.pop("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY", None)
    else:
        os.environ["BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"] = public_key
    return old


def _restore_env(old: dict[str, str | None]) -> None:
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_required_marketplace_bootstraps_from_signed_artifact() -> None:
    work = _private_monorepo_test_work()
    try:
        _, metadata, public_key = _marketplace_artifact_fixture(work)
        old = _with_marketplace_bootstrap_env(work, metadata, public_key)
        try:
            data = extension_store._load_with_changes()[0]
        finally:
            _restore_env(old)
        record = data["extensions"][extension_store.MARKETPLACE_EXTENSION_ID]
        if record["source"]["type"] != "better_agent_signed":
            raise AssertionError(record["source"])
        if record["manifest"]["entrypoints"]["frontend"] != "ui/index.html":
            raise AssertionError(record["manifest"])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_required_marketplace_unreachable_metadata_falls_back_to_visible_placeholder_error() -> None:
    work = _private_monorepo_test_work()
    try:
        _, metadata, public_key = _marketplace_artifact_fixture(work)
        old = _with_marketplace_bootstrap_env(work, metadata, public_key)
        os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = (work / "missing" / "api" / "marketplace").as_uri()
        try:
            data = extension_store._load_with_changes()[0]
        finally:
            _restore_env(old)
        record = data["extensions"][extension_store.MARKETPLACE_EXTENSION_ID]
        if record["source"]["type"] != "private_placeholder":
            raise AssertionError(record["source"])
        if record["manifest"]["entrypoints"]["frontend"]:
            raise AssertionError(record["manifest"])
        if "No such file" not in record["source"].get("error", ""):
            raise AssertionError(record["source"])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_marketplace_downloads_use_better_agent_user_agent() -> None:
    seen: list[tuple[str, str]] = []
    real_urlopen = extension_store.urllib.request.urlopen

    class FakeResponse:
        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, *args):
            return self.body

    def fake_urlopen(req, timeout):
        seen.append((req.get_header("Accept"), req.get_header("User-agent")))
        if req.get_header("Accept") == "application/json":
            return FakeResponse(b'{"ok":true}')
        return FakeResponse(b"artifact")

    old_insecure = os.environ.get("BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS")
    os.environ["BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"] = "1"
    extension_store.urllib.request.urlopen = fake_urlopen
    try:
        payload = extension_store._fetch_json("http://example.test/metadata")
        artifact = extension_store._download_artifact("http://example.test/artifact")
    finally:
        extension_store.urllib.request.urlopen = real_urlopen
        if old_insecure is None:
            os.environ.pop("BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS", None)
        else:
            os.environ["BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"] = old_insecure
    if payload != {"ok": True}:
        raise AssertionError(payload)
    if artifact != b"artifact":
        raise AssertionError(artifact)
    if seen != [
        ("application/json", "BetterAgentMarketplace/1.0"),
        ("application/gzip", "BetterAgentMarketplace/1.0"),
    ]:
        raise AssertionError(seen)


def test_reinstall_and_state_changes_evict_persistent_backend() -> None:
    work = _private_monorepo_test_work()
    evicted: list[str] = []
    original_evict = extension_backend_loader.evict_persistent_backend
    extension_backend_loader.evict_persistent_backend = evicted.append
    try:
        repo, _commit = _make_repo(work, extension_id="ofek.evict")
        extension_store.install_from_repo(repo_url=repo.as_uri(), extension_path="extensions/requirements")
        if evicted:
            raise AssertionError(evicted)
        extension_store.install_from_repo(repo_url=repo.as_uri(), extension_path="extensions/requirements")
        extension_store.set_enabled("ofek.evict", False)
        extension_store.set_enabled("ofek.evict", True)
        extension_store.uninstall("ofek.evict")
    finally:
        extension_backend_loader.evict_persistent_backend = original_evict
        shutil.rmtree(work, ignore_errors=True)
    if evicted != ["ofek.evict", "ofek.evict", "ofek.evict", "ofek.evict"]:
        raise AssertionError(evicted)


def test_rejects_path_escape() -> None:
    try:
        extension_store.install_from_repo(
            repo_url="https://example.test/private.git",
            extension_path="../escape",
        )
    except extension_store.ExtensionError as exc:
        if "relative path" not in str(exc):
            raise
    else:
        raise AssertionError("path escape was accepted")


def test_rejects_embedded_repo_credentials() -> None:
    try:
        extension_store.install_from_repo(
            repo_url="https://token@example.test/private.git",
            extension_path="extensions/requirements",
        )
    except extension_store.ExtensionError as exc:
        if "must not embed credentials" not in str(exc):
            raise
    else:
        raise AssertionError("credential-bearing repo URL was accepted")


def test_file_repo_is_allowed_only_under_private_repo() -> None:
    work = _private_monorepo_test_work()
    old_test = os.environ.pop("BETTER_CLAUDE_TEST_ALLOW_FILE_EXTENSION_REPO", None)
    try:
        repo, _commit = _make_repo(work)
        record = extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/requirements",
        )
        if record["manifest"]["id"] != "ofek.requirements":
            raise AssertionError(record)
    finally:
        if old_test is not None:
            os.environ["BETTER_CLAUDE_TEST_ALLOW_FILE_EXTENSION_REPO"] = old_test
        else:
            os.environ.pop("BETTER_CLAUDE_TEST_ALLOW_FILE_EXTENSION_REPO", None)
        shutil.rmtree(work, ignore_errors=True)


def test_file_repo_rejects_paths_outside_private_extensions() -> None:
    work = Path(tempfile.mkdtemp(prefix="bc-test-extension-repo-"))
    old_test = os.environ.pop("BETTER_CLAUDE_TEST_ALLOW_FILE_EXTENSION_REPO", None)
    try:
        repo, _commit = _make_repo(work)
        try:
            extension_store.install_from_repo(
                repo_url=repo.as_uri(),
                extension_path="extensions/requirements",
            )
        except extension_store.ExtensionError as exc:
            if "trusted extension file root" not in str(exc):
                raise
        else:
            raise AssertionError("outside file repo was accepted with dev flag")
    finally:
        if old_test is not None:
            os.environ["BETTER_CLAUDE_TEST_ALLOW_FILE_EXTENSION_REPO"] = old_test
        else:
            os.environ.pop("BETTER_CLAUDE_TEST_ALLOW_FILE_EXTENSION_REPO", None)
        shutil.rmtree(work, ignore_errors=True)


def test_subscription_extensions_fail_closed_without_entitlement_url() -> None:
    raw = {
        "kind": "better-agent-extension",
        "id": "ofek-dev.paid",
        "name": "Paid",
        "version": "1.0.0",
        "marketplace": {
            "product_id": "prod_paid",
            "subscription_required": True,
        },
    }
    manifest = _validate_manifest(raw)
    try:
        extension_store._verify_entitlement(manifest, "token")  # type: ignore[attr-defined]
    except extension_store.ExtensionError as exc:
        if "entitlement_url" not in str(exc):
            raise
    else:
        raise AssertionError("subscription extension without entitlement_url was accepted")


def test_subscription_extensions_use_configured_marketplace_entitlement_url() -> None:
    raw = {
        "kind": "better-agent-extension",
        "id": "ofek-dev.paid-central",
        "name": "Paid Central",
        "version": "1.0.0",
        "marketplace": {
            "product_id": "prod_paid",
            "subscription_required": True,
        },
    }
    manifest = _validate_manifest(raw)
    old_urlopen = extension_store.urllib.request.urlopen  # type: ignore[attr-defined]
    old_url = os.environ.get("BETTER_AGENT_MARKETPLACE_ENTITLEMENT_URL")
    calls = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"active": true, "expires_at": "2999-01-01T00:00:00+00:00"}'

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return _Response()

    os.environ["BETTER_AGENT_MARKETPLACE_ENTITLEMENT_URL"] = "https://marketplace.test/api/entitlements/verify"
    extension_store.urllib.request.urlopen = fake_urlopen  # type: ignore[attr-defined]
    try:
        entitlement = extension_store._verify_entitlement(manifest, "token")  # type: ignore[attr-defined]
    finally:
        extension_store.urllib.request.urlopen = old_urlopen  # type: ignore[attr-defined]
        if old_url is None:
            os.environ.pop("BETTER_AGENT_MARKETPLACE_ENTITLEMENT_URL", None)
        else:
            os.environ["BETTER_AGENT_MARKETPLACE_ENTITLEMENT_URL"] = old_url

    if entitlement["status"] != "active":
        raise AssertionError(entitlement)
    request, timeout = calls[0]
    if request.full_url != "https://marketplace.test/api/entitlements/verify":
        raise AssertionError(request.full_url)
    if request.get_header("Authorization") != "Bearer token":
        raise AssertionError("missing bearer token")
    if timeout != 10:
        raise AssertionError(timeout)


def test_expired_entitlement_is_not_active() -> None:
    if extension_store._entitlement_active(  # type: ignore[attr-defined]
        {"status": "active", "expires_at": "2000-01-01T00:00:00+00:00"}
    ):
        raise AssertionError("expired entitlement was accepted")


def test_builtin_feature_gate_rejects_inactive_entitlement() -> None:
    extension_id = "fixture.requirements"
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_id,
            "name": "Requirements",
            "version": "1.0.0",
            "description": "",
            "surfaces": ["backend_feature", "runtime_mcp"],
            "entrypoints": {
                "backend": "",
                "frontend": "",
                "mcp": [],
                "instructions": [],
                "frontend_modules": [],
            },
            "permissions": {},
            "marketplace": {
                "product_id": "requirements.pro",
                "subscription_required": True,
                "entitlement_url": "https://marketplace.test/entitlements",
            },
        },
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/requirements",
            "ref": "",
            "commit_sha": "abc",
            "install_path": str(Path(_TMP_HOME) / "installed-requirements"),
        },
        "entitlement": {
            "status": "active",
            "product_id": "requirements.pro",
            "token_present": True,
            "last_checked_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2000-01-01T00:00:00+00:00",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]
    if extension_store.is_builtin_feature_enabled(extension_id):
        raise AssertionError("expired builtin extension entitlement enabled feature gate")


def test_installed_extension_instructions_are_managed_blocks() -> None:
    work = _private_monorepo_test_work()
    import config_store
    original_state = config_store._load_state()
    try:
        # Redirect the claude provider config dir into the tempdir so the managed
        # block lands there, never in the real ~/.claude/CLAUDE.md.
        claude_home = work / "claude-home"
        claude_home.mkdir()
        state = config_store._load_state()
        state["providers"] = [
            {"id": "test-claude", "kind": "claude", "name": "Claude", "config_dir": str(claude_home)}
        ]
        state["default_provider_id"] = "test-claude"
        config_store._save_state(state)

        repo, _commit = _make_instructions_repo(work)
        extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/instructions",
        )
        ext_id = "ofek.instructions"
        instructions_file = claude_home / "CLAUDE.md"

        def has_block() -> bool:
            return instructions_file.is_file() and "Requirement analysis capability" in instructions_file.read_text(encoding="utf-8")

        if has_block():
            raise AssertionError("instruction block was ambiently exposed without user opt-in")
        runtime_content = "\n".join(
            str(item.get("content") or "") for item in extension_store.user_instruction_contexts()
        )
        if "Requirement analysis capability" not in runtime_content:
            raise AssertionError("Better Agent runtime lost extension instructions")

        extension_store.set_native_harness_exposed(ext_id, "instructions", "rules", True)
        if not has_block():
            raise AssertionError("instruction block not injected after native exposure")

        extension_store.set_instruction_enabled(ext_id, level="global", enabled=False)
        if has_block():
            raise AssertionError("block not removed on global disable")

        extension_store.set_instruction_enabled(ext_id, level="global", enabled=True)
        if not has_block():
            raise AssertionError("block not restored on global enable")

        extension_store.uninstall(ext_id)
        if has_block():
            raise AssertionError("block not removed on uninstall")
    finally:
        config_store._save_state(original_state)
        shutil.rmtree(work, ignore_errors=True)


def test_builtin_harness_instructions_are_visible_extension() -> None:
    # Seed bundled public extensions from the repo. _load()/get_extension() are
    # pure reads; list_extensions_with_reconciliation is the explicit seed path.
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    record = extension_store.get_extension(
        extension_store.BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID
    )
    if not record:
        raise AssertionError("harness instructions extension was not installed")
    manifest = record["manifest"]
    if "instructions" not in manifest["surfaces"]:
        raise AssertionError("harness instructions extension does not expose instructions")
    item = manifest["entrypoints"]["instructions"][0]
    install_path = Path(record["source"]["install_path"])
    content = (install_path / item["path"]).read_text(encoding="utf-8")
    if "after every wait_agent result" not in content:
        raise AssertionError("subagent wait instruction is not owned by harness extension")
    if "Better Agent groups action/tool blocks" not in content:
        raise AssertionError("action grouping instruction is not owned by harness extension")


def test_disabled_extension_has_no_blocks_anywhere() -> None:
    import project_store
    import config_store

    work = _private_monorepo_test_work()
    original_state = config_store._load_state()
    try:
        claude_home = work / "claude-home"
        claude_home.mkdir()
        state = config_store._load_state()
        state["providers"] = [
            {"id": "test-claude", "kind": "claude", "name": "Claude", "config_dir": str(claude_home)}
        ]
        state["default_provider_id"] = "test-claude"
        config_store._save_state(state)

        project_path = work / "proj"
        project_path.mkdir()
        project_store.add_project(str(project_path), "Proj")

        repo, _commit = _make_instructions_repo(work)
        extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/instructions",
        )
        ext_id = "ofek.instructions"
        extension_store.set_native_harness_exposed(ext_id, "instructions", "rules", True)
        extension_store.set_native_harness_exposed(ext_id, "instructions", "projrules", True)
        extension_store.set_instruction_enabled(ext_id, level="project", enabled=True, project_path=str(project_path))

        global_file = claude_home / "CLAUDE.md"
        project_file = project_path / "CLAUDE.md"
        if "Requirement analysis capability" not in global_file.read_text(encoding="utf-8"):
            raise AssertionError("global block missing after install")
        if "Project-scoped rules" not in project_file.read_text(encoding="utf-8"):
            raise AssertionError("project block missing after project enable")

        # Disable the whole extension -> blocks must vanish from BOTH files.
        extension_store.set_enabled(ext_id, False)
        global_text = global_file.read_text(encoding="utf-8")
        project_text = project_file.read_text(encoding="utf-8")
        if "better-agent:extension:" in global_text or "better-claude:extension:" in global_text:
            raise AssertionError("global block survived disable")
        if "better-agent:extension:" in project_text or "better-claude:extension:" in project_text:
            raise AssertionError("project block survived disable")

        # Re-enable -> global block returns; project block returns only after re-enabling project level.
        extension_store.set_enabled(ext_id, True)
        if "Requirement analysis capability" not in global_file.read_text(encoding="utf-8"):
            raise AssertionError("global block not restored on enable")
    finally:
        try:
            extension_store.uninstall(ext_id)
        except extension_store.ExtensionError:
            pass
        config_store._save_state(original_state)
        shutil.rmtree(work, ignore_errors=True)


def test_reconcile_all_sweeps_orphans_and_clears_disabled() -> None:
    import project_store
    import config_store
    import extension_instructions as ei

    work = _private_monorepo_test_work()
    original_state = config_store._load_state()
    try:
        claude_home = work / "claude-home"
        claude_home.mkdir()
        state = config_store._load_state()
        state["providers"] = [
            {"id": "test-claude", "kind": "claude", "name": "Claude", "config_dir": str(claude_home)}
        ]
        state["default_provider_id"] = "test-claude"
        config_store._save_state(state)
        project_store.add_project(str(work / "proj"), "Proj")

        global_file = claude_home / "CLAUDE.md"
        # Orphan block for an extension that was never installed.
        ei._pcs.apply_managed_instruction_blocks(
            owner="extension:ghost.uninstalled",
            sections=[("rules", "haunted instructions")],
            scope="global",
            project_root=None,
            providers=config_store.list_provider_metadata(),
        )
        if "haunted instructions" not in global_file.read_text(encoding="utf-8"):
            raise AssertionError("orphan block not written")

        # The orphan owner is not installed -> reconcile_all must purge it.
        # (Other installed extensions may legitimately have blocks, so assert
        # the ghost's content is gone, not that the file has no markers.)
        swept = extension_store.reconcile_all_instructions()
        if swept < 1:
            raise AssertionError("orphan block not swept")
        if "haunted instructions" in global_file.read_text(encoding="utf-8"):
            raise AssertionError("orphan block survived reconcile_all")
    finally:
        config_store._save_state(original_state)
        shutil.rmtree(work, ignore_errors=True)


def test_legacy_provider_capabilities_field_is_aliased() -> None:
    """Extensions authored with the old provider_capabilities field/surface still work."""
    import config_store

    work = _private_monorepo_test_work()
    original_state = config_store._load_state()
    try:
        claude_home = work / "claude-home"
        claude_home.mkdir()
        state = config_store._load_state()
        state["providers"] = [
            {"id": "test-claude", "kind": "claude", "name": "Claude", "config_dir": str(claude_home)}
        ]
        state["default_provider_id"] = "test-claude"
        config_store._save_state(state)

        repo = work / "legacy-repo"
        package = repo / "extensions" / "legacy"
        package.mkdir(parents=True)
        manifest = {
            "kind": "better-agent-extension",
            "id": "ofek.legacy",
            "name": "Legacy",
            "version": "1.0.0",
            "description": "Legacy field name",
            "surfaces": ["provider_capabilities"],
            "entrypoints": {
                "provider_capabilities": [{"name": "rules", "path": "capabilities/rules.md"}],
            },
            "protocol": {
                "version": 1,
                "smoke_test": {
                    "required_paths": ["better-agent-extension.json", "capabilities/rules.md"],
                    "python_modules": [],
                },
            },
            "permissions": {},
            "marketplace": {},
        }
        (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        (package / "capabilities").mkdir()
        (package / "capabilities" / "rules.md").write_text("Legacy global rules\n", encoding="utf-8")
        _git(repo, "init")
        _git(repo, "config", "user.email", "t@example.test")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "add", "extensions")
        _git(repo, "commit", "-m", "legacy extension")

        record = extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/legacy",
        )
        extension_store.set_native_harness_exposed("ofek.legacy", "instructions", "rules", True)
        instructions = record["manifest"]["entrypoints"]["instructions"]
        if not any(i.get("name") == "rules" and i.get("level") == "global" for i in instructions):
            raise AssertionError(f"legacy field not normalized to instructions: {instructions}")
        if record["manifest"]["surfaces"] != ["instructions"]:
            raise AssertionError(f"legacy surface not aliased: {record['manifest']['surfaces']}")
        if "Legacy global rules" not in (claude_home / "CLAUDE.md").read_text(encoding="utf-8"):
            raise AssertionError("legacy instruction content not applied as a managed block")
        extension_store.uninstall("ofek.legacy")
    finally:
        config_store._save_state(original_state)
        shutil.rmtree(work, ignore_errors=True)


def test_manifest_accepts_skill_entrypoints_and_requires_skill_md() -> None:
    package = _write_private_extension_package(
        "ofek.skillful",
        "extensions/skillful",
        {
            "surfaces": ["skills"],
            "entrypoints": {"skills": [{"name": "get-requirements", "path": "skills/get-requirements"}]},
        },
        {
            "skills/get-requirements/SKILL.md": (
                "---\n"
                "name: get-requirements\n"
                "description: Requirements search.\n"
                "---\n"
                "Use the requirements MCP.\n"
            )
        },
    )
    manifest = _validate_manifest(
        json.loads((package / "better-agent-extension.json").read_text(encoding="utf-8"))
    )
    extension_store._validate_declared_files(manifest, package)
    if manifest["entrypoints"]["skills"][0]["path"] != "skills/get-requirements":
        raise AssertionError("skill entrypoint path not preserved")

    broken = _write_private_extension_package(
        "ofek.broken-skill",
        "extensions/broken-skill",
        {
            "surfaces": ["skills"],
            "entrypoints": {"skills": [{"name": "missing", "path": "skills/missing"}]},
        },
    )
    broken_manifest = _validate_manifest(
        json.loads((broken / "better-agent-extension.json").read_text(encoding="utf-8"))
    )
    try:
        extension_store._validate_declared_files(broken_manifest, broken)
    except extension_store.ExtensionError:
        pass
    else:
        raise AssertionError("missing skill SKILL.md was accepted")


def test_extension_enable_disable_installs_runtime_skills() -> None:
    work = _private_monorepo_test_work()
    home = Path(tempfile.mkdtemp(prefix="bc-test-extension-skills-home-"))
    original_home = os.environ.get("HOME")
    repo = work / "skill-repo"
    package = repo / "extensions" / "skillful"
    package.mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.skill-runtime",
        "name": "Skill runtime",
        "version": "1.0.0",
        "description": "Runtime skill extension",
        "surfaces": ["skills"],
        "entrypoints": {
            "skills": [{"name": "get-requirements", "path": "skills/get-requirements"}],
        },
        "protocol": {
            "version": 1,
            "smoke_test": {
                "required_paths": ["better-agent-extension.json", "skills/get-requirements/SKILL.md"],
                "python_modules": [],
            },
        },
        "permissions": {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "skills" / "get-requirements").mkdir(parents=True)
    (package / "skills" / "get-requirements" / "SKILL.md").write_text(
        "---\nname: get-requirements\ndescription: Requirements.\n---\nUse MCP.\n",
        encoding="utf-8",
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "extensions")
    _git(repo, "commit", "-m", "skill extension")
    try:
        os.environ["HOME"] = str(home)
        target = home / ".agents" / "skills" / "get-requirements"
        record = extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/skillful",
        )
        extension_store.set_native_harness_exposed(
            record["manifest"]["id"], "skill", "get-requirements", True
        )
        installed = target / "SKILL.md"
        if "Requirements." not in installed.read_text(encoding="utf-8"):
            raise AssertionError("extension skill did not replace the runtime skill copy")
        marker = target / extension_store._RUNTIME_SKILL_OWNER_FILE
        if marker.read_text(encoding="utf-8").strip() != record["manifest"]["id"]:
            raise AssertionError("runtime skill owner marker not written")
        extension_store.set_enabled(record["manifest"]["id"], False)
        if target.exists():
            raise AssertionError("disabled extension did not remove runtime skill copy")
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home
        try:
            extension_store.uninstall("ofek.skill-runtime")
        except extension_store.ExtensionError:
            pass
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def test_optional_permissions_allow_forbid() -> None:
    """Optional perms are fail-closed off until granted; required perms always on."""
    from pathlib import Path

    work = _private_monorepo_test_work()
    try:
        package = work / "perm-ext"
        package.mkdir(parents=True)
        manifest = {
            "kind": "better-agent-extension",
            "id": "ofek.perm",
            "name": "Perm",
            "version": "1.0.0",
            "description": "optional perms",
            "surfaces": [],
            "entrypoints": {},
            "protocol": {
                "version": 1,
                "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
            },
            "permissions": {"session_state": True, "filesystem": "optional", "network": "optional"},
            "marketplace": {},
        }
        (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        record = extension_store._install_from_package_dir(
            package_dir=package,
            source={"type": "local", "repo_url": "", "extension_path": "", "ref": "", "commit_sha": "local"},
            force_enabled=False,
            persist=True,
        )
        eid = "ofek.perm"

        # Required perm always active; optional perms fail-closed (inactive) before grant.
        if not extension_store.has_permission(record, "session_state"):
            raise AssertionError("required permission not active")
        if extension_store.has_permission(record, "filesystem"):
            raise AssertionError("optional permission active before grant (not fail-closed)")
        if sorted(extension_store.optional_permissions(record)) != ["filesystem", "network"]:
            raise AssertionError(extension_store.optional_permissions(record))

        # Grant filesystem -> active + in effective set.
        extension_store.set_permission_grant(eid, "filesystem", True)
        granted = extension_store.get_extension(eid)
        if not extension_store.has_permission(granted, "filesystem"):
            raise AssertionError("optional permission not active after grant")
        if "filesystem" not in extension_store.effective_permissions(granted):
            raise AssertionError("granted permission missing from effective set")

        # Revoke -> inactive again.
        extension_store.set_permission_grant(eid, "filesystem", False)
        revoked = extension_store.get_extension(eid)
        if extension_store.has_permission(revoked, "filesystem"):
            raise AssertionError("optional permission still active after revoke")

        # Granting a REQUIRED permission must be rejected.
        try:
            extension_store.set_permission_grant(eid, "session_state", True)
        except extension_store.ExtensionError:
            pass
        else:
            raise AssertionError("granting a required permission should be rejected")

        # config exposes the permissions block for the panel.
        cfg = extension_store.extension_config(eid)
        if cfg.get("permissions", {}).get("optional") != ["filesystem", "network"]:
            raise AssertionError("config permissions block missing optional list")

        extension_store.uninstall(eid)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_command_based_mcp_server() -> None:
    """MCP items may declare a command (stdio binary) instead of a python file."""
    manifest = _validate_manifest(
        {
            "kind": "better-agent-extension",
            "id": "ofek.cmdmcp",
            "name": "Cmd",
            "version": "1.0.0",
            "entrypoints": {"mcp": [{"name": "ccc", "command": "ccc", "args": ["mcp"]}]},
            "permissions": {},
            "marketplace": {},
        }
    )
    item = manifest["entrypoints"]["mcp"][0]
    if item.get("command") != "ccc" or item.get("python") != "" or item.get("module") != "" or item.get("args") != ["mcp"]:
        raise AssertionError(item)

    module_manifest = _validate_manifest(
        {
            "kind": "better-agent-extension",
            "id": "ofek.modulemcp",
            "name": "Module",
            "version": "1.0.0",
            "entrypoints": {"mcp": [{"name": "mod-mcp", "module": "compiled_mcp.server"}]},
            "permissions": {},
            "marketplace": {},
        }
    )
    module_item = module_manifest["entrypoints"]["mcp"][0]
    if module_item.get("module") != "compiled_mcp.server" or module_item.get("python") != "" or module_item.get("command") != "":
        raise AssertionError(module_item)

    # No entrypoint -> rejected; more than one entrypoint kind -> rejected.
    for bad in (
        [{"name": "x", "python": "", "command": ""}],
        [{"name": "x", "python": "m.py", "command": "c"}],
        [{"name": "x", "python": "m.py", "module": "pkg.m"}],
        [{"name": "x", "module": "bad/module"}],
    ):
        try:
            _validate_manifest(
                {
                    "kind": "better-agent-extension",
                    "id": "ofek.badcmd",
                    "name": "Bad",
                    "version": "1.0.0",
                    "entrypoints": {"mcp": bad},
                    "permissions": {},
                    "marketplace": {},
                }
            )
        except extension_store.ExtensionError:
            continue
        raise AssertionError(f"should reject mcp item {bad}")


def test_module_based_mcp_server_config() -> None:
    package = _write_private_extension_package(
        "ofek.module-mcp",
        "extensions/module-mcp",
        {
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "module-mcp",
                        "module": "compiled_mcp.server",
                        "args": ["serve"],
                    }
                ],
            },
            "permissions": {},
        },
        {
            "compiled_mcp/__init__.py": "",
            "compiled_mcp/server.py": "",
        },
    )
    record = extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "artifact",
            "repo_url": "https://example.test/module-mcp.tar.gz",
            "extension_path": "",
            "ref": "",
            "commit_sha": "module-mcp",
            "artifact_sha256": "0" * 64,
            "artifact_url": "https://example.test/module-mcp.tar.gz",
        },
        persist=True,
    )
    try:
        configs = extension_store.runtime_mcp_server_configs(
            {
                "app_session_id": "s1",
                "backend_url": "http://127.0.0.1:8000",
                "internal_token": "token",
                "cwd": str(package),
                "model": "m",
            },
            interacts_with_user=True,
            bare=False,
        )
        config = configs.get("module-mcp")
        if not config:
            raise AssertionError(configs)
        if config["command"] != sys.executable:
            raise AssertionError(config)
        if config["args"] != ["-m", "compiled_mcp.server", "serve"]:
            raise AssertionError(config)
        pythonpath = str(config["env"].get("PYTHONPATH") or "")
        if str(Path(record["source"]["install_path"]).resolve()) not in pythonpath.split(os.pathsep):
            raise AssertionError(config["env"])
    finally:
        try:
            extension_store.uninstall("ofek.module-mcp")
        except Exception:
            pass


def test_installed_extension_exports_runtime_mcp_server_config() -> None:
    work = _private_monorepo_test_work("bc-test-runtime-extension-repo-")
    try:
        repo, _commit = _make_runtime_repo(work)
        record = extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/scheduler",
        )
        _configure_internal_llm_defaults("default_session")
        configs = extension_store.runtime_mcp_server_configs(
            {
                "app_session_id": "s1",
                "backend_url": "http://127.0.0.1:8000",
                "internal_token": "token",
                "cwd": str(work),
                "model": "m",
            },
            interacts_with_user=True,
            bare=False,
        )
        config = configs.get("ofek-scheduler")
        if not config:
            raise AssertionError(configs)
        if config["command"] != sys.executable:
            raise AssertionError(config)
        if Path(config["args"][0]).resolve() != (Path(record["source"]["install_path"]) / "mcp" / "server.py").resolve():
            raise AssertionError(config)
        if config["env"]["BETTER_CLAUDE_EXTENSION_ID"] != "ofek.scheduler":
            raise AssertionError(config)
        # internal_loopback grants a PER-EXTENSION token (minted via
        # extension_token_registry), never the global input token passthrough —
        # identity is derived from this secret, not self-asserted. Both env
        # aliases must carry it.
        token_claude = config["env"].get("BETTER_CLAUDE_INTERNAL_TOKEN")
        token_agent = config["env"].get("BETTER_AGENT_INTERNAL_TOKEN")
        if not token_claude or token_claude != token_agent:
            raise AssertionError(f"internal_loopback runtime MCP should receive a per-extension internal token: {config['env']}")
        if token_claude == "token":
            raise AssertionError("internal_loopback MCP must not receive the raw global input token (per-extension minting required)")
        if config["env"]["OF_EXTENSION_TEST"] != "1":
            raise AssertionError(config)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_runtime_mcp_without_internal_loopback_does_not_receive_token() -> None:
    package = Path(tempfile.mkdtemp(prefix="bc-test-no-loopback-mcp-")) / "no-loopback"
    try:
        (package / "mcp").mkdir(parents=True)
        manifest = {
        "kind": "better-agent-extension",
        "id": "ofek.no-loopback",
        "name": "No Loopback",
            "version": "1.0.0",
            "description": "Runtime MCP without internal loopback.",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "no-loopback",
                        "python": "mcp/server.py",
                        "interacts_with_user": True,
                        "bare_allowed": False,
                        "requires_backend_auth": False,
                    }
                ],
            },
            "protocol": {
                "version": 1,
            "smoke_test": {
                "required_paths": ["better-agent-extension.json", "mcp/server.py"],
                "python_modules": ["mcp.server"],
            },
        },
            "permissions": {},
            "marketplace": {},
        }
        (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        (package / "mcp" / "server.py").write_text("print('mcp server')\n", encoding="utf-8")
        record = extension_store._install_from_package_dir(
            package_dir=package,
            source={
                "type": "test",
                "repo_url": "",
                "extension_path": "no-loopback",
                "ref": "",
                "commit_sha": "no-loopback",
            },
            persist=True,
        )
        configs = extension_store.runtime_mcp_server_configs(
            {
                "app_session_id": "s1",
                "backend_url": "http://127.0.0.1:8000",
                "internal_token": "token",
                "cwd": str(package),
                "model": "m",
            },
            interacts_with_user=True,
            bare=False,
        )
        config = configs.get("no-loopback")
        if not config:
            raise AssertionError(configs)
        env = config["env"]
        if "BETTER_CLAUDE_INTERNAL_TOKEN" in env or "BETTER_AGENT_INTERNAL_TOKEN" in env:
            raise AssertionError(f"non-internal extension received token env: {env}")
        if env["BETTER_CLAUDE_EXTENSION_ID"] != record["manifest"]["id"]:
            raise AssertionError(env)
    finally:
        shutil.rmtree(package.parent, ignore_errors=True)


def test_legacy_string_mcp_entrypoints_do_not_crash_runtime_config() -> None:
    install_root = Path(tempfile.mkdtemp(prefix="bc-test-legacy-string-mcp-"))
    try:
        data = extension_store._load()  # type: ignore[attr-defined]
        data["extensions"]["ofek.legacy-string-mcp"] = {
            "manifest": {
                "kind": extension_store.MANIFEST_KIND,
                "id": "ofek.legacy-string-mcp",
                "name": "Legacy string MCP",
                "version": "1.0.0",
                "description": "Legacy persisted record",
                "surfaces": ["runtime_mcp"],
                "entrypoints": {
                    "backend": "",
                    "frontend": "",
                    "mcp": ["communicate"],
                    "instructions": [],
                    "team_definitions": [],
                    "frontend_modules": [],
                    "settings": [],
                    "python_requirements": [],
                },
                "permissions": {},
                "dependencies": [],
                "marketplace": {"subscription_required": False},
            },
            "enabled": True,
            "installed_at": "2026-06-21T00:00:00+00:00",
            "updated_at": "2026-06-21T00:00:00+00:00",
            "source": {
                "type": "legacy",
                "repo_url": "",
                "extension_path": "",
                "ref": "",
                "commit_sha": "legacy",
                "install_path": str(install_root),
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
        configs = extension_store.runtime_mcp_server_configs(
            {
                "app_session_id": "s1",
                "backend_url": "http://127.0.0.1:8000",
                "internal_token": "token",
                "cwd": str(install_root),
                "model": "m",
            },
            interacts_with_user=True,
            bare=False,
        )
        if "communicate" in configs:
            raise AssertionError(configs)
        if extension_store.extension_mcp_servers("ofek.legacy-string-mcp"):
            raise AssertionError("reserved legacy MCP server should be hidden")
    finally:
        shutil.rmtree(install_root, ignore_errors=True)


def test_builtin_feature_extensions_are_toggleable_and_uninstall_removes_record() -> None:
    ask_id = "fixture.project-structure"
    package = _write_private_extension_package(
        ask_id,
        "extensions/project-structure",
        {
            "name": "Project Structure",
            "surfaces": ["backend_feature"],
            "permissions": {},
        },
    )
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(_TRUSTED_TEST_ROOT),
            "extension_path": "extensions/project-structure",
            "ref": "",
            "commit_sha": "project-structure-private",
        },
        persist=True,
    )
    records = {item["manifest"]["id"]: item for item in extension_store.list_extensions()}
    if ask_id not in records:
        raise AssertionError("private project-structure extension missing from extension list")
    if records[ask_id]["source"]["type"] != "better_agent_local":
        raise AssertionError(records[ask_id]["source"])
    if not extension_store.is_builtin_feature_enabled(ask_id):
        raise AssertionError("private project-structure extension should default active")

    disabled = extension_store.set_enabled(ask_id, False)
    if disabled["enabled"] is not False:
        raise AssertionError(disabled)
    if extension_store.is_builtin_feature_enabled(ask_id):
        raise AssertionError("disabled private project-structure extension still active")

    enabled = extension_store.set_enabled(ask_id, True)
    if enabled["enabled"] is not True:
        raise AssertionError(enabled)
    if not extension_store.is_builtin_feature_enabled(ask_id):
        raise AssertionError("enabled private project-structure extension is not active")

    extension_store.uninstall(ask_id)
    if extension_store.get_extension(ask_id) is not None:
        raise AssertionError("private extension record still exists after uninstall")
    if extension_store.is_builtin_feature_enabled(ask_id):
        raise AssertionError("uninstalled private project-structure extension still active")
    try:
        extension_store.set_enabled(ask_id, True)
    except extension_store.ExtensionError as exc:
        if "not installed" not in str(exc):
            raise
    else:
        raise AssertionError("uninstalled private Project Structure extension was re-enabled without install")


def test_assistant_uninstall_removes_singleton_state_and_session() -> None:
    import assistant_ui
    import session_manager

    assistant_id = "fixture.assistant"
    if not assistant_id:
        raise AssertionError("assistant builtin id missing")
    package = _write_private_extension_package(
        assistant_id,
        "extensions/assistant",
        {
            "name": "Assistant",
            "surfaces": ["backend_feature"],
            "permissions": {"session_state": True},
        },
        files={"prompts/system.md": "Assistant role prompt."},
    )
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(_TRUSTED_TEST_ROOT),
            "extension_path": "extensions/assistant",
            "ref": "",
            "commit_sha": "assistant-private",
        },
        persist=True,
    )

    sess = assistant_ui.ensure_singleton("board")
    sid = sess["id"]
    state_path = assistant_ui._state_path()  # type: ignore[attr-defined]
    if not state_path.exists():
        raise AssertionError("assistant singleton state was not created")
    if session_manager.manager.get(sid) is None:
        raise AssertionError("assistant singleton session was not created")

    extension_store.uninstall(assistant_id)
    if state_path.exists():
        raise AssertionError("assistant singleton state still exists after uninstall")
    if session_manager.manager.get(sid) is not None:
        raise AssertionError("assistant singleton session still exists after uninstall")


def test_public_todos_extension_is_seeded_and_toggleable() -> None:
    old_repo = os.environ.pop("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH", None)
    try:
        records = {item["manifest"]["id"]: item for item in extension_store.list_extensions_with_reconciliation(include_hidden=True)[0]}
    finally:
        if old_repo is not None:
            os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = old_repo
    todos_id = extension_store.BUILTIN_TODOS_EXTENSION_ID
    record = records.get(todos_id)
    if not record:
        raise AssertionError("public todos extension missing from extension list")
    if record["source"]["type"] != "better_agent_bundled":
        raise AssertionError(record["source"])
    if not extension_store.is_extension_runtime_ready(todos_id):
        raise AssertionError("public todos extension should default runtime-ready")
    permissions = record["manifest"].get("permissions") or {}
    if permissions.get("reads_session_fields") != ["current_todos", "current_tasks"]:
        raise AssertionError(permissions)
    if permissions.get("mutates_session_fields") != ["current_todos", "current_tasks"]:
        raise AssertionError(permissions)

    extension_store.set_enabled(todos_id, False)
    if extension_store.is_extension_runtime_ready(todos_id):
        raise AssertionError("disabled todos extension should not be runtime-ready")
    extension_store.set_enabled(todos_id, True)


def test_public_session_bridge_backend_entrypoint_is_exposed() -> None:
    old_repo = os.environ.pop("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH", None)
    try:
        records = {item["manifest"]["id"]: item for item in extension_store.list_extensions_with_reconciliation(include_hidden=True)[0]}
    finally:
        if old_repo is not None:
            os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = old_repo
    session_bridge_id = extension_store.BUILTIN_SESSION_BRIDGE_EXTENSION_ID
    record = records.get(session_bridge_id)
    if not record:
        raise AssertionError("public session bridge extension missing from extension list")
    permissions = record["manifest"].get("permissions") or {}
    if permissions.get("backend_routes") is not True:
        raise AssertionError(permissions)
    spec = extension_store.backend_entrypoint_spec(session_bridge_id)
    if not spec:
        raise AssertionError("session bridge backend entrypoint was not exposed")
    if spec["entrypoint_kind"] != "file" or not spec["entrypoint"].endswith("backend/routes.py"):
        raise AssertionError(spec)


def test_backend_entrypoint_does_not_require_internal_llm_assignment() -> None:
    import config_store

    project_structure_id = "fixture.project-structure"
    # Clear providers so the project_structure_edit LLM task is genuinely
    # unready (tasks resolve via inheritance from the default provider, so
    # clearing assignments alone no longer gates readiness). Restored below.
    old_state = config_store._load_state()
    config_store._save_state({**old_state, "providers": [], "default_provider_id": None})
    # Reset any tombstone/record a prior test left: reconcile honors the
    # deleted_extensions tombstone for managed private ids and would otherwise
    # skip re-seeding project-structure, leaving the fixture uninstalled.
    with extension_store._store_lock():
        data = extension_store._read_store_unlocked()
        data["extensions"].pop(project_structure_id, None)
        (data.get("deleted_extensions") or {}).pop(project_structure_id, None)
        extension_store._write_store_unlocked(data)
    package = _TRUSTED_TEST_ROOT / "extensions" / "project-structure"
    if package.exists():
        shutil.rmtree(package)
    (package / "backend").mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": project_structure_id,
        "core_roles": ["project-structure"],
        "name": "Project structure",
        "version": "1.0.0",
        "surfaces": ["backend_feature", "frontend_feature"],
        "entrypoints": {
            "backend_module": "backend.routes",
            "page": {
                "label": "Project structure",
                "open": {
                    "type": "ensure",
                    "endpoint": f"/api/extensions/{"fixture.project-structure"}/backend/project-structure-edit/ensure",
                    "path_template": "/s/{session_id}",
                },
                "badge": {
                    "endpoint": f"/api/extensions/{"fixture.project-structure"}/backend/project-updates/total",
                },
            },
        },
        "permissions": {"backend_routes": True, "internal_loopback": True},
        "protocol": {
            "version": 1,
            "smoke_test": {
                "required_paths": ["better-agent-extension.json"],
                "python_modules": ["backend.routes"],
            },
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "backend" / "routes.py").write_text(
        "from fastapi import APIRouter\n\n"
        "def create_router(context):\n"
        "    return APIRouter()\n",
        encoding="utf-8",
    )
    extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "artifact",
            "repo_url": "",
            "extension_path": "",
            "ref": "",
            "commit_sha": "fixture-project-structure",
        },
        persist=True,
    )

    try:
        spec = extension_store.backend_entrypoint_spec(project_structure_id)
        if spec is None:
            raise AssertionError("backend route spec should mount without internal LLM assignment")
    finally:
        config_store._save_state(old_state)


def test_builtin_mcp_registry_respects_feature_extension_state() -> None:
    import extension_registry

    active = extension_registry.active_builtin_mcp_extensions(
        {"mode": "native", "app_session_id": "s1"},
        interacts_with_user=True,
        bare=False,
    )
    if "get-requirements" in {item.mcp_server for item in active}:
        raise AssertionError("requirements MCP should require private extension install")

    _configure_internal_llm_defaults("project_structure_edit")
    active = extension_registry.active_builtin_mcp_extensions(
        {"mode": "native", "app_session_id": "s1"},
        interacts_with_user=True,
        bare=False,
    )
    if "project-updates" in {item.mcp_server for item in active}:
        raise AssertionError("project-updates MCP should come from installed private extension")


def test_private_requirements_mcp_requires_internal_llm_defaults() -> None:
    import config_store

    config_store.set_internal_llm_assignments({})

    extension_id = "fixture.requirements"
    work = _private_monorepo_test_work()
    package = work / "extensions" / "requirements"
    package.mkdir(parents=True)
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND,
            "id": extension_id,
            "name": "Requirements",
            "version": "1.0.0",
            "description": "",
            "surfaces": ["backend_feature", "runtime_mcp"],
            "entrypoints": {
                "backend": "",
                "frontend": "",
                "mcp": [
                    {
                        "name": "better-agent-requirements",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "interacts_with_user": True,
                        "bare_allowed": False,
                        "requires_backend_auth": True,
                    }
                ],
                "instructions": [],
            },
            "permissions": {"session_state": True, "spawn_runs": True, "internal_loopback": True},
            "core_roles": ["requirements"],
            "marketplace": {
                "product_id": "requirements.pro",
                "subscription_required": True,
                "entitlement_url": "https://marketplace.test/entitlements",
            },
        },
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
    (package / "mcp").mkdir(parents=True)
    (package / "mcp" / "server.py").write_text("print('requirements')\n", encoding="utf-8")
    # The package dir must contain the manifest file so the smoke test (now
    # required for runtime readiness) can validate required_paths.
    (package / "better-agent-extension.json").write_text(
        json.dumps(data["extensions"][extension_id]["manifest"]), encoding="utf-8"
    )
    data["extensions"][extension_id]["manifest"] = _validate_manifest(
        data["extensions"][extension_id]["manifest"]
    )
    extension_store._save(data)  # type: ignore[attr-defined]

    configs = extension_store.runtime_mcp_server_configs(
        {
            "mode": "native",
            "app_session_id": "s1",
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "secret",
        },
        interacts_with_user=True,
        bare=False,
    )
    if "better-agent-requirements" in configs:
        raise AssertionError("requirements MCP active before requirement_analysis default is configured")

    _configure_internal_llm_defaults("requirement_analysis")
    configs = extension_store.runtime_mcp_server_configs(
        {
            "mode": "native",
            "app_session_id": "s1",
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "secret",
        },
        interacts_with_user=True,
        bare=False,
    )
    if "better-agent-requirements" in configs:
        raise AssertionError("requirements MCP active before requirement_analysis package exists")

    (package / "requirement_analysis").mkdir()
    (package / "requirement_analysis" / "__init__.py").write_text("", encoding="utf-8")
    configs = extension_store.runtime_mcp_server_configs(
        {
            "mode": "native",
            "app_session_id": "s1",
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "secret",
        },
        interacts_with_user=True,
        bare=False,
    )
    if "better-agent-requirements" not in configs:
        raise AssertionError("requirements MCP inactive after requirement_analysis default is configured")


def test_marketplace_extension_can_use_builtin_id_after_uninstall() -> None:
    extension_id = "fixture.requirements"
    work = _private_monorepo_test_work()
    try:
        repo, commit = _make_repo(work, extension_id=extension_id)
        record = extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/requirements",
        )
        if record["manifest"]["id"] != extension_id:
            raise AssertionError(record["manifest"])
        if record["source"]["type"] != "git":
            raise AssertionError(record["source"])
        if record["source"]["commit_sha"] != commit:
            raise AssertionError(record["source"])
        if not extension_store.is_builtin_feature_enabled(extension_id):
            raise AssertionError("marketplace-installed builtin id is not active")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_builtin_extension_list_row_is_not_duplicated_by_stale_external_record() -> None:
    builtin_id = "fixture.project-structure"
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][builtin_id] = {
        "manifest": {
            "kind": "better-agent-extension",
            "id": builtin_id,
                "name": "Stale project-structure external",
            "version": "1.0.0",
            "description": "",
            "surfaces": ["instructions"],
            "entrypoints": {
                "backend": "",
                "frontend": "",
                "mcp": [],
                "instructions": [],
            },
            "permissions": {},
            "marketplace": {
                "product_id": "",
                "subscription_required": False,
                "entitlement_url": "",
            },
        },
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/extensions.git",
                "extension_path": "extensions/project-structure",
            "ref": "",
            "commit_sha": "abc",
            "install_path": str(Path(_TMP_HOME) / "installed-ask"),
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
    matches = [
        item
        for item in extension_store.list_extensions()
        if item["manifest"]["id"] == builtin_id
    ]
    if len(matches) != 1:
        raise AssertionError(matches)


def test_list_extensions_reports_builtin_reconciliation_once() -> None:
    old_agent_home = os.environ["BETTER_AGENT_HOME"]
    old_claude_home = os.environ["BETTER_CLAUDE_HOME"]
    old_repo = os.environ.pop("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH", None)
    temp_home = tempfile.mkdtemp(prefix="bc-test-public-reconcile-")
    try:
        os.environ["BETTER_AGENT_HOME"] = temp_home
        os.environ["BETTER_CLAUDE_HOME"] = temp_home
        extension_store._STORE_PATH = None
        builtin_id = extension_store.BUILTIN_TODOS_EXTENSION_ID
        data = extension_store._blank_store()  # type: ignore[attr-defined]
        data["extensions"][builtin_id] = {
            "manifest": {"id": builtin_id},
            "enabled": False,
            "installed_at": "old",
            "updated_at": "old",
            "source": {
                "type": "better_agent_bundled",
                "repo_url": "",
                "extension_path": "extensions/todos",
                "ref": "",
                "commit_sha": "stale",
                "install_path": str(Path(temp_home) / "missing"),
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

        records, changed = extension_store.list_extensions_with_reconciliation()
        reconciled = next(item for item in records if item["manifest"]["id"] == builtin_id)
        if changed is not True:
            raise AssertionError("first list did not report reconciliation")
        if reconciled["enabled"] is not False:
            raise AssertionError("public builtin reconciliation did not preserve enabled state")

        _records, changed_again = extension_store.list_extensions_with_reconciliation()
        if changed_again is not False:
            raise AssertionError("second list reported reconciliation without changes")
    finally:
        os.environ["BETTER_AGENT_HOME"] = old_agent_home
        os.environ["BETTER_CLAUDE_HOME"] = old_claude_home
        extension_store._STORE_PATH = None
        if old_repo is not None:
            os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = old_repo
        shutil.rmtree(temp_home, ignore_errors=True)


def test_list_extensions_reuses_reconciled_store_until_fingerprint_changes() -> None:
    old_agent_home = os.environ.get("BETTER_AGENT_HOME")
    old_claude_home = os.environ.get("BETTER_CLAUDE_HOME")
    temp_home = tempfile.mkdtemp(prefix="ba-ext-reconcile-cache-")
    original_load_with_changes = extension_store._load_with_changes  # type: ignore[attr-defined]
    calls = 0
    try:
        os.environ["BETTER_AGENT_HOME"] = temp_home
        os.environ["BETTER_CLAUDE_HOME"] = temp_home

        def counted_load_with_changes():
            nonlocal calls
            calls += 1
            return original_load_with_changes()

        extension_store._load_with_changes = counted_load_with_changes  # type: ignore[attr-defined]
        extension_store._save(extension_store._blank_store())  # type: ignore[attr-defined]

        extension_store.list_extensions_with_reconciliation(include_hidden=True)
        extension_store.list_extensions_with_reconciliation(include_hidden=True)
        if calls != 1:
            raise AssertionError(f"unchanged reconciled store loaded with changes {calls} times")

        data = extension_store._load()  # type: ignore[attr-defined]
        data["extensions"]["test.cache.invalidate"] = {
            "manifest": {
                "id": "test.cache.invalidate",
                "name": "Cache Invalidate",
                "version": "1.0.0",
                "description": "",
            },
            "enabled": False,
            "installed_at": "now",
            "updated_at": "now",
            "source": {"type": "local", "install_path": temp_home},
            "entitlement": {"status": "not_required"},
        }
        extension_store._save(data)  # type: ignore[attr-defined]
        extension_store.list_extensions_with_reconciliation(include_hidden=True)
        if calls != 2:
            raise AssertionError("store write did not invalidate reconciliation cache")
    finally:
        extension_store._load_with_changes = original_load_with_changes  # type: ignore[attr-defined]
        if old_agent_home is None:
            os.environ.pop("BETTER_AGENT_HOME", None)
        else:
            os.environ["BETTER_AGENT_HOME"] = old_agent_home
        if old_claude_home is None:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        else:
            os.environ["BETTER_CLAUDE_HOME"] = old_claude_home
        shutil.rmtree(temp_home, ignore_errors=True)


def test_load_with_changes_reconciles_outside_store_lock() -> None:
    original_store_lock = extension_store._store_lock  # type: ignore[attr-defined]
    original_read = extension_store._read_store_unlocked  # type: ignore[attr-defined]
    original_reconcile = extension_store._reconcile_loaded_store  # type: ignore[attr-defined]
    original_write = extension_store._write_store_unlocked  # type: ignore[attr-defined]
    original_fingerprint = extension_store._refresh_store_fingerprint_cache  # type: ignore[attr-defined]
    locked = False
    written: dict | None = None
    store = {
        "schema_version": extension_store.STORE_SCHEMA_VERSION,
        "extensions": {"remove.me": {"manifest": {"id": "remove.me"}}},
        "deleted_extensions": {},
    }

    class FakeLock:
        def __enter__(self):
            nonlocal locked
            if locked:
                raise AssertionError("store lock re-entered")
            locked = True

        def __exit__(self, *args):
            nonlocal locked
            locked = False

    def fake_read_store_unlocked():
        if not locked:
            raise AssertionError("store read must run under the store lock")
        return json.loads(json.dumps(store))

    def fake_reconcile(data: dict):
        if locked:
            raise AssertionError("reconcile must not run while holding the store lock")
        data["extensions"].pop("remove.me")
        data["extensions"]["add.me"] = {"manifest": {"id": "add.me"}}
        return True, True, []

    def fake_fingerprint():
        if not locked:
            raise AssertionError("store fingerprint must run under the store lock")
        return (1, 1)

    def fake_write_store_unlocked(data: dict):
        nonlocal written
        if not locked:
            raise AssertionError("store write must run under the store lock")
        written = json.loads(json.dumps(data))

    try:
        extension_store._store_lock = lambda: FakeLock()  # type: ignore[attr-defined]
        extension_store._read_store_unlocked = fake_read_store_unlocked  # type: ignore[attr-defined]
        extension_store._reconcile_loaded_store = fake_reconcile  # type: ignore[attr-defined]
        extension_store._refresh_store_fingerprint_cache = fake_fingerprint  # type: ignore[attr-defined]
        extension_store._write_store_unlocked = fake_write_store_unlocked  # type: ignore[attr-defined]

        data, changed, public_changed = extension_store._load_with_changes()  # type: ignore[attr-defined]
        if changed is not True or public_changed is not True:
            raise AssertionError("reconcile change flags were not preserved")
        if "add.me" not in data["extensions"] or "remove.me" in data["extensions"]:
            raise AssertionError("reconciled store was not returned")
        if written != data:
            raise AssertionError("reconciled store was not written under the lock")
    finally:
        extension_store._store_lock = original_store_lock  # type: ignore[attr-defined]
        extension_store._read_store_unlocked = original_read  # type: ignore[attr-defined]
        extension_store._reconcile_loaded_store = original_reconcile  # type: ignore[attr-defined]
        extension_store._write_store_unlocked = original_write  # type: ignore[attr-defined]
        extension_store._refresh_store_fingerprint_cache = original_fingerprint  # type: ignore[attr-defined]


def test_load_with_changes_retries_when_store_changes_during_reconcile() -> None:
    original_store_lock = extension_store._store_lock  # type: ignore[attr-defined]
    original_read = extension_store._read_store_unlocked  # type: ignore[attr-defined]
    original_reconcile = extension_store._reconcile_loaded_store  # type: ignore[attr-defined]
    original_write = extension_store._write_store_unlocked  # type: ignore[attr-defined]
    original_fingerprint = extension_store._refresh_store_fingerprint_cache  # type: ignore[attr-defined]
    locked = False
    read_count = 0
    fingerprint_calls = 0
    written: dict | None = None

    class FakeLock:
        def __enter__(self):
            nonlocal locked
            if locked:
                raise AssertionError("store lock re-entered")
            locked = True

        def __exit__(self, *args):
            nonlocal locked
            locked = False

    def fake_read_store_unlocked():
        nonlocal read_count
        if not locked:
            raise AssertionError("store read must run under the store lock")
        read_count += 1
        if read_count == 1:
            return {
                "schema_version": extension_store.STORE_SCHEMA_VERSION,
                "extensions": {"base": {"manifest": {"id": "base"}}},
                "deleted_extensions": {},
            }
        return {
            "schema_version": extension_store.STORE_SCHEMA_VERSION,
            "extensions": {
                "base": {"manifest": {"id": "base"}},
                "concurrent": {"manifest": {"id": "concurrent"}},
            },
            "deleted_extensions": {},
        }

    def fake_reconcile(data: dict):
        if locked:
            raise AssertionError("reconcile must not run while holding the store lock")
        data["extensions"]["reconciled"] = {"manifest": {"id": "reconciled"}}
        return True, False, []

    def fake_fingerprint():
        nonlocal fingerprint_calls
        if not locked:
            raise AssertionError("store fingerprint must run under the store lock")
        fingerprint_calls += 1
        return (2, 1) if fingerprint_calls == 2 else (1, 1)

    def fake_write_store_unlocked(data: dict):
        nonlocal written
        if not locked:
            raise AssertionError("store write must run under the store lock")
        written = json.loads(json.dumps(data))

    try:
        extension_store._store_lock = lambda: FakeLock()  # type: ignore[attr-defined]
        extension_store._read_store_unlocked = fake_read_store_unlocked  # type: ignore[attr-defined]
        extension_store._reconcile_loaded_store = fake_reconcile  # type: ignore[attr-defined]
        extension_store._refresh_store_fingerprint_cache = fake_fingerprint  # type: ignore[attr-defined]
        extension_store._write_store_unlocked = fake_write_store_unlocked  # type: ignore[attr-defined]

        data, changed, public_changed = extension_store._load_with_changes()  # type: ignore[attr-defined]
        if changed is not True or public_changed is not False:
            raise AssertionError("reconcile change flags were not preserved")
        if read_count != 2:
            raise AssertionError(f"store was not reread after concurrent change: {read_count}")
        extensions = data["extensions"]
        if "concurrent" not in extensions or "reconciled" not in extensions:
            raise AssertionError(f"concurrent or reconciled record missing: {extensions}")
        if written != data:
            raise AssertionError("retried reconciled store was not written")
    finally:
        extension_store._store_lock = original_store_lock  # type: ignore[attr-defined]
        extension_store._read_store_unlocked = original_read  # type: ignore[attr-defined]
        extension_store._reconcile_loaded_store = original_reconcile  # type: ignore[attr-defined]
        extension_store._write_store_unlocked = original_write  # type: ignore[attr-defined]
        extension_store._refresh_store_fingerprint_cache = original_fingerprint  # type: ignore[attr-defined]


def test_local_refresh_recovery_rolls_back_exact_quarantine_on_reconcile_failure() -> None:
    originals = (
        extension_store._store_lock,
        extension_store._read_store_unlocked,
        extension_store._reconcile_loaded_store,
        extension_store._write_store_unlocked,
        extension_store._refresh_store_fingerprint_cache,
        extension_store._reconcile_recovered_cohorts,
        extension_store._evict_extension_backend,
    )
    quarantine = {
        "reason": "repeated_slow_backend_calls",
        "attributed_extension_id": "ofek.rollback-base",
        "attributed_generation": "old",
        "cohort": ["ofek.rollback-base", "ofek.rollback-dependent"],
    }
    previous = {
        "schema_version": extension_store.STORE_SCHEMA_VERSION,
        "extensions": {
            extension_id: {
                "manifest": {"id": extension_id},
                "enabled": False,
                "quarantine": dict(quarantine),
            }
            for extension_id in quarantine["cohort"]
        },
        "deleted_extensions": {},
    }
    writes: list[dict] = []

    class FakeLock:
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    def fake_reconcile(data: dict):
        for extension_id in quarantine["cohort"]:
            data["extensions"][extension_id]["enabled"] = True
            data["extensions"][extension_id].pop("quarantine")
        return True, True, list(quarantine["cohort"])

    try:
        extension_store._store_lock = lambda: FakeLock()  # type: ignore[attr-defined]
        extension_store._read_store_unlocked = lambda: json.loads(json.dumps(previous))  # type: ignore[attr-defined]
        extension_store._reconcile_loaded_store = fake_reconcile  # type: ignore[attr-defined]
        extension_store._write_store_unlocked = lambda data: writes.append(json.loads(json.dumps(data)))  # type: ignore[attr-defined]
        extension_store._refresh_store_fingerprint_cache = lambda: (1, 1)  # type: ignore[attr-defined]
        extension_store._reconcile_recovered_cohorts = lambda *_args: (_ for _ in ()).throw(RuntimeError("injected"))  # type: ignore[attr-defined]
        extension_store._evict_extension_backend = lambda _extension_id: None  # type: ignore[attr-defined]
        try:
            extension_store._load_with_changes()  # type: ignore[attr-defined]
        except RuntimeError as exc:
            if str(exc) != "injected":
                raise
        else:
            raise AssertionError("reconcile failure did not propagate")
        if writes[-1] != previous:
            raise AssertionError("exact quarantined cohort was not restored")
    finally:
        (
            extension_store._store_lock,
            extension_store._read_store_unlocked,
            extension_store._reconcile_loaded_store,
            extension_store._write_store_unlocked,
            extension_store._refresh_store_fingerprint_cache,
            extension_store._reconcile_recovered_cohorts,
            extension_store._evict_extension_backend,
        ) = originals


def test_required_marketplace_extension_auto_installs_from_private_repo() -> None:
    record = extension_store.get_extension(extension_store.MARKETPLACE_EXTENSION_ID)
    if record is None:
        raise AssertionError("marketplace extension was not auto-installed")
    if record["source"]["type"] != "better_agent_local":
        raise AssertionError(record["source"])
    if record["enabled"] is not True:
        raise AssertionError("marketplace extension is not enabled")


def test_required_marketplace_extension_is_listed_in_public_extension_list() -> None:
    # The marketplace ships as a first-party packaged UI (settings slot + backend
    # bridge), surfaced by default. It is NOT hidden from the extension list.
    with extension_store._store_lock():
        data = extension_store._read_store_unlocked()
        (data.get("deleted_extensions") or {}).pop(extension_store.MARKETPLACE_EXTENSION_ID, None)
        extension_store._write_store_unlocked(data)
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    record = extension_store.get_extension(extension_store.MARKETPLACE_EXTENSION_ID)
    if record is None:
        raise AssertionError("marketplace extension was not auto-installed")
    listed_ids = {item["manifest"]["id"] for item in extension_store.list_extensions()}
    if extension_store.MARKETPLACE_EXTENSION_ID not in listed_ids:
        raise AssertionError("marketplace extension should be listed")


def test_obsolete_marketplace_id_is_purged_from_store_and_frontend_modules() -> None:
    old_agent_home = os.environ["BETTER_AGENT_HOME"]
    old_home = os.environ["BETTER_CLAUDE_HOME"]
    old_repo = os.environ.pop("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH", None)
    old_base_url = os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL")
    temp_home = tempfile.mkdtemp(prefix="bc-test-obsolete-marketplace-")
    os.environ["BETTER_AGENT_HOME"] = temp_home
    os.environ["BETTER_CLAUDE_HOME"] = temp_home
    os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = (Path(temp_home) / "missing" / "marketplace").as_uri()
    store_path = Path(temp_home) / "extensions" / "extensions.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    obsolete_root = Path(temp_home) / "obsolete-marketplace"
    obsolete_frontend = obsolete_root / "ui"
    obsolete_frontend.mkdir(parents=True)
    (obsolete_frontend / "index.html").write_text("<!doctype html>", encoding="utf-8")
    required_root = Path(temp_home) / "required-marketplace"
    required_frontend = required_root / "ui"
    required_frontend.mkdir(parents=True)
    required_mcp = required_root / "mcp"
    required_mcp.mkdir(parents=True)
    (required_frontend / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (required_mcp / "server.py").write_text("print('marketplace')\n", encoding="utf-8")
    obsolete_record = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND,
            "id": "better-agent.marketplace",
            "name": "Marketplace",
            "version": "0.1.0",
            "description": "Stale marketplace",
            "surfaces": ["frontend_feature", "backend_feature"],
            "entrypoints": {
                "backend": "",
                "frontend": "ui/index.html",
                "mcp": [],
                "instructions": [],
                "skills": [],
                "team_definitions": [],
                "frontend_modules": [
                    {
                        "slot": "settings",
                        "id": "marketplace",
                        "label": "Marketplace",
                        "kind": "iframe",
                        "module": "ui/index.html",
                    }
                ],
            },
            "permissions": {},
            "marketplace": {},
        },
        "enabled": True,
        "installed_at": "1970-01-01T00:00:00+00:00",
        "updated_at": "1970-01-01T00:00:00+00:00",
        "source": {
            "type": "marketplace",
            "repo_url": "https://ofek-dev.com/api/marketplace/extensions/better-agent.marketplace/artifact",
            "extension_path": "",
            "ref": "",
            "commit_sha": "obsolete",
            "install_path": str(obsolete_root),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    required_record = {
        **obsolete_record,
        "manifest": {
            **obsolete_record["manifest"],
            "id": extension_store.MARKETPLACE_EXTENSION_ID,
            "description": "Required marketplace",
            "surfaces": ["runtime_mcp"],
            "entrypoints": {
                "mcp": [
                    {
                        "name": "ofek-dev-marketplace",
                        "python": "mcp/server.py",
                        "args": [],
                        "env": {},
                        "interacts_with_user": True,
                        "bare_allowed": False,
                        "requires_backend_auth": True,
                    }
                ],
            },
            "permissions": {"internal_loopback": True},
            "protocol": {
                "version": 1,
                "smoke_test": {
                    "required_paths": ["better-agent-extension.json", "mcp/server.py"],
                    "python_modules": ["mcp.server"],
                },
            },
        },
        "source": {
            **obsolete_record["source"],
            "type": "better_agent_signed",
            "repo_url": "https://ofek-dev.com/api/marketplace/extensions/ofek-dev.marketplace/artifact",
            "commit_sha": "required",
            "install_path": str(required_root),
        },
    }
    store_path.write_text(
        json.dumps(
            {
                "schema_version": extension_store.STORE_SCHEMA_VERSION,
                "extensions": {
                    "better-agent.marketplace": obsolete_record,
                    extension_store.MARKETPLACE_EXTENSION_ID: required_record,
                },
            }
        ),
        encoding="utf-8",
    )
    try:
        with extension_store._override_store_path(store_path):
            data = extension_store._load_with_changes()[0]  # type: ignore[attr-defined]
        if "better-agent.marketplace" in data["extensions"]:
            raise AssertionError("obsolete marketplace id was not purged")
        required = data["extensions"].get(extension_store.MARKETPLACE_EXTENSION_ID)
        if required is None:
            raise AssertionError("required marketplace replacement was not present")
        if required["enabled"] is not True:
            raise AssertionError("required marketplace replacement is not enabled")
        persisted = json.loads(store_path.read_text(encoding="utf-8"))
        if "better-agent.marketplace" in persisted["extensions"]:
            raise AssertionError("obsolete marketplace id was not removed from disk")
        listed_ids = {item["manifest"]["id"] for item in extension_store.list_extensions()}
        if "better-agent.marketplace" in listed_ids:
            raise AssertionError("obsolete marketplace id was still listed")
        frontend_ids = {item["extension_id"] for item in extension_store.frontend_entrypoints()}
        if "better-agent.marketplace" in frontend_ids:
            raise AssertionError("obsolete marketplace id still exported frontend modules")
        mcp_names = {
            item["name"]
            for item in required["manifest"]["entrypoints"]["mcp"]
        }
        if "ofek-dev-marketplace" not in mcp_names:
            raise AssertionError("required marketplace MCP was not present")
    finally:
        os.environ["BETTER_AGENT_HOME"] = old_agent_home
        os.environ["BETTER_CLAUDE_HOME"] = old_home
        if old_repo is not None:
            os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = old_repo
        if old_base_url is None:
            os.environ.pop("BETTER_AGENT_MARKETPLACE_BASE_URL", None)
        else:
            os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = old_base_url
        shutil.rmtree(temp_home, ignore_errors=True)


def test_required_marketplace_extension_installs_public_bundled_package() -> None:
    old_agent_home = os.environ["BETTER_AGENT_HOME"]
    old_home = os.environ["BETTER_CLAUDE_HOME"]
    old_repo = os.environ.pop("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH", None)
    old_base_url = os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL")
    temp_home = tempfile.mkdtemp(prefix="bc-test-marketplace-placeholder-")
    os.environ["BETTER_AGENT_HOME"] = temp_home
    os.environ["BETTER_CLAUDE_HOME"] = temp_home
    os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = (Path(temp_home) / "missing" / "marketplace").as_uri()
    try:
        extension_store.list_extensions_with_reconciliation(include_hidden=True)
        record = extension_store.get_extension(extension_store.MARKETPLACE_EXTENSION_ID)
        if record is None:
            raise AssertionError("marketplace extension was not installed")
        if record["source"]["type"] != "better_agent_bundled":
            raise AssertionError(record["source"])
        if record["enabled"] is not True:
            raise AssertionError("marketplace extension is not enabled")
        mcp_names = {
            item["name"]
            for item in record["manifest"]["entrypoints"]["mcp"]
        }
        if "ofek-dev-marketplace" not in mcp_names:
            raise AssertionError("marketplace extension should expose marketplace MCP")
    finally:
        os.environ["BETTER_AGENT_HOME"] = old_agent_home
        os.environ["BETTER_CLAUDE_HOME"] = old_home
        if old_repo is not None:
            os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = old_repo
        if old_base_url is None:
            os.environ.pop("BETTER_AGENT_MARKETPLACE_BASE_URL", None)
        else:
            os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = old_base_url
        shutil.rmtree(temp_home, ignore_errors=True)


def test_required_marketplace_extension_cannot_be_disabled_or_uninstalled() -> None:
    extension_id = extension_store.MARKETPLACE_EXTENSION_ID
    try:
        extension_store.set_enabled(extension_id, False)
    except extension_store.ExtensionError as exc:
        if "Required extension" not in str(exc):
            raise
    else:
        raise AssertionError("required marketplace extension was disabled")
    try:
        extension_store.uninstall(extension_id)
    except extension_store.ExtensionError as exc:
        if "Required extension" not in str(exc):
            raise
    else:
        raise AssertionError("required marketplace extension was uninstalled")


def test_uninstall_installed_extension_removes_package_snapshot() -> None:
    work = _private_monorepo_test_work()
    try:
        repo, _commit = _make_repo(work)
        record = extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/requirements",
        )
        install_path = Path(record["source"]["install_path"])
        if not install_path.exists():
            raise AssertionError("installed package missing before uninstall")
        extension_store.uninstall("ofek.requirements")
        if install_path.exists():
            raise AssertionError("installed package snapshot still exists after uninstall")
        if extension_store.get_extension("ofek.requirements") is not None:
            raise AssertionError("extension record still exists after uninstall")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_frontend_extension_exports_frontend_modules() -> None:
    work = _private_monorepo_test_work()
    try:
        repo = work / "module-extension-repo"
        package = repo / "extensions" / "module"
        (package / "ui").mkdir(parents=True)
        manifest = {
            "kind": "better-agent-extension",
            "id": "ofek.settings-module",
            "name": "Settings Module",
            "version": "1.0.0",
            "description": "Settings module extension",
            "surfaces": ["frontend_feature"],
            "entrypoints": {
                "frontend": "ui/index.html",
                "frontend_modules": [
                    {
                        "slot": "settings",
                        "id": "settings",
                        "label": "Settings Module",
                        "module": "ui/settings.entry.js",
                    }
                ],
            },
            "protocol": {
                "version": 1,
                "smoke_test": {
                    "required_paths": [
                        "better-agent-extension.json",
                        "ui/index.html",
                        "ui/settings.entry.js",
                    ],
                    "python_modules": [],
                },
            },
            "permissions": {},
            "marketplace": {},
        }
        (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        (package / "ui" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        (package / "ui" / "settings.entry.js").write_text(
            "export function mount() { return () => {}; }\n",
            encoding="utf-8",
        )
        _git(repo, "init")
        _git(repo, "config", "user.email", "test@example.test")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "add module extension")

        extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/module",
        )
        entries = extension_store.frontend_entrypoints()
        entry = next(item for item in entries if item["extension_id"] == "ofek.settings-module")
        modules = entry["frontend_modules"]
        record = extension_store.get_extension("ofek.settings-module") or {}
        v = str((record.get("source") or {}).get("commit_sha") or "")[:12]
        if modules != [
            {
                "slot": "settings",
                "id": "settings",
                "label": "Settings Module",
                "kind": "module",
                "module": "ui/settings.entry.js",
                "module_url": f"/api/extensions/ofek.settings-module/frontend/ui/settings.entry.js?v={v}",
            }
        ]:
            raise AssertionError(modules)
        config_modules = extension_store.extension_config("ofek.settings-module")["frontend_modules"]
        if config_modules[0]["enabled"] is not True:
            raise AssertionError(config_modules)
        if config_modules[0]["loadable"] is not True:
            raise AssertionError(config_modules)
        disabled = extension_store.set_frontend_module_enabled(
            "ofek.settings-module",
            "settings",
            "settings",
            False,
        )
        if disabled is not False:
            raise AssertionError("frontend module should be disabled")
        config_modules = extension_store.extension_config("ofek.settings-module")["frontend_modules"]
        if config_modules[0]["enabled"] is not False:
            raise AssertionError(config_modules)
        if config_modules[0]["module_url"]:
            raise AssertionError(config_modules)
        filtered_entry = next(
            item for item in extension_store.frontend_entrypoints()
            if item["extension_id"] == "ofek.settings-module"
        )
        if filtered_entry["frontend_modules"]:
            raise AssertionError(filtered_entry["frontend_modules"])
        extension_store.set_frontend_module_enabled(
            "ofek.settings-module",
            "settings",
            "settings",
            True,
        )
        data = extension_store._load()  # type: ignore[attr-defined]
        data["extensions"]["ofek.settings-module"]["enabled"] = False
        extension_store._save(data)  # type: ignore[attr-defined]
        disabled_modules = extension_store.extension_config("ofek.settings-module")["frontend_modules"]
        if disabled_modules[0]["loadable"] is not False or disabled_modules[0]["module_url"]:
            raise AssertionError(disabled_modules)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_frontend_entrypoints_use_persisted_smoke_result() -> None:
    work = _private_monorepo_test_work()
    original_smoke = extension_store._run_extension_smoke_test  # type: ignore[attr-defined]
    try:
        repo = work / "persisted-smoke-repo"
        package = repo / "extensions" / "persisted-smoke"
        (package / "ui").mkdir(parents=True)
        manifest = {
            "kind": "better-agent-extension",
            "id": "ofek.persisted-smoke",
            "name": "Persisted Smoke",
            "version": "1.0.0",
            "description": "Frontend entrypoint should not rerun smoke.",
            "surfaces": ["frontend_feature"],
            "entrypoints": {"frontend": "ui/index.html"},
            "protocol": {
                "version": 1,
                "smoke_test": {
                    "required_paths": ["better-agent-extension.json", "ui/index.html"],
                    "python_modules": [],
                },
            },
            "permissions": {},
            "marketplace": {},
        }
        (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        (package / "ui" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        _git(repo, "init")
        _git(repo, "config", "user.email", "test@example.test")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "add persisted smoke extension")

        extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/persisted-smoke",
        )

        def fail_smoke(*_args, **_kwargs):
            raise AssertionError("frontend_entrypoints reran extension smoke")

        extension_store._run_extension_smoke_test = fail_smoke  # type: ignore[attr-defined]
        entries = extension_store.frontend_entrypoints()
        if "ofek.persisted-smoke" not in {item["extension_id"] for item in entries}:
            raise AssertionError(entries)
    finally:
        extension_store._run_extension_smoke_test = original_smoke  # type: ignore[attr-defined]
        extension_store.uninstall("ofek.persisted-smoke")
        shutil.rmtree(work, ignore_errors=True)


def test_manifest_rejects_frontend_module_outside_frontend_asset_directory() -> None:
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek.bad-module",
                "name": "Bad Module",
                "version": "1.0.0",
                "description": "",
                "surfaces": ["frontend_feature"],
                "entrypoints": {
                    "frontend": "ui/index.html",
                    "frontend_modules": [
                        {
                            "slot": "settings",
                            "id": "settings",
                            "label": "Bad Module",
                            "module": "other/settings.entry.js",
                        }
                    ],
                },
                "permissions": {},
                "marketplace": {},
            }
        )
    except extension_store.ExtensionError as exc:
        if "frontend asset directory" not in str(exc):
            raise
        return
    raise AssertionError("manifest with escaping frontend module path was accepted")


def test_frontend_extension_exports_iframe_module() -> None:
    work = _private_monorepo_test_work()
    try:
        repo = work / "iframe-extension-repo"
        package = repo / "extensions" / "iframe"
        (package / "ui").mkdir(parents=True)
        manifest = {
            "kind": "better-agent-extension",
            "id": "ofek.iframe-panel",
            "name": "Iframe Panel",
            "version": "1.0.0",
            "description": "Embedded iframe panel",
            "surfaces": ["frontend_feature"],
            "entrypoints": {
                "frontend": "ui/index.html",
                "frontend_modules": [
                    {
                        "slot": "settings",
                        "id": "panel",
                        "label": "Iframe Panel",
                        "kind": "iframe",
                        "module": "ui/index.html",
                    }
                ],
            },
            "protocol": {
                "version": 1,
                "smoke_test": {
                    "required_paths": ["better-agent-extension.json", "ui/index.html"],
                    "python_modules": [],
                },
            },
            "permissions": {},
            "marketplace": {},
        }
        (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        (package / "ui" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        _git(repo, "init")
        _git(repo, "config", "user.email", "test@example.test")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "add iframe extension")

        extension_store.install_from_repo(
            repo_url=repo.as_uri(),
            extension_path="extensions/iframe",
        )
        entries = extension_store.frontend_entrypoints()
        entry = next(item for item in entries if item["extension_id"] == "ofek.iframe-panel")
        modules = entry["frontend_modules"]
        record = extension_store.get_extension("ofek.iframe-panel") or {}
        v = str((record.get("source") or {}).get("commit_sha") or "")[:12]
        if modules != [
            {
                "slot": "settings",
                "id": "panel",
                "label": "Iframe Panel",
                "kind": "iframe",
                "module": "ui/index.html",
                "module_url": f"/api/extensions/ofek.iframe-panel/frontend/ui/index.html?v={v}",
            }
        ]:
            raise AssertionError(modules)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_manifest_rejects_invalid_frontend_module_kind() -> None:
    try:
        _validate_manifest(
            {
                "kind": "better-agent-extension",
                "id": "ofek.bad-kind",
                "name": "Bad Kind",
                "version": "1.0.0",
                "description": "",
                "surfaces": ["frontend_feature"],
                "entrypoints": {
                    "frontend": "ui/index.html",
                    "frontend_modules": [
                        {
                            "slot": "settings",
                            "id": "settings",
                            "label": "Bad Kind",
                            "kind": "shadow-dom",
                            "module": "ui/settings.entry.js",
                        }
                    ],
                },
                "permissions": {},
                "marketplace": {},
            }
        )
    except extension_store.ExtensionError as exc:
        if "frontend_modules.kind" not in str(exc):
            raise
        return
    raise AssertionError("manifest with unknown frontend_modules kind was accepted")


def test_manifest_validates_mcp_predicate() -> None:
    items = extension_store._validate_mcp_entrypoints(
        [{
            "name": "ext-with-predicate",
            "python": "mcp/server.py",
            "predicate": {
                "equals": {"mode": "native"},
                "not_equals": {"working_mode": "search_worker"},
                "nonempty": ["continuation_chain"],
            },
        }],
        extension_id="ofek-dev.test",
    )
    predicate = items[0]["predicate"]
    assert predicate["equals"] == {"mode": "native"}
    assert predicate["not_equals"] == {"working_mode": "search_worker"}
    assert predicate["nonempty"] == ["continuation_chain"]
    assert extension_store._mcp_predicate_matches(
        predicate, {"mode": "native", "working_mode": "", "continuation_chain": ["a"]}
    ) is True
    assert extension_store._mcp_predicate_matches(
        predicate, {"mode": "native", "working_mode": "search_worker", "continuation_chain": ["a"]}
    ) is False
    try:
        extension_store._validate_mcp_predicate({"bogus": 1})
    except extension_store.ExtensionError:
        pass
    else:
        raise AssertionError("unknown predicate key was accepted")


def test_manifest_accepts_session_event_hook_and_todos_fields() -> None:
    manifest = _validate_manifest(
        {
            "kind": "better-agent-extension",
            "id": extension_store.BUILTIN_TODOS_EXTENSION_ID,
            "name": "Todos",
            "version": "1.0.0",
            "surfaces": ["backend_feature"],
            "entrypoints": {
                "backend": "backend/routes.py",
                "hooks": {"session_event": "/session-event"},
            },
            "permissions": {
                "backend_routes": True,
                "internal_loopback": True,
                "reads_session_fields": ["current_todos", "current_tasks"],
                "mutates_session_fields": ["current_todos", "current_tasks"],
            },
        }
    )
    if manifest["entrypoints"]["hooks"]["session_event"] != "/session-event":
        raise AssertionError(manifest["entrypoints"]["hooks"])
    if manifest["permissions"]["backend_routes"] is not True:
        raise AssertionError(manifest["permissions"])
    if manifest["permissions"]["mutates_session_fields"] != ["current_todos", "current_tasks"]:
        raise AssertionError(manifest["permissions"])
    if manifest["permissions"]["reads_session_fields"] != ["current_todos", "current_tasks"]:
        raise AssertionError(manifest["permissions"])


def test_v1_store_migrates_source_types_to_v2_without_wipe() -> None:
    temp_home = tempfile.mkdtemp(prefix="bc-test-v1-migrate-")
    try:
        store_path = Path(temp_home) / "extensions" / "extensions.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        def record(extension_id: str, source_type: str) -> dict:
            return {
                "manifest": {"id": extension_id},
                "source": {"type": source_type},
            }
        v1_store = {
            "schema_version": 1,
            "extensions": {
                "vendor.bundled": record("vendor.bundled", "public_builtin"),
                "vendor.local": record("vendor.local", "private_local"),
                "vendor.signed": record("vendor.signed", "required_artifact"),
                "vendor.market": record("vendor.market", "artifact"),
            },
            "deleted_extensions": {},
        }
        store_path.write_text(json.dumps(v1_store), encoding="utf-8")

        with extension_store._override_store_path(store_path):
            data = extension_store._read_store_unlocked()  # type: ignore[attr-defined]
        if data["schema_version"] != 2:
            raise AssertionError(data["schema_version"])
        types = {k: v["source"]["type"] for k, v in data["extensions"].items()}
        if types != {
            "vendor.bundled": "better_agent_bundled",
            "vendor.local": "better_agent_local",
            "vendor.signed": "better_agent_signed",
            "vendor.market": "artifact",
        }:
            raise AssertionError(types)

        persisted = json.loads(store_path.read_text(encoding="utf-8"))
        if persisted["schema_version"] != 2:
            raise AssertionError("migration was not persisted to disk")
        if persisted["extensions"]["vendor.local"]["source"]["type"] != "better_agent_local":
            raise AssertionError(persisted["extensions"]["vendor.local"])
    finally:
        shutil.rmtree(temp_home, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_v1_store_migrates_source_types_to_v2_without_wipe()
        test_manifest_validation_rejects_unknown_permissions()
        test_manifest_validates_mcp_predicate()
        test_manifest_accepts_session_event_hook_and_todos_fields()
        test_manifest_accepts_extension_protocol_smoke_test()
        test_manifest_rejects_string_only_mcp_entrypoints()
        test_manifest_rejects_reserved_mcp_server_names()
        test_manifest_allows_builtin_mcp_replacements()
        test_manifest_validates_managed_run_env_permission()
        test_manifest_accepts_remote_services_with_network_permission()
        test_manifest_rejects_remote_services_without_network_permission()
        test_manifest_rejects_unsafe_remote_service_urls()
        test_manifest_accepts_backend_module_entrypoint()
        test_installed_extension_config_exposes_remote_services()
        test_manifest_rejects_mismatched_builtin_mcp_replacement()
        test_manifest_rejects_root_level_frontend_entrypoint()
        test_manifest_rejects_missing_team_definition_file()
        test_manifest_rejects_frontend_module_outside_frontend_asset_directory()
        test_install_from_private_monorepo_path_and_toggle()
        test_package_install_rejects_symlink_entries()
        test_rejects_path_escape()
        test_rejects_embedded_repo_credentials()
        test_file_repo_is_allowed_only_under_private_repo()
        test_file_repo_rejects_paths_outside_private_extensions()
        test_subscription_extensions_fail_closed_without_entitlement_url()
        test_subscription_extensions_use_configured_marketplace_entitlement_url()
        test_reinstall_and_state_changes_evict_persistent_backend()
        test_expired_entitlement_is_not_active()
        test_install_from_marketplace_metadata_installs_artifact()
        test_update_installed_extensions_updates_marketplace_metadata_record()
        test_update_installed_extensions_updates_git_record_and_preserves_enabled()
        test_builtin_feature_gate_rejects_inactive_entitlement()
        test_installed_extension_instructions_are_managed_blocks()
        test_builtin_harness_instructions_are_visible_extension()
        test_disabled_extension_has_no_blocks_anywhere()
        test_reconcile_all_sweeps_orphans_and_clears_disabled()
        test_legacy_provider_capabilities_field_is_aliased()
        test_manifest_accepts_skill_entrypoints_and_requires_skill_md()
        test_extension_enable_disable_installs_runtime_skills()
        test_extension_skill_native_install_preserves_edits_and_runtime_mode_skips_native_copy()
        test_runtime_skill_replace_is_atomic_and_repairs_gutted_targets()
        test_extension_store_save_preserves_concurrent_marketplace_mcp_records()
        test_extension_store_save_does_not_resurrect_concurrently_uninstalled_extension()
        test_extension_store_rehydrate_skips_tombstoned_installed_snapshot()
        test_extension_store_rehydrates_installed_artifact_snapshot()
        test_install_smoke_test_rejects_bad_python_module_import()
        test_optional_permissions_allow_forbid()
        test_command_based_mcp_server()
        test_module_based_mcp_server_config()
        test_installed_extension_exports_runtime_mcp_server_config()
        test_runtime_mcp_without_internal_loopback_does_not_receive_token()
        test_dynamic_runtime_mcp_can_be_disabled_per_run()
        test_recorded_runtime_mcp_outside_builtin_maps_can_be_disabled_per_run()
        test_native_mcp_reconcile_omits_disabled_recorded_runtime_mcp()
        test_legacy_string_mcp_entrypoints_do_not_crash_runtime_config()
        test_builtin_mcp_registry_respects_feature_extension_state()
        test_builtin_feature_extensions_are_toggleable_and_uninstall_removes_record()
        test_public_todos_extension_is_seeded_and_toggleable()
        test_public_session_bridge_backend_entrypoint_is_exposed()
        test_backend_entrypoint_does_not_require_internal_llm_assignment()
        test_marketplace_extension_can_use_builtin_id_after_uninstall()
        test_builtin_extension_list_row_is_not_duplicated_by_stale_external_record()
        test_list_extensions_reports_builtin_reconciliation_once()
        test_list_extensions_reuses_reconciled_store_until_fingerprint_changes()
        test_load_with_changes_reconciles_outside_store_lock()
        test_load_with_changes_retries_when_store_changes_during_reconcile()
        test_local_refresh_recovery_rolls_back_exact_quarantine_on_reconcile_failure()
        test_required_marketplace_extension_is_listed_in_public_extension_list()
        test_obsolete_marketplace_id_is_purged_from_store_and_frontend_modules()
        test_required_marketplace_extension_installs_public_bundled_package()
        test_required_marketplace_extension_cannot_be_disabled_or_uninstalled()
        test_uninstall_installed_extension_removes_package_snapshot()
        test_frontend_extension_exports_frontend_modules()
        test_frontend_entrypoints_use_persisted_smoke_result()
        test_frontend_extension_exports_iframe_module()
        test_manifest_rejects_invalid_frontend_module_kind()
        test_installed_extension_exports_team_definition_sources()
        test_manifest_dependencies_accepted_and_deduped()
        test_manifest_dependencies_reject_self_and_bad_id()
        test_install_smoke_test_rejects_missing_protocol_files()
        test_runtime_ready_requires_protocol_smoke_to_pass()
        test_runtime_ready_accepts_persisted_manifest_without_protocol()
        test_runtime_ready_only_spawn_runs_requires_default_session_llm()
        test_set_enabled_enforces_dependencies()
        test_slow_call_quarantine_respects_per_route_grace_but_not_unboundedly()
        test_slow_call_quarantine_disables_extension_and_dependents_durably()
        test_incidents_are_fenced_to_same_generation_activation()
        test_new_generation_recovers_exact_auto_quarantine_cohort()
        test_legacy_quarantine_is_annotated_without_enabling_then_recovers()
        test_legacy_quarantine_rejects_ambiguous_or_invalid_cohorts()
        test_legacy_quarantine_retains_then_exactly_once_drains_lag_spool()
        test_user_disabled_quarantine_member_blocks_auto_recovery()
        test_required_runtime_path_extensions_are_managed_builtins()
        test_prune_extension_versions_keeps_active_and_newest_fallbacks()
        test_prune_extension_versions_tolerates_vanishing_dir()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        shutil.rmtree(_TMP_OS_HOME, ignore_errors=True)
        shutil.rmtree(_TRUSTED_TEST_ROOT, ignore_errors=True)
    print("PASS extension store")

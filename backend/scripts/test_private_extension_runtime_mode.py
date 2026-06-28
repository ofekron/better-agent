from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import _test_home

_test_home.isolate("private-runtime-mode-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extension_package_loader  # noqa: E402
import extension_store  # noqa: E402


EXTENSION_ID = "ofek-dev.runtime-mode"
ENV_NAME = "BETTER_AGENT_PRIVATE_EXTENSION_RUNTIME"


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"ok - {msg}")


def _write_package(package: Path) -> None:
    (package / "mcp").mkdir(parents=True)
    (package / "frontend").mkdir()
    manifest = {
        "manifest_version": 1,
        "kind": "better-agent-extension",
        "id": EXTENSION_ID,
        "name": "Runtime Mode",
        "version": "1.0.0",
        "description": "Runtime mode test extension",
        "entrypoints": {
            "mcp": [
                {
                    "name": "runtime-mode",
                    "python": "mcp/server.py",
                    "user_facing": True,
                    "bare_allowed": True,
                }
            ],
            "frontend": "frontend/index.js",
            "frontend_modules": [
                {
                    "slot": "session-sidebar",
                    "id": "runtime-mode-panel",
                    "label": "Runtime Mode",
                    "kind": "module",
                    "module": "frontend/panel.js",
                }
            ],
            "instructions": [],
        },
        "permissions": {},
        "protocol": {
            "version": 1,
            "smoke_test": {
                "required_paths": ["mcp/server.py", "frontend/index.js", "frontend/panel.js"],
                "python_modules": ["mcp.server"],
            },
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "mcp" / "__init__.py").write_text("", encoding="utf-8")
    (package / "mcp" / "server.py").write_text("print('source mcp')\n", encoding="utf-8")
    (package / "frontend" / "index.js").write_text("export const source = true;\n", encoding="utf-8")
    (package / "frontend" / "panel.js").write_text("export const panel = true;\n", encoding="utf-8")


def _install(package: Path, repo: Path) -> dict:
    record = extension_store._install_from_package_dir(
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(repo),
            "extension_path": "extensions/runtime-mode",
            "ref": "",
            "commit_sha": "abc1234567890",
        },
        persist=True,
    )
    extension_store.set_harness_delivery_mode(EXTENSION_ID, "runtime")
    install_root = Path(record["source"]["install_path"]).resolve()
    (install_root / "mcp" / "server.py").write_text("print('packaged mcp')\n", encoding="utf-8")
    (install_root / "frontend" / "index.js").write_text("export const packaged = true;\n", encoding="utf-8")
    return record


def _runtime_config() -> dict:
    configs = extension_store.runtime_mcp_server_configs(
        {
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "token",
            "app_session_id": "s1",
            "cwd": "/tmp/project",
            "model": "m",
        },
        user_facing=True,
        bare=False,
    )
    config = configs.get("runtime-mode")
    if not config:
        raise AssertionError(configs)
    return config


def _frontend_version() -> str:
    entries = extension_store.frontend_entrypoints()
    entry = next((item for item in entries if item["extension_id"] == EXTENSION_ID), None)
    if not entry:
        raise AssertionError(entries)
    return str(entry["entrypoint_url"]).rsplit("?v=", 1)[1]


def _with_mode(mode: str, fn) -> None:
    old = os.environ.get(ENV_NAME)
    os.environ[ENV_NAME] = mode
    try:
        fn()
    finally:
        if old is None:
            os.environ.pop(ENV_NAME, None)
        else:
            os.environ[ENV_NAME] = old


def test_private_local_source_mode_uses_source_tree() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "private"
        package = repo / "extensions" / "runtime-mode"
        _write_package(package)
        record = _install(package, repo)
        source_root = package.resolve()
        install_root = Path(record["source"]["install_path"]).resolve()

        def run() -> None:
            check(extension_store.runtime_package_root_for_record(record) == source_root, "record resolves to source root")
            check(extension_package_loader.package_root(EXTENSION_ID) == source_root, "package loader uses source root")
            config = _runtime_config()
            check(Path(config["args"][0]).resolve() == source_root / "mcp" / "server.py", "runtime MCP uses source script")
            check(str(source_root) in str(config["env"].get("PYTHONPATH") or "").split(os.pathsep), "PYTHONPATH uses source root")
            asset = extension_store.resolve_frontend_asset(EXTENSION_ID, "frontend/index.js")
            check(asset == source_root / "frontend" / "index.js", "frontend asset resolves to source root")
            version = _frontend_version()
            check(version.startswith("abc123456789-"), "source frontend version includes file mtime")
            check(install_root != source_root, "packaged snapshot remains separate")

        _with_mode("source", run)


def test_private_local_packaged_mode_uses_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "private"
        package = repo / "extensions" / "runtime-mode"
        _write_package(package)
        record = _install(package, repo)
        install_root = Path(record["source"]["install_path"]).resolve()

        def run() -> None:
            check(extension_store.runtime_package_root_for_record(record) == install_root, "record resolves to packaged root")
            check(extension_package_loader.package_root(EXTENSION_ID) == install_root, "package loader uses packaged root")
            config = _runtime_config()
            check(Path(config["args"][0]).resolve() == install_root / "mcp" / "server.py", "runtime MCP uses packaged script")
            check(str(install_root) in str(config["env"].get("PYTHONPATH") or "").split(os.pathsep), "PYTHONPATH uses packaged root")
            asset = extension_store.resolve_frontend_asset(EXTENSION_ID, "frontend/index.js")
            check(asset == install_root / "frontend" / "index.js", "frontend asset resolves to packaged root")
            check(_frontend_version() == "abc123456789", "packaged frontend version stays commit-based")

        _with_mode("packaged", run)


def test_invalid_mode_fails_to_packaged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "private"
        package = repo / "extensions" / "runtime-mode"
        _write_package(package)
        record = _install(package, repo)
        install_root = Path(record["source"]["install_path"]).resolve()

        def run() -> None:
            check(extension_store.private_local_runtime_mode() == "packaged", "invalid mode resolves to packaged")
            check(extension_store.runtime_package_root_for_record(record) == install_root, "invalid mode uses packaged root")

        _with_mode("bad-mode", run)


if __name__ == "__main__":
    test_private_local_source_mode_uses_source_tree()
    test_private_local_packaged_mode_uses_snapshot()
    test_invalid_mode_fails_to_packaged()
    print("\nALL PASS")

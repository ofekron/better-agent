from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_home

HOME = Path(_test_home.isolate("ba-test-private-extension-refresh-"))

import extension_store


def main() -> None:
    repo = HOME / "private-repo"
    package = repo / "extensions" / "requirements"
    source = BACKEND.parent / "better-agent-private" / "extensions" / "requirements"
    shutil.copytree(source, package)

    extension_id = "ofek-dev.requirements"
    record = extension_store._install_private_package_snapshot(
        extension_id,
        package,
        commit_sha="same-revision",
    )
    record["enabled"] = False
    record["manifest"].pop("core_roles", None)
    original_package_sha = record["source"]["package_sha256"]
    cache_file = package / "__pycache__" / "ignored.pyc"
    cache_file.parent.mkdir()
    cache_file.write_bytes(b"generated")
    assert extension_store._hash_public_package(package) == original_package_sha
    runtime_file = package / "mcp" / "refresh_probe.py"
    runtime_file.write_text("VALUE = 1\n", encoding="utf-8")
    data = {
        "schema_version": extension_store.STORE_SCHEMA_VERSION,
        "extensions": {extension_id: record},
        "deleted_extensions": {},
    }

    original_root = extension_store._local_required_marketplace_repo_root
    original_discover = extension_store._discover_private_extensions
    extension_store._local_required_marketplace_repo_root = lambda: repo
    extension_store._discover_private_extensions = lambda _root: {
        extension_id: "extensions/requirements",
    }
    try:
        changed = extension_store._ensure_private_extensions(data)
    finally:
        extension_store._local_required_marketplace_repo_root = original_root
        extension_store._discover_private_extensions = original_discover

    refreshed = data["extensions"][extension_id]
    expected_revision = extension_store._hash_public_package(package)
    assert changed is True
    assert refreshed["manifest"]["core_roles"] == ["requirements"]
    assert refreshed["source"]["commit_sha"] == "installed"
    assert refreshed["source"]["package_sha256"] == expected_revision
    assert Path(refreshed["source"]["install_path"]).name == expected_revision
    assert refreshed["enabled"] is False
    print("PASS: local extension content drift refreshes immutable snapshot")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)

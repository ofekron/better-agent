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
    manifest = extension_store.validate_manifest(json.loads(
        (package / "better-agent-extension.json").read_text(encoding="utf-8")
    ))
    original_package_sha = extension_store._hash_public_package(package)
    record = {
        "manifest": manifest,
        "enabled": False,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "better_agent_local",
            "repo_url": str(repo),
            "extension_path": "extensions/requirements",
            "commit_sha": "same-revision",
            "package_sha256": original_package_sha,
            "install_path": str(HOME / "missing-install"),
        },
        "entitlement": {"status": "not_required"},
    }
    record["manifest"].pop("core_roles", None)
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

    original_repo_root = extension_store._repo_root
    extension_store._repo_root = lambda: repo
    try:
        changed = extension_store._ensure_local_extensions(data)
    finally:
        extension_store._repo_root = original_repo_root

    refreshed = data["extensions"][extension_id]
    expected_revision = extension_store._hash_public_package(package)
    assert changed is True
    assert refreshed["manifest"]["core_roles"] == ["requirements"]
    assert refreshed["source"]["commit_sha"] == "same-revision"
    assert refreshed["source"]["package_sha256"] == expected_revision
    assert Path(refreshed["source"]["install_path"]).name == expected_revision
    assert refreshed["enabled"] is False
    assert refreshed["installed_at"] == "2026-01-01T00:00:00+00:00"

    outside = HOME / "outside"
    shutil.copytree(package, outside)
    escaped = json.loads(json.dumps(record))
    escaped["source"]["extension_path"] = "../outside"
    assert extension_store._local_package_from_record(escaped) is None
    (repo / "escape-link").symlink_to(outside, target_is_directory=True)
    escaped["source"]["extension_path"] = "escape-link"
    assert extension_store._local_package_from_record(escaped) is None
    print("PASS: local extension content drift refreshes immutable snapshot")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)

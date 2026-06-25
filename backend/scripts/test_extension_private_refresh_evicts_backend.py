from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import _test_home

_test_home.isolate("ba-test-local-refresh-eviction-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extension_store  # noqa: E402


def main() -> None:
    repo = Path(tempfile.mkdtemp(prefix="ba-local-extension-source-")).resolve()
    package = repo / "extensions" / "refresh-evict"
    package.mkdir(parents=True)
    extension_id = "fixture.refresh-evict"
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": "Refresh eviction",
        "version": "1.0.0",
        "description": "Original",
        "surfaces": [],
        "entrypoints": {},
        "permissions": {},
        "protocol": {
            "version": 1,
            "smoke_test": {
                "required_paths": ["better-agent-extension.json"],
                "python_modules": [],
            },
        },
        "marketplace": {},
    }
    manifest_path = package / "better-agent-extension.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    package_sha = extension_store._hash_public_package(package)
    record = {
        "manifest": extension_store.validate_manifest(manifest),
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "better_agent_local",
            "repo_url": str(repo),
            "extension_path": "extensions/refresh-evict",
            "commit_sha": "same-commit",
            "package_sha256": package_sha,
            "install_path": str(repo / "missing-install"),
        },
        "entitlement": {"status": "not_required"},
    }
    data = {
        "schema_version": extension_store.STORE_SCHEMA_VERSION,
        "extensions": {extension_id: record},
        "deleted_extensions": {},
    }
    manifest["description"] = "Refreshed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    import extension_backend_loader

    original_repo_root = extension_store._repo_root
    extension_store._repo_root = lambda: repo
    try:
        with patch.object(extension_backend_loader, "evict_persistent_backend") as evict:
            changed, recovered = extension_store._ensure_local_extensions(data)
    finally:
        extension_store._repo_root = original_repo_root
        shutil.rmtree(repo, ignore_errors=True)

    assert changed is True
    assert recovered == []
    assert data["extensions"][extension_id]["manifest"]["description"] == "Refreshed"
    evict.assert_called_once_with(extension_id)
    print("PASS: local extension refresh evicts persistent backend")


if __name__ == "__main__":
    main()

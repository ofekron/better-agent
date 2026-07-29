"""Shared fixtures for installed extensions inside an isolated test home."""
from __future__ import annotations

import json
from pathlib import Path


def install_core_role(root: Path, role: str) -> str:
    import extension_store
    import paths

    resolved_root = root.resolve()
    if not paths.is_test_mode() or resolved_root != paths.ba_home().resolve():
        raise RuntimeError("core-role fixture requires the engaged isolated test home")
    if role not in extension_store.CORE_ROLES:
        raise ValueError(f"unknown core role: {role}")

    extension_id = f"test-fixture-{role}"
    owner = extension_store.extension_id_for_role(role)
    if owner is not None:
        if owner != extension_id:
            raise AssertionError(f"role {role!r} already belongs to {owner!r}")
        return owner

    package = resolved_root / "extension-fixtures" / extension_id
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "core_roles": [role],
        "entrypoints": {},
        "permissions": {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
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
    extension_store.set_enabled(extension_id, True)
    owner = extension_store.extension_id_for_role(role)
    if owner != extension_id:
        raise AssertionError(f"role {role!r} resolved to {owner!r}")
    return extension_id

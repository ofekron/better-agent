from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Sequence


def install_extension_fixture(
    home: str,
    extension_id: str,
    *,
    core_roles: Sequence[str] = (),
) -> str:
    import extension_store
    package = Path(home) / "private-fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {},
        "marketplace": {},
    }
    if core_roles:
        manifest["core_roles"] = list(core_roles)
    extension_id = extension_store.validate_manifest(manifest)["id"]
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
    return extension_id


def install_machine_nodes_extension(home: str) -> str:
    return install_extension_fixture(
        home,
        "test.machine-nodes",
        core_roles=("machine-nodes",),
    )

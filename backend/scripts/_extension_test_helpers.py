from __future__ import annotations

import json
import shutil
from pathlib import Path


def install_machine_nodes_extension(home: str) -> str:
    import extension_store

    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": "test.machine-nodes",
        "name": "Machine nodes test fixture",
        "version": "1.0.0",
        "description": "Test-owned machine nodes role provider",
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {},
        "core_roles": ["machine-nodes"],
        "marketplace": {},
    }
    extension_id = extension_store.validate_manifest(manifest)["id"]
    package = Path(home) / "private-fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
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

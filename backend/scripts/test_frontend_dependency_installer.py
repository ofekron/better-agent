from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "frontend" / "scripts" / "install-frontend-deps.mjs"


def test_staged_install_preserves_repository_local_dependencies() -> None:
    source = {
        "dependencies": {
            "local-package": "file:../vendor/example/package.tgz",
            "remote-package": "^1.0.0",
        },
        "packages": {
            "node_modules/local-package": {
                "resolved": "file:../vendor/example/package.tgz",
            },
        },
    }
    script = (
        f"import {{ absolutizeLocalReferences }} from {json.dumps(INSTALLER.as_uri())};"
        f"console.log(JSON.stringify(absolutizeLocalReferences("
        f"{json.dumps(source)}, {json.dumps(str(ROOT / 'frontend'))})));"
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    normalized = json.loads(completed.stdout)
    expected = (ROOT / "vendor" / "example" / "package.tgz").as_uri()
    assert normalized["dependencies"]["local-package"] == expected
    assert (
        normalized["packages"]["node_modules/local-package"]["resolved"]
        == expected
    )
    assert normalized["dependencies"]["remote-package"] == "^1.0.0"


if __name__ == "__main__":
    test_staged_install_preserves_repository_local_dependencies()
    print("frontend dependency installer tests passed")

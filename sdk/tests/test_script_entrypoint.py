from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_script_entrypoint_does_not_shadow_mcp_sdk(tmp_path: Path) -> None:
    package_root = tmp_path / "extension"
    script = package_root / "mcp" / "server.py"
    script.parent.mkdir(parents=True)
    (script.parent / "__init__.py").write_text("", encoding="utf-8")
    script.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "print(FastMCP.__name__)\n",
        encoding="utf-8",
    )
    sdk_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(None, (
                str(package_root),
                str(sdk_root),
                os.environ.get("PYTHONPATH", ""),
            ))
        ),
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "better_agent_sdk.script_entrypoint",
            str(package_root),
            str(script),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FastMCP"

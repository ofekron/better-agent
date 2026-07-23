#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from app_entry import _dispatch
import operation_cli
import provider


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        os.environ["BETTER_AGENT_HOME"] = raw
        directory = operation_cli.install_launcher()
        posix = directory / "better-agent-cli"
        windows = directory / "better-agent-cli.cmd"
        assert posix.is_file() and os.access(posix, os.X_OK)
        assert windows.is_file()
        env = provider.build_better_agent_run_env(
            backend_url="http://127.0.0.1:8000",
            internal_token="test-token",
            app_session_id="session-one",
            cwd="/tmp/project",
            model="model",
            provider_id="provider",
            bare_config=False,
            user_facing=True,
            disabled_builtin_extensions=[],
        )
        assert str(directory) == env["PATH"].split(os.pathsep)[0]
        assert env["BETTER_AGENT_OPERATION_CLI"] == "better-agent-cli"
        assert _dispatch(["--operation-cli", "--list"])[0] == "operation_cli"
        configs = operation_cli.available_configs()
        assert {"capabilities", "communicate", "open-config-panel", "ui"} <= set(configs)
        output = json.dumps({"groups": sorted(configs)}, separators=(",", ":"))
        assert "communicate" in output
    print("operation CLI tests passed")


if __name__ == "__main__":
    main()

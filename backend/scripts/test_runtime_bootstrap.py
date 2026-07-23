#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import time

_STATE_HOME = tempfile.mkdtemp(prefix="better-agent-runtime-bootstrap-")
os.environ["BETTER_AGENT_HOME"] = _STATE_HOME

from better_agent_sdk.runtime_transport import RuntimeTransport
import provider
import runtime_bootstrap


def main() -> None:
    try:
        env = provider.build_better_agent_run_env(
            backend_url="http://127.0.0.1:8000",
            internal_token="not-exported",
            run_id="run-bootstrap-test",
            app_session_id="session-one",
            cwd="/tmp/project",
            model="model",
            provider_id="provider",
            bare_config=False,
            user_facing=True,
            disabled_builtin_extensions=[],
        )
        assert "BETTER_AGENT_INTERNAL_TOKEN" not in env
        assert "BETTER_CLAUDE_INTERNAL_TOKEN" not in env
        address = env["BETTER_AGENT_RUNTIME_BOOTSTRAP"]
        response = RuntimeTransport(address).request(
            {"version": 1, "kind": "catalog"}
        )
        assert response["secret"] == "not-exported"
        try:
            RuntimeTransport(address).request({"version": 1, "kind": "catalog"})
        except Exception:
            pass
        else:
            raise AssertionError("runtime bootstrap handle was reusable")
        deadline = time.monotonic() + 5
        while runtime_bootstrap.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime_bootstrap.active_count() == 0
        state_files = [
            path
            for path in Path(_STATE_HOME).rglob("*")
            if path.is_file()
        ]
        assert all(b"not-exported" not in path.read_bytes() for path in state_files)
        for runner_name in ("runner.py", "runner_codex.py", "runner_better_agent.py"):
            runner_source = Path(__file__).parents[1].joinpath(runner_name).read_text()
            assert "_load_internal_token" not in runner_source
            assert '/ "internal_token"' not in runner_source
        print("runtime bootstrap tests passed")
    finally:
        shutil.rmtree(_STATE_HOME)


if __name__ == "__main__":
    main()

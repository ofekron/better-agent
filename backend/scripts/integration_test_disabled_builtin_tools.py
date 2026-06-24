#!/usr/bin/env python3
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home
ba_home = _test_home.isolate("bc-disabled-tools-")

try:
    import config_store
    import runner_codex
    import runner_gemini

    disabled = config_store.set_disabled_builtin_tools([
        "create_session",
        "unknown",
        "mssg",
        "ask",
        "ask",
        "create_sub_session",
        "delegate_task",
    ])
    assert disabled == ["ask", "create_session", "create_sub_session", "delegate_task", "mssg"]
    assert config_store.get_disabled_builtin_tools() == disabled
    disabled_extensions = config_store.set_disabled_builtin_extensions([
        "ofek-dev.requirements",
        "unknown",
        "ofek-dev.team-orchestration",
        "ofek-dev.requirements",
    ])
    assert disabled_extensions == ["ofek-dev.requirements", "ofek-dev.team-orchestration"]
    assert config_store.get_disabled_builtin_extensions() == disabled_extensions

    assert runner_codex._disabled_builtin_tools({
        "disabled_builtin_tools": disabled + ["unknown"],
    }) == {"ask", "create_session", "create_sub_session", "delegate_task", "mssg"}

    gemini_config = runner_gemini._with_communicate_mcp(
        {
            "app_session_id": "sid",
            "backend_url": "http://localhost:8000",
            "internal_token": "token",
            "disabled_builtin_tools": disabled + ["unknown"],
        },
        {},
    )
    env = gemini_config["mcp_servers"]["communicate"]["env"]
    assert env["BETTER_CLAUDE_DISABLED_BUILTIN_TOOLS"] == (
        "ask,create_session,create_sub_session,delegate_task,mssg"
    )
finally:
    shutil.rmtree(ba_home)

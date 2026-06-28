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
    import extension_store
    import runner_codex
    import runner_gemini

    disabled = config_store.set_disabled_builtin_tools([
        "create_session",
        "unknown",
        "mssg",
        "async_communicate",
        "ask",
        "ask",
        "create_sub_session",
        "delegate_task",
    ])
    assert disabled == ["ask", "async_communicate", "create_session", "create_sub_session", "delegate_task", "mssg"]
    assert config_store.get_disabled_builtin_tools() == disabled
    disabled_extensions = config_store.set_disabled_builtin_extensions([
        extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID,
        "unknown",
        extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID,
        extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID,
    ])
    assert disabled_extensions == [
        extension_store.BUILTIN_REQUIREMENTS_EXTENSION_ID,
        extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID,
    ]
    assert config_store.get_disabled_builtin_extensions() == disabled_extensions

    assert runner_codex._disabled_builtin_tools({
        "disabled_builtin_tools": disabled + ["unknown"],
    }) == {"ask", "async_communicate", "create_session", "create_sub_session", "delegate_task", "mssg"}

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
        "ask,async_communicate,create_session,create_sub_session,delegate_task,mssg"
    )
finally:
    shutil.rmtree(ba_home)

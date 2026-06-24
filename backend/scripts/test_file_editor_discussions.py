#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _test_home
tmp = _test_home.isolate("ba-file-discussions-")

try:
    import file_editor
    import working_mode
    from session_manager import manager as session_manager

    file_path = "/tmp/example.ts"
    session = session_manager.create(
        name="file editor",
        cwd="/tmp",
        model="test-model",
        orchestration_mode="native",
    )
    working_mode.mark_working_mode(
        session["id"],
        mode=file_editor.MODE,
        meta={
            "project_cwd": "/tmp",
            "file_paths": [file_path],
            "original_contents": {file_path: "before"},
            "persistent": True,
        },
    )

    discussion = file_editor.start_discussion(
        session["id"],
        file_path=file_path,
        line=7,
        title="Check this branch",
        opened_by="agent",
    )

    persisted = file_editor.get_discussion(session["id"], discussion["id"])
    assert persisted["file_path"] == file_path
    assert persisted["line"] == 7
    assert persisted["opened_by"] == "agent"

    prompt = file_editor.format_discussion_prompt(persisted, "What should change?")
    assert f"Discussion id: {discussion['id']}" in prompt
    assert f"File: {file_path}" in prompt
    assert "Line: 7" in prompt
    assert prompt.endswith("What should change?")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

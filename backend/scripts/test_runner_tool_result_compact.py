from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-runner-tool-result-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner  # noqa: E402


def test_tool_success_result_uses_compact_json() -> None:
    result = runner._tool_success_result({"success": True, "value": {"nested": ["x", "y"]}})
    text = result["content"][0]["text"]
    assert text == '{"success":true,"value":{"nested":["x","y"]}}'
    assert "\n" not in text


if __name__ == "__main__":
    test_tool_success_result_uses_compact_json()
    print("OK: runner tool result compact")

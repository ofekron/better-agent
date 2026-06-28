"""The Better Agent capabilities builtin MCP exposes exactly the three
management tools (list/load/release). This is the in-repo home for the
capability-scoping glue (moved out of the standalone provider-config-sync
package, which stays generic open-source).
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="cap_builtin_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import runner  # noqa: E402


def _tool_name(t) -> str:
    return getattr(t, "name", None) or getattr(t, "__name__", "")


def test_builds_three_management_tools():
    tools = runner._build_capability_tools(
        app_session_id="sess-1",
        backend_url="http://localhost:8000",
        internal_token="tok",
    )
    assert len(tools) == 3
    names = {_tool_name(t) for t in tools}
    assert names == {"list_capabilities", "load_capability", "release_capability"}, names


if __name__ == "__main__":
    import shutil

    try:
        test_builds_three_management_tools()
        print("OK")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

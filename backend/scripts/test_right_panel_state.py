from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_TMP_HOME = _test_home.isolate("bc-test-right-panel-")

from main import _right_panel_patch_from_body  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def test_right_panel_mutator_persists_full_ui_state() -> None:
    session = session_manager.create(
        name="panel",
        model="sonnet",
        cwd="/tmp/right-panel",
        orchestration_mode="native",
    )

    session_manager.set_right_panel(
        session["id"],
        open=True,
        tab="notes",
        tab_set=True,
        width=534,
        mobile_height=312,
        todos_dismissed=True,
        auto_opened_by=["notes", "files"],
        sidebar_minimized=True,
    )
    session_manager.flush_pending_persists()

    saved = session_manager.get(session["id"])
    assert saved["right_panel_open"] is True
    assert saved["right_panel_active_tab"] == "notes"
    assert saved["right_panel_width"] == 534
    assert saved["right_panel_mobile_height"] == 312
    assert saved["right_panel_todos_dismissed"] is True
    assert saved["right_panel_auto_opened_by"] == ["notes", "files"]
    assert saved["sidebar_minimized"] is True


def test_right_panel_endpoint_parser_accepts_session_ui_fields() -> None:
    patch = _right_panel_patch_from_body({
        "open": True,
        "tab": "board",
        "width": 600,
        "mobile_height": 280,
        "todos_dismissed": False,
        "auto_opened_by": ["board", "board", "files"],
        "sidebar_minimized": True,
    })

    assert patch == {
        "open": True,
        "tab": "board",
        "tab_set": True,
        "width": 600,
        "mobile_height": 280,
        "todos_dismissed": False,
        "auto_opened_by": ["board", "files"],
        "sidebar_minimized": True,
    }


def main() -> int:
    try:
        test_right_panel_mutator_persists_full_ui_state()
        test_right_panel_endpoint_parser_accepts_session_ui_fields()
        print("PASS right panel state")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

"""A Windows directory has two spellings; both name one project.

A session hosted on a Windows node is stored with whatever cwd its creator
used — `C:\\Users\\Lenovo\\better-agent` from a tool call, `C:/Users/Lenovo/
better-agent` from the UI. `session_matches_project` compared those strings
exactly, so the same directory became two projects and node-hosted sessions
were invisible in the node's own view.

POSIX paths must NOT be normalized: a backslash is a legal filename
character there, so rewriting separators would merge distinct directories.

Run:
    cd backend && PYTHONPATH=. .venv/bin/python scripts/test_project_path_matching.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _test_home
_test_home.isolate("bc_test_project_path_")

import session_manager  # noqa: E402

WIN_BACK = "C:\\Users\\Lenovo\\better-agent"
WIN_FWD = "C:/Users/Lenovo/better-agent"


def test_windows_spellings_are_one_project() -> None:
    cases = [
        ({"cwd": WIN_BACK}, WIN_FWD, True, "backslash record, forward query"),
        ({"cwd": WIN_FWD}, WIN_BACK, True, "forward record, backslash query"),
        ({"cwd": WIN_BACK}, WIN_BACK, True, "identical spelling"),
        ({"cwd": "c:\\Users\\Lenovo\\better-agent"}, WIN_FWD, True, "drive-letter case"),
        ({"cwd": WIN_FWD + "/"}, WIN_FWD, True, "trailing separator"),
        ({"cwd": "\\\\share\\team\\proj"}, "//share/team/proj", True, "UNC share"),
        ({"cwd": "C:/Users/Lenovo/other"}, WIN_FWD, False, "different windows dir"),
    ]
    for record, query, expected, label in cases:
        got = session_manager.session_matches_project(record, query)
        assert got is expected, f"{label}: expected {expected}, got {got}"


def test_posix_paths_keep_exact_matching() -> None:
    """Backslash is a real filename character on POSIX — never fold it."""
    assert session_manager.session_matches_project(
        {"cwd": "/Users/ofekron/better-claude"}, "/Users/ofekron/better-claude",
    ) is True
    assert session_manager.session_matches_project(
        {"cwd": "/tmp/a\\b"}, "/tmp/a/b",
    ) is False, "a POSIX file named 'a\\b' is not the directory a/b"
    assert session_manager.session_matches_project(
        {"cwd": "/tmp/one"}, "/tmp/two",
    ) is False


def test_existing_rules_still_hold() -> None:
    assert session_manager.session_matches_project({"cwd": "/anything"}, None) is True
    assert session_manager.session_matches_project({"cwd": "/anything"}, "") is True
    assert session_manager.session_matches_project(
        {"all_projects": True, "cwd": "/elsewhere"}, WIN_FWD,
    ) is True


def test_frontend_mirror_uses_the_shared_helper() -> None:
    """useSession.ts documents itself as mirroring the backend rule.

    An exact-comparison mirror re-hides the sessions the backend now returns,
    so lock that both the list filter and the project-entry selector go
    through the shared helper.
    """
    frontend = Path(__file__).resolve().parent.parent.parent / "frontend" / "src"
    helper = frontend / "utils" / "projectPath.ts"
    assert helper.is_file(), "frontend canonical project-path helper is missing"
    for rel in ("hooks/useSession.ts", "utils/uiSelection.ts", "App.tsx"):
        src = (frontend / rel).read_text(encoding="utf-8")
        assert "sameProjectPath" in src, f"{rel} no longer uses the shared helper"


if __name__ == "__main__":
    test_windows_spellings_are_one_project()
    test_posix_paths_keep_exact_matching()
    test_existing_rules_still_hold()
    test_frontend_mirror_uses_the_shared_helper()
    print("OK: windows project paths match regardless of spelling")

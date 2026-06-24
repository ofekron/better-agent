from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-project-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import project_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def main() -> None:
    work = tempfile.mkdtemp(prefix="bc-test-session-project-work-")
    try:
        cwd = str(Path(work, "app").resolve())
        session_manager.create(
            name="created from cli",
            model="sonnet",
            cwd=cwd,
            orchestration_mode="native",
            source="cli",
        )

        projects = project_store.list_projects()
        if not any(project.get("path") == cwd for project in projects):
            raise AssertionError(f"session cwd missing from projects: {projects}")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("PASS session create adds project")

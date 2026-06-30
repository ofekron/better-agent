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
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _project_paths() -> set[str]:
    return {project.get("path") for project in project_store.list_projects()}


def main() -> None:
    work = tempfile.mkdtemp(prefix="bc-test-session-project-work-")
    try:
        implicit = session_manager.create(
            name="implicit cwd",
            model="sonnet",
            orchestration_mode="native",
            source="web",
        )
        if implicit.get("cwd") != str(Path.home()):
            raise AssertionError(f"implicit cwd should remain runnable: {implicit}")
        if implicit.get("cwd_explicit") is not False:
            raise AssertionError(f"implicit cwd explicitness not tracked: {implicit}")
        if session_store.should_auto_register_project(implicit):
            project_store.add_project(
                implicit["cwd"],
                node_id=implicit.get("node_id") or "primary",
            )
        if str(Path.home()) in _project_paths():
            raise AssertionError("implicit home cwd was registered as a project")

        empty_string = session_manager.create(
            name="empty string cwd",
            model="sonnet",
            cwd="",
            orchestration_mode="native",
            source="web",
        )
        if empty_string.get("cwd") != str(Path.home()):
            raise AssertionError(f"empty string cwd should remain runnable: {empty_string}")
        if empty_string.get("cwd_explicit") is not False:
            raise AssertionError(f"empty string cwd explicitness not tracked: {empty_string}")
        if str(Path.home()) in _project_paths():
            raise AssertionError("empty string home cwd was registered as a project")

        explicit_home = session_manager.create(
            name="explicit home",
            model="sonnet",
            cwd=str(Path.home()),
            orchestration_mode="native",
            source="web",
        )
        if explicit_home.get("cwd_explicit") is not True:
            raise AssertionError(f"explicit cwd not tracked: {explicit_home}")
        if str(Path.home()) not in _project_paths():
            raise AssertionError("explicit home cwd was not registered as a project")

        project_store.remove_project(str(Path.home()))
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

        bare_cwd = str(Path(work, "bare").resolve())
        session_manager.create(
            name="bare config",
            model="sonnet",
            cwd=bare_cwd,
            orchestration_mode="native",
            source="web",
            bare_config=True,
        )
        if bare_cwd in _project_paths():
            raise AssertionError("bare_config cwd was registered as a project")

        import_cwd = str(Path(work, "imported").resolve())
        session_manager.create(
            name="imported",
            model="sonnet",
            cwd=import_cwd,
            orchestration_mode="native",
            source="import",
            user_initiated=True,
        )
        if import_cwd in _project_paths():
            raise AssertionError("imported session cwd was registered as a project")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("PASS session create adds project")

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-project-home-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import project_store  # noqa: E402


def main() -> None:
    work = tempfile.mkdtemp(prefix="bc-test-project-work-")
    try:
        project = Path(work, "repo").resolve()
        project.mkdir()
        subprocess.run(["git", "-C", str(project), "init"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(project), "remote", "add", "origin", "https://example.test/repo.git"],
            check=True,
            capture_output=True,
        )

        record = project_store.add_project(str(project))
        if record is None:
            raise AssertionError("add_project returned None")
        if record.get("git_remote") != "https://example.test/repo.git":
            raise AssertionError(f"git_remote missing from project record: {record}")
        if (project / ".better-claude").exists():
            raise AssertionError("add_project wrote repo-local Better Agent metadata")

        record.pop("git_remote", None)
        projects = project_store.list_projects()
        projects[0].pop("git_remote", None)
        project_store._write_file(projects)

        changed = project_store.backfill_git_remotes()
        if changed != 1:
            raise AssertionError(f"expected one backfilled remote, got {changed}")
        if (project / ".better-claude").exists():
            raise AssertionError("backfill wrote repo-local Better Agent metadata")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("PASS project store keeps metadata in BC home")

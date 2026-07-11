from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import _test_home


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TMP_HOME = _test_home.isolate("bc-test-session-project-")

import project_store  # noqa: E402
from bff_runtime_contract import project_candidate_from_session  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="bc-test-session-project-work-"))
    try:
        implicit = session_manager.create(
            name="implicit cwd", model="sonnet", orchestration_mode="native"
        )
        assert project_candidate_from_session(implicit) is None

        explicit = session_manager.create(
            name="explicit",
            model="sonnet",
            cwd=str(work),
            orchestration_mode="native",
            source="web",
        )
        assert project_store.list_projects() == []
        candidate = project_candidate_from_session(explicit)
        assert candidate is not None
        project_store.add_project(
            candidate["path"], node_id=candidate.get("node_id") or "primary"
        )
        assert {project["path"] for project in project_store.list_projects()} == {
            str(work.resolve())
        }

        bare = session_manager.create(
            name="bare",
            model="sonnet",
            cwd=str(work / "bare"),
            orchestration_mode="native",
            bare_config=True,
        )
        imported = session_manager.create(
            name="imported",
            model="sonnet",
            cwd=str(work / "imported"),
            orchestration_mode="native",
            source="import",
        )
        assert project_candidate_from_session(bare) is None
        assert project_candidate_from_session(imported) is None
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("PASS session creation publishes BFF project candidates")

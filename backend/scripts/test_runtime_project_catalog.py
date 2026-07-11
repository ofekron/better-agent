from __future__ import annotations

import shutil
import sys
from pathlib import Path

import _test_home


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TMP_HOME = _test_home.isolate("ba-runtime-project-catalog-")

import runtime_project_catalog  # noqa: E402


def test_catalog_is_replaceable_projection() -> None:
    projects = runtime_project_catalog.replace([
        {"path": "/tmp/one", "node_id": "primary", "name": "One"},
        {"path": "/remote/two", "node_id": "node-2", "name": "Two"},
    ])
    assert runtime_project_catalog.list_projects() == projects
    runtime_project_catalog.replace([])
    assert runtime_project_catalog.list_projects() == []


def test_catalog_rejects_invalid_or_duplicate_entries() -> None:
    invalid = [
        "not-a-list",
        [{}],
        [{"path": "/tmp/x"}, {"path": "/tmp/x", "node_id": "primary"}],
    ]
    for value in invalid:
        try:
            runtime_project_catalog.replace(value)
        except ValueError:
            continue
        raise AssertionError(f"invalid project catalog accepted: {value!r}")


if __name__ == "__main__":
    try:
        test_catalog_is_replaceable_projection()
        test_catalog_rejects_invalid_or_duplicate_entries()
        print("PASS test_runtime_project_catalog")
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-task-hot-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from stores import task_store  # noqa: E402


def main() -> None:
    work = tempfile.mkdtemp(prefix="bc-test-task-hot-work-")
    original_loads = task_store.json.loads
    loads = {"count": 0}

    def counted_loads(*args, **kwargs):
        loads["count"] += 1
        return original_loads(*args, **kwargs)

    task_store.json.loads = counted_loads
    try:
        cwd = str(Path(work).resolve())
        created = task_store.create(
            cwd=cwd,
            name="task",
            prompt="do work",
        )
        task_store._data_cache = None
        loads["count"] = 0

        first = task_store.list_for_project(cwd)
        second = task_store.list_for_project(cwd)
        if loads["count"] != 1:
            raise AssertionError(f"task store reparsed on hot reads: {loads['count']}")
        first[0]["name"] = "mutated"
        if second[0]["name"] != "task":
            raise AssertionError("task list returned shared mutable cache data")

        updated = task_store.update(created["id"], {"name": "updated"})
        if updated is None:
            raise AssertionError("task update failed")
        after_write_loads = loads["count"]
        third = task_store.get(created["id"])
        if loads["count"] != after_write_loads:
            raise AssertionError("task store reparsed after write refreshed cache")
        if third is None or third.get("name") != "updated":
            raise AssertionError(f"updated task not visible from cache: {third}")
    finally:
        task_store.json.loads = original_loads
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("PASS task store hot reads reuse parsed cache")

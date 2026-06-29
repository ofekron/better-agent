from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-project-hot-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import project_store  # noqa: E402


def main() -> None:
    work = tempfile.mkdtemp(prefix="bc-test-project-hot-work-")
    original_read = project_store._read_legacy_deletions
    reads = {"count": 0}

    def counted_read():
        reads["count"] += 1
        return original_read()

    project_store._read_legacy_deletions = counted_read
    try:
        one = Path(work, "one")
        two = Path(work, "two")
        project_store.add_project(str(one))
        project_store.add_project(str(two))
        project_store.remove_project(str(one))

        reads["count"] = 0
        project_store.list_projects()
        project_store.list_projects()
        if reads["count"] != 1:
            raise AssertionError(f"legacy deletions reparsed on hot reads: {reads['count']}")

        project_store.add_project(str(one))
        after_write_reads = reads["count"]
        if after_write_reads <= 1:
            raise AssertionError("legacy deletion write did not refresh projection")
        project_store.list_projects()
        if reads["count"] != after_write_reads:
            raise AssertionError("legacy deletions reparsed after post-write hot read")
    finally:
        project_store._read_legacy_deletions = original_read
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("PASS project store hot reads reuse deletion projection")

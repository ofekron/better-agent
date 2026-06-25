from __future__ import annotations

import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-project-mapping-hot-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import project_mapping_store  # noqa: E402


def main() -> None:
    original_read_json = project_mapping_store.read_json
    reads = {"count": 0}

    def counted_read_json(*args, **kwargs):
        reads["count"] += 1
        return original_read_json(*args, **kwargs)

    project_mapping_store.read_json = counted_read_json
    try:
        project_mapping_store._write_file([
            {
                "group_id": "manual-1",
                "confidence": "manual",
                "label": "Manual",
                "members": [],
            }
        ])

        project_mapping_store._raw_cache = None
        reads["count"] = 0
        first = project_mapping_store.list_mappings()
        second = project_mapping_store.list_mappings()
        if reads["count"] != 1:
            raise AssertionError(f"mapping file reparsed on hot reads: {reads['count']}")
        first[0]["label"] = "mutated"
        if second[0]["label"] != "Manual":
            raise AssertionError("list_mappings returned shared mutable cache data")

        project_mapping_store.update_group("manual-1", label="Updated")
        after_write_reads = reads["count"]
        third = project_mapping_store.list_mappings()
        if reads["count"] != after_write_reads:
            raise AssertionError("mapping file reparsed after write refreshed cache")
        if third[0]["label"] != "Updated":
            raise AssertionError(f"updated mapping not visible from cache: {third}")
    finally:
        project_mapping_store.read_json = original_read_json
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("PASS project mapping hot reads reuse raw cache")

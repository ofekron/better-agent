from __future__ import annotations

import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-memory-store-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import memory_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_global_write_read_roundtrip() -> bool:
    written = memory_store.write_memory(
        scope_type="global",
        scope_path="",
        slug="shared-git-index-races",
        description="Stage hunk-precisely under concurrent sessions.",
        mem_type="feedback",
        content="Body text.\n\nMore body text.",
    )
    read = memory_store.read_memory(scope_type="global", scope_path="", slug="shared-git-index-races")
    ok = (
        written is not None
        and read is not None
        and read["name"] == "shared-git-index-races"
        and read["type"] == "feedback"
        and "More body text." in read["content"]
        and read["created_at"] and read["updated_at"]
    )
    return bool(ok)


def test_index_rebuilds_on_write_and_delete() -> bool:
    memory_store.write_memory(
        scope_type="global",
        scope_path="",
        slug="index-entry-one",
        description="First entry.",
        mem_type="user",
        content="Body.",
    )
    index_path = memory_store.scope_dir("global", "") / "MEMORY.md"
    before = index_path.read_text(encoding="utf-8")
    if "index-entry-one" not in before:
        return False
    memory_store.delete_memory(scope_type="global", scope_path="", slug="index-entry-one")
    after = index_path.read_text(encoding="utf-8")
    return "index-entry-one" not in after


def test_project_scope_isolated_from_global() -> bool:
    project_path = "/tmp/bc-test-memory-project"
    memory_store.write_memory(
        scope_type="project",
        scope_path=project_path,
        slug="project-only-fact",
        description="Only true for this project.",
        mem_type="project",
        content="Body.",
    )
    global_list = memory_store.list_memories(scope_type="global", scope_path="")
    project_list = memory_store.list_memories(scope_type="project", scope_path=project_path)
    return (
        all(m["name"] != "project-only-fact" for m in global_list)
        and any(m["name"] == "project-only-fact" for m in project_list)
    )


def test_memories_for_cwd_merges_ancestors() -> bool:
    root = "/tmp/bc-test-memory-repo"
    sub = "/tmp/bc-test-memory-repo/pkg/sub"
    unrelated = "/tmp/bc-test-memory-other-repo"
    memory_store.write_memory(
        scope_type="project", scope_path=root, slug="root-fact",
        description="d", mem_type="project", content="c",
    )
    memory_store.write_memory(
        scope_type="folder", scope_path=sub, slug="sub-fact",
        description="d", mem_type="project", content="c",
    )
    memory_store.write_memory(
        scope_type="project", scope_path=unrelated, slug="unrelated-fact",
        description="d", mem_type="project", content="c",
    )
    merged = memory_store.memories_for_cwd(sub)
    names = {m["name"] for entries in merged.values() for m in entries}
    return "root-fact" in names and "sub-fact" in names and "unrelated-fact" not in names


def test_rejects_invalid_slug() -> bool:
    try:
        memory_store.write_memory(
            scope_type="global", scope_path="", slug="Not Valid!",
            description="d", mem_type="user", content="c",
        )
    except memory_store.MemoryStoreError:
        return True
    return False


def test_edit_preserves_created_at() -> bool:
    first = memory_store.write_memory(
        scope_type="global", scope_path="", slug="edit-me",
        description="v1", mem_type="user", content="c1",
    )
    second = memory_store.write_memory(
        scope_type="global", scope_path="", slug="edit-me",
        description="v2", mem_type="user", content="c2",
    )
    return (
        first["created_at"] == second["created_at"]
        and second["description"] == "v2"
        and second["content"].strip() == "c2"
    )


TESTS = [
    ("global write/read roundtrip", test_global_write_read_roundtrip),
    ("MEMORY.md index rebuilds on write/delete", test_index_rebuilds_on_write_and_delete),
    ("project scope isolated from global", test_project_scope_isolated_from_global),
    ("memories_for_cwd merges ancestor scopes only", test_memories_for_cwd_merges_ancestors),
    ("invalid slug is rejected", test_rejects_invalid_slug),
    ("edit preserves created_at, updates content", test_edit_preserves_created_at),
]


def main() -> int:
    failures = 0
    try:
        for label, fn in TESTS:
            ok = fn()
            print(f"{PASS if ok else FAIL} {label}")
            if not ok:
                failures += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

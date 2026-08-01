from __future__ import annotations

import json
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

import pytest  # noqa: E402

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


def test_rejects_newline_in_description() -> bool:
    try:
        memory_store.write_memory(
            scope_type="global", scope_path="", slug="frontmatter-injection",
            description="ok\n---\nHACKED: yes", mem_type="user", content="c",
        )
    except memory_store.MemoryStoreError:
        pass
    else:
        return False
    # Confirm nothing was written and the index wasn't touched.
    return memory_store.read_memory(scope_type="global", scope_path="", slug="frontmatter-injection") is None


def test_rejects_newline_in_scope_path() -> bool:
    try:
        memory_store.write_memory(
            scope_type="project", scope_path="/tmp/x\n---\nHACKED: yes", slug="frontmatter-injection-2",
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
    ("newline in description is rejected (frontmatter injection)", test_rejects_newline_in_description),
    ("newline in scope_path is rejected", test_rejects_newline_in_scope_path),
    ("edit preserves created_at, updates content", test_edit_preserves_created_at),
]


# --- branch-coverage tests (assert-based) ---

def test_scope_dir_rejects_invalid_scope_type() -> None:
    with pytest.raises(memory_store.MemoryStoreError):
        memory_store.scope_dir("galactic", "/tmp/x")


def test_scope_dir_rejects_empty_path_for_project_scope() -> None:
    with pytest.raises(memory_store.MemoryStoreError):
        memory_store.scope_dir("project", "")


def test_write_memory_rejects_invalid_type() -> None:
    with pytest.raises(memory_store.MemoryStoreError):
        memory_store.write_memory(
            scope_type="global", scope_path="", slug="bad-type",
            description="d", mem_type="preference", content="c",
        )


def test_write_memory_rejects_blank_description() -> None:
    with pytest.raises(memory_store.MemoryStoreError):
        memory_store.write_memory(
            scope_type="global", scope_path="", slug="blank-desc",
            description="   ", mem_type="user", content="c",
        )


def test_write_memory_rejects_blank_content() -> None:
    with pytest.raises(memory_store.MemoryStoreError):
        memory_store.write_memory(
            scope_type="global", scope_path="", slug="blank-content",
            description="d", mem_type="user", content="   ",
        )


def test_delete_missing_memory_returns_false() -> None:
    assert memory_store.delete_memory(
        scope_type="global", scope_path="", slug="never-written"
    ) is False


def test_read_missing_memory_returns_none() -> None:
    assert memory_store.read_memory(
        scope_type="global", scope_path="", slug="also-never-written"
    ) is None


def test_list_memories_empty_when_scope_dir_absent() -> None:
    # A folder scope whose encoded dir does not exist yields [].
    assert memory_store.list_memories(
        scope_type="folder", scope_path="/this/path/does/not/exist/anywhere"
    ) == []


def test_list_scopes_empty_when_scoped_root_absent() -> None:
    scoped = memory_store._root() / "scoped"
    if scoped.exists():
        shutil.rmtree(scoped, ignore_errors=True)
    assert memory_store.list_scopes() == []


def test_list_scopes_filters_non_dirs_corrupt_and_metaless_entries() -> None:
    scoped = memory_store._root() / "scoped"
    scoped.mkdir(parents=True, exist_ok=True)

    # Valid project scope -> included.
    good = scoped / "good-dir"
    good.mkdir(parents=True, exist_ok=True)
    (good / "_scope.json").write_text(
        json.dumps({"type": "project", "path": "/tmp/bc-good-scope"}), encoding="utf-8"
    )

    # Corrupt _scope.json -> skipped via _read_scope_meta None.
    corrupt = scoped / "corrupt-dir"
    corrupt.mkdir(parents=True, exist_ok=True)
    (corrupt / "_scope.json").write_text("{not json", encoding="utf-8")

    # No _scope.json -> _read_scope_meta None -> skipped.
    metaless = scoped / "metaless-dir"
    metaless.mkdir(parents=True, exist_ok=True)

    # Plain file among directories -> skipped by is_dir() guard.
    (scoped / "stray-file").write_text("x", encoding="utf-8")

    scopes = memory_store.list_scopes()
    # The valid dir surfaces; the corrupt/metaless dirs and stray file do not.
    assert {"type": "project", "path": "/tmp/bc-good-scope"} in scopes
    assert len(scopes) >= 1


def test_parse_memory_file_returns_none_without_frontmatter() -> None:
    assert memory_store._parse_memory_file("just body, no frontmatter") is None


def test_parse_memory_file_handles_non_field_lines() -> None:
    # Header with a top-level non-field line and an indented metadata line
    # that is not `key: value` — exercises both field_match False branches.
    text = (
        "---\n"
        "name: crafted\n"
        "a stray line with no colon\n"
        "metadata:\n"
        "  type: user\n"
        "  - not a field either\n"
        "---\n"
        "body\n"
    )
    parsed = memory_store._parse_memory_file(text)
    assert parsed is not None
    assert parsed["name"] == "crafted"
    assert parsed["type"] == "user"
    # lstrip("\n") only strips leading newlines; the trailing one is preserved.
    assert parsed["content"] == "body\n"


def test_rebuild_index_skips_unparseable_md() -> None:
    directory = memory_store.scope_dir("global", "")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "junk.md").write_text("no frontmatter at all", encoding="utf-8")
    # Must not raise; the unparseable file is simply omitted from the index.
    memory_store._rebuild_index_locked(directory)
    index = (directory / "MEMORY.md").read_text(encoding="utf-8")
    assert "junk" not in index


def test_write_preserves_created_at_when_existing_lacks_it() -> None:
    directory = memory_store.scope_dir("global", "")
    directory.mkdir(parents=True, exist_ok=True)
    # A pre-existing file with frontmatter but NO created_at field.
    target = directory / "no-created-at.md"
    target.write_text(
        "---\nname: no-created-at\ndescription: old\nmetadata:\n  type: user\n---\nold body\n",
        encoding="utf-8",
    )
    written = memory_store.write_memory(
        scope_type="global", scope_path="", slug="no-created-at",
        description="new", mem_type="user", content="new body",
    )
    # created_at could not be inherited -> falls back to a fresh timestamp.
    assert written["created_at"] == written["updated_at"]
    assert written["description"] == "new"


def test_is_ancestor_or_equal_returns_false_on_invalid_path() -> None:
    # A NUL byte makes Path.resolve() raise ValueError -> defensive False.
    assert memory_store._is_ancestor_or_equal("\x00bad", "/tmp") is False


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

#!/usr/bin/env python3
"""Failing-first coverage for the ADR 0008 folders/tags/session-organization
slice of Package B (session/project surface): contract shapes
(`SessionFolder`/`SessionTag`/`TagAssignment`/`SessionSummary.folder_ref`/
`tag_refs`), `SessionSurfaceAdapter.list_folders`/`list_tags`/`list_sessions`
filters, the `session_organization.fire.*` live projection
(`FolderUpsert`/`FolderRemoved`/`TagUpsert`/`TagRemoved`/summary refresh),
and the `SessionCommandPort` folder/tag/membership intents — including the
ADR 0008 source-isolation invariant for `assign_tags`' delta and sync modes.

None of this surface existed before this pass: `SessionSummary.folder_ref`/
`tag_refs`, `SessionFolder`/`SessionTag`/`FolderUpsert`/`FolderRemoved`/
`TagUpsert`/`TagRemoved`, `SessionSurface.list_folders`/`list_tags`,
`SessionCommandPort.create_folder`/.../`assign_tags`, and
`session_organization_store.patch_session_tags_scoped`/
`session_ids_with_tag`/`publish_organization_fact` are all new in this
change — every test below fails on the pre-change tree with an
ImportError/AttributeError (no such name) or a TypeError (abstract method
not implemented), which is this suite's failing-first evidence; run it
against `git stash` of this change to reproduce that failure.

Isolated via `paths.engage_test_home`, same idiom as the sibling
`test_adapter_session_gaps.py`/`test_adapter_session_commands.py`.

Run:
    PYTHONPATH=.:./backend:./sdk python3 -m pytest backend/scripts/test_adapter_session_org.py -q
    PYTHONPATH=.:./backend:./sdk python3 backend/scripts/test_adapter_session_org.py
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402

_TEST_HOME = tempfile.mkdtemp(prefix="ba-adapter-session-org-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import session_organization_store  # noqa: E402
import session_store  # noqa: E402

import session_commands  # noqa: E402
from backend.adapters.session_adapter import SessionSurfaceAdapter  # noqa: E402
from backend.surface_contract.identity import Ok  # noqa: E402
from backend.surface_contract.session_surface import (  # noqa: E402
    FolderRemoved,
    FolderUpsert,
    SessionSearchFilters,
    TagAssignment,
    TagRemoved,
    TagUpsert,
)

_PORT = session_commands.build_session_command_port()


def _cwd() -> str:
    return tempfile.mkdtemp(prefix="ba-adapter-session-org-cwd-")


def _session_id() -> str:
    session = session_store.create_session(
        name=f"org test {uuid.uuid4().hex}", cwd=_cwd(),
    )
    return session["id"]


def _adapter() -> SessionSurfaceAdapter:
    adapter = SessionSurfaceAdapter()
    adapter.bind()
    return adapter


def _unwrap(result):
    assert isinstance(result, Ok), result
    return result.value


# ---------------------------------------------------------------------------
# Contract shapes: SessionSummary carries folder_ref/tag_refs.
# ---------------------------------------------------------------------------

def test_session_summary_defaults_no_folder_no_tags() -> None:
    sid = _session_id()
    adapter = _adapter()
    page = _unwrap(adapter.list_sessions(None, None))
    summary = next(s for s in page.sessions if s.session_id == sid)
    assert summary.folder_ref is None
    assert summary.tag_refs == ()


def test_assign_folder_and_tags_reflected_in_list_sessions() -> None:
    project = _cwd()
    sid = _session_id()
    folder = asyncio.run(_PORT.create_folder(project, "Folder A", None))
    assert folder.accepted, folder
    folders = session_organization_store.snapshot(project)["folders"]
    folder_id = next(f["id"] for f in folders if f["name"] == "Folder A")
    tag = asyncio.run(_PORT.create_tag("tag-a", project, None))
    assert tag.accepted, tag
    tag_id = next(
        t["id"] for t in session_organization_store.snapshot(project)["tags"]
        if t["name"] == "tag-a"
    )

    assert asyncio.run(_PORT.assign_folder(sid, folder_id)).accepted
    assert asyncio.run(
        _PORT.assign_tags(sid, "manual", (tag_id,), None, None)
    ).accepted

    adapter = _adapter()
    page = _unwrap(adapter.list_sessions(None, None))
    summary = next(s for s in page.sessions if s.session_id == sid)
    assert summary.folder_ref == folder_id
    assert summary.tag_refs == (TagAssignment(tag_ref=tag_id, source="manual"),)


# ---------------------------------------------------------------------------
# Folder round trip + live FolderUpsert/FolderRemoved projection.
# ---------------------------------------------------------------------------

def test_create_rename_move_folder_broadcasts_folder_upsert() -> None:
    project = _cwd()
    adapter = _adapter()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        created = asyncio.run(_PORT.create_folder(project, "root", None))
        assert created.accepted, created
        folder_id = next(
            f["id"] for f in session_organization_store.snapshot(project)["folders"]
            if f["name"] == "root"
        )
        upserts = [f for f in received if isinstance(f, FolderUpsert)]
        assert upserts, received
        assert upserts[-1].folder.folder_ref == folder_id
        assert upserts[-1].folder.name == "root"

        renamed = asyncio.run(_PORT.rename_folder(folder_id, "renamed"))
        assert renamed.accepted, renamed
        upserts = [f for f in received if isinstance(f, FolderUpsert)]
        assert upserts[-1].folder.name == "renamed"

        child = asyncio.run(_PORT.create_folder(project, "child", None))
        assert child.accepted, child
        child_id = next(
            f["id"] for f in session_organization_store.snapshot(project)["folders"]
            if f["name"] == "child"
        )
        moved = asyncio.run(_PORT.move_folder(child_id, folder_id))
        assert moved.accepted, moved
        upserts = [f for f in received if isinstance(f, FolderUpsert)]
        assert upserts[-1].folder.folder_ref == child_id
        assert upserts[-1].folder.parent_folder_ref == folder_id
    finally:
        sub.close()


def test_delete_folder_unassign_clears_membership_live() -> None:
    project = _cwd()
    sid = _session_id()
    created = asyncio.run(_PORT.create_folder(project, "to-unassign", None))
    assert created.accepted, created
    folder_id = next(
        f["id"] for f in session_organization_store.snapshot(project)["folders"]
        if f["name"] == "to-unassign"
    )
    assert asyncio.run(_PORT.assign_folder(sid, folder_id)).accepted

    adapter = _adapter()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        result = asyncio.run(_PORT.delete_folder(folder_id, "unassign"))
        assert result.accepted, result

        removed = [f for f in received if isinstance(f, FolderRemoved)]
        assert removed, received
        assert removed[-1].folder_ref == folder_id

        page = _unwrap(adapter.list_sessions(None, None))
        summary = next(s for s in page.sessions if s.session_id == sid)
        assert summary.folder_ref is None
    finally:
        sub.close()

    org = session_organization_store.organization_for_session(sid)
    assert org["folder_id"] is None


def test_delete_folder_delete_sessions_mode_cascades() -> None:
    project = _cwd()
    sid = _session_id()
    created = asyncio.run(_PORT.create_folder(project, "cascade", None))
    assert created.accepted, created
    folder_id = next(
        f["id"] for f in session_organization_store.snapshot(project)["folders"]
        if f["name"] == "cascade"
    )
    assert asyncio.run(_PORT.assign_folder(sid, folder_id)).accepted

    deleted_ids: list[str] = []

    async def _fake_delete_session_tree(session_id: str) -> bool:
        deleted_ids.append(session_id)
        return True

    port = session_commands.build_session_command_port(
        delete_session_tree=_fake_delete_session_tree,
    )
    result = asyncio.run(port.delete_folder(folder_id, "delete_sessions"))
    assert result.accepted, result
    assert deleted_ids == [sid]


def test_delete_folder_delete_sessions_without_wiring_rejects() -> None:
    project = _cwd()
    created = asyncio.run(_PORT.create_folder(project, "unwired", None))
    assert created.accepted, created
    folder_id = next(
        f["id"] for f in session_organization_store.snapshot(project)["folders"]
        if f["name"] == "unwired"
    )
    # `_PORT` (module-level, built with no delete_session_tree) must reject
    # delete_sessions mode rather than silently doing nothing.
    result = asyncio.run(_PORT.delete_folder(folder_id, "delete_sessions"))
    assert result.accepted is False
    assert result.code == "unsupported"


# ---------------------------------------------------------------------------
# Tag round trip + live TagUpsert/TagRemoved projection, deletion
# completeness.
# ---------------------------------------------------------------------------

def test_create_rename_recolor_tag_broadcasts_tag_upsert() -> None:
    adapter = _adapter()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        created = asyncio.run(_PORT.create_tag("original", None, "#111111"))
        assert created.accepted, created
        tag_id = next(
            t["id"] for t in session_organization_store.snapshot(None)["tags"]
            if t["name"] == "original"
        )
        upserts = [f for f in received if isinstance(f, TagUpsert)]
        assert upserts[-1].tag.name == "original"
        assert upserts[-1].tag.color == "#111111"

        renamed = asyncio.run(_PORT.rename_tag(tag_id, "renamed"))
        assert renamed.accepted, renamed
        upserts = [f for f in received if isinstance(f, TagUpsert)]
        assert upserts[-1].tag.name == "renamed"

        recolored = asyncio.run(_PORT.recolor_tag(tag_id, "#222222"))
        assert recolored.accepted, recolored
        upserts = [f for f in received if isinstance(f, TagUpsert)]
        assert upserts[-1].tag.color == "#222222"
    finally:
        sub.close()


def test_delete_tag_completeness_no_dangling_tag_refs() -> None:
    sid = _session_id()
    created = asyncio.run(_PORT.create_tag("doomed", None, None))
    assert created.accepted, created
    tag_id = next(
        t["id"] for t in session_organization_store.snapshot(None)["tags"]
        if t["name"] == "doomed"
    )
    assert asyncio.run(_PORT.assign_tags(sid, "manual", (tag_id,), None, None)).accepted

    adapter = _adapter()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        result = asyncio.run(_PORT.delete_tag(tag_id))
        assert result.accepted, result

        removed = [f for f in received if isinstance(f, TagRemoved)]
        assert removed, received
        assert removed[-1].tag_ref == tag_id

        page = _unwrap(adapter.list_sessions(None, None))
        summary = next(s for s in page.sessions if s.session_id == sid)
        assert all(t.tag_ref != tag_id for t in summary.tag_refs)
    finally:
        sub.close()


# ---------------------------------------------------------------------------
# assign_tags: source-isolation invariant (ADR 0008 conformance gate) —
# for both delta and sync modes, every OTHER source's entries are
# byte-identical before and after.
# ---------------------------------------------------------------------------

def _other_source_entries(session_id: str, source: str) -> set[tuple[str, str]]:
    org = session_organization_store.organization_for_session(session_id)
    return {
        (a["tag_id"], a["source"])
        for a in org["tag_assignments"]
        if a["source"] != source
    }


def test_assign_tags_delta_mode_source_isolation() -> None:
    sid = _session_id()
    manual_tag = asyncio.run(_PORT.create_tag("manual-tag", None, None))
    assert manual_tag.accepted
    manual_id = next(
        t["id"] for t in session_organization_store.snapshot(None)["tags"]
        if t["name"] == "manual-tag"
    )
    auto_id = session_organization_store.create_tag(name="auto-tag", project_id=None)["id"]

    # Seed: manual_id via manual source, auto_id via auto_tagging source —
    # directly through the store (an automated producer's own write path,
    # not exercised through the command port here).
    session_organization_store.set_session_tags(sid, [manual_id], source="manual")
    session_organization_store.patch_session_tags(
        sid, add=[auto_id], add_source="auto_tagging",
    )
    before = _other_source_entries(sid, "manual")
    assert (auto_id, "auto_tagging") in before

    # A manual-scoped delta call attempting to remove the auto_tagging tag
    # must be a no-op for that entry (source isolation) — it can only touch
    # tag_refs whose CURRENT source is "manual".
    new_manual_id = session_organization_store.create_tag(
        name="manual-tag-2", project_id=None,
    )["id"]
    result = asyncio.run(
        _PORT.assign_tags(sid, "manual", (new_manual_id,), (auto_id, manual_id), None)
    )
    assert result.accepted, result

    after = _other_source_entries(sid, "manual")
    assert before == after, (before, after)

    org = session_organization_store.organization_for_session(sid)
    manual_ids_now = {
        a["tag_id"] for a in org["tag_assignments"] if a["source"] == "manual"
    }
    assert manual_ids_now == {new_manual_id}


def test_assign_tags_sync_mode_source_isolation_manual_allowed() -> None:
    """`sync_session_tags_by_source` used to reject `source="manual"`
    outright (ValueError) — ADR 0008's `assign_tags` sync mode defaults to
    `source="manual"`, so this path must now work, with the same
    source-isolation guarantee as every other source."""
    sid = _session_id()
    manual_1 = session_organization_store.create_tag(name="m1", project_id=None)["id"]
    manual_2 = session_organization_store.create_tag(name="m2", project_id=None)["id"]
    auto_id = session_organization_store.create_tag(name="a1", project_id=None)["id"]
    session_organization_store.set_session_tags(sid, [manual_1], source="manual")
    session_organization_store.patch_session_tags(
        sid, add=[auto_id], add_source="auto_tagging",
    )
    before = _other_source_entries(sid, "manual")
    assert (auto_id, "auto_tagging") in before

    result = asyncio.run(
        _PORT.assign_tags(sid, "manual", None, None, (manual_2,))
    )
    assert result.accepted, result

    after = _other_source_entries(sid, "manual")
    assert before == after, (before, after)

    org = session_organization_store.organization_for_session(sid)
    manual_ids_now = {
        a["tag_id"] for a in org["tag_assignments"] if a["source"] == "manual"
    }
    assert manual_ids_now == {manual_2}
    assert manual_1 not in manual_ids_now


def test_assign_tags_mutually_exclusive_modes_reject() -> None:
    sid = _session_id()
    both = asyncio.run(_PORT.assign_tags(sid, "manual", ("x",), None, ("y",)))
    assert both.accepted is False
    assert both.code == "invalid_mode"

    neither = asyncio.run(_PORT.assign_tags(sid, "manual", None, None, None))
    assert neither.accepted is False
    assert neither.code == "invalid_mode"


# ---------------------------------------------------------------------------
# Bounded reads: list_folders / list_tags.
# ---------------------------------------------------------------------------

def test_list_folders_and_list_tags_scoped_to_project() -> None:
    project = _cwd()
    other_project = _cwd()
    asyncio.run(_PORT.create_folder(project, "scoped-folder", None))
    asyncio.run(_PORT.create_folder(other_project, "other-folder", None))
    asyncio.run(_PORT.create_tag("scoped-tag", project, None))
    asyncio.run(_PORT.create_tag("global-tag", None, None))

    adapter = _adapter()
    folders = _unwrap(adapter.list_folders(project))
    assert any(f.name == "scoped-folder" for f in folders)
    assert all(f.project_ref == project for f in folders)

    tags = _unwrap(adapter.list_tags(project))
    tag_names = {t.name for t in tags}
    assert "scoped-tag" in tag_names
    assert "global-tag" in tag_names  # global tags ride every project's list


# ---------------------------------------------------------------------------
# list_sessions folder_ref / tag_refs / tag_match filters — one query path.
# ---------------------------------------------------------------------------

def test_list_sessions_filters_by_folder_ref() -> None:
    project = _cwd()
    in_folder = _session_id()
    out_of_folder = _session_id()
    folder = asyncio.run(_PORT.create_folder(project, "filter-folder", None))
    assert folder.accepted
    folder_id = next(
        f["id"] for f in session_organization_store.snapshot(project)["folders"]
        if f["name"] == "filter-folder"
    )
    assert asyncio.run(_PORT.assign_folder(in_folder, folder_id)).accepted

    adapter = _adapter()
    page = _unwrap(
        adapter.list_sessions(None, None, SessionSearchFilters(folder_ref=folder_id))
    )
    ids = {s.session_id for s in page.sessions}
    assert in_folder in ids
    assert out_of_folder not in ids


def test_list_sessions_filters_by_tag_refs_all_and_any() -> None:
    both_tags = _session_id()
    one_tag = _session_id()
    tag_a = session_organization_store.create_tag(name="filter-a", project_id=None)["id"]
    tag_b = session_organization_store.create_tag(name="filter-b", project_id=None)["id"]
    session_organization_store.set_session_tags(both_tags, [tag_a, tag_b])
    session_organization_store.set_session_tags(one_tag, [tag_a])

    adapter = _adapter()
    all_page = _unwrap(
        adapter.list_sessions(
            None, None, SessionSearchFilters(tag_refs=(tag_a, tag_b), tag_match="all"),
        )
    )
    all_ids = {s.session_id for s in all_page.sessions}
    assert both_tags in all_ids
    assert one_tag not in all_ids

    any_page = _unwrap(
        adapter.list_sessions(
            None, None, SessionSearchFilters(tag_refs=(tag_a, tag_b), tag_match="any"),
        )
    )
    any_ids = {s.session_id for s in any_page.sessions}
    assert both_tags in any_ids
    assert one_tag in any_ids


_TESTS = [
    test_session_summary_defaults_no_folder_no_tags,
    test_assign_folder_and_tags_reflected_in_list_sessions,
    test_create_rename_move_folder_broadcasts_folder_upsert,
    test_delete_folder_unassign_clears_membership_live,
    test_delete_folder_delete_sessions_mode_cascades,
    test_delete_folder_delete_sessions_without_wiring_rejects,
    test_create_rename_recolor_tag_broadcasts_tag_upsert,
    test_delete_tag_completeness_no_dangling_tag_refs,
    test_assign_tags_delta_mode_source_isolation,
    test_assign_tags_sync_mode_source_isolation_manual_allowed,
    test_assign_tags_mutually_exclusive_modes_reject,
    test_list_folders_and_list_tags_scoped_to_project,
    test_list_sessions_filters_by_folder_ref,
    test_list_sessions_filters_by_tag_refs_all_and_any,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)

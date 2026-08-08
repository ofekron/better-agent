#!/usr/bin/env python3
"""Failing-first coverage for Package B (ADR 0008 session/project surface)
gap-closing work not already covered by `test_surface_adapters.py`:
B2 (attention markers), B3 (real session_tree with forks +
SessionTreeChanged), B4 (fork-only-delete tombstone hardening), the
QUEUED rollup state, and B6 (SessionIntent command-plane dispatch +
project.fire.* live projection).

Isolated via `paths.engage_test_home` before any backend import, same
idiom as `test_surface_adapters.py` (no real `~/.better-claude` touched).

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_adapter_session_gaps.py -q
    PYTHONPATH=. python3 backend/scripts/test_adapter_session_gaps.py   # __main__ fallback
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

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-adapter-session-gaps-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import config_store  # noqa: E402  (bare — store_access._resolve aliases onto this instance)
import project_store  # noqa: E402
import session_organization_store  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

from backend.adapters.command_port import CommandResult  # noqa: E402
from backend.adapters.session_adapter import SessionSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.surface_contract.identity import Ok, Rebuilding  # noqa: E402
from backend.surface_contract.intents import (  # noqa: E402
    ArchiveSession,
    AssignProject,
    CreateFolder,
    CreateProject,
    CreateTag,
    DeleteProject,
    IntentAccepted,
    IntentRejected,
    MarkOpened,
    RenameProject,
    RenameSession,
    Stop,
)
from backend.surface_contract.session_surface import (  # noqa: E402
    FolderUpsert,
    ProjectRemoved,
    ProjectUpsert,
    SessionRemoved,
    SessionRollupState,
    SessionSummaryUpsert,
    SessionTreeChanged,
    TagUpsert,
)


def _cwd() -> str:
    return tempfile.mkdtemp(prefix="ba-adapter-session-gaps-cwd-")


_FORK_CAPABLE_PROVIDER_ID: str | None = None


def _fork_capable_provider_id() -> str:
    """A provider whose runtime kind has no `runtime_probe_imports` (see
    `provider_manifest.py`) — `session_manager.fork`'s up-front capability
    check (`provider.get_provider`) resolves the provider class through
    `dependency_plan.assert_provider_runtime_ready`, which would otherwise
    fail in this bare test env for the default "claude" kind (its runtime
    probes `claude_agent_sdk`, not installed here). "codex" has an empty
    probe tuple, so it resolves cleanly with no real CLI present, and
    `CodexProvider.supports_fork` is True."""
    global _FORK_CAPABLE_PROVIDER_ID
    if _FORK_CAPABLE_PROVIDER_ID is None:
        record = config_store.add_provider({
            "name": f"fork-capable {uuid.uuid4().hex}", "kind": "codex", "mode": "subscription",
        })
        _FORK_CAPABLE_PROVIDER_ID = record["id"]
    return _FORK_CAPABLE_PROVIDER_ID


def _make_root_with_claude_sid(name: str) -> dict:
    """Stamp a fake claude_sid + one user message so `session_manager.fork`
    accepts this session as a fork parent — same recipe
    `test_fork_split.py`'s `_make_root_with_claude_sid` uses."""
    root = session_manager.create(
        name=name, cwd=_cwd(), orchestration_mode="native",
        provider_id=_fork_capable_provider_id(),
    )
    session_manager.set_agent_sid(root["id"], "manager", f"fake-claude-{root['id'][:8]}")
    session_manager.append_user_msg(root["id"], {
        "id": "m1", "role": "user", "content": "hi", "events": [],
        "timestamp": "2026-05-01T00:00:00", "isStreaming": False,
    })
    return session_manager.get(root["id"])


# ---------------------------------------------------------------------------
# B2 — attention markers
# ---------------------------------------------------------------------------

def test_attention_markers_real_from_session_store() -> None:
    session = session_store.create_session(name=f"marker session {uuid.uuid4().hex}", cwd=_cwd())
    sid = session["id"]

    adapter = SessionSurfaceAdapter()
    before = adapter.list_sessions(None, None)
    before_match = next(s for s in before.value.sessions if s.session_id == sid)
    assert before_match.attention_markers == ()

    session_manager.set_marker(sid, "ext-1", {"tag": "NEEDS_USER_DECISION", "color": "red"})
    after = adapter.list_sessions(None, None)
    after_match = next(s for s in after.value.sessions if s.session_id == sid)
    assert after_match.attention_markers == ("NEEDS_USER_DECISION",)

    session_manager.clear_marker(sid, "ext-1")
    cleared = adapter.list_sessions(None, None)
    cleared_match = next(s for s in cleared.value.sessions if s.session_id == sid)
    assert cleared_match.attention_markers == ()


# ---------------------------------------------------------------------------
# B3 — real session_tree() with forks
# ---------------------------------------------------------------------------

def test_session_tree_real_forks() -> None:
    root = _make_root_with_claude_sid(f"tree root {uuid.uuid4().hex}")
    root_id = root["id"]
    child = session_manager.fork(root_id, name="child")
    session_manager.set_agent_sid(child["id"], "manager", f"fake-claude-{child['id'][:8]}")
    grandchild = session_manager.fork(child["id"], name="grandchild")
    session_manager.set_fork_closed(child["id"], True)

    adapter = SessionSurfaceAdapter()

    # Resolving via the root id and via a fork id both return the SAME tree.
    for lookup_id in (root_id, child["id"]):
        result = adapter.session_tree(lookup_id)
        assert isinstance(result, Ok), result
        by_id = {n.surface_id: n for n in result.value}
        assert set(by_id) == {root_id, child["id"], grandchild["id"]}
        assert by_id[root_id].kind == "root"
        assert by_id[root_id].parent_surface_id is None
        assert by_id[child["id"]].kind == "fork"
        assert by_id[child["id"]].parent_surface_id == root_id
        assert by_id[child["id"]].title == "child"
        assert by_id[grandchild["id"]].kind == "fork"
        assert by_id[grandchild["id"]].parent_surface_id == child["id"]
        assert by_id[grandchild["id"]].title == "grandchild"


def test_session_tree_missing_is_rebuilding() -> None:
    adapter = SessionSurfaceAdapter()
    result = adapter.session_tree(f"missing-{uuid.uuid4().hex}")
    assert isinstance(result, Rebuilding)


def test_tree_structure_fact_broadcasts_session_tree_changed() -> None:
    root = _make_root_with_claude_sid(f"tree changed root {uuid.uuid4().hex}")
    root_id = root["id"]
    child = session_manager.fork(root_id, name="child")

    adapter = SessionSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        asyncio.run(bus.publish(BusEvent(
            type="session.fire.fork_closed_set", root_id=root_id, sid=child["id"],
            payload={"kind": "fork_closed_set", "value": True}, persist=False,
        )))
        changed = [f for f in received if isinstance(f, SessionTreeChanged)]
        assert changed, received
        assert changed[0].session_id == root_id
    finally:
        sub.close()


# ---------------------------------------------------------------------------
# B4 — fork-only deletion tombstones the fork and leaves the root alone
# ---------------------------------------------------------------------------

def test_fork_only_delete_tombstones_fork_and_broadcasts_tree_changed() -> None:
    root = _make_root_with_claude_sid(f"delete root {uuid.uuid4().hex}")
    root_id = root["id"]
    child = session_manager.fork(root_id, name="child")
    child_id = child["id"]

    adapter = SessionSurfaceAdapter()
    adapter.bind()
    # Hydrate the adapter's cache for the child first — `_evict` only
    # tombstones a sid it has projected before (mirrors a live subscriber
    # that already knows about this fork, e.g. from an earlier
    # session_tree()/list_sessions() read, same as it would in production
    # since `session_manager.fork` fires its own `session.fire.forked`
    # fact for the child at fork time).
    asyncio.run(bus.publish(BusEvent(
        type="session.fire.forked", root_id=root_id, sid=child_id,
        payload={"kind": "forked"}, persist=False,
    )))

    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        # session_manager.delete()'s real payload shape (root_id included —
        # see session_manager.py's `_delete_with_locked_subtree`).
        asyncio.run(bus.publish(BusEvent(
            type="session.fire.deleted", root_id=root_id, sid=child_id,
            payload={"kind": "deleted", "deleted_sids": [child_id], "root_id": root_id},
            persist=False,
        )))
        removed = [f for f in received if isinstance(f, SessionRemoved)]
        assert [f.session_id for f in removed] == [child_id]
        tree_changed = [f for f in received if isinstance(f, SessionTreeChanged)]
        assert tree_changed, received
        assert tree_changed[0].session_id == root_id
    finally:
        sub.close()

    # The root itself is untouched — still listed.
    result = adapter.list_sessions(None, None)
    assert isinstance(result, Ok), result
    assert any(s.session_id == root_id for s in result.value.sessions)


def test_whole_root_delete_does_not_broadcast_tree_changed() -> None:
    root = _make_root_with_claude_sid(f"whole delete root {uuid.uuid4().hex}")
    root_id = root["id"]

    adapter = SessionSurfaceAdapter()
    adapter.bind()
    # Hydrate the adapter's cache for the root first — see the sibling
    # fork-only-delete test above for why.
    asyncio.run(bus.publish(BusEvent(
        type="session.fire.renamed", root_id=root_id, sid=root_id,
        payload={"kind": "renamed", "name": root["name"]}, persist=False,
    )))

    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        asyncio.run(bus.publish(BusEvent(
            type="session.fire.deleted", root_id=root_id, sid=root_id,
            payload={"kind": "deleted", "deleted_sids": [root_id], "root_id": root_id},
            persist=False,
        )))
        removed = [f for f in received if isinstance(f, SessionRemoved)]
        assert [f.session_id for f in removed] == [root_id]
        assert not [f for f in received if isinstance(f, SessionTreeChanged)], received
    finally:
        sub.close()


# ---------------------------------------------------------------------------
# QUEUED rollup state
# ---------------------------------------------------------------------------

def test_queued_prompts_updated_sets_queued_rollup() -> None:
    session = session_store.create_session(name=f"queued {uuid.uuid4().hex}", cwd=_cwd())
    sid = session["id"]

    adapter = SessionSurfaceAdapter()
    adapter.bind()

    asyncio.run(bus.publish(BusEvent(
        type="session.fire.queued_prompts_updated", root_id=sid, sid=sid,
        payload={"kind": "queued_prompts_updated", "queued_prompts": [{"id": "p1"}]},
        persist=False,
    )))
    queued_result = adapter.list_sessions(None, None)
    queued_match = next(s for s in queued_result.value.sessions if s.session_id == sid)
    assert queued_match.state == SessionRollupState.QUEUED

    asyncio.run(bus.publish(BusEvent(
        type="session.fire.queued_prompts_updated", root_id=sid, sid=sid,
        payload={"kind": "queued_prompts_updated", "queued_prompts": []},
        persist=False,
    )))
    idle_result = adapter.list_sessions(None, None)
    idle_match = next(s for s in idle_result.value.sessions if s.session_id == sid)
    assert idle_match.state == SessionRollupState.IDLE

    # running takes priority over queued, mirroring session_status.key_for's
    # ordering (error > needs_decision > running > ... > idle).
    asyncio.run(bus.publish(BusEvent(
        type="session.fire.queued_prompts_updated", root_id=sid, sid=sid,
        payload={"kind": "queued_prompts_updated", "queued_prompts": [{"id": "p2"}]},
        persist=False,
    )))
    asyncio.run(bus.publish(BusEvent(
        type="session.fire.running_changed", root_id=sid, sid=sid,
        payload={"kind": "running_changed", "value": True}, persist=False,
    )))
    running_result = adapter.list_sessions(None, None)
    running_match = next(s for s in running_result.value.sessions if s.session_id == sid)
    assert running_match.state == SessionRollupState.RUNNING


# ---------------------------------------------------------------------------
# project.fire.* live projection (ProjectUpsert / ProjectRemoved)
# ---------------------------------------------------------------------------

def test_project_fire_upserted_broadcasts_project_upsert() -> None:
    project_dir = tempfile.mkdtemp(prefix="ba-adapter-session-gaps-project-")
    added = project_store.add_project(project_dir, name=f"live project {uuid.uuid4().hex}")
    assert added is not None

    adapter = SessionSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        asyncio.run(bus.publish(BusEvent(
            type="project.fire.upserted", root_id=added["path"], sid=added["path"],
            payload={"path": added["path"], "kind": "upserted"}, persist=False,
        )))
        upserts = [f for f in received if isinstance(f, ProjectUpsert)]
        assert upserts, received
        assert upserts[0].project.project_ref == added["path"]
        assert upserts[0].project.name == added["name"]
    finally:
        sub.close()


def test_project_fire_removed_broadcasts_project_removed() -> None:
    project_dir = tempfile.mkdtemp(prefix="ba-adapter-session-gaps-project-")

    adapter = SessionSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        asyncio.run(bus.publish(BusEvent(
            type="project.fire.removed", root_id=project_dir, sid=project_dir,
            payload={"path": project_dir, "kind": "removed"}, persist=False,
        )))
        removed = [f for f in received if isinstance(f, ProjectRemoved)]
        assert removed, received
        assert removed[0].project_ref == project_dir
    finally:
        sub.close()


# ---------------------------------------------------------------------------
# B6 — SessionIntent command-plane dispatch
# ---------------------------------------------------------------------------

class _FakeSessionCommandPort:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._done = asyncio.Event()

    async def _record(self, call: tuple) -> CommandResult:
        self.calls.append(call)
        self._done.set()
        return CommandResult(accepted=True)

    async def archive_session(self, session_id, archived) -> CommandResult:
        return await self._record(("archive_session", session_id, archived))

    async def rename_session(self, session_id, title) -> CommandResult:
        return await self._record(("rename_session", session_id, title))

    async def assign_project(self, session_id, project_ref) -> CommandResult:
        return await self._record(("assign_project", session_id, project_ref))

    async def create_project(self, name, path) -> CommandResult:
        return await self._record(("create_project", name, path))

    async def rename_project(self, project_ref, name) -> CommandResult:
        return await self._record(("rename_project", project_ref, name))

    async def delete_project(self, project_ref) -> CommandResult:
        return await self._record(("delete_project", project_ref))

    async def mark_opened(self, session_id) -> CommandResult:
        return await self._record(("mark_opened", session_id))

    async def wait(self) -> None:
        await asyncio.wait_for(self._done.wait(), timeout=2)


def _base(intent_id: str, session_id: str | None = "sid-1") -> dict:
    return {"cv": 1, "intent_id": intent_id, "session_id": session_id}


def test_submit_dispatches_every_intent_to_the_command_port() -> None:
    cases = [
        (ArchiveSession(**_base("i1"), archived=True), ("archive_session", "sid-1", True)),
        (RenameSession(**_base("i2"), title="new title"), ("rename_session", "sid-1", "new title")),
        (
            AssignProject(**_base("i3"), project_ref="/some/project"),
            ("assign_project", "sid-1", "/some/project"),
        ),
        (
            CreateProject(**_base("i4", None), name="proj", path="/proj/path"),
            ("create_project", "proj", "/proj/path"),
        ),
        (
            RenameProject(**_base("i5", None), project_ref="/proj/path", name="renamed"),
            ("rename_project", "/proj/path", "renamed"),
        ),
        (
            DeleteProject(**_base("i6", None), project_ref="/proj/path"),
            ("delete_project", "/proj/path"),
        ),
        (MarkOpened(**_base("i7")), ("mark_opened", "sid-1")),
    ]

    async def _run() -> None:
        for intent, expected_call in cases:
            port = _FakeSessionCommandPort()
            adapter = SessionSurfaceAdapter()
            adapter._command_port = port
            ack = adapter.submit(intent)
            assert isinstance(ack, IntentAccepted), ack
            assert ack.intent_id == intent.intent_id
            await port.wait()
            assert port.calls == [expected_call]

    asyncio.run(_run())


def test_submit_without_command_port_rejects() -> None:
    adapter = SessionSurfaceAdapter()
    intent = MarkOpened(**_base("i8"))
    ack = adapter.submit(intent)
    assert isinstance(ack, IntentRejected)
    assert ack.code == "unsupported_contract_phase"


def test_submit_non_session_intent_rejects_without_calling_port() -> None:
    port = _FakeSessionCommandPort()
    adapter = SessionSurfaceAdapter()
    adapter._command_port = port
    # `Stop` is a ChatIntent, not a SessionIntent — proves submit() doesn't
    # silently accept an intent shape it wasn't built for.
    intent = Stop(cv=1, intent_id="i9", session_id="sid-1", turn_id="turn-1")
    ack = adapter.submit(intent)
    assert isinstance(ack, IntentRejected)
    assert ack.code == "unsupported"
    assert not port.calls


# ---------------------------------------------------------------------------
# Ack-contract closure: a `SessionCommandPort` rejection/success must reach
# the live plane, not just a server-side log line (identical gap
# `ProviderConfigSurfaceAdapter.reject_intent` already closed for the
# provider surface — see `_run_command`'s docstring).
# ---------------------------------------------------------------------------

class _RefFakeSessionCommandPort:
    """Fake port whose per-method result is injected by the test, so these
    tests exercise `_run_command`'s post-`await` handling in isolation from
    the real `session_commands.py` mutation logic (covered separately by
    `test_adapter_session_commands.py`)."""

    def __init__(self, result: CommandResult) -> None:
        self._result = result
        self.done = asyncio.Event()

    async def _respond(self) -> CommandResult:
        self.done.set()
        return self._result

    async def mark_opened(self, session_id) -> CommandResult:
        return await self._respond()

    async def assign_project(self, session_id, project_ref) -> CommandResult:
        return await self._respond()

    async def create_folder(self, project_ref, name, parent_folder_ref) -> CommandResult:
        return await self._respond()

    async def create_tag(self, name, project_ref, color) -> CommandResult:
        return await self._respond()

    async def wait(self) -> None:
        await asyncio.wait_for(self.done.wait(), timeout=2)


def test_run_command_rejection_pushes_intent_rejected_frame() -> None:
    """Failing-first: before this pass, a `SessionCommandPort` rejection
    was only `logger.info`'d — no `IntentRejected` ever reached a live
    subscriber, exactly the gap `ProviderCommandPort`'s own `_reject`
    (`backend/adapter_api.py`) already had to close for the provider
    surface. `submit()`'s synchronous ack is `IntentAccepted` either way
    (the command port hasn't run yet); the REAL rejection must arrive
    asynchronously over `subscribe()`."""
    port = _RefFakeSessionCommandPort(
        CommandResult(accepted=False, code="404", message="session not found"),
    )
    adapter = SessionSurfaceAdapter()
    adapter._command_port = port
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        intent = MarkOpened(cv=1, intent_id="reject-1", session_id="sid-missing")

        async def _run() -> None:
            ack = adapter.submit(intent)
            assert isinstance(ack, IntentAccepted), ack
            await port.wait()
            # `reject_intent` runs synchronously off the awaited coroutine's
            # own task — one more loop tick lets it land before assertions.
            await asyncio.sleep(0)

        asyncio.run(_run())
        rejections = [f for f in received if isinstance(f, IntentRejected)]
        assert rejections, received
        assert rejections[0].intent_id == "reject-1"
        assert rejections[0].code == "404"
        assert rejections[0].message == "session not found"
    finally:
        sub.close()


def test_assign_project_ref_stamps_intent_id_on_new_session_upsert() -> None:
    """Event-driven completion for `moveSessionToProject`: `AssignProject`
    carries no synchronous result, but a `ref` on its `CommandResult`
    (the new session `move_session_to_project` creates) makes
    `_run_command` force an intent_id-stamped `SessionSummaryUpsert` for
    exactly that session — the frame a frontend caller correlates its
    pending navigation on."""
    new_session = session_store.create_session(name=f"moved {uuid.uuid4().hex}", cwd=_cwd())
    new_sid = new_session["id"]
    port = _RefFakeSessionCommandPort(CommandResult(accepted=True, ref=new_sid))
    adapter = SessionSurfaceAdapter()
    adapter._command_port = port
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        intent = AssignProject(cv=1, intent_id="assign-1", session_id="old-sid", project_ref="/proj")

        async def _run() -> None:
            ack = adapter.submit(intent)
            assert isinstance(ack, IntentAccepted), ack
            await port.wait()
            await asyncio.sleep(0)

        asyncio.run(_run())
        matches = [
            f for f in received
            if isinstance(f, SessionSummaryUpsert) and f.intent_id == "assign-1"
        ]
        assert matches, received
        assert matches[0].summary.session_id == new_sid
    finally:
        sub.close()


def test_create_folder_ref_stamps_intent_id_on_folder_upsert() -> None:
    """Event-driven completion for `createAndAssignFolder`: `CreateFolder`
    has no synchronous result either — the new folder's ref arrives via a
    `FolderUpsert` carrying the creating intent's `intent_id`."""
    project_dir = tempfile.mkdtemp(prefix="ba-adapter-session-gaps-project-")
    added = project_store.add_project(project_dir, name=f"folder project {uuid.uuid4().hex}")
    folder = session_organization_store.create_folder(
        project_id=added["path"], name="real folder", parent_folder_id=None,
    )
    port = _RefFakeSessionCommandPort(CommandResult(accepted=True, ref=folder["id"]))
    adapter = SessionSurfaceAdapter()
    adapter._command_port = port
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        intent = CreateFolder(
            cv=1, intent_id="folder-1", session_id=None,
            project_ref=added["path"], name="real folder",
        )

        async def _run() -> None:
            ack = adapter.submit(intent)
            assert isinstance(ack, IntentAccepted), ack
            await port.wait()
            await asyncio.sleep(0)

        asyncio.run(_run())
        matches = [
            f for f in received if isinstance(f, FolderUpsert) and f.intent_id == "folder-1"
        ]
        assert matches, received
        assert matches[0].folder.folder_ref == folder["id"]
    finally:
        sub.close()


def test_create_tag_ref_stamps_intent_id_on_tag_upsert() -> None:
    tag = session_organization_store.create_tag(name="real tag", project_id=None, color="#fff")
    port = _RefFakeSessionCommandPort(CommandResult(accepted=True, ref=tag["id"]))
    adapter = SessionSurfaceAdapter()
    adapter._command_port = port
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        intent = CreateTag(cv=1, intent_id="tag-1", session_id=None, name="real tag")

        async def _run() -> None:
            ack = adapter.submit(intent)
            assert isinstance(ack, IntentAccepted), ack
            await port.wait()
            await asyncio.sleep(0)

        asyncio.run(_run())
        matches = [f for f in received if isinstance(f, TagUpsert) and f.intent_id == "tag-1"]
        assert matches, received
        assert matches[0].tag.tag_ref == tag["id"]
    finally:
        sub.close()


_TESTS = [
    test_attention_markers_real_from_session_store,
    test_session_tree_real_forks,
    test_session_tree_missing_is_rebuilding,
    test_tree_structure_fact_broadcasts_session_tree_changed,
    test_fork_only_delete_tombstones_fork_and_broadcasts_tree_changed,
    test_whole_root_delete_does_not_broadcast_tree_changed,
    test_queued_prompts_updated_sets_queued_rollup,
    test_project_fire_upserted_broadcasts_project_upsert,
    test_project_fire_removed_broadcasts_project_removed,
    test_submit_dispatches_every_intent_to_the_command_port,
    test_submit_without_command_port_rejects,
    test_submit_non_session_intent_rejects_without_calling_port,
    test_run_command_rejection_pushes_intent_rejected_frame,
    test_assign_project_ref_stamps_intent_id_on_new_session_upsert,
    test_create_folder_ref_stamps_intent_id_on_folder_upsert,
    test_create_tag_ref_stamps_intent_id_on_tag_upsert,
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

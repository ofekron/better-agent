"""Backend tests for the fork-split feature (schema v2 tree storage).

Pins the contract for:
  * session_store tree-shape: forks embedded in root file, _fork_index
    resolves any sid back to its root file in O(1), legacy v1 fork files
    raise on read.
  * SessionManager: per-root locking, set_fork_closed mutator, fork()
    fires `forked` listener event, get_root_tree returns the full tree.
  * session_ws_broadcaster: maps `forked` -> `session_forked` WS frame
    and `fork_closed_set` -> `session_metadata_updated` patch.
  * REST endpoints: /fork_and_send (claude-sid gate), /close_fork,
    GET /api/sessions/{id} returns the root tree, GET /api/sessions
    returns roots only with fork_count.

Run with:
    cd backend && .venv/bin/python scripts/test_fork_split.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Per CLAUDE.md, isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module so the dev's real session store is
# never touched.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-fork-split-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import main  # noqa: E402
import session_store  # noqa: E402
import session_manager as _sm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

# Make persists SYNCHRONOUS for the whole test process: with a 0s
# debounce, every `_persist_root` takes the leading-edge path and writes
# through immediately, so no daemon Timer is ever armed. This removes the
# entire race class — a deferred write can neither (a) land after a
# disk-read assertion nor (b) fire into a just-`rmtree`d home at teardown.
_sm_mod.PERSIST_DEBOUNCE_S = 0.0


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_home() -> None:
    """Wipe the test home + reset session_store/session_manager in-memory
    state between tests so each test runs on a clean slate."""
    sessions_dir = Path(_TMP_HOME) / "sessions"
    for _ in range(5):
        if not sessions_dir.exists():
            break
        shutil.rmtree(sessions_dir, ignore_errors=True)
        if not sessions_dir.exists():
            break
        time.sleep(0.05)
    session_store._fork_index.clear()
    session_store._root_forks.clear()
    session_store._root_index_signatures.clear()
    session_store._index_loaded = False
    session_store._index_fingerprint = None
    # Reset the in-memory summary index too, else list_sessions returns
    # the previous test's stale forks (fork_count drift).
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._kind_by_sid.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()
    session_manager._root_file_fingerprints.clear()
    session_manager._root_file_checked_at.clear()


def _tree_files(sessions_dir: Path) -> list[Path]:
    """Session TREE files on disk, excluding sidecars."""
    return [p for p in sessions_dir.glob("*.json")
            if not session_store._is_sidecar_json(p.name)]


def _make_root_with_claude_sid(name: str = "root") -> dict:
    """Create a root and stamp it with a fake claude_sid + one user
    message so fork_session() will accept it as a fork parent."""
    root = session_manager.create(name=name, cwd="/tmp")
    session_manager.set_agent_sid(root["id"], "manager", f"fake-claude-{root['id'][:8]}")
    session_manager.append_user_msg(root["id"], {
        "id": "m1", "role": "user", "content": "hi", "events": [],
        "timestamp": "2026-05-01T00:00:00", "isStreaming": False,
    })
    return session_manager.get(root["id"])


def _stamp_sid(node_id: str) -> None:
    """Give a freshly-forked node its own claude_sid so it can itself be
    a fork parent (used to test nested forks)."""
    session_manager.set_agent_sid(node_id, "manager", f"fake-claude-{node_id[:8]}")


# ──────────────────────────────────────────────────────────────────────
# session_store tree shape
# ──────────────────────────────────────────────────────────────────────

def test_create_session_has_tree_fields() -> bool:
    _reset_home()
    s = session_store.create_session(name="r", cwd="/tmp")
    ok = (
        s.get("forks") == []
        and s.get("fork_point_seq") is None
        and s.get("fork_closed") is False
        and s.get("parent_session_id") is None
        and s.get("_schema_version") == session_store.SCHEMA_VERSION
    )
    print(f"{PASS if ok else FAIL} create_session populates tree fields")
    return ok


def test_fork_session_embeds_in_root_file() -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])

    sessions_dir = Path(_TMP_HOME) / "sessions"
    files = _tree_files(sessions_dir)
    if len(files) != 1:
        print(f"{FAIL} fork_session_embeds_in_root_file: expected 1 file, got {len(files)}")
        return False
    on_disk = json.loads(files[0].read_text())
    embedded = on_disk.get("forks") or []
    ok = (
        files[0].name == f"{root['id']}.json"
        and len(embedded) == 1
        and embedded[0]["id"] == child["id"]
        and embedded[0]["parent_session_id"] == root["id"]
        and embedded[0]["fork_point_seq"] == 0  # parent had one msg at seq 0
    )
    print(f"{PASS if ok else FAIL} fork_session embeds child in root file")
    return ok


def test_fork_index_resolves_any_sid() -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])
    _stamp_sid(child["id"])
    grandchild = session_manager.fork(child["id"])

    ok = (
        session_store._resolve_root_id(root["id"]) == root["id"]
        and session_store._resolve_root_id(child["id"]) == root["id"]
        and session_store._resolve_root_id(grandchild["id"]) == root["id"]
        and session_store._resolve_root_id("nonexistent-id") is None
    )
    print(f"{PASS if ok else FAIL} _fork_index resolves root + child + nested fork ids")
    return ok


def test_get_session_returns_node_for_fork_id() -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])
    session_manager.flush_pending_persists()

    fetched_root = session_store.get_session(root["id"])
    fetched_child = session_store.get_session(child["id"])
    ok = (
        fetched_root is not None
        and fetched_root["id"] == root["id"]
        and fetched_child is not None
        and fetched_child["id"] == child["id"]
        and fetched_child["parent_session_id"] == root["id"]
    )
    print(f"{PASS if ok else FAIL} get_session returns node for either root or fork id")
    return ok


def test_get_root_tree_returns_full_tree() -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])
    session_manager.flush_pending_persists()

    # Both ids should yield the same root tree.
    via_root = session_store.get_root_tree(root["id"])
    via_child = session_store.get_root_tree(child["id"])
    ok = (
        via_root is not None
        and via_root["id"] == root["id"]
        and len(via_root.get("forks") or []) == 1
        and via_root["forks"][0]["id"] == child["id"]
        and via_child is not None
        and via_child["id"] == root["id"]  # walks up to the root
    )
    print(f"{PASS if ok else FAIL} get_root_tree returns the full tree from any sid")
    return ok


def test_list_sessions_returns_roots_only() -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    fork1 = session_manager.fork(root["id"])
    fork2 = session_manager.fork(root["id"])

    listed = session_store.list_sessions()
    ids = {s["id"] for s in listed}
    fork_count = next(
        (s.get("fork_count") for s in listed if s["id"] == root["id"]),
        None,
    )
    ok = (
        len(listed) == 1
        and root["id"] in ids
        and fork1["id"] not in ids
        and fork2["id"] not in ids
        and fork_count == 2
    )
    print(f"{PASS if ok else FAIL} list_sessions hides forks; fork_count = 2")
    return ok


def test_iter_all_sessions_walks_tree() -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])
    _stamp_sid(child["id"])
    grandchild = session_manager.fork(child["id"])
    session_manager.flush_pending_persists()

    all_ids = {s["id"] for s in session_store.iter_all_sessions()}
    ok = (
        root["id"] in all_ids
        and child["id"] in all_ids
        and grandchild["id"] in all_ids
        and len(all_ids) == 3
    )
    print(f"{PASS if ok else FAIL} iter_all_sessions yields root + every embedded fork")
    return ok


def test_delete_fork_splices_from_parent() -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])

    deleted = session_manager.delete(child["id"])
    after = session_store.get_root_tree(root["id"])
    ok = (
        deleted is True
        and after is not None
        and len(after.get("forks") or []) == 0
        and session_store._resolve_root_id(child["id"]) is None
        # Root file still present.
        and (Path(_TMP_HOME) / "sessions" / f"{root['id']}.json").exists()
    )
    print(f"{PASS if ok else FAIL} delete(fork_id) splices fork out, keeps root file")
    return ok


def test_delete_root_removes_whole_tree() -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])
    _stamp_sid(child["id"])
    grandchild = session_manager.fork(child["id"])
    session_manager.flush_pending_persists()

    deleted = session_manager.delete(root["id"])
    session_manager.flush_pending_persists()
    files = _tree_files(Path(_TMP_HOME) / "sessions")
    ok = (
        deleted is True
        and len(files) == 0
        and session_store._resolve_root_id(root["id"]) is None
        and session_store._resolve_root_id(child["id"]) is None
        and session_store._resolve_root_id(grandchild["id"]) is None
    )
    print(f"{PASS if ok else FAIL} delete(root) drops file + every descendant from index")
    return ok


def test_legacy_fork_file_raises_on_read() -> bool:
    """Pre-v2 layout had each fork as its own top-level file with
    `parent_session_id` set. v2 raises on read so the user knows to
    wipe sessions/."""
    _reset_home()
    sessions_dir = Path(_TMP_HOME) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "id": "legacy-fork-id",
        "name": "legacy",
        "model": "claude-sonnet-4-6",
        "cwd": "/tmp",
        "created_at": "2026-04-01T00:00:00",
        "updated_at": "2026-04-01T00:00:00",
        "orchestration_mode": "manager",
        "agent_session_id": None,
        "parent_session_id": "some-other-root",  # the v1 marker
        "forked_from_agent_sid": "fake-sid",
        "messages": [],
        "next_seq": 0,
    }
    (sessions_dir / "legacy-fork-id.json").write_text(json.dumps(legacy))

    raised = False
    try:
        session_store._migrate_session(json.loads(
            (sessions_dir / "legacy-fork-id.json").read_text()
        ))
    except ValueError as e:
        raised = "schema v2" in str(e).lower() or "wipe" in str(e).lower()

    print(f"{PASS if raised else FAIL} legacy v1 fork file raises ValueError on read")
    return raised


# ──────────────────────────────────────────────────────────────────────
# SessionManager mutators + listeners
# ──────────────────────────────────────────────────────────────────────

def test_set_fork_closed_persists_and_fires_event() -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])

    captured: list[dict] = []
    listener = lambda sid, change: captured.append({"sid": sid, **change})
    session_manager.add_listener(listener)
    try:
        session_manager.set_fork_closed(child["id"], True)
    finally:
        session_manager._listeners.remove(listener)

    after = session_manager.get(child["id"])
    fired = [c for c in captured if c.get("kind") == "fork_closed_set"]
    ok = (
        after is not None
        and after["fork_closed"] is True
        and len(fired) == 1
        and fired[0]["sid"] == child["id"]
        and fired[0]["value"] is True
    )
    print(f"{PASS if ok else FAIL} set_fork_closed persists + fires fork_closed_set")
    return ok


def test_fork_fires_forked_event() -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()

    captured: list[dict] = []
    listener = lambda sid, change: captured.append({"sid": sid, **change})
    session_manager.add_listener(listener)
    try:
        child = session_manager.fork(root["id"])
    finally:
        session_manager._listeners.remove(listener)

    fired = [c for c in captured if c.get("kind") == "forked"]
    ok = (
        len(fired) == 1
        and fired[0]["sid"] == child["id"]
        and fired[0]["parent_session_id"] == root["id"]
        and fired[0]["session"]["id"] == child["id"]
    )
    print(f"{PASS if ok else FAIL} fork() fires `forked` event with parent_session_id")
    return ok


def test_mutating_fork_persists_whole_root_tree() -> bool:
    """Critical invariant: mutating an embedded fork via session_manager
    must persist the WHOLE root file (not just the fork). Verify by
    setting fork_closed and re-reading the root file directly."""
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])

    session_manager.set_fork_closed(child["id"], True)
    session_manager.flush_pending_persists()

    # Read the on-disk root file directly — bypass cache.
    on_disk = json.loads(
        (Path(_TMP_HOME) / "sessions" / f"{root['id']}.json").read_text()
    )
    embedded = on_disk.get("forks") or []
    ok = (
        len(embedded) == 1
        and embedded[0]["id"] == child["id"]
        and embedded[0]["fork_closed"] is True
    )
    print(f"{PASS if ok else FAIL} fork mutation persists into the root file on disk")
    return ok


def test_per_root_lock_is_shared_across_siblings() -> bool:
    """Two forks of the same root share one lock — different roots get
    different locks. Verify by identity."""
    _reset_home()
    root_a = _make_root_with_claude_sid("a")
    root_b = _make_root_with_claude_sid("b")
    child_a1 = session_manager.fork(root_a["id"])
    child_a2 = session_manager.fork(root_a["id"])

    la_via_root = session_manager._lock_for_root(root_a["id"])
    la_via_c1 = session_manager._lock_for_root(
        session_manager._root_id_for(child_a1["id"])
    )
    la_via_c2 = session_manager._lock_for_root(
        session_manager._root_id_for(child_a2["id"])
    )
    lb_via_root = session_manager._lock_for_root(root_b["id"])

    ok = (
        la_via_root is la_via_c1
        and la_via_root is la_via_c2
        and la_via_root is not lb_via_root
    )
    print(f"{PASS if ok else FAIL} per-root lock shared across siblings, distinct between roots")
    return ok


# ──────────────────────────────────────────────────────────────────────
# session_ws_broadcaster (synchronous mapping check)
# ──────────────────────────────────────────────────────────────────────

def test_broadcaster_maps_forked_to_session_forked() -> bool:
    """Bypass the dispatch (no event loop in this test) and check that
    `forked` change kind produces a `session_forked` payload."""
    _reset_home()
    from session_ws_broadcaster import SessionWSBroadcaster
    captured: list[dict] = []

    class FakeCoord:
        async def broadcast(self, payload: dict) -> None:
            captured.append(payload)

    bcast = SessionWSBroadcaster(FakeCoord())
    # Patch _dispatch to bypass asyncio (sync test path).
    bcast._dispatch = lambda payload: captured.append(payload)

    bcast.on_change("child-id", {
        "kind": "forked",
        "session": {"id": "child-id"},
        "parent_session_id": "root-id",
    })

    ok = (
        len(captured) == 1
        and captured[0]["type"] == "session_forked"
        and captured[0]["data"]["session"]["id"] == "child-id"
        and captured[0]["data"]["parent_session_id"] == "root-id"
    )
    print(f"{PASS if ok else FAIL} broadcaster maps `forked` -> session_forked WS frame")
    return ok


def test_broadcaster_maps_fork_closed_set_to_metadata_patch() -> bool:
    _reset_home()
    from session_ws_broadcaster import SessionWSBroadcaster
    captured: list[dict] = []

    class FakeCoord: pass

    bcast = SessionWSBroadcaster(FakeCoord())
    bcast._dispatch = lambda payload: captured.append(payload)

    bcast.on_change("fork-id", {
        "kind": "fork_closed_set",
        "value": True,
    })

    ok = (
        len(captured) == 1
        and captured[0]["type"] == "session_metadata_updated"
        and captured[0]["data"]["session_id"] == "fork-id"
        and captured[0]["data"]["patch"] == {"fork_closed": True}
    )
    print(f"{PASS if ok else FAIL} broadcaster maps `fork_closed_set` -> metadata patch")
    return ok


def test_dispatch_raw_annotates_app_session_id() -> bool:
    """orchestrator._dispatch_raw must stamp `data.app_session_id` on
    every event so a multi-pane client can route per-pane. Without
    this, all live events from non-focused panes would misroute to
    the focused pane in split-fork view."""
    _reset_home()
    import asyncio
    from orchestrator import Coordinator

    coord = Coordinator()

    captured: list[dict] = []

    async def cb(event: dict) -> None:
        captured.append(event)

    coord.register_ws("sess-A", cb)

    async def run() -> None:
        # Event without app_session_id — should be annotated.
        await coord.dispatch_raw("sess-A", {
            "type": "manager_event",
            "data": {"foo": "bar"},
        })
        # Event already carrying app_session_id — should be left alone
        # (no dict copy needed) but still routed.
        await coord.dispatch_raw("sess-A", {
            "type": "messages_replay",
            "data": {"app_session_id": "sess-A", "messages": []},
        })
        # Event for a session with NO callback — should not raise.
        await coord.dispatch_raw("sess-B", {
            "type": "manager_event",
            "data": {"foo": "baz"},
        })

    asyncio.run(run())

    ok = (
        len(captured) == 2
        and captured[0]["data"]["app_session_id"] == "sess-A"
        and captured[0]["data"]["foo"] == "bar"
        and captured[1]["data"]["app_session_id"] == "sess-A"
    )
    print(f"{PASS if ok else FAIL} _dispatch_raw annotates data.app_session_id on every frame")
    return ok


def test_broadcaster_drops_unknown_kinds() -> bool:
    _reset_home()
    from session_ws_broadcaster import SessionWSBroadcaster
    captured: list[dict] = []

    class FakeCoord: pass

    bcast = SessionWSBroadcaster(FakeCoord())
    bcast._dispatch = lambda payload: captured.append(payload)

    # `agent_sid_set` is backend-internal — no WS frame. `created`,
    # `renamed`, `deleted` and `forked` are handled and covered by
    # their own tests; anything else (e.g. `assistant_msg_appended`,
    # `running_content_updated`) is silently dropped.
    bcast.on_change("sid", {"kind": "agent_sid_set", "mode": "manager"})
    bcast.on_change("sid", {"kind": "assistant_msg_appended"})
    bcast.on_change("sid", {"kind": "running_content_updated"})

    ok = len(captured) == 0
    print(f"{PASS if ok else FAIL} broadcaster drops kinds outside the allowlist")
    return ok


def test_broadcaster_maps_renamed_to_session_renamed() -> bool:
    """C3: renamed change → session_renamed WS frame for multi-tab convergence."""
    _reset_home()
    from session_ws_broadcaster import SessionWSBroadcaster
    captured: list[dict] = []

    class FakeCoord: pass

    bcast = SessionWSBroadcaster(FakeCoord())
    bcast._dispatch = lambda payload: captured.append(payload)
    bcast.on_change("sess-1", {"kind": "renamed", "name": "fresh"})
    ok = (
        len(captured) == 1
        and captured[0]["type"] == "session_renamed"
        and captured[0]["data"]["session_id"] == "sess-1"
        and captured[0]["data"]["name"] == "fresh"
    )
    print(f"{PASS if ok else FAIL} broadcaster maps `renamed` -> session_renamed WS frame")
    return ok


def test_broadcaster_maps_deleted_to_session_deleted() -> bool:
    """C3: deleted change → session_deleted WS frame for multi-tab convergence."""
    _reset_home()
    from session_ws_broadcaster import SessionWSBroadcaster
    captured: list[dict] = []

    class FakeCoord: pass

    bcast = SessionWSBroadcaster(FakeCoord())
    bcast._dispatch = lambda payload: captured.append(payload)
    bcast.on_change("sess-1", {"kind": "deleted"})
    ok = (
        len(captured) == 1
        and captured[0]["type"] == "session_deleted"
        and captured[0]["data"]["session_id"] == "sess-1"
    )
    print(f"{PASS if ok else FAIL} broadcaster maps `deleted` -> session_deleted WS frame")
    return ok


def test_broadcaster_maps_created_to_session_created() -> bool:
    """DIV-4 regression: a fresh `kind:created` change MUST produce a
    `session_created` WS frame so other tabs add the session to their
    sidebar without polling. Ephemeral sessions (working_mode set) are
    filtered out to match the FE sidebar's `!working_mode` filter."""
    _reset_home()
    from session_ws_broadcaster import SessionWSBroadcaster
    captured: list[dict] = []

    class FakeCoord: pass

    bcast = SessionWSBroadcaster(FakeCoord())
    bcast._dispatch = lambda payload: captured.append(payload)

    # User-facing new session — must emit.
    bcast.on_change("sid-A", {
        "kind": "created",
        "session": {"id": "sid-A", "name": "user session", "working_mode": None},
    })
    # Ephemeral (file-edit) session — must be filtered out.
    bcast.on_change("sid-B", {
        "kind": "created",
        "session": {"id": "sid-B", "name": "ephemeral", "working_mode": "file_editing"},
    })

    if len(captured) != 1:
        print(f"  expected 1 captured frame (only sid-A), got {len(captured)}")
        return False
    f = captured[0]
    ok = (
        f["type"] == "session_created"
        and f["data"]["session"]["id"] == "sid-A"
        and f["data"]["session"].get("working_mode") is None
    )
    print(f"{PASS if ok else FAIL} broadcaster maps `created` -> session_created WS frame (ephemeral filtered)")
    return ok


def test_broadcaster_maps_selectors_set_to_metadata_patch() -> bool:
    """DIV-4 regression: a `kind:selectors_set` change MUST produce a
    `session_metadata_updated` WS frame carrying the model/cwd patch
    and `originated_by` from `client_id` so the originating tab skips
    its own echo."""
    _reset_home()
    from session_ws_broadcaster import SessionWSBroadcaster
    captured: list[dict] = []

    class FakeCoord: pass

    bcast = SessionWSBroadcaster(FakeCoord())
    bcast._dispatch = lambda payload: captured.append(payload)

    # Model change only.
    bcast.on_change("sid-A", {
        "kind": "selectors_set",
        "model": "claude-opus-4-7[1m]",
        "cwd": None,
        "client_id": "tab-1",
    })
    # cwd change only, no client_id.
    bcast.on_change("sid-B", {
        "kind": "selectors_set",
        "model": None,
        "cwd": "/tmp/proj",
        "client_id": None,
    })
    # No fields → must NOT emit (avoid empty patches).
    bcast.on_change("sid-C", {
        "kind": "selectors_set",
        "model": None,
        "cwd": None,
        "client_id": "tab-1",
    })

    if len(captured) != 2:
        print(f"  expected 2 frames (sid-C suppressed), got {len(captured)}")
        return False
    f1, f2 = captured
    ok1 = (
        f1["type"] == "session_metadata_updated"
        and f1["data"]["session_id"] == "sid-A"
        and f1["data"]["patch"] == {"model": "claude-opus-4-7[1m]"}
        and f1["data"]["originated_by"] == "tab-1"
    )
    ok2 = (
        f2["type"] == "session_metadata_updated"
        and f2["data"]["session_id"] == "sid-B"
        and f2["data"]["patch"] == {"cwd": "/tmp/proj"}
        and f2["data"]["originated_by"] is None
    )
    ok = ok1 and ok2
    print(f"{PASS if ok else FAIL} broadcaster maps `selectors_set` -> metadata patch with originated_by")
    return ok


# ──────────────────────────────────────────────────────────────────────
# REST endpoints
# ──────────────────────────────────────────────────────────────────────

def test_rest_get_session_returns_root_tree(client: TestClient) -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])

    # Hitting either id resolves to the root tree.
    r1 = client.get(f"/api/sessions/{root['id']}")
    r2 = client.get(f"/api/sessions/{child['id']}")
    if r1.status_code != 200 or r2.status_code != 200:
        print(f"{FAIL} rest_get_session_returns_root_tree: status {r1.status_code}/{r2.status_code}")
        return False
    t1 = r1.json()
    t2 = r2.json()
    ok = (
        t1["id"] == root["id"]
        and len(t1.get("forks") or []) == 1
        and t1["forks"][0]["id"] == child["id"]
        and t2["id"] == root["id"]  # fork id resolves up
    )
    print(f"{PASS if ok else FAIL} GET /api/sessions/{{id}} returns root tree (works for fork id too)")
    return ok


def test_rest_list_sessions_excludes_forks(client: TestClient) -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    fork1 = session_manager.fork(root["id"])
    fork2 = session_manager.fork(root["id"])

    r = client.get("/api/sessions")
    if r.status_code != 200:
        print(f"{FAIL} rest_list_sessions_excludes_forks: {r.status_code}")
        return False
    body = r.json()
    ids = {s["id"] for s in body.get("sessions", [])}
    fork_count = next(
        (s.get("fork_count") for s in body["sessions"] if s["id"] == root["id"]),
        None,
    )
    ok = (
        root["id"] in ids
        and fork1["id"] not in ids
        and fork2["id"] not in ids
        and fork_count == 2
    )
    print(f"{PASS if ok else FAIL} GET /api/sessions returns roots only with fork_count=2")
    return ok


def test_rest_close_fork_persists(client: TestClient) -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])

    r = client.post(f"/api/sessions/{child['id']}/close_fork")
    if r.status_code != 200:
        print(f"{FAIL} rest_close_fork_persists: {r.status_code}")
        return False
    body = r.json()
    after = session_manager.get(child["id"])
    ok = (
        body == {"id": child["id"], "fork_closed": True}
        and after is not None
        and after["fork_closed"] is True
    )
    print(f"{PASS if ok else FAIL} POST /close_fork persists fork_closed=true")
    return ok


def test_rest_reopen_fork_inverts_close(client: TestClient) -> bool:
    """Reopen flips the flag back. Idempotent: reopening an already-open
    fork is a no-op (still returns 200)."""
    _reset_home()
    root = _make_root_with_claude_sid()
    child = session_manager.fork(root["id"])
    session_manager.set_fork_closed(child["id"], True)

    r = client.post(f"/api/sessions/{child['id']}/reopen_fork")
    if r.status_code != 200:
        print(f"{FAIL} rest_reopen_fork_inverts_close: {r.status_code}")
        return False
    after = session_manager.get(child["id"])
    ok = (
        r.json() == {"id": child["id"], "fork_closed": False}
        and after is not None
        and after["fork_closed"] is False
    )
    # Idempotent — reopening an already-open fork stays 200.
    r2 = client.post(f"/api/sessions/{child['id']}/reopen_fork")
    ok = ok and r2.status_code == 200
    print(f"{PASS if ok else FAIL} POST /reopen_fork inverts close (idempotent)")
    return ok


def test_nested_fork_lands_under_parent_fork() -> bool:
    """Forking a fork (nested) appends the new child to the FORK's
    `forks` array, not the root's. The root file still holds the
    full tree on disk."""
    _reset_home()
    root = _make_root_with_claude_sid()
    f1 = session_manager.fork(root["id"])
    _stamp_sid(f1["id"])
    f2 = session_manager.fork(f1["id"])
    session_manager.flush_pending_persists()

    on_disk = json.loads(
        (Path(_TMP_HOME) / "sessions" / f"{root['id']}.json").read_text()
    )
    # Walk: root.forks[0] should be f1, and f1.forks[0] should be f2.
    root_forks = on_disk.get("forks") or []
    if len(root_forks) != 1 or root_forks[0]["id"] != f1["id"]:
        print(f"{FAIL} nested_fork: f1 not under root")
        return False
    f1_on_disk = root_forks[0]
    f1_forks = f1_on_disk.get("forks") or []
    ok = (
        len(f1_forks) == 1
        and f1_forks[0]["id"] == f2["id"]
        and f2["parent_session_id"] == f1["id"]
        and f2["fork_point_seq"] is not None
        # f2's fork_point_seq is f1's last seq at fork time, which is
        # f1's next_seq - 1 (>= root's last seq).
    )
    print(f"{PASS if ok else FAIL} nested fork lands under parent fork (root file is full tree)")
    return ok


def test_fork_name_does_not_stack_suffix() -> bool:
    """Forking a session whose name already ends in '(fork)' should not
    yield 'X (fork) (fork)' — instead bump a numeric counter."""
    _reset_home()
    root = _make_root_with_claude_sid("Project")
    f1 = session_manager.fork(root["id"])
    _stamp_sid(f1["id"])
    f2 = session_manager.fork(f1["id"])
    _stamp_sid(f2["id"])
    f3 = session_manager.fork(f2["id"])

    ok = (
        f1["name"] == "Project (fork)"
        and f2["name"] == "Project (fork 2)"
        and f3["name"] == "Project (fork 3)"
    )
    print(f"{PASS if ok else FAIL} fork name dedup: '(fork)' -> '(fork 2)' -> '(fork 3)'")
    return ok


def test_rest_fork_and_send_rejects_without_claude_sid(client: TestClient) -> bool:
    _reset_home()
    # Create a root WITHOUT giving it a claude_sid — fork_session() must reject.
    root = session_manager.create(name="bare", cwd="/tmp")

    r = client.post(
        f"/api/sessions/{root['id']}/fork_and_send",
        json={"prompt": "hi"},
    )
    ok = r.status_code == 400 and "claude session id" in r.json().get("detail", "").lower()
    print(f"{PASS if ok else FAIL} POST /fork_and_send rejects when parent has no claude_sid")
    return ok


def test_rest_fork_and_send_rejects_empty_prompt(client: TestClient) -> bool:
    _reset_home()
    root = _make_root_with_claude_sid()

    r = client.post(
        f"/api/sessions/{root['id']}/fork_and_send",
        json={"prompt": "   "},
    )
    ok = r.status_code == 400 and "prompt" in r.json().get("detail", "").lower()
    print(f"{PASS if ok else FAIL} POST /fork_and_send rejects empty/whitespace prompt")
    return ok


def test_rest_fork_endpoint_creates_embedded_fork(client: TestClient) -> bool:
    """The vanilla /fork endpoint (used by the top-toolbar Fork button)
    must also produce an embedded child."""
    _reset_home()
    root = _make_root_with_claude_sid()

    r = client.post(f"/api/sessions/{root['id']}/fork")
    if r.status_code != 200:
        print(f"{FAIL} rest_fork_endpoint_creates_embedded_fork: {r.status_code}")
        return False
    child = r.json()
    sessions_dir = Path(_TMP_HOME) / "sessions"
    files = _tree_files(sessions_dir)
    on_disk = json.loads(files[0].read_text())
    ok = (
        len(files) == 1
        and child["parent_session_id"] == root["id"]
        and child["fork_point_seq"] == 0
        and on_disk["forks"][0]["id"] == child["id"]
    )
    print(f"{PASS if ok else FAIL} POST /fork creates an embedded fork (one file on disk)")
    return ok


# ──────────────────────────────────────────────────────────────────────
# Test runner
# ──────────────────────────────────────────────────────────────────────

def main_runner() -> int:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    authenticate_client(client)
    tests_no_client = [
        test_create_session_has_tree_fields,
        test_fork_session_embeds_in_root_file,
        test_fork_index_resolves_any_sid,
        test_get_session_returns_node_for_fork_id,
        test_get_root_tree_returns_full_tree,
        test_list_sessions_returns_roots_only,
        test_iter_all_sessions_walks_tree,
        test_delete_fork_splices_from_parent,
        test_delete_root_removes_whole_tree,
        test_legacy_fork_file_raises_on_read,
        test_set_fork_closed_persists_and_fires_event,
        test_fork_fires_forked_event,
        test_mutating_fork_persists_whole_root_tree,
        test_per_root_lock_is_shared_across_siblings,
        test_broadcaster_maps_forked_to_session_forked,
        test_broadcaster_maps_fork_closed_set_to_metadata_patch,
        test_dispatch_raw_annotates_app_session_id,
        test_broadcaster_drops_unknown_kinds,
        test_broadcaster_maps_renamed_to_session_renamed,
        test_broadcaster_maps_deleted_to_session_deleted,
        test_broadcaster_maps_created_to_session_created,
        test_broadcaster_maps_selectors_set_to_metadata_patch,
        test_nested_fork_lands_under_parent_fork,
        test_fork_name_does_not_stack_suffix,
    ]
    tests_with_client = [
        test_rest_get_session_returns_root_tree,
        test_rest_list_sessions_excludes_forks,
        test_rest_close_fork_persists,
        test_rest_reopen_fork_inverts_close,
        test_rest_fork_and_send_rejects_without_claude_sid,
        test_rest_fork_and_send_rejects_empty_prompt,
        test_rest_fork_endpoint_creates_embedded_fork,
    ]
    results = [t() for t in tests_no_client]
    results += [t(client) for t in tests_with_client]

    failed = sum(1 for r in results if not r)
    print()
    if failed == 0:
        print(f"{PASS} all {len(results)} tests passed")
        rc = 0
    else:
        print(f"{FAIL} {failed}/{len(results)} tests failed")
        rc = 1

    session_manager.flush_pending_persists()
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main_runner())

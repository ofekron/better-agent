"""Locks the per-session unread cursor semantics:

1. APPEND-new-UUID path bumps `_unread_counts` + fires
   `unread_changed`.
2. REPLACE same-UUID path (Gemini cumulative streaming) does NOT bump.
3. `mark_seen(sid, uid)` persists `last_seen_event_uid`, zeroes the
   counter, fires `seen_advanced`.
4. Persistence: after a backend "restart" (drop the manager singleton,
   re-import), the persisted `last_seen_event_uid` survives and the
   recomputed unread count walks `msg.events` past that marker.
5. Worker forks (`kind != "user"`) do NOT bump root unread when their
   own events apply.

Run with:
    cd backend && .venv/bin/python scripts/test_unread_cursor.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-unread-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
import session_manager as session_manager_module  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _mk_session(mode: str = "native") -> tuple[str, dict]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/test-unread",
        orchestration_mode=mode, source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy(mode)
    scaffold = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, scaffold)
    return sid, scaffold


def _native_event(uuid: str, text: str = "x") -> dict:
    """Native-mode `agent_message` shape — apply_event's append path
    sees this top-level uuid via _event_uuid → data.uuid."""
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": text},
        },
    }


def _capture_fires() -> tuple[list[dict], callable]:
    """Subscribe a sync listener that captures `kind` events."""
    events: list[dict] = []

    def listener(sid: str, change: dict) -> None:
        events.append({"sid": sid, **change})

    # Suppress the DeprecationWarning that add_listener fires — tests
    # use this path legitimately.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        session_manager.add_listener(listener)
    return events, listener


def test_append_bumps_unread() -> None:
    sid, msg = _mk_session("native")
    fires, _ = _capture_fires()

    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    # Both events go to the SAME assistant message.
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_native_event("uuid-a"),
        ctx=ctx, source_is_provider_stream=True,
    )
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_native_event("uuid-b"),
        ctx=ctx, source_is_provider_stream=True,
    )

    unread_fires = [f for f in fires if f.get("kind") == "unread_changed"]
    # NEW BEHAVIOR: 1 fire (for the first event in the message).
    # Subsequent events in the same message don't increment the count.
    assert len(unread_fires) == 1, (
        f"expected 1 unread_changed fire, got {len(unread_fires)}: {unread_fires}"
    )
    assert session_manager.get_unread_count(sid) == 1, (
        f"unread_count expected 1, got {session_manager.get_unread_count(sid)}"
    )
    print(f"{PASS} append_bumps_unread")


def test_replace_does_not_bump() -> None:
    sid, msg = _mk_session("native")

    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_native_event("uuid-streaming", text="chunk-1"),
        ctx=ctx, source_is_provider_stream=True,
    )
    base_count = session_manager.get_unread_count(sid)
    assert base_count == 1, f"expected 1 after first append, got {base_count}"

    # Now REPLACE — same UUID, mutated content (Gemini streaming pattern).
    # apply_event takes the REPLACE branch and returns BEFORE the
    # bump_unread call.
    fires, _ = _capture_fires()
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_native_event("uuid-streaming", text="chunk-1-extended"),
        ctx=ctx, source_is_provider_stream=True,
    )

    unread_fires = [f for f in fires if f.get("kind") == "unread_changed"]
    assert len(unread_fires) == 0, (
        f"REPLACE path must NOT bump unread; got {unread_fires}"
    )
    assert session_manager.get_unread_count(sid) == 1, (
        "unread_count must stay at 1 across same-UUID replace"
    )
    print(f"{PASS} replace_does_not_bump")


def test_mark_seen_zeros() -> None:
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    for u in ("u1", "u2", "u3"):
        strategy.apply_event(
            app_session_id=sid, msg=msg,
            event=_native_event(u),
            ctx=ctx, source_is_provider_stream=True,
        )
    # 3 events in ONE message = 1 unread message
    assert session_manager.get_unread_count(sid) == 1

    fires, _ = _capture_fires()
    session_manager.mark_seen(sid, None)  # ack head
    seen_fires = [f for f in fires if f.get("kind") == "seen_advanced"]
    assert len(seen_fires) == 1, f"expected 1 seen_advanced fire, got {seen_fires}"
    assert seen_fires[0].get("unread_count") == 0
    assert seen_fires[0].get("last_seen_event_uid") == "u3"
    assert session_manager.get_unread_count(sid) == 0
    print(f"{PASS} mark_seen_zeros")


def test_mark_seen_does_not_copy_session_tree() -> None:
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_native_event("copy-guard"),
        ctx=ctx, source_is_provider_stream=True,
    )
    original_deepcopy = session_manager_module.copy.deepcopy

    def guarded_deepcopy(value):
        if isinstance(value, dict) and value.get("id") == sid:
            raise AssertionError("mark_seen copied the full session tree")
        return original_deepcopy(value)

    session_manager_module.copy.deepcopy = guarded_deepcopy
    try:
        result = session_manager.mark_seen(sid, None)
    finally:
        session_manager_module.copy.deepcopy = original_deepcopy
    assert result == {"last_seen_event_uid": "copy-guard"}, result
    assert session_manager.get_unread_count(sid) == 0
    print(f"{PASS} mark_seen_does_not_copy_session_tree")


def test_mark_seen_uses_journal_latest_uid() -> None:
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_native_event("scan-fallback"),
        ctx=ctx, source_is_provider_stream=True,
    )

    original = session_manager_module._event_uuid_safe
    import event_ingester as event_ingester_module
    original_latest = event_ingester_module.event_ingester.latest_render_event_uid

    def guarded_event_uuid(_event):
        raise AssertionError("mark_seen scanned live message events")

    event_ingester_module.event_ingester.latest_render_event_uid = (
        lambda root_id, *, sid_filter=None: "journal-head"
    )
    session_manager_module._event_uuid_safe = guarded_event_uuid
    try:
        result = session_manager.mark_seen(sid, None)
    finally:
        session_manager_module._event_uuid_safe = original
        event_ingester_module.event_ingester.latest_render_event_uid = original_latest
    assert result == {"last_seen_event_uid": "journal-head"}, result
    assert session_manager.get_unread_count(sid) == 0
    print(f"{PASS} mark_seen_uses_journal_latest_uid")


def test_mark_seen_avoids_full_tree_write() -> None:
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_native_event("sidecar-head"),
        ctx=ctx, source_is_provider_stream=True,
    )

    original_write = session_store.write_session_full

    def guarded_write(*_args, **_kwargs):
        raise AssertionError("mark_seen wrote the full session tree")

    session_store.write_session_full = guarded_write
    try:
        result = session_manager.mark_seen(sid, None)
    finally:
        session_store.write_session_full = original_write
    assert result == {"last_seen_event_uid": "sidecar-head"}, result
    assert session_store.read_seen_cursors(sid).get(sid) == "sidecar-head"

    session_manager._roots.clear()
    session_manager._event_hydrated_roots.clear()
    session_manager._unread_counts.clear()
    session_manager._unread_hydrated.clear()
    loaded = session_manager.get(sid)
    assert loaded and loaded.get("last_seen_event_uid") == "sidecar-head"
    print(f"{PASS} mark_seen_avoids_full_tree_write")


def test_seen_cursor_write_is_idempotent() -> None:
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_native_event("idempotent-sidecar"),
        ctx=ctx, source_is_provider_stream=True,
    )
    session_manager.mark_seen(sid, "idempotent-sidecar")
    seen_path = Path(_TMP_HOME) / "sessions" / f"{sid}.seen.json"
    before = seen_path.stat().st_mtime_ns
    session_manager.mark_seen(sid, "idempotent-sidecar")
    after = seen_path.stat().st_mtime_ns
    assert after == before, "duplicate mark_seen rewrote the seen sidecar"
    print(f"{PASS} seen_cursor_write_is_idempotent")


def test_mark_unread_clears_seen_sidecar() -> None:
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_native_event("clear-sidecar"),
        ctx=ctx, source_is_provider_stream=True,
    )
    session_manager.mark_seen(sid, "clear-sidecar")
    assert session_store.read_seen_cursors(sid).get(sid) == "clear-sidecar"

    session_manager.mark_unread(sid)
    assert session_store.read_seen_cursors(sid).get(sid) is None

    session_manager._roots.clear()
    session_manager._event_hydrated_roots.clear()
    session_manager._unread_counts.clear()
    session_manager._unread_hydrated.clear()
    loaded = session_manager.get(sid)
    assert loaded and loaded.get("last_seen_event_uid") is None
    print(f"{PASS} mark_unread_clears_seen_sidecar")


def test_persistence_across_reload() -> None:
    """Persist `last_seen_event_uid`, then drop the in-memory
    SessionManager state and re-hydrate. Counter must rebuild
    correctly from disk: 0 after ack of head, plus N for any
    subsequent events."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid)
    for u in ("p1", "p2", "p3"):
        strategy.apply_event(
            app_session_id=sid, msg=msg,
            event=_native_event(u),
            ctx=ctx, source_is_provider_stream=True,
        )
    session_manager.mark_seen(sid, "p3")
    # Apply one more — should be unread again.
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_native_event("p4"),
        ctx=ctx, source_is_provider_stream=True,
    )
    assert session_manager.get_unread_count(sid) == 1

    # Drop in-memory state — clear cache + hydration markers so the
    # next read re-walks msg.events from disk.
    session_manager._roots.clear()
    session_manager._unread_counts.clear()
    session_manager._unread_hydrated.clear()

    # Lazy hydration computes from persisted `last_seen_event_uid="p3"`
    # plus the persisted "p4" event = 1.
    after = session_manager.get_unread_count(sid)
    assert after == 1, f"reload should hydrate to 1, got {after}"
    print(f"{PASS} persistence_across_reload")


def test_worker_fork_does_not_bump_root() -> None:
    """A `delegate_fork` session is `kind != user`; bumping unread on
    its sid is a no-op. Mirrors the production guard at
    `_is_user_kind` in session_manager."""
    sid, _msg = _mk_session("native")
    # Direct mutator call (no orchs setup needed) — the bug we're
    # guarding against is that worker forks would otherwise bump
    # counters that nothing renders.
    # Synthesize a delegate_fork via the session_manager API.
    fork = session_manager.create_delegate_fork(
        parent_agent_session_id=sid,
        caller_agent_session_id=sid,
        parent_agent_sid_at_fork="fake-sid",
        parent_line_count_at_fork=0,
        orchestration_mode="native",
    )
    fork_id = fork["id"]
    # Refresh manager's cache for the parent root so the fork is
    # discoverable.
    session_manager._roots.pop(sid, None)
    # Bumps on the fork sid must be no-ops.
    pre_root = session_manager.get_unread_count(sid)
    session_manager.bump_unread(fork_id, "dummy-msg-id")
    post_root = session_manager.get_unread_count(sid)
    assert post_root == pre_root, (
        f"worker fork bump leaked into root unread "
        f"(pre={pre_root}, post={post_root})"
    )
    assert session_manager.get_unread_count(fork_id) == 0, (
        "worker fork unread must stay 0 (mutator filters by kind)"
    )
    print(f"{PASS} worker_fork_does_not_bump_root")


def main() -> int:
    try:
        test_append_bumps_unread()
        test_replace_does_not_bump()
        test_mark_seen_zeros()
        test_mark_seen_does_not_copy_session_tree()
        test_mark_seen_uses_journal_latest_uid()
        test_mark_seen_avoids_full_tree_write()
        test_seen_cursor_write_is_idempotent()
        test_mark_unread_clears_seen_sidecar()
        test_persistence_across_reload()
        test_worker_fork_does_not_bump_root()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

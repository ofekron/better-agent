"""Locks the per-project aggregate enrichment computed by
`projects_api._project_aggregates`:

1. Two user-kind sessions running in the same cwd → running_count=2.
2. After one completes, running_count=1.
3. Two sessions with unread messages → unread_session_count=2.
4. Worker forks (`delegate_fork`, etc.) are excluded — they don't
   inflate either count.
5. `/api/sessions` enrichment carries `is_running` + `unread_count`
   per row; sidebar consumers read it directly.

Run with:
    cd backend && .venv/bin/python scripts/test_project_aggregates.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-projagg-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_installation  # noqa: E402

_test_installation.activate(Path(_TMP_HOME))

from starlette.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

# Import after the env tempdir is set — main.py wires the coordinator
# singleton at import time.
import main as backend_main  # noqa: E402
import projects_api  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


CWD = "/tmp/test-projagg"


def _mk_session() -> str:
    sess = session_manager.create(
        name="t", model="sonnet", cwd=CWD,
        orchestration_mode="native", source="cli",
    )
    return sess["id"]


def _native_event(uuid: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": "x"},
        },
    }


def test_running_count_aggregation() -> None:
    s1 = _mk_session()
    s2 = _mk_session()
    s3 = _mk_session()
    coord = backend_main.coordinator
    # Simulate production flow: active_run_ids is set before run_state_add
    # so _prune_dead_entries sees the entries as managed (not orphaned).
    coord.active_run_ids[s1] = ["r1"]
    coord.active_run_ids[s2] = ["r2"]
    coord.run_state_add(s1, run_id="r1", kind="native", target_message_id=None)
    coord.run_state_add(s2, run_id="r2", kind="native", target_message_id=None)
    coord.turn_manager._refresh_cache()
    projects_api.invalidate_project_aggregates()

    aggs = projects_api._project_aggregates()
    key = (CWD, "primary")
    assert key in aggs, f"project {CWD} missing from aggs: {aggs}"
    rc = aggs[key]["running_count"]
    assert rc == 2, f"expected running_count=2 ({s1},{s2} running; {s3} idle), got {rc}"

    coord.active_run_ids.pop(s1, None)
    coord.run_state_remove(s1, "r1")
    coord.turn_manager._refresh_cache()
    projects_api.invalidate_project_aggregates()
    aggs = projects_api._project_aggregates()
    rc = aggs[key]["running_count"]
    assert rc == 1, f"after one completes expected 1, got {rc}"
    coord.active_run_ids.pop(s2, None)
    coord.run_state_remove(s2, "r2")
    print(f"{PASS} running_count_aggregation")


def test_unread_session_count_aggregation() -> None:
    s1 = _mk_session()
    s2 = _mk_session()
    # Append assistant scaffolds + 2 events on s1, 3 on s2.
    for sid, n in [(s1, 2), (s2, 3)]:
        strategy = get_strategy("native")
        scaffold = strategy.build_assistant_scaffold()
        session_manager.append_assistant_msg(sid, scaffold)
        msg_ref = session_manager._cached(sid)["messages"][-1]
        ctx = ApplyEventCtx(root_id=sid)
        for i in range(n):
            strategy.apply_event(
                app_session_id=sid, msg=msg_ref,
                event=_native_event(f"{sid[:4]}-{i}"),
                ctx=ctx, source_is_provider_stream=False,
            )
        session_manager.warm_unread(sid)
    projects_api.invalidate_project_aggregates()

    aggs = projects_api._project_aggregates()
    key = (CWD, "primary")
    total = aggs[key]["unread_session_count"]
    assert total == 2, f"expected unread_session_count=2, got {total}"
    print(f"{PASS} unread_session_count_aggregation")


def test_worker_fork_excluded_from_aggregates() -> None:
    """A delegate_fork lives embedded in its parent's tree — it's NOT
    a sidebar root, so `session_manager.list()` already filters it out.
    Result: no matter what state the worker fork is in, it can't leak
    into the project aggregate."""
    root = _mk_session()
    fork = session_manager.create_delegate_fork(
        parent_agent_session_id=root,
        caller_agent_session_id=root,
        parent_agent_sid_at_fork="fake-sid",
        parent_line_count_at_fork=0,
        orchestration_mode="native",
    )
    session_manager._roots.pop(root, None)

    # Run on the fork — running flag stays off at the user level by
    # design (mutator filter), so the aggregate shouldn't see it.
    coord = backend_main.coordinator
    coord.active_run_ids[fork["id"]] = ["rw"]
    coord.run_state_add(fork["id"], run_id="rw", kind="worker", target_message_id=None)
    coord.turn_manager._refresh_cache()
    projects_api.invalidate_project_aggregates()

    aggs = projects_api._project_aggregates()
    key = (CWD, "primary")
    # Only the root session counts. It's NOT running (we didn't
    # run_state_add on the root sid), so running_count is 0.
    rc = aggs.get(key, {"running_count": 0})["running_count"]
    assert rc == 0, (
        f"worker fork must not inflate project running_count; got {rc}"
    )
    coord.active_run_ids.pop(fork["id"], None)
    coord.run_state_remove(fork["id"], "rw")
    print(f"{PASS} worker_fork_excluded_from_aggregates")


def _sessions_page(accept_encoding: str) -> dict:
    """Fetch `/api/sessions` over the real ASGI stack. The endpoint returns a
    gzip-negotiated `Response`, not a dict, and its query params are FastAPI
    `Query` defaults — so the sidebar payload is only reachable through an
    actual request."""
    client = TestClient(
        backend_main.app,
        client=("127.0.0.1", 50000),
        base_url="http://localhost:8000",
    )
    client.headers.update({
        "Authorization": f"Bearer {auth.create_token('native')}",
        "Accept-Encoding": accept_encoding,
    })
    resp = client.get("/api/sessions", params={"limit": 200, "project_path": CWD})
    assert resp.status_code == 200, f"/api/sessions -> {resp.status_code}: {resp.text}"
    encoded = resp.headers.get("content-encoding", "")
    if accept_encoding == "identity":
        assert encoded != "gzip", "identity request must not be gzip-encoded"
    return resp.json()


def _enriched_row(payload: dict, sid: str) -> dict:
    rows = payload["sessions"]
    row = next((r for r in rows if r.get("id") == sid), None)
    assert row is not None, f"session {sid} missing from /api/sessions output"
    return row


# The contract this test exists to protect: the sidebar-enrichment fields
# `get_sessions` decorates onto every row. Timestamps and other summary
# fields are deliberately NOT part of it — `session_store.list_sessions`
# documents that the summary index may lag in-memory mutations by up to
# `PERSIST_DEBOUNCE_S`, so comparing whole rows across two requests races
# that window.
_ENRICHMENT_FIELDS = ("is_running", "unread_count")


def _enrichment(row: dict) -> dict:
    return {field: row.get(field) for field in _ENRICHMENT_FIELDS}


def test_session_list_enrichment() -> None:
    """The `/api/sessions` enrichment carries `is_running` +
    `unread_count` per row. Mirrors the sidebar's render path."""
    sid = _mk_session()
    coord = backend_main.coordinator
    coord.active_run_ids[sid] = ["rr"]
    coord.run_state_add(sid, run_id="rr", kind="native", target_message_id=None)
    coord.turn_manager._refresh_cache()
    projects_api.invalidate_project_aggregates()
    # Force one event so unread > 0.
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, scaffold)
    msg_ref = session_manager._cached(sid)["messages"][-1]
    ctx = ApplyEventCtx(root_id=sid)
    strategy.apply_event(
        app_session_id=sid, msg=msg_ref,
        event=_native_event("enrich-u"),
        ctx=ctx, source_is_provider_stream=False,
    )
    session_manager.warm_unread(sid)

    expected = {"is_running": True, "unread_count": 1}
    # `request` only carries Accept-Encoding into this endpoint (transport
    # encoding + response-cache key), so the enrichment fields must be
    # identical on both encodings.
    for accept_encoding in ("gzip", "identity"):
        row = _enriched_row(_sessions_page(accept_encoding), sid)
        assert _enrichment(row) == expected, (
            f"enrichment for accept-encoding={accept_encoding} expected "
            f"{expected}, got {_enrichment(row)}"
        )
    coord.active_run_ids.pop(sid, None)
    coord.run_state_remove(sid, "rr")
    print(f"{PASS} session_list_enrichment")


def test_projection_snapshot_changes_without_tick_refresh() -> None:
    """Run admission/removal must immediately update the REST projection.

    Reconnect snapshots cannot depend on the two-second diagnostic cache:
    a client can miss the non-replayable monitoring frame and reconnect
    before that cache refreshes.
    """
    sid = _mk_session()
    coord = backend_main.coordinator
    version_before = session_manager.projected_state_version()
    projects_api.invalidate_project_aggregates()
    key = (CWD, "primary")
    running_before = projects_api._project_aggregates().get(
        key, {"running_count": 0}
    )["running_count"]

    coord.active_run_ids[sid] = ["projection-run"]
    coord.run_state_add(
        sid,
        run_id="projection-run",
        kind="native",
        target_message_id=None,
    )
    running, monitoring = session_manager.projected_state_snapshot()
    assert monitoring.get(sid) == "active", monitoring
    assert sid in running
    assert session_manager.projected_state_version() > version_before
    assert projects_api._project_aggregates()[key]["running_count"] == running_before + 1
    row = _enriched_row(_sessions_page("identity"), sid)
    assert row.get("monitoring_state") == "active", row

    version_running = session_manager.projected_state_version()
    coord.active_run_ids.pop(sid, None)
    coord.run_state_remove(sid, "projection-run")
    running, monitoring = session_manager.projected_state_snapshot()
    assert monitoring.get(sid, "stopped") == "stopped", monitoring
    assert sid not in running
    assert session_manager.projected_state_version() > version_running
    assert projects_api._project_aggregates().get(
        key, {"running_count": 0}
    )["running_count"] == running_before
    row = _enriched_row(_sessions_page("identity"), sid)
    assert row.get("monitoring_state") == "stopped", row
    version_stopped = session_manager.projected_state_version()
    session_manager.recompute_state(sid)
    assert session_manager.projected_state_version() == version_stopped
    print(f"{PASS} projection_snapshot_changes_without_tick_refresh")


def test_aggregates_invalidate_without_manual_call() -> None:
    """Regression: every OTHER test in this file manually calls
    `projects_api.invalidate_project_aggregates()` after each mutation —
    that was papering over a real gap. In production nothing called it
    except project CRUD (`main.py:_broadcast_projects_changed`); no
    session-level mutation (running/unread/error/marker) ever did, so
    once `/api/projects` was read once, the badges froze stale for the
    life of the process. `main.py` now wires
    `event_bus_subscribers.bind_project_aggregates_invalidation()`
    alongside the WS broadcaster bind, so a `session.error_changed` bus
    event (fired by `set_unseen_error`) must invalidate the cache with
    NO manual `invalidate_project_aggregates()` call in this test.

    `session_manager._fire`'s bus-publish path only activates once
    `session_manager.bind_loop` has run (real app startup does this in
    `app_lifecycle.py`; this standalone script never starts the ASGI
    lifespan, so it's bound here) — and the publish is scheduled via
    `create_task`, landing on the loop's NEXT tick, not synchronously
    inside `set_unseen_error`. `asyncio.sleep(0)` yields once to let
    that scheduled subscriber actually run before asserting."""
    async def _scenario() -> None:
        session_manager.bind_loop(asyncio.get_running_loop())
        sid = _mk_session()
        key = (CWD, "primary")

        aggs = projects_api._project_aggregates()
        assert aggs.get(key, {"errored_count": 0})["errored_count"] == 0, (
            "setup: session must start with no error"
        )

        session_manager.set_unseen_error(sid, "boom")
        await asyncio.sleep(0)

        # No `invalidate_project_aggregates()` call here — the bus
        # subscription wired by `bind_project_aggregates_invalidation`
        # in main.py must have already invalidated the cache reactively.
        aggs = projects_api._project_aggregates()
        ec = aggs[key]["errored_count"]
        assert ec == 1, (
            f"project errored_count did not update after set_unseen_error "
            f"with no manual invalidate call — the aggregate cache is "
            f"stale/frozen; expected 1, got {ec}"
        )

        session_manager.clear_unseen_error(sid)
        await asyncio.sleep(0)
        aggs = projects_api._project_aggregates()
        ec = aggs[key]["errored_count"]
        assert ec == 0, (
            f"project errored_count did not clear after clear_unseen_error "
            f"with no manual invalidate call; expected 0, got {ec}"
        )

    asyncio.run(_scenario())
    print(f"{PASS} aggregates_invalidate_without_manual_call")


def test_waiting_for_user_marker_reflected_without_summary_rebuild() -> None:
    """Coverage lock: `session_status.compute`'s waiting_for_user reads
    `session["markers"]` off the SUMMARY snapshot (what
    `session_manager.list()` returns). `set_marker`/`clear_marker` route
    through `session_store.set_marker_projection`, which ALREADY
    refreshes that summary field synchronously
    (`_replace_summary_projection_field(sid, "markers", ...)` at
    session_store.py:1422) — so, unlike the errored-dimension case
    above, this path was never actually stale. This test locks that in
    and additionally proves the project aggregate's
    `waiting_for_user_count` reacts through the
    `bind_project_aggregates_invalidation` bus wiring with no manual
    `invalidate_project_aggregates()` call, same as the errored case."""
    from session_status import MARKER_TAG_NEEDS_DECISION

    async def _scenario() -> None:
        # Rebind: a prior test's `asyncio.run` closed its loop, so
        # `session_manager._loop` (bound once, module-singleton) may be
        # stale/closed here — same reasoning as
        # `test_aggregates_invalidate_without_manual_call` above.
        session_manager.bind_loop(asyncio.get_running_loop())
        sid = _mk_session()
        key = (CWD, "primary")

        aggs = projects_api._project_aggregates()
        assert aggs.get(key, {"waiting_for_user_count": 0})["waiting_for_user_count"] == 0, (
            "setup: session must start with no pending marker"
        )

        session_manager.set_marker(
            sid, "ofek-dev.user-attention",
            {"tag": MARKER_TAG_NEEDS_DECISION, "color": "#f97316"},
        )
        await asyncio.sleep(0)
        # No manual invalidate_project_aggregates() call — relies on the
        # same bus-driven invalidation as the errored-dimension test above.
        aggs = projects_api._project_aggregates()
        wc = aggs[key]["waiting_for_user_count"]
        assert wc == 1, (
            f"project waiting_for_user_count did not update after set_marker "
            f"with no summary rebuild in between; expected 1, got {wc}"
        )

        session_manager.clear_marker(sid, "ofek-dev.user-attention")
        await asyncio.sleep(0)
        aggs = projects_api._project_aggregates()
        wc = aggs[key]["waiting_for_user_count"]
        assert wc == 0, (
            f"project waiting_for_user_count did not clear after clear_marker "
            f"with no summary rebuild in between; expected 0, got {wc}"
        )

    asyncio.run(_scenario())
    print(f"{PASS} waiting_for_user_marker_reflected_without_summary_rebuild")


def test_aggregates_rebalance_after_cwd_move_without_manual_call() -> None:
    """Regression: `set_selectors` is the only mutator of a session's
    `cwd` (fires kind `selectors_set`), and `_project_aggregates` buckets
    counters by `(cwd, node_id)` — moving a session to a different
    project's cwd must invalidate BOTH the old bucket (losing a member)
    and the new one (gaining it) with no manual
    `invalidate_project_aggregates()` call. `selectors_set` was missing
    from `_PROJECT_AGGREGATE_INVALIDATING_KINDS` until this fix — without
    it, a moved session's `running_count` would keep counting it under
    the OLD project and never appear under the new one until some
    unrelated event happened to fire for it."""
    other_cwd = "/tmp/test-projagg-other"

    async def _scenario() -> None:
        session_manager.bind_loop(asyncio.get_running_loop())
        sid = _mk_session()
        old_key = (CWD, "primary")
        new_key = (other_cwd, "primary")

        coord = backend_main.coordinator
        coord.active_run_ids[sid] = ["rmove"]
        coord.run_state_add(sid, run_id="rmove", kind="native", target_message_id=None)
        coord.turn_manager._refresh_cache()
        projects_api.invalidate_project_aggregates()

        aggs = projects_api._project_aggregates()
        assert aggs[old_key]["running_count"] >= 1, (
            "setup: session must be counted running under its original cwd"
        )

        session_manager.set_selectors(sid, cwd=other_cwd)
        await asyncio.sleep(0)

        # No manual invalidate_project_aggregates() call here.
        aggs = projects_api._project_aggregates()
        assert sid not in [
            s.get("id") for s in session_manager.list()
            if (s.get("cwd") or "") == CWD
        ], "setup: session must no longer report the original cwd"
        new_rc = aggs.get(new_key, {"running_count": 0})["running_count"]
        assert new_rc >= 1, (
            f"project running_count did not pick up the session under its "
            f"NEW cwd after set_selectors with no manual invalidate call; "
            f"got {new_rc}"
        )

        coord.active_run_ids.pop(sid, None)
        coord.run_state_remove(sid, "rmove")

    asyncio.run(_scenario())
    print(f"{PASS} aggregates_rebalance_after_cwd_move_without_manual_call")


def main() -> int:
    try:
        test_running_count_aggregation()
        test_unread_session_count_aggregation()
        test_worker_fork_excluded_from_aggregates()
        test_session_list_enrichment()
        test_projection_snapshot_changes_without_tick_refresh()
        test_aggregates_invalidate_without_manual_call()
        test_waiting_for_user_marker_reflected_without_summary_rebuild()
        test_aggregates_rebalance_after_cwd_move_without_manual_call()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

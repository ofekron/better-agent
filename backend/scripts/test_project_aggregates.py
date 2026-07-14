"""Locks the per-project aggregate enrichment computed by
`main._project_aggregates`:

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
import json
import os
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-projagg-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import Coordinator  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

# Import after the env tempdir is set — main.py wires the coordinator
# singleton at import time.
import main as backend_main  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


CWD = "/tmp/test-projagg"


def _reset_aggregate_cache() -> None:
    with backend_main._project_aggregates_condition:
        backend_main._project_aggregates_cache = ()
        backend_main._project_aggregates_cached_gen = -1
        backend_main._project_aggregates_desired_gen += 1
        backend_main._project_aggregates_expires_at = 0.0
        backend_main._project_aggregates_git_gen = -1
        backend_main._project_aggregates_producer = None
        backend_main._project_aggregates_condition.notify_all()


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
    backend_main._invalidate_project_aggregates()

    aggs = backend_main._project_aggregates()
    key = (CWD, "primary")
    assert key in aggs, f"project {CWD} missing from aggs: {aggs}"
    rc = aggs[key]["running_count"]
    assert rc == 2, f"expected running_count=2 ({s1},{s2} running; {s3} idle), got {rc}"

    coord.run_state_remove(s1, "r1")
    coord.turn_manager._refresh_cache()
    backend_main._invalidate_project_aggregates()
    aggs = backend_main._project_aggregates()
    rc = aggs[key]["running_count"]
    assert rc == 1, f"after one completes expected 1, got {rc}"
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
    backend_main._invalidate_project_aggregates()

    aggs = backend_main._project_aggregates()
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
    backend_main._invalidate_project_aggregates()

    aggs = backend_main._project_aggregates()
    key = (CWD, "primary")
    # Only the root session counts. It's NOT running (we didn't
    # run_state_add on the root sid), so running_count is 0.
    rc = aggs.get(key, {"running_count": 0})["running_count"]
    assert rc == 0, (
        f"worker fork must not inflate project running_count; got {rc}"
    )
    coord.run_state_remove(fork["id"], "rw")
    print(f"{PASS} worker_fork_excluded_from_aggregates")


def test_session_list_enrichment() -> None:
    """The `/api/sessions` enrichment carries `is_running` +
    `unread_count` per row. Mirrors the sidebar's render path."""
    sid = _mk_session()
    coord = backend_main.coordinator
    coord.active_run_ids[sid] = ["rr"]
    coord.run_state_add(sid, run_id="rr", kind="native", target_message_id=None)
    coord.turn_manager._refresh_cache()
    backend_main._invalidate_project_aggregates()
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

    # Drive the enrichment by hand — get_sessions is an async coroutine.
    payload = asyncio.run(backend_main.get_sessions(
        offset=0,
        limit=50,
        project_path=None,
        search=None,
        show_archived=False,
        file_edit_mode=None,
        folder_ids=None,
        folder_view=None,
        tag_ids=None,
        provider_ids=None,
        model_ids=None,
        modes=None,
        sources=None,
        search_fields=None,
        sort_by=None,
        cwd_prefix=None,
    ))
    rows = json.loads(payload.body)["sessions"]
    target = next((r for r in rows if r.get("id") == sid), None)
    assert target is not None, f"session {sid} missing from /api/sessions output"
    assert target.get("is_running") is True, (
        f"is_running expected True, got {target.get('is_running')}"
    )
    assert target.get("unread_count") == 1, (
        f"unread_count expected 1, got {target.get('unread_count')}"
    )
    coord.run_state_remove(sid, "rr")
    print(f"{PASS} session_list_enrichment")


def test_empty_results_are_cached() -> None:
    _reset_aggregate_cache()
    calls = 0

    def empty_list():
        nonlocal calls
        calls += 1
        return []

    with patch.object(session_manager, "list", empty_list):
        assert backend_main._project_aggregates() == {}
        assert backend_main._project_aggregates() == {}
    assert calls == 1, f"empty aggregate recomputed {calls} times"
    print(f"{PASS} empty_results_are_cached")


def test_cached_results_are_defensive_copies() -> None:
    _reset_aggregate_cache()
    sessions = [{"id": "copy-sid", "cwd": CWD, "node_id": "primary"}]
    with (
        patch.object(session_manager, "list", return_value=sessions),
        patch.object(session_manager, "monitoring_projection_snapshot", return_value={}),
        patch.object(session_manager, "unread_counts_snapshot", return_value={}),
        patch(
            "git_repo_info.repo_common_dir_with_expiry",
            return_value=(None, float("inf"), 0),
        ),
        patch("git_repo_info.cache_generation_snapshot", return_value=0),
    ):
        first = backend_main._project_aggregates()
        first[(CWD, "primary")]["running_count"] = 99
        second = backend_main._project_aggregates()
    assert second[(CWD, "primary")]["running_count"] == 0
    print(f"{PASS} cached_results_are_defensive_copies")


def test_concurrent_cold_reads_have_one_producer() -> None:
    _reset_aggregate_cache()
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    active = 0
    max_active = 0
    guard = threading.Lock()

    def blocked_list():
        nonlocal calls, active, max_active
        with guard:
            calls += 1
            active += 1
            max_active = max(max_active, active)
        entered.set()
        assert release.wait(5)
        with guard:
            active -= 1
        return []

    results: list[dict] = []
    with patch.object(session_manager, "list", blocked_list):
        threads = [
            threading.Thread(
                target=lambda: results.append(backend_main._project_aggregates())
            )
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        assert entered.wait(5)
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert calls == 1, f"expected one producer, got {calls}"
    assert max_active == 1
    assert results == [{}] * 8
    print(f"{PASS} concurrent_cold_reads_have_one_producer")


def test_invalidation_retries_once_without_overlapping_producers() -> None:
    _reset_aggregate_cache()
    entered = [threading.Event(), threading.Event()]
    releases = [threading.Event(), threading.Event()]
    calls = 0

    def blocked_list():
        nonlocal calls
        index = calls
        calls += 1
        if index < 2:
            entered[index].set()
            assert releases[index].wait(5)
        return []

    result: list[dict] = []
    with patch.object(session_manager, "list", blocked_list):
        thread = threading.Thread(
            target=lambda: result.append(backend_main._project_aggregates())
        )
        thread.start()
        assert entered[0].wait(5)
        backend_main._invalidate_project_aggregates()
        releases[0].set()
        assert entered[1].wait(5)
        releases[1].set()
        thread.join(5)
        assert not thread.is_alive()
    assert calls == 2
    assert result == [{}]
    print(f"{PASS} invalidation_retries_once_without_overlapping_producers")


def test_producer_exception_releases_waiters() -> None:
    _reset_aggregate_cache()
    entered = threading.Event()
    release = threading.Event()
    waiter_blocked = threading.Event()
    calls = 0

    def flaky_list():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
            raise RuntimeError("expected")
        return []

    errors: list[BaseException] = []
    results: list[dict] = []
    original_wait = backend_main._project_aggregates_condition.wait

    def observed_wait(*args, **kwargs):
        waiter_blocked.set()
        return original_wait(*args, **kwargs)

    def producer():
        try:
            backend_main._project_aggregates()
        except BaseException as exc:
            errors.append(exc)

    with (
        patch.object(session_manager, "list", flaky_list),
        patch.object(
            backend_main._project_aggregates_condition,
            "wait",
            side_effect=observed_wait,
        ),
    ):
        producer_thread = threading.Thread(target=producer)
        producer_thread.start()
        assert entered.wait(5)
        waiter_thread = threading.Thread(
            target=lambda: results.append(backend_main._project_aggregates())
        )
        waiter_thread.start()
        assert waiter_blocked.wait(5)
        release.set()
        producer_thread.join(5)
        waiter_thread.join(5)
        assert not producer_thread.is_alive()
        assert not waiter_thread.is_alive()
        assert backend_main._project_aggregates() == {}
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert results == [{}]
    assert calls == 2
    print(f"{PASS} producer_exception_releases_waiters")


def test_second_invalidation_hands_uncached_result_only_to_producer() -> None:
    _reset_aggregate_cache()
    entered = [threading.Event() for _ in range(3)]
    release = [threading.Event() for _ in range(3)]
    waiter_blocked = threading.Event()
    calls = 0

    def changing_list():
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        assert release[index].wait(5)
        return [{
            "id": f"sid-{index}",
            "cwd": f"/tmp/pass-{index}",
            "node_id": "primary",
        }]

    original_wait = backend_main._project_aggregates_condition.wait

    def observed_wait(*args, **kwargs):
        waiter_blocked.set()
        return original_wait(*args, **kwargs)

    producer_results: list[dict] = []
    waiter_results: list[dict] = []
    with (
        patch.object(session_manager, "list", changing_list),
        patch.object(session_manager, "monitoring_projection_snapshot", return_value={}),
        patch.object(session_manager, "unread_counts_snapshot", return_value={}),
        patch(
            "git_repo_info.repo_common_dir_with_expiry",
            side_effect=lambda cwd: (None, float("inf"), 0),
        ),
        patch("git_repo_info.cache_generation_snapshot", return_value=0),
        patch.object(
            backend_main._project_aggregates_condition,
            "wait",
            side_effect=observed_wait,
        ),
    ):
        producer = threading.Thread(
            target=lambda: producer_results.append(
                backend_main._project_aggregates()
            )
        )
        producer.start()
        assert entered[0].wait(5)
        backend_main._invalidate_project_aggregates()
        release[0].set()
        assert entered[1].wait(5)
        waiter = threading.Thread(
            target=lambda: waiter_results.append(
                backend_main._project_aggregates()
            )
        )
        waiter.start()
        assert waiter_blocked.wait(5)
        backend_main._invalidate_project_aggregates()
        release[1].set()
        assert entered[2].wait(5)
        release[2].set()
        producer.join(5)
        waiter.join(5)
        assert not producer.is_alive()
        assert not waiter.is_alive()
        cached = backend_main._project_aggregates()
    assert set(producer_results[0]) == {("/tmp/pass-1", "primary")}
    assert set(waiter_results[0]) == {("/tmp/pass-2", "primary")}
    assert set(cached) == {("/tmp/pass-2", "primary")}
    assert calls == 3
    print(f"{PASS} second_invalidation_hands_uncached_result_only_to_producer")


def test_selector_owner_emits_precise_cwd_change() -> None:
    sid = _mk_session()
    with patch.object(session_manager, "_fire") as fire:
        session_manager.set_selectors(sid, cwd=CWD)
        same = fire.call_args.args[1]
        session_manager.set_selectors(sid, provider_id="codex")
        provider_only = fire.call_args.args[1]
        session_manager.set_selectors(sid, cwd=f"{CWD}-moved")
        moved = fire.call_args.args[1]
    assert same["cwd_changed"] is False
    assert provider_only["cwd_changed"] is False
    assert moved["cwd_changed"] is True
    print(f"{PASS} selector_owner_emits_precise_cwd_change")


def main() -> int:
    try:
        test_running_count_aggregation()
        test_unread_session_count_aggregation()
        test_worker_fork_excluded_from_aggregates()
        test_session_list_enrichment()
        test_empty_results_are_cached()
        test_cached_results_are_defensive_copies()
        test_concurrent_cold_reads_have_one_producer()
        test_invalidation_retries_once_without_overlapping_producers()
        test_producer_exception_releases_waiters()
        test_second_invalidation_hands_uncached_result_only_to_producer()
        test_selector_owner_emits_precise_cwd_change()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import _test_home

_test_home.isolate("bc-test-sync-wait-graph-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ask_status_store  # noqa: E402
import main  # noqa: E402
import session_bridge  # noqa: E402
from orchestrator import Coordinator  # noqa: E402
from orchs.manager._delegation import (  # noqa: E402
    _run_with_sync_wait,
    run_delegation_locked,
)
from sync_wait_graph import CircularSyncWaitError, SyncWaitGraph  # noqa: E402
from stores import worker_store  # noqa: E402


def test_rejects_self_and_transitive_cycles() -> None:
    graph = SyncWaitGraph()

    with pytest.raises(CircularSyncWaitError, match="A -> A"):
        with graph.waiting("A", "A"):
            raise AssertionError("self wait entered")

    with graph.waiting("A", "B"):
        with graph.waiting("B", "C"):
            with pytest.raises(CircularSyncWaitError, match="C -> A -> B -> C"):
                with graph.waiting("C", "A"):
                    raise AssertionError("cycle entered")
            assert graph.snapshot() == {"A": {"B": 1}, "B": {"C": 1}}

    assert graph.snapshot() == {}


def test_duplicate_edges_are_reference_counted_and_pruned() -> None:
    graph = SyncWaitGraph()

    with graph.waiting("A", "B"):
        with graph.waiting("A", "B"):
            assert graph.snapshot() == {"A": {"B": 2}}
        assert graph.snapshot() == {"A": {"B": 1}}

    assert graph.snapshot() == {}


def test_exception_prunes_graph_without_suppressing_error() -> None:
    graph = SyncWaitGraph()

    with pytest.raises(RuntimeError, match="boom"):
        with graph.waiting("A", "B"):
            raise RuntimeError("boom")

    assert graph.snapshot() == {}


def test_cancelled_wait_prunes_graph() -> None:
    async def run() -> None:
        graph = SyncWaitGraph()
        entered = asyncio.Event()
        block = asyncio.Event()

        async def wait() -> None:
            with graph.waiting("A", "B"):
                entered.set()
                await block.wait()

        task = asyncio.create_task(wait())
        await entered.wait()
        assert graph.snapshot() == {"A": {"B": 1}}
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert graph.snapshot() == {}

    asyncio.run(run())


def test_direct_ask_rejects_cycle_but_cached_result_does_not_wait() -> None:
    async def run() -> None:
        coordinator = Coordinator()
        with coordinator.sync_wait_graph.waiting("target", "sender"):
            with pytest.raises(CircularSyncWaitError):
                await coordinator.ask_team_message(
                    sender_session_id="sender",
                    target_session_id="target",
                    message="cycle",
                )

            cached = {"success": True, "assistant_content": "already done"}
            ask_status_store.claim_route(
                "ask_cached_cycle",
                sender_session_id="sender",
                target_session_id="target",
            )
            ask_status_store.write_status(
                "ask_cached_cycle",
                result=cached,
            )
            result = await coordinator.ask_team_message(
                sender_session_id="sender",
                target_session_id="target",
                message="reattach",
                ask_id="ask_cached_cycle",
            )
            assert result == cached
            ask_status_store.delete_status("ask_cached_cycle")

    asyncio.run(run())


def test_direct_ask_rejects_completed_and_in_progress_route_mismatches() -> None:
    async def run() -> None:
        coordinator = Coordinator()
        ask_status_store.claim_route(
            "ask_completed_mismatch",
            sender_session_id="sender",
            target_session_id="target",
        )
        ask_status_store.write_status(
            "ask_completed_mismatch",
            result={"success": True},
        )
        with pytest.raises(ValueError, match="different route"):
            await coordinator.ask_team_message(
                sender_session_id="sender",
                target_session_id="other-target",
                message="wrong completed route",
                ask_id="ask_completed_mismatch",
            )

        ask_status_store.claim_route(
            "ask_progress_mismatch",
            sender_session_id="sender",
            target_session_id="target",
        )
        ask_status_store.write_status(
            "ask_progress_mismatch",
            lifecycle_msg_id="life",
            queue_item_id="queue",
        )
        with pytest.raises(ValueError, match="different route"):
            await coordinator.ask_team_message(
                sender_session_id="other-sender",
                target_session_id="target",
                message="wrong in-progress route",
                ask_id="ask_progress_mismatch",
            )

        assert coordinator.sync_wait_graph.snapshot() == {}

    asyncio.run(run())


def test_ask_route_claim_is_atomic() -> None:
    barrier = threading.Barrier(2)

    def claim(target: str) -> str:
        barrier.wait()
        try:
            ask_status_store.claim_route(
                "ask_atomic_route",
                sender_session_id="sender",
                target_session_id=target,
            )
        except ValueError:
            return "rejected"
        return target

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(claim, "target-a"),
            executor.submit(claim, "target-b"),
        ]
        results = {future.result() for future in futures}

    assert "rejected" in results
    assert len(results) == 2
    status = ask_status_store.read_status("ask_atomic_route")
    assert status["target_session_id"] in {"target-a", "target-b"}


def test_same_route_ask_id_dispatches_once() -> None:
    async def run() -> None:
        coordinator = Coordinator()
        entered = asyncio.Event()
        release = asyncio.Event()
        dispatches = 0

        async def fake_wait(**kwargs) -> dict:
            nonlocal dispatches
            dispatches += 1
            entered.set()
            await release.wait()
            result = {"success": True, "assistant_content": "done"}
            ask_status_store.write_status(kwargs["ask_id"], result=result)
            return result

        coordinator._ask_team_message_wait = fake_wait  # type: ignore[method-assign]
        calls = [
            asyncio.create_task(coordinator.ask_team_message(
                sender_session_id="sender",
                target_session_id="target",
                message="same",
                ask_id="ask_same_route",
            ))
            for _ in range(2)
        ]
        await entered.wait()
        assert dispatches == 1
        with pytest.raises(ValueError, match="different route"):
            await asyncio.wait_for(
                coordinator.ask_team_message(
                    sender_session_id="sender",
                    target_session_id="other-target",
                    message="conflict",
                    ask_id="ask_same_route",
                ),
                timeout=0.1,
            )
        release.set()
        first, second = await asyncio.gather(*calls)
        assert first == second
        assert dispatches == 1
        assert coordinator._ask_call_gates == {}

    asyncio.run(run())


def test_distinct_ask_ids_dispatch_independently() -> None:
    async def run() -> None:
        coordinator = Coordinator()
        both_entered = asyncio.Event()
        release = asyncio.Event()
        dispatched_ids = []

        async def fake_wait(**kwargs) -> dict:
            dispatched_ids.append(kwargs["ask_id"])
            if len(dispatched_ids) == 2:
                both_entered.set()
            await release.wait()
            result = {
                "success": True,
                "queued_id": f"queue-{kwargs['ask_id']}",
            }
            ask_status_store.write_status(kwargs["ask_id"], result=result)
            return result

        coordinator._ask_team_message_wait = fake_wait  # type: ignore[method-assign]
        calls = [
            asyncio.create_task(coordinator.ask_team_message(
                sender_session_id="sender",
                target_session_id="target",
                message="same payload",
                ask_id=ask_id,
            ))
            for ask_id in ("ask_distinct_a", "ask_distinct_b")
        ]
        await both_entered.wait()
        assert set(dispatched_ids) == {"ask_distinct_a", "ask_distinct_b"}
        assert coordinator.sync_wait_graph.snapshot() == {
            "sender": {"target": 2},
        }
        release.set()
        results = await asyncio.gather(*calls)
        assert {result["queued_id"] for result in results} == {
            "queue-ask_distinct_a",
            "queue-ask_distinct_b",
        }
        assert coordinator.sync_wait_graph.snapshot() == {}
        assert coordinator._ask_call_gates == {}

    asyncio.run(run())


def test_pool_ask_binds_worker_after_waiting_for_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        ask_id = "ask_pool_available"
        item = {
            "id": "pool-item",
            "tag": "review",
            "sender_session_id": "sender",
            "prompt": "review",
            "pool_affinity_key": "project",
            "wait_for_ask_response": True,
            "ask_id": ask_id,
        }
        queued_items = iter((item, item, None))
        targets = iter((None, {"agent_session_id": "worker"}))
        popped = []
        dispatches = 0
        coordinator = Coordinator()

        async def fake_wait(**kwargs) -> dict:
            nonlocal dispatches
            dispatches += 1
            result = {"success": True, "assistant_content": "done"}
            ask_status_store.write_status(kwargs["ask_id"], result=result)
            return result

        async def no_wait(_seconds: float) -> None:
            return None

        async def no_broadcast(_cwd) -> None:
            return None

        ask_status_store.claim_route(
            ask_id,
            sender_session_id="sender",
            target_session_id="",
            target_selector={
                "kind": "pool",
                "value": "review",
                "pool_affinity_key": "project",
            },
        )
        ask_status_store.write_status(
            ask_id,
            pool_queue_item_id="pool-item",
            pool_tag="review",
            sender_session_id="sender",
        )
        coordinator._ask_team_message_wait = fake_wait  # type: ignore[method-assign]
        coordinator.broadcast_workers_changed = no_broadcast  # type: ignore[method-assign]
        monkeypatch.setattr(main, "coordinator", coordinator)
        monkeypatch.setattr(worker_store, "peek_pool_task", lambda _tag: next(queued_items))
        monkeypatch.setattr(
            main,
            "_pick_pool_worker_for_sender",
            lambda *_args: next(targets),
        )
        monkeypatch.setattr(
            worker_store,
            "pop_pool_task",
            lambda tag, item_id: popped.append((tag, item_id)),
        )
        monkeypatch.setattr(main.asyncio, "sleep", no_wait)

        await main._process_worker_pool_queue("review")

        status = ask_status_store.read_status(ask_id)
        assert status["route_kind"] == "pool"
        assert status["route_value"] == "review"
        assert status["route_affinity_key"] == "project"
        assert status["target_session_id"] == "worker"
        assert status["result"]["success"] is True
        assert dispatches == 1
        assert popped == [("review", "pool-item")]
        assert coordinator.sync_wait_graph.snapshot() == {}

    asyncio.run(run())


def test_pool_ask_queues_when_no_worker_is_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        enqueued = []

        async def enqueue(**kwargs) -> dict:
            enqueued.append(kwargs)
            return {"item": {"id": "queued-pool-item"}}

        async def wait_for_result(ask_id: str, queued: dict) -> dict:
            assert ask_id == "ask_pool_queued"
            assert queued["item"]["id"] == "queued-pool-item"
            return {"success": True, "queued": True}

        monkeypatch.setattr(main, "_pick_pool_worker_for_sender", lambda *_args: None)
        monkeypatch.setattr(main, "_enqueue_worker_pool_message", enqueue)
        monkeypatch.setattr(main, "_wait_for_pool_ask_result", wait_for_result)

        result = await main._ask_wait_and_grab_last_assistant_mssg_in_turn(
            {
                "target_worker_pool": "review",
                "pool_affinity_key": "project",
                "ask_id": "ask_pool_queued",
            },
            "sender",
            "review",
            "",
            "",
        )

        assert result == {"success": True, "queued": True}
        assert enqueued[0]["wait_for_ask_response"] is True
        status = ask_status_store.read_status("ask_pool_queued")
        assert status["route_kind"] == "pool"
        assert status["route_value"] == "review"
        assert status["route_affinity_key"] == "project"
        assert status["pool_queue_item_id"] == "queued-pool-item"

    asyncio.run(run())


def test_manager_delegation_rejects_cycle() -> None:
    async def run() -> None:
        coordinator = Coordinator()
        called = False

        async def operation() -> dict:
            nonlocal called
            called = True
            return {"success": True}

        with coordinator.sync_wait_graph.waiting("worker", "caller"):
            with pytest.raises(CircularSyncWaitError):
                await _run_with_sync_wait(
                    coordinator,
                    "caller",
                    "worker",
                    operation,
                )
            assert called is False

    asyncio.run(run())


def test_manager_delegation_returns_terminal_error_on_cycle() -> None:
    async def run() -> None:
        graph = SyncWaitGraph()
        coordinator = SimpleNamespace(sync_wait_graph=graph)
        events = []

        async def ws_callback(event: dict) -> None:
            events.append(event)

        with graph.waiting("worker", "caller"):
            result = await run_delegation_locked(
                coordinator,
                app_session_id="caller",
                ws_callback=ws_callback,
                cancel_event=asyncio.Event(),
                delegation_id="del_cycle",
                worker_run_id="worker-del_cycle",
                instructions="work",
                instructions_preview="work",
                worker_agent_session_id="worker",
                worker_session={},
                worker_description="worker",
                worker_orchestration_mode="native",
                worker_parent_claude_sid=None,
                session_is_registered_worker=False,
                target_message_id=None,
                run_mode="direct",
                model="",
                cwd="/repo",
                panel={},
            )

        assert result["success"] is False
        assert "circular synchronous wait rejected" in result["error"]
        assert events == [{
            "type": "worker_complete",
            "data": {"delegation_id": "del_cycle", **result},
        }]

    asyncio.run(run())


def test_session_bridge_uses_concrete_run_identity_and_skips_extension_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        coordinator = Coordinator()
        called = False

        async def fake_run_turn(_sid: str, _prompt: str, **_kwargs) -> dict:
            nonlocal called
            called = True
            return {"text": "done"}

        monkeypatch.setattr(session_bridge, "_coordinator_for_wait_graph", lambda: coordinator)
        monkeypatch.setattr(session_bridge, "_run_turn", fake_run_turn)

        with coordinator.sync_wait_graph.waiting("run-child", "caller"):
            result = await session_bridge._run_turn_with_sync_wait(
                "caller",
                "run-child",
                "prompt",
            )
            assert "circular synchronous wait rejected" in result["error"]
            assert called is False

        assert await session_bridge._run_turn_with_sync_wait(
            "",
            "extension-target",
            "prompt",
        ) == {"text": "done"}
        assert called is True

    asyncio.run(run())

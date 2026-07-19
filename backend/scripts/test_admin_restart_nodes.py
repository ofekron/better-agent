#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-admin-restart-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import capability_api  # noqa: E402
import node_link  # noqa: E402
import node_store  # noqa: E402
from daemonhost.jsonio import write_json  # noqa: E402
from daemonhost.paths import switch_request_path  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _run() -> None:
    sent: list[str] = []

    def fake_snapshot() -> list[dict]:
        return [
            {"id": "primary", "role": "primary", "state": "connected"},
            {"id": "node-a", "role": "worker_node", "state": "connected"},
            {"id": "node-b", "role": "worker_node", "state": "disconnected"},
            {"id": "node-c", "role": "worker_node", "state": "unknown"},
            {"id": "node-d", "role": "worker_node", "state": "connected"},
        ]

    async def fake_send_restart(node_id: str) -> None:
        if node_id == "node-d":
            raise node_link.NodeOffline(node_id)
        sent.append(node_id)

    killed: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    old_supervisor = os.environ.get("BETTER_CLAUDE_RUN_SH_SUPERVISOR")
    old_snapshot = node_store.snapshot
    old_send_restart = node_link.send_restart
    old_kill = main.os.kill
    old_active_checkout = os.environ.get("BETTER_AGENT_ACTIVE_CHECKOUT")
    try:
        os.environ["BETTER_CLAUDE_RUN_SH_SUPERVISOR"] = "1"
        node_store.snapshot = fake_snapshot
        node_link.send_restart = fake_send_restart
        main.os.kill = fake_kill

        invalid_root = os.path.join(_TMP_HOME, "invalid-main")
        os.makedirs(os.path.join(invalid_root, "backend", ".venv", "bin"), exist_ok=True)
        for relative in ("backend/main.py", "backend/.venv/bin/python"):
            with open(os.path.join(invalid_root, relative), "w", encoding="utf-8"):
                pass
        os.environ["BETTER_AGENT_ACTIVE_CHECKOUT"] = invalid_root
        from daemonhost import pointer
        pointer.confirm_healthy(invalid_root)
        preparation_calls = 0

        async def count_preparation() -> list[str]:
            nonlocal preparation_calls
            preparation_calls += 1
            return []

        old_prepare = main._restart_connected_worker_nodes
        main._restart_connected_worker_nodes = count_preparation
        try:
            handler = capability_api._ACTIONS[("switch-control", "switch.request")].handler
            try:
                await handler(capability_api._SwitchTargetPayload(target="dev"))
            except ValueError:
                pass
            else:
                raise AssertionError("unknown line switch target must be rejected")
            assert preparation_calls == 0, "invalid switch restarted worker nodes before validation"
        finally:
            main._restart_connected_worker_nodes = old_prepare

        valid_dev = os.path.join(_TMP_HOME, "invalid")
        os.makedirs(os.path.join(valid_dev, "backend", ".venv", "bin"), exist_ok=True)
        for relative in (
            "backend/main.py",
            "backend/.venv/bin/python",
            "daemonhost/__init__.py",
            "daemonhost/pointer.py",
            "daemonhost/jsonio.py",
            "daemonhost/paths.py",
        ):
            path = os.path.join(valid_dev, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8"):
                pass
        preparation_started = asyncio.Event()
        release_preparation = asyncio.Event()
        preparation_calls = 0

        async def blocking_preparation() -> list[str]:
            nonlocal preparation_calls
            preparation_calls += 1
            preparation_started.set()
            await release_preparation.wait()
            return []

        main._restart_connected_worker_nodes = blocking_preparation
        try:
            first_call = asyncio.create_task(handler(capability_api._SwitchTargetPayload(target="dev")))
            await asyncio.wait_for(preparation_started.wait(), timeout=1)
            duplicate_result = await handler(capability_api._SwitchTargetPayload(target="dev"))
            assert duplicate_result["status"] == "preparing", duplicate_result
            assert preparation_calls == 1, "duplicate caller repeated worker-node preparation"
            release_preparation.set()
            first_result = await asyncio.wait_for(first_call, timeout=1)
            assert first_result["status"] == "pending", first_result
        finally:
            release_preparation.set()
            main._restart_connected_worker_nodes = old_prepare

        result = await main.admin_restart({"request_id": "restart-test"})
        await asyncio.sleep(0.35)

        assert sent == ["node-a"], f"expected only connected worker restart, got {sent!r}"
        assert result["restarted_nodes"] == ["node-a"], result
        assert killed, "primary restart signal was not scheduled"
        status = await main.admin_restart_status("restart-test")
        assert status["accepted"] is True, status
        assert status["status"] == "pending", status
        assert status["refresh_result"] is None, status

        write_json(switch_request_path(), {
            "request_id": "line-switch-test",
            "target": "dev",
            "status": "accepted",
            "error": "",
        })
        switch_status = await main.admin_restart_status("line-switch-test")
        assert switch_status["accepted"] is True, switch_status
        assert switch_status["status"] == "accepted", switch_status
        write_json(switch_request_path(), {
            "request_id": "line-switch-test",
            "target": "dev",
            "status": "failed",
            "error": "backend failed",
        })
        switch_status = await main.admin_restart_status("line-switch-test")
        assert switch_status["status"] == "failed", switch_status
        assert switch_status["error"] == "backend failed", switch_status
    finally:
        if old_supervisor is None:
            os.environ.pop("BETTER_CLAUDE_RUN_SH_SUPERVISOR", None)
        else:
            os.environ["BETTER_CLAUDE_RUN_SH_SUPERVISOR"] = old_supervisor
        node_store.snapshot = old_snapshot
        node_link.send_restart = old_send_restart
        main.os.kill = old_kill
        if old_active_checkout is None:
            os.environ.pop("BETTER_AGENT_ACTIVE_CHECKOUT", None)
        else:
            os.environ["BETTER_AGENT_ACTIVE_CHECKOUT"] = old_active_checkout
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


async def _run_idle_wait_test() -> None:
    session = session_manager.create(
        name="idle-wait",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    session_manager.add_queued_prompt(
        sid,
        {
            "id": "accepted-queued-1",
            "lifecycle_msg_id": "life-accepted-queued-1",
            "content": "accepted request",
            "kind": "send",
            "queue_position": 0,
            "images_count": 0,
            "orchestration_mode": "native",
            "client_id": "client-accepted-queued",
        },
    )

    waiter = asyncio.create_task(main._wait_for_all_agents_idle())
    try:
        await asyncio.sleep(0.05)
        assert not waiter.done(), "idle wait returned while accepted request was queued"
        session_manager.remove_queued_prompt(sid, "accepted-queued-1")
        await asyncio.wait_for(waiter, timeout=1.5)
    finally:
        if not waiter.done():
            waiter.cancel()
        session_manager.remove_queued_prompt(sid, "accepted-queued-1")


def main_test() -> int:
    try:
        asyncio.run(_run())
        asyncio.run(_run_idle_wait_test())
    except Exception as exc:
        print(f"{FAIL}: admin refresh restarts connected worker nodes: {exc}")
        return 1
    print(f"{PASS}: admin refresh restarts connected worker nodes")
    print(f"{PASS}: idle refresh waits for accepted queued requests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())

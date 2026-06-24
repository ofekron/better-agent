from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_hooks_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hook_store  # noqa: E402
from event_bus import BusEvent, EventBus, bus  # noqa: E402
from hook_runner import bind_configured_hooks  # noqa: E402


def _event(event_type: str = "lifecycle.turn_complete") -> BusEvent:
    return BusEvent(
        type=event_type,
        root_id="root-1",
        sid="sid-1",
        payload={"value": 42},
        msg_id="msg-1",
    )


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition timed out")


async def _run() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(f"  {'OK' if cond else 'FAIL'}  {label}")
        if not cond:
            failures.append(label)

    try:
        hook_store.replace_hooks([{
            "id": "bad",
            "pattern": "x",
            "command": "echo nope",
        }])
        check(False, "string commands are rejected")
    except hook_store.HookConfigError:
        check(True, "string commands are rejected")

    config_path = Path(os.environ["BETTER_CLAUDE_HOME"]) / "hooks" / "hooks.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{broken", encoding="utf-8")
    try:
        hook_store.list_hooks()
        check(False, "malformed hook config fails closed")
    except hook_store.HookConfigError:
        check(True, "malformed hook config fails closed")
    config_path.unlink()

    out = Path(os.environ["BETTER_CLAUDE_HOME"]) / "hook-out.json"
    script = Path(os.environ["BETTER_CLAUDE_HOME"]) / "hook.py"
    script.write_text(
        "import json, os, sys\n"
        "data=json.load(sys.stdin)\n"
        "data['env_seen']=os.environ.get('BETTER_CLAUDE_HOOK_ID')\n"
        f"open({str(out)!r}, 'w').write(json.dumps(data))\n",
        encoding="utf-8",
    )
    hooks = hook_store.replace_hooks([{
        "id": "capture",
        "name": "capture hook",
        "pattern": "lifecycle.*",
        "command": [sys.executable, str(script)],
        "timeout_seconds": 2,
    }])
    check(hooks[0]["enabled"] is True, "hook defaults to enabled")
    check(hooks[0]["timeout_seconds"] == 2.0, "timeout normalizes to float")

    meta: list[BusEvent] = []

    async def record_meta(ev: BusEvent) -> None:
        meta.append(ev)

    bus.unsubscribe("hook-test-meta")
    bus.subscribe("hook.*", record_meta, name="hook-test-meta")
    bind_configured_hooks()
    await bus.publish(_event())
    await _wait_until(out.exists)
    captured = json.loads(out.read_text(encoding="utf-8"))
    check(captured["event"]["type"] == "lifecycle.turn_complete", "event envelope reaches hook stdin")
    check(captured["event"]["payload"] == {"value": 42}, "payload reaches hook stdin")
    check(captured["env_seen"] == "capture", "hook env includes hook id")
    await _wait_until(lambda: any(ev.type == "hook.completed" for ev in meta))
    check(any(ev.type == "hook.started" for ev in meta), "hook.started published")
    check(any(ev.type == "hook.completed" for ev in meta), "hook.completed published")
    check(all(ev.persist is False for ev in meta), "hook meta events are not persisted")

    out.unlink()
    await bus.publish(_event("hook.started"))
    await asyncio.sleep(0.1)
    check(not out.exists(), "hook meta events do not recursively trigger hooks")

    slow = Path(os.environ["BETTER_CLAUDE_HOME"]) / "slow.py"
    slow.write_text("import time\ntime.sleep(1)\n", encoding="utf-8")
    hook_store.replace_hooks([{
        "id": "slow",
        "pattern": "timeout.*",
        "command": [sys.executable, str(slow)],
        "timeout_seconds": 0.05,
    }])
    meta.clear()
    await bus.publish(_event("timeout.test"))
    await _wait_until(lambda: any(ev.type == "hook.failed" for ev in meta))
    failed = next(ev for ev in meta if ev.type == "hook.failed")
    check(failed.payload.get("error_class") == "TimeoutError", "timed-out hook publishes hook.failed")

    local_bus = EventBus()
    check(local_bus.describe() == [], "independent EventBus remains independent")

    import hook_runner

    original_list_hooks = hook_runner.hook_store.list_hooks
    list_started = asyncio.Event()

    def slow_list_hooks():
        list_started.set()
        import time
        time.sleep(0.2)
        return []

    hook_runner.hook_store.list_hooks = slow_list_hooks
    try:
        task = asyncio.create_task(hook_runner._dispatch_matching_hooks(_event("offloop.test")))
        await asyncio.wait_for(list_started.wait(), timeout=1)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.05)
        check(not task.done(), "hook list runs off the event loop")
        await task
    finally:
        hook_runner.hook_store.list_hooks = original_list_hooks

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nhook system checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import _test_home

_test_home.isolate("bc-terminal-authority-")

from provider import Provider
from turn_manager import _should_defer_dead_runner_fallback

failures = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


class FakeProvider(Provider):
    KIND = "fake"

    def build_env(self):
        return {}

    def start_run(self, **kwargs):
        return None

    def cancel_turn(self, run_id: str) -> None:
        return None

    def cancel_all(self) -> None:
        return None

    def _write_backend_state(self, rs) -> None:
        return None

    def recover_in_flight(self, loop=None, run_id_filter=None):
        return []

    def prune_old_runs(self, max_age_days: int = 7) -> int:
        return 0

    async def run_headless(self, **kwargs):
        return None

    async def rewind(self, rewind_session_id: str, message_uuid: str) -> None:
        return None

    def list_models(self):
        return []


async def scenario():
    provider = FakeProvider({"id": "fake"})
    delayed_provider = FakeProvider({"id": "fake-delayed"})
    delayed_provider._runs = {}

    async def publish_delayed():
        await asyncio.sleep(0.05)
        delayed_provider._runs["delayed"] = SimpleNamespace()

    delayed_task = asyncio.create_task(publish_delayed())
    delayed_provider._lifecycle_spawn_tasks = {delayed_task}
    await delayed_provider.await_run_started("delayed", timeout=1.0)
    check(
        "delayed" in delayed_provider._runs,
        "provider start receipt waits for delayed run publication",
    )

    missing_provider = FakeProvider({"id": "fake-missing"})
    missing_provider._runs = {}
    missing_provider._lifecycle_spawn_tasks = set()
    try:
        await missing_provider.await_run_started("missing", timeout=0.1)
        missing_raised = False
    except RuntimeError:
        missing_raised = True
    check(
        missing_raised,
        "provider start receipt fails when no spawn task can publish the run",
    )

    timeout_provider = FakeProvider({"id": "fake-timeout"})
    timeout_provider._runs = {}
    timeout_task = asyncio.create_task(asyncio.sleep(10))
    timeout_provider._lifecycle_spawn_tasks = {timeout_task}
    try:
        await timeout_provider.await_run_started("timeout", timeout=0.02)
        timeout_raised = False
    except TimeoutError:
        timeout_raised = True
    check(timeout_raised, "provider start receipt times out while unpublished")
    timeout_task.cancel()
    try:
        await timeout_task
    except asyncio.CancelledError:
        pass

    pending_task = asyncio.create_task(asyncio.sleep(10))
    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task

    provider._runs = {
        "pending": SimpleNamespace(complete_task=pending_task),
        "finalized": SimpleNamespace(complete_task=pending_task, turn_finalized=True),
        "done": SimpleNamespace(complete_task=done_task),
        "missing_task": SimpleNamespace(),
    }

    check(
        provider.is_terminal_event_pending("pending"),
        "active completion watcher is terminal authority",
    )
    check(
        _should_defer_dead_runner_fallback(provider, "pending"),
        "TurnManager defers fallback while terminal authority is active",
    )
    check(
        not provider.is_terminal_event_pending("finalized"),
        "post-complete process-exit watcher is not terminal authority",
    )
    check(
        not provider.is_terminal_event_pending("done"),
        "finished completion watcher is not pending",
    )
    check(
        not _should_defer_dead_runner_fallback(provider, "done"),
        "TurnManager allows fallback after terminal authority finishes",
    )
    check(
        not provider.is_terminal_event_pending("missing_task"),
        "runs without completion watcher do not block fallback",
    )
    check(
        not provider.is_terminal_event_pending("missing_run"),
        "missing run does not block fallback",
    )

    pending_task.cancel()
    try:
        await pending_task
    except asyncio.CancelledError:
        pass

    source = (BACKEND / "turn_manager.py").read_text(encoding="utf-8")
    start_idx = source.find("provider.start_run,")
    receipt_idx = source.find("await provider.await_run_started(run_id)")
    check(
        start_idx >= 0 and receipt_idx > start_idx,
        "TurnManager waits for provider start receipt after start_run",
    )
    check(
        "if _should_defer_dead_runner_fallback(provider, run_id):" in source,
        "dead-runner branch uses the terminal-authority gate",
    )


def main():
    asyncio.run(scenario())
    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: terminal completion authority gates dead-runner fallback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

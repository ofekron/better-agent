import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import _test_home

_test_home.isolate("bc-terminal-authority-")

from provider import Provider, schedule_loop_task
from provider_lifecycle import LifecycleOutcome, RunLifecycleCoordinator
from turn_manager import (
    _await_provider_run_started_or_cancelled,
    _should_defer_dead_runner_fallback,
)

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

    def _persists_backend_state(self, rs) -> bool:
        return False

    def _backend_state_fields(self, rs) -> dict:
        return {}

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

    cross_thread_provider = FakeProvider({"id": "fake-cross-thread"})
    cross_thread_provider._runs = {}
    cross_thread_gate = asyncio.Event()
    cross_thread_scheduled = asyncio.Event()
    owner_loop = asyncio.get_running_loop()

    async def cross_thread_spawn():
        cross_thread_scheduled.set()
        await cross_thread_gate.wait()
        cross_thread_provider._publish_started_run(
            "cross-thread", SimpleNamespace(),
        )

    def schedule_cross_thread_start():
        receipt = schedule_loop_task(
            owner_loop,
            cross_thread_spawn(),
            name="test-cross-thread-provider-start",
        )
        assert receipt is not None
        cross_thread_provider._lifecycle_spawn_tasks = {receipt}
        cross_thread_provider._track_run_start_receipt("cross-thread", receipt)

    await asyncio.to_thread(schedule_cross_thread_start)
    await asyncio.wait_for(cross_thread_scheduled.wait(), timeout=1.0)
    cross_thread_wait = asyncio.create_task(
        cross_thread_provider.await_run_started("cross-thread", timeout=1.0)
    )
    await asyncio.sleep(0)
    check(
        not cross_thread_wait.done(),
        "cross-thread start receipt survives loop-admission gap",
    )
    cross_thread_gate.set()
    await cross_thread_wait
    check(
        "cross-thread" in cross_thread_provider._runs,
        "cross-thread provider publishes before startup wait completes",
    )

    failed_cross_thread_provider = FakeProvider({"id": "fake-cross-thread-failure"})
    failed_cross_thread_provider._runs = {}

    async def cross_thread_failure():
        raise LookupError("cross-thread spawn failed")

    def schedule_failed_cross_thread_start():
        receipt = schedule_loop_task(
            owner_loop,
            cross_thread_failure(),
            name="test-cross-thread-provider-failure",
        )
        assert receipt is not None
        failed_cross_thread_provider._lifecycle_spawn_tasks = {receipt}
        failed_cross_thread_provider._track_run_start_receipt(
            "cross-thread-failure", receipt,
        )

    await asyncio.to_thread(schedule_failed_cross_thread_start)
    try:
        await failed_cross_thread_provider.await_run_started(
            "cross-thread-failure", timeout=1.0,
        )
        cross_thread_failure_preserved = False
    except LookupError as exc:
        cross_thread_failure_preserved = str(exc) == "cross-thread spawn failed"
    check(
        cross_thread_failure_preserved,
        "cross-thread startup preserves the real spawn failure",
    )

    cancelled_admission_ran = threading.Event()

    async def cancelled_before_admission():
        cancelled_admission_ran.set()

    def schedule_and_cancel_before_admission():
        receipt = schedule_loop_task(
            owner_loop,
            cancelled_before_admission(),
            name="test-cancel-before-loop-admission",
        )
        assert receipt is not None
        assert receipt.cancel()

    await asyncio.to_thread(schedule_and_cancel_before_admission)
    await asyncio.sleep(0)
    check(
        not cancelled_admission_ran.is_set(),
        "cancelled cross-thread receipt fences queued loop admission",
    )

    running_provider = FakeProvider({"id": "fake-running-start"})
    running_provider._runs = {}
    running_started = asyncio.Event()
    running_cancelled = asyncio.Event()
    running_receipt = []

    async def running_start():
        running_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            running_cancelled.set()

    def schedule_running_start():
        receipt = schedule_loop_task(
            owner_loop,
            running_start(),
            name="test-cancel-running-provider-start",
        )
        assert receipt is not None
        running_receipt.append(receipt)
        running_provider._track_run_start_receipt("running-start", receipt)

    await asyncio.to_thread(schedule_running_start)
    await asyncio.wait_for(running_started.wait(), timeout=1.0)
    check(
        running_provider.cancel_run_start("running-start"),
        "running provider startup receipt accepts cancellation",
    )
    await asyncio.wait_for(running_cancelled.wait(), timeout=1.0)
    check(
        running_receipt[0].cancelled(),
        "running provider startup task is cancelled before publication",
    )

    delayed_provider = FakeProvider({"id": "fake-delayed"})
    delayed_provider._runs = {}
    publish_allowed = asyncio.Event()
    waiting_on_receipt = asyncio.Event()

    async def publish_delayed():
        await publish_allowed.wait()
        delayed_provider._publish_started_run("delayed", SimpleNamespace())

    original_has_published_run = delayed_provider._has_published_run

    async def observed_has_published_run(run_id):
        result = await original_has_published_run(run_id)
        if run_id in delayed_provider._run_started_waiters:
            waiting_on_receipt.set()
        return result

    delayed_task = asyncio.create_task(publish_delayed())
    delayed_provider._lifecycle_spawn_tasks = {delayed_task}
    delayed_provider._track_run_start_receipt("delayed", delayed_task)
    delayed_provider._has_published_run = observed_has_published_run
    delayed_wait = asyncio.create_task(delayed_provider.await_run_started("delayed", timeout=1.0))
    await asyncio.wait_for(waiting_on_receipt.wait(), timeout=1.0)
    check(not delayed_wait.done(), "provider start receipt blocks until publication")
    publish_allowed.set()
    await delayed_wait
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

    cancellation_provider = FakeProvider({"id": "fake-cancel-start"})
    cancellation_provider.cancelled_runs = []
    cancellation_provider._runs = {}
    lifecycle = RunLifecycleCoordinator(asyncio.get_running_loop())
    admitted = await lifecycle.admit("cancel-before-publish")
    assert admitted.token is not None
    receipt_started = asyncio.Event()
    publish_gate = asyncio.Event()

    async def gated_receipt(_run_id):
        receipt_started.set()
        await publish_gate.wait()

    def cancel_run(run_id):
        cancellation_provider.cancelled_runs.append(run_id)
        cancellation_provider.cancel_task = asyncio.create_task(lifecycle.cancel(run_id))
        return True

    cancellation_provider.await_run_started = gated_receipt
    cancellation_provider.cancel_run = cancel_run
    cancellation = asyncio.Event()
    waiting = asyncio.create_task(
        _await_provider_run_started_or_cancelled(
            cancellation_provider, "cancel-before-publish", cancellation,
        )
    )
    await asyncio.wait_for(receipt_started.wait(), timeout=1.0)
    check(not waiting.done(), "TurnManager waits while provider publication is pending")
    cancellation.set()
    await asyncio.wait_for(waiting, timeout=1.0)
    check(waiting.result() is None, "pre-publication cancellation wins promptly")
    check(
        cancellation_provider.cancelled_runs == ["cancel-before-publish"],
        "pre-publication cancellation fences the provider lifecycle",
    )
    cancelled = await cancellation_provider.cancel_task
    check(cancelled.outcome is LifecycleOutcome.ACCEPTED, "lifecycle cancellation completed")
    check(
        (await lifecycle.publish(admitted.token, object())).outcome is LifecycleOutcome.STALE,
        "cancelled lifecycle reservation cannot publish after cancellation",
    )

    tie_provider = FakeProvider({"id": "fake-start-cancel-tie"})
    tie_provider.cancelled_runs = []

    async def immediate_receipt(_run_id):
        return None

    def cancel_tied_run(run_id):
        tie_provider.cancelled_runs.append(run_id)
        return True

    tie_provider.await_run_started = immediate_receipt
    tie_provider.cancel_run = cancel_tied_run
    tied_cancel = asyncio.Event()
    tied_cancel.set()
    await _await_provider_run_started_or_cancelled(tie_provider, "tied", tied_cancel)
    check(
        tie_provider.cancelled_runs == ["tied"],
        "cancellation wins when publication and cancellation complete together",
    )

    failure_provider = FakeProvider({"id": "fake-start-failure"})
    failure_provider._runs = {}

    async def failed_receipt(_run_id):
        raise LookupError("spawn identity")

    failure_provider.await_run_started = failed_receipt
    try:
        await _await_provider_run_started_or_cancelled(
            failure_provider, "failed-start", asyncio.Event(),
        )
        preserved_failure = False
    except LookupError as exc:
        preserved_failure = str(exc) == "spawn identity"
    check(preserved_failure, "provider startup failure preserves its original identity")

    timeout_provider = FakeProvider({"id": "fake-timeout"})
    timeout_provider._runs = {}
    timeout_gate = asyncio.Event()
    timeout_task = asyncio.create_task(timeout_gate.wait())
    timeout_provider._lifecycle_spawn_tasks = {timeout_task}
    timeout_provider._track_run_start_receipt("timeout", timeout_task)
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

    pending_gate = asyncio.Event()
    pending_task = asyncio.create_task(pending_gate.wait())
    done_task = asyncio.get_running_loop().create_future()
    done_task.set_result(None)

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
    receipt_idx = source.find("await _await_provider_run_started_or_cancelled(")
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

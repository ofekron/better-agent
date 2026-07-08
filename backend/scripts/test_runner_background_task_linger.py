from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402

_TMP_HOME = _test_home.isolate("bc-test-runner-bg-task-")

import proc_control  # noqa: E402
import runner  # noqa: E402

PASS = "  PASS"
FAIL = "  FAIL"


class _NoDetachedProcessControl:
    def has_detached_descendants(self, *_args, **_kwargs) -> bool:
        return False

    def has_uninterruptible_detached_descendant(self, *_args, **_kwargs):
        return None

    def kill_detached_descendant_groups(self, *_args, **_kwargs) -> int:
        return 0


def _notification(task_id: str = "task-1") -> "runner.TaskNotificationMessage":
    return runner.TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status="completed",
        output_file="",
        summary="done",
        uuid=f"notification-{task_id}",
        session_id="sid-1",
    )


class _FakeTaskClient:
    async def receive_messages(self):
        await asyncio.sleep(0.03)
        yield _notification()


class _FakeContinuationClient:
    """Terminal notification → the CLI starts a continuation turn: user
    msg, assistant msg, then (much later than the poll interval) the
    turn's ResultMessage. The babysitter must stay alive until the
    result — exiting earlier SIGKILLs the CLI mid-inference."""

    def __init__(self) -> None:
        self.result_yielded_at: float | None = None

    async def receive_messages(self):
        await asyncio.sleep(0.03)
        yield _notification()
        await asyncio.sleep(0.04)
        yield runner.UserMessage(
            content="<task-notification>done</task-notification>",
        )
        await asyncio.sleep(0.04)
        yield runner.AssistantMessage(content=[], model="m")
        await asyncio.sleep(0.3)
        self.result_yielded_at = time.monotonic()
        yield runner.ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid-1",
            result="continuation done",
        )


class _FakeChainedNotificationClient:
    """Two background tasks. Task-2's terminal notification arrives DURING
    task-1's continuation turn; task-1's ResultMessage must re-open the
    grace window (not zero it) so task-2's continuation isn't reaped
    mid-start."""

    def __init__(self) -> None:
        self.second_result_yielded_at: float | None = None

    async def receive_messages(self):
        await asyncio.sleep(0.03)
        yield _notification("task-1")
        await asyncio.sleep(0.04)
        yield runner.AssistantMessage(content=[], model="m")   # continuation A
        await asyncio.sleep(0.04)
        yield _notification("task-2")                          # during turn A
        await asyncio.sleep(0.04)
        yield runner.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="sid-1", result="A done",
        )
        await asyncio.sleep(0.1)                               # B's TTFT
        yield runner.AssistantMessage(content=[], model="m")   # continuation B
        await asyncio.sleep(0.3)
        self.second_result_yielded_at = time.monotonic()
        yield runner.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="sid-1", result="B done",
        )


class _FakeHungContinuationClient:
    """Continuation turn that never yields a ResultMessage — the linger
    hard cap must bound the wait."""

    async def receive_messages(self):
        await asyncio.sleep(0.03)
        yield _notification()
        await asyncio.sleep(0.04)
        yield runner.AssistantMessage(content=[], model="m")
        await asyncio.sleep(3600)


class _FakeRunawayStreamingClient:
    """Continuation that keeps streaming assistant messages forever with
    no ResultMessage. The cap must LATCH: continued streaming must not
    re-arm busy into unbounded successive cap windows."""

    async def receive_messages(self):
        await asyncio.sleep(0.03)
        yield _notification()
        while True:
            await asyncio.sleep(0.02)
            yield runner.AssistantMessage(content=[], model="m")


class _DescendantsThenNoneProcessControl:
    """Detached work alive for the first polls, then gone — models a
    run_in_background shell exiting during the linger."""

    def __init__(self, busy_polls: int = 3) -> None:
        self._remaining = busy_polls

    def has_detached_descendants(self, *_args, **_kwargs) -> bool:
        if self._remaining > 0:
            self._remaining -= 1
            return True
        return False

    def has_uninterruptible_detached_descendant(self, *_args, **_kwargs):
        return None

    def kill_detached_descendant_groups(self, *_args, **_kwargs) -> int:
        return 0


class _FakeShellExitContinuationClient:
    """No task notifications at all — the continuation trigger is the
    detached shell's exit (has_desc true→false must open the grace
    window). The continuation's messages lag the shell exit."""

    def __init__(self) -> None:
        self.result_yielded_at: float | None = None

    async def receive_messages(self):
        await asyncio.sleep(0.15)
        yield runner.AssistantMessage(content=[], model="m")
        await asyncio.sleep(0.2)
        self.result_yielded_at = time.monotonic()
        yield runner.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="sid-1", result="done",
        )
        await asyncio.sleep(3600)


class _Log:
    def info(self, *_args, **_kwargs) -> None:
        return None

    def warning(self, *_args, **_kwargs) -> None:
        return None


async def _run_case() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    original_process_control = proc_control.process_control
    proc_control.process_control = lambda: _NoDetachedProcessControl()  # type: ignore[assignment]
    original_expect = runner._LingerStreamState._CONTINUATION_EXPECT_S
    original_cap = runner._LingerStreamState._CONTINUATION_CAP_S
    runner._LingerStreamState._CONTINUATION_EXPECT_S = 0.05
    try:
        active_dir = Path(_TMP_HOME) / "active"
        active_dir.mkdir()
        tasks = {"task-1"}
        await runner._linger_for_background_work(
            active_dir,
            _Log(),
            client=_FakeTaskClient(),
            outstanding_tasks=tasks,
            poll_interval_s=0.01,
        )
        results.append((
            "background task keeps babysitter alive until notification",
            not tasks and (active_dir / "lingering").exists(),
            f"tasks={tasks} lingering={(active_dir / 'lingering').exists()}",
        ))

        idle_dir = Path(_TMP_HOME) / "idle"
        idle_dir.mkdir()
        await runner._linger_for_background_work(
            idle_dir,
            _Log(),
            client=_FakeTaskClient(),
            outstanding_tasks=set(),
            poll_interval_s=0.01,
        )
        results.append((
            "no outstanding tasks exits without lingering sentinel",
            not (idle_dir / "lingering").exists(),
            f"lingering={(idle_dir / 'lingering').exists()}",
        ))

        cont_dir = Path(_TMP_HOME) / "continuation"
        cont_dir.mkdir()
        cont_client = _FakeContinuationClient()
        exited_at_holder: list[float] = []
        await runner._linger_for_background_work(
            cont_dir,
            _Log(),
            client=cont_client,
            outstanding_tasks={"task-1"},
            poll_interval_s=0.01,
        )
        exited_at_holder.append(time.monotonic())
        survived_until_result = (
            cont_client.result_yielded_at is not None
            and exited_at_holder[0] >= cont_client.result_yielded_at
        )
        results.append((
            "continuation turn keeps babysitter alive until its ResultMessage",
            survived_until_result,
            f"result_yielded_at={cont_client.result_yielded_at} "
            f"exited_at={exited_at_holder[0]}",
        ))

        chained_dir = Path(_TMP_HOME) / "chained"
        chained_dir.mkdir()
        runner._LingerStreamState._CONTINUATION_EXPECT_S = 0.25
        chained_client = _FakeChainedNotificationClient()
        await runner._linger_for_background_work(
            chained_dir,
            _Log(),
            client=chained_client,
            outstanding_tasks={"task-1", "task-2"},
            poll_interval_s=0.01,
        )
        chained_exit = time.monotonic()
        results.append((
            "chained notification during a continuation survives to the second result",
            chained_client.second_result_yielded_at is not None
            and chained_exit >= chained_client.second_result_yielded_at,
            f"second_result={chained_client.second_result_yielded_at} "
            f"exited={chained_exit}",
        ))
        runner._LingerStreamState._CONTINUATION_EXPECT_S = 0.05

        runner._LingerStreamState._CONTINUATION_CAP_S = 0.2
        hung_dir = Path(_TMP_HOME) / "hung"
        hung_dir.mkdir()
        start = time.monotonic()
        await runner._linger_for_background_work(
            hung_dir,
            _Log(),
            client=_FakeHungContinuationClient(),
            outstanding_tasks={"task-1"},
            poll_interval_s=0.01,
        )
        elapsed = time.monotonic() - start
        results.append((
            "hung continuation is bounded by the hard cap",
            elapsed < 2.0,
            f"elapsed={elapsed:.2f}s",
        ))

        runaway_dir = Path(_TMP_HOME) / "runaway"
        runaway_dir.mkdir()
        start = time.monotonic()
        await runner._linger_for_background_work(
            runaway_dir,
            _Log(),
            client=_FakeRunawayStreamingClient(),
            outstanding_tasks={"task-1"},
            poll_interval_s=0.01,
        )
        elapsed = time.monotonic() - start
        results.append((
            "runaway streaming continuation cannot re-arm past the cap",
            elapsed < 2.0,
            f"elapsed={elapsed:.2f}s",
        ))
        runner._LingerStreamState._CONTINUATION_CAP_S = original_cap

        proc_control.process_control = (  # type: ignore[assignment]
            lambda: _DescendantsThenNoneProcessControl()
        )
        runner._LingerStreamState._CONTINUATION_EXPECT_S = 0.3
        shell_dir = Path(_TMP_HOME) / "shell-exit"
        shell_dir.mkdir()
        shell_client = _FakeShellExitContinuationClient()
        await runner._linger_for_background_work(
            shell_dir,
            _Log(),
            client=shell_client,
            outstanding_tasks=set(),
            poll_interval_s=0.01,
        )
        shell_exit = time.monotonic()
        results.append((
            "detached-shell exit opens grace; continuation survives to result",
            shell_client.result_yielded_at is not None
            and shell_exit >= shell_client.result_yielded_at,
            f"result={shell_client.result_yielded_at} exited={shell_exit}",
        ))
    finally:
        proc_control.process_control = original_process_control  # type: ignore[assignment]
        runner._LingerStreamState._CONTINUATION_EXPECT_S = original_expect
        runner._LingerStreamState._CONTINUATION_CAP_S = original_cap
    return results


def main() -> int:
    results = asyncio.run(_run_case())
    failed = 0
    for name, ok, detail in results:
        print(f"{PASS if ok else FAIL}: {name} ({detail})")
        if not ok:
            failed += 1
    if failed:
        print(f"FAILED: {failed}")
        return 1
    print("OK: runner background task linger")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

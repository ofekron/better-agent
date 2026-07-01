from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runner-bg-task-")
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import proc_control  # noqa: E402
import runner  # noqa: E402

PASS = "  PASS"
FAIL = "  FAIL"


class _NoDetachedProcessControl:
    def has_detached_descendants(self, *_args, **_kwargs) -> bool:
        return False

    def kill_detached_descendant_groups(self, *_args, **_kwargs) -> int:
        return 0


class _FakeTaskClient:
    async def receive_messages(self):
        await asyncio.sleep(0.03)
        yield runner.TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="task-1",
            status="completed",
            output_file="",
            summary="done",
            uuid="notification-1",
            session_id="sid-1",
        )


class _Log:
    def info(self, *_args, **_kwargs) -> None:
        return None

    def warning(self, *_args, **_kwargs) -> None:
        return None


async def _run_case() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    original_process_control = proc_control.process_control
    proc_control.process_control = lambda: _NoDetachedProcessControl()  # type: ignore[assignment]
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
    finally:
        proc_control.process_control = original_process_control  # type: ignore[assignment]
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

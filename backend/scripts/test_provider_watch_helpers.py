from __future__ import annotations

from pathlib import Path

import pytest

import provider_watch_helpers as pwh

pytestmark = pytest.mark.anyio


class FakeQueue:
    def __init__(self, *, raise_on: int | None = None) -> None:
        self.events: list = []
        self._raise_on = raise_on
        self._count = 0

    def put_nowait(self, event) -> None:
        self._count += 1
        if self._raise_on is not None and self._count == self._raise_on:
            raise RuntimeError("queue full")
        self.events.append(event)


class FakeLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple] = []
        self.exceptions: list[str] = []

    def warning(self, fmt, *args) -> None:
        self.warnings.append((fmt, args))

    def exception(self, fmt, *args) -> None:
        self.exceptions.append(fmt % args if args else fmt)


class TestWaitForCompleteOrProcessDeath:
    async def test_complete_present_returns_immediately(self, monkeypatch) -> None:
        monkeypatch.setattr(pwh, "path_exists_off_loop", _async_const(True))
        monkeypatch.setattr(pwh, "popen_is_running_off_loop", _async_const(True))
        called: list = []
        monkeypatch.setattr(pwh.asyncio, "sleep", _record_sleep(called))

        await pwh.wait_for_complete_or_process_death(
            complete_path=Path("/x/complete.json"),
            popen=object(),
            poll_interval=0.1,
        )
        assert called == []  # no polling needed

    async def test_running_then_complete_appears(self, monkeypatch) -> None:
        exists = _async_sequence([False, True])
        running = _async_const(True)
        monkeypatch.setattr(pwh, "path_exists_off_loop", exists)
        monkeypatch.setattr(pwh, "popen_is_running_off_loop", running)
        sleeps: list = []
        monkeypatch.setattr(pwh.asyncio, "sleep", _record_sleep(sleeps))

        await pwh.wait_for_complete_or_process_death(
            complete_path=Path("/x/complete.json"),
            popen=object(),
            poll_interval=0.5,
        )
        assert sleeps == [0.5]  # one poll between the two checks

    async def test_dead_process_grace_window_elapses(self, monkeypatch) -> None:
        # process dead, complete never appears → grace loop sleeps until elapsed
        monkeypatch.setattr(pwh, "path_exists_off_loop", _async_const(False))
        monkeypatch.setattr(pwh, "popen_is_running_off_loop", _async_const(False))
        sleeps: list = []
        monkeypatch.setattr(pwh.asyncio, "sleep", _record_sleep(sleeps))

        await pwh.wait_for_complete_or_process_death(
            complete_path=Path("/x/complete.json"),
            popen=object(),
            poll_interval=0.001,
        )
        # grace window = poll_interval * 6; at least one sleep happened
        assert len(sleeps) >= 1
        assert all(s == 0.001 for s in sleeps)

    async def test_dead_process_complete_during_grace(self, monkeypatch) -> None:
        exists = _async_sequence([False, False, True])  # outer False, grace False, grace True
        monkeypatch.setattr(pwh, "path_exists_off_loop", exists)
        monkeypatch.setattr(pwh, "popen_is_running_off_loop", _async_const(False))
        monkeypatch.setattr(pwh.asyncio, "sleep", _record_sleep([]))

        await pwh.wait_for_complete_or_process_death(
            complete_path=Path("/x/complete.json"),
            popen=object(),
            poll_interval=0.001,
        )


class TestEmitEarlyFailure:
    async def test_happy_path(self) -> None:
        logger = FakeLogger()
        queue = FakeQueue()
        cleaned: list = []
        before: list = []

        await pwh.emit_early_failure(
            logger=logger,  # type: ignore[arg-type]
            log_prefix="claude",
            run_id="r1",
            msg="boom",
            queue=queue,
            cleanup=lambda: cleaned.append(True),
            before_enqueue=lambda: before.append(True),
        )

        assert len(logger.warnings) == 1
        assert before == [True]
        assert cleaned == [True]
        assert len(queue.events) == 2
        assert queue.events[0].type == "error"
        assert queue.events[0].data == {"error": "boom"}
        assert queue.events[1].type == "complete"
        assert queue.events[1].data == {
            "success": False,
            "error": "boom",
            "session_id": None,
            "token_usage": None,
        }

    async def test_before_enqueue_optional(self) -> None:
        logger = FakeLogger()
        queue = FakeQueue()
        cleaned: list = []

        await pwh.emit_early_failure(
            logger=logger,  # type: ignore[arg-type]
            log_prefix="claude",
            run_id="r1",
            msg="boom",
            queue=queue,
            cleanup=lambda: cleaned.append(True),
        )

        assert cleaned == [True]
        assert len(queue.events) == 2

    async def test_queue_failure_logged_and_cleanup_still_runs(self) -> None:
        logger = FakeLogger()
        queue = FakeQueue(raise_on=1)  # first put_nowait raises
        cleaned: list = []

        await pwh.emit_early_failure(
            logger=logger,  # type: ignore[arg-type]
            log_prefix="claude",
            run_id="r1",
            msg="boom",
            queue=queue,
            cleanup=lambda: cleaned.append(True),
        )

        assert len(logger.exceptions) == 1
        assert cleaned == [True]
        assert queue.events == []  # nothing enqueued


# --- helpers ---


def _async_const(value):
    async def _fn(*_args, **_kwargs):
        return value

    return _fn


def _async_sequence(values):
    it = iter(values)

    async def _fn(*_args, **_kwargs):
        try:
            return next(it)
        except StopIteration:
            return values[-1]

    return _fn


def _record_sleep(into: list):
    async def _sleep(interval):
        into.append(interval)

    return _sleep

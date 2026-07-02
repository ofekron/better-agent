from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="ba-test-runner-watchdog-")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import runner  # noqa: E402
import proc_control  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _SilentResponse:
    async def __anext__(self):
        await asyncio.Event().wait()


class _OneResultResponse:
    def __init__(self) -> None:
        self._sent = False

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        await asyncio.sleep(0.02)
        return _FakeResultMessage()


class _DelayedResultResponse:
    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s
        self._sent = False

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        await asyncio.sleep(self._delay_s)
        return _FakeResultMessage()


class _FakeResultMessage:
    is_error = False
    subtype = "success"
    result = "done"
    usage = None
    model_usage = None


class _FakeClient:
    def __init__(self, response) -> None:
        self._response = response
        self.queries = []
        self.interrupted = False

    async def query(self, prompt):
        self.queries.append(prompt)

    def receive_response(self):
        return self._response

    async def interrupt(self):
        self.interrupted = True


class _BusyProcessControl:
    def has_detached_descendants(self, *_args, **_kwargs) -> bool:
        return True

    def kill_detached_descendant_groups(self, *_args, **_kwargs) -> int:
        return 0


class _FailingProcessControl:
    def has_detached_descendants(self, *_args, **_kwargs) -> bool:
        raise RuntimeError("probe failed")


class _QuietLog:
    def __init__(self) -> None:
        self.exceptions = 0

    def exception(self, *_args, **_kwargs) -> None:
        self.exceptions += 1


async def _run_turn(run_dir: Path, response, *, timeout_s: float) -> dict:
    state: dict = {}
    holder = [None]
    original_result_message = runner.ResultMessage
    runner.ResultMessage = _FakeResultMessage
    try:
        return await runner._run_one_turn(
            client=_FakeClient(response),
            prompt="hello",
            images=[],
            files=[],
            run_dir=run_dir,
            turn_id="turn-1",
            pre_query_byte_offset=0,
            state=state,
            state_path=run_dir / "state.json",
            cwd=str(run_dir),
            claude_config_dir=run_dir / "claude",
            log=runner.logger,
            current_turn_holder=holder,
            no_progress_timeout_s=timeout_s,
        )
    finally:
        runner.ResultMessage = original_result_message
        if holder[0] is not None:
            raise AssertionError("current turn holder was not cleared")


async def t_silent_receive_times_out() -> None:
    run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
    started = time.monotonic()
    result = await _run_turn(run_dir, _SilentResponse(), timeout_s=0.05)
    elapsed = time.monotonic() - started
    if elapsed > 1.0:
        raise AssertionError(f"silent receive took too long: {elapsed:.3f}s")
    if result["final_success"]:
        raise AssertionError("silent receive wrongly succeeded")
    if "no response progress" not in (result["error"] or ""):
        raise AssertionError(f"missing watchdog error: {result['error']!r}")
    complete = run_dir / "turns" / "turn-1" / "complete.json"
    if not complete.exists():
        raise AssertionError("turn complete.json was not written")


async def t_periodic_progress_is_not_timed_out() -> None:
    run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
    result = await _run_turn(run_dir, _OneResultResponse(), timeout_s=0.2)
    if not result["final_success"]:
        raise AssertionError(f"progressing turn failed: {result['error']!r}")


async def t_default_watchdog_is_bounded() -> None:
    expected = 5 * 60
    actual = runner._RESPONSE_NO_PROGRESS_TIMEOUT_S
    if actual != expected:
        raise AssertionError(f"default watchdog is {actual}s, expected {expected}s")


async def t_loopback_activity_keeps_watchdog_alive() -> None:
    original_poll = runner._RESPONSE_ACTIVITY_POLL_S
    runner._RESPONSE_ACTIVITY_POLL_S = 0.01
    activity = runner._RunnerActivity()
    stop = asyncio.Event()

    async def mark_activity() -> None:
        while not stop.is_set():
            activity.mark()
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.02)
            except asyncio.TimeoutError:
                pass

    marker = asyncio.create_task(mark_activity())
    try:
        result = await runner._receive_response_message(
            _DelayedResultResponse(0.18),
            timeout_s=0.05,
            activity=activity,
        )
    finally:
        stop.set()
        marker.cancel()
        try:
            await marker
        except asyncio.CancelledError:
            pass
        runner._RESPONSE_ACTIVITY_POLL_S = original_poll
    if not isinstance(result, _FakeResultMessage):
        raise AssertionError(f"unexpected result {result!r}")


async def t_background_activity_keeps_watchdog_alive() -> None:
    original_poll = runner._RESPONSE_ACTIVITY_POLL_S
    runner._RESPONSE_ACTIVITY_POLL_S = 0.01

    async def background_activity() -> bool:
        return True

    try:
        result = await runner._receive_response_message(
            _DelayedResultResponse(0.18),
            timeout_s=0.05,
            activity=runner._RunnerActivity(),
            background_activity=background_activity,
        )
    finally:
        runner._RESPONSE_ACTIVITY_POLL_S = original_poll
    if not isinstance(result, _FakeResultMessage):
        raise AssertionError(f"unexpected result {result!r}")


async def t_background_activity_stop_rearms_watchdog_timeout() -> None:
    original_poll = runner._RESPONSE_ACTIVITY_POLL_S
    runner._RESPONSE_ACTIVITY_POLL_S = 0.01
    calls = 0

    async def background_activity() -> bool:
        nonlocal calls
        calls += 1
        return calls <= 3

    try:
        try:
            await runner._receive_response_message(
                _SilentResponse(),
                timeout_s=0.05,
                activity=runner._RunnerActivity(),
                background_activity=background_activity,
            )
        except runner.ResponseNoProgressError:
            return
    finally:
        runner._RESPONSE_ACTIVITY_POLL_S = original_poll
    raise AssertionError("silent receive did not time out after background cleared")


async def t_detached_background_work_keeps_turn_receive_alive() -> None:
    original_poll = runner._RESPONSE_ACTIVITY_POLL_S
    original_process_control = proc_control.process_control
    runner._RESPONSE_ACTIVITY_POLL_S = 0.01
    proc_control.process_control = lambda: _BusyProcessControl()  # type: ignore[assignment]
    try:
        run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        result = await _run_turn(
            run_dir,
            _DelayedResultResponse(0.18),
            timeout_s=0.05,
        )
    finally:
        proc_control.process_control = original_process_control  # type: ignore[assignment]
        runner._RESPONSE_ACTIVITY_POLL_S = original_poll
    if not result["final_success"]:
        raise AssertionError(f"background turn failed: {result['error']!r}")


async def t_outstanding_task_counts_as_background_activity() -> None:
    log = _QuietLog()
    active = await runner._background_response_activity_active(
        outstanding_tasks={"task-1"},
        process_controller=_FailingProcessControl(),
        log=log,
    )
    if not active:
        raise AssertionError("outstanding task did not count as background activity")
    if log.exceptions:
        raise AssertionError("outstanding task did not short-circuit process probe")


async def t_background_probe_failure_fails_closed() -> None:
    log = _QuietLog()
    active = await runner._background_response_activity_active(
        outstanding_tasks=set(),
        process_controller=_FailingProcessControl(),
        log=log,
    )
    if active:
        raise AssertionError("failing process probe counted as background activity")
    if log.exceptions != 1:
        raise AssertionError(f"probe failure was not logged once: {log.exceptions}")


async def t_watchdog_times_out_after_loopback_activity_stops() -> None:
    original_poll = runner._RESPONSE_ACTIVITY_POLL_S
    runner._RESPONSE_ACTIVITY_POLL_S = 0.01
    activity = runner._RunnerActivity()
    try:
        try:
            await runner._receive_response_message(
                _SilentResponse(),
                timeout_s=0.05,
                activity=activity,
            )
        except runner.ResponseNoProgressError:
            return
    finally:
        runner._RESPONSE_ACTIVITY_POLL_S = original_poll
    raise AssertionError("silent receive did not time out after activity stopped")


async def t_loopback_retry_marks_activity() -> None:
    activity = runner._RunnerActivity()
    original_urlopen = runner.urllib.request.urlopen
    runner._set_active_runner_activity(activity)

    def fail_urlopen(*_args, **_kwargs):
        raise urllib.error.URLError("backend down")

    runner.urllib.request.urlopen = fail_urlopen
    before = activity.last_progress_at()
    await asyncio.sleep(0.01)
    try:
        try:
            runner._post_loopback_sync(
                {"ok": True},
                backend_url="http://127.0.0.1:1",
                internal_token="token",
                url_path="/missing",
                timeout=0.02,
                non_json_t_key="runner.mssg_non_json",
                log_prefix="test POST",
                backoff_cap=0.01,
            )
        except urllib.error.URLError:
            pass
    finally:
        runner.urllib.request.urlopen = original_urlopen
        runner._set_active_runner_activity(None)
    after = activity.last_progress_at()
    if after <= before:
        raise AssertionError("loopback retry did not mark runner activity")


async def t_claude_config_dir_expands_home_vars() -> None:
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = "/tmp/ba-runner-home-test"
    try:
        resolved = runner._resolve_claude_config_dir("$HOME/.claude-zai")
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
    expected = Path("/tmp/ba-runner-home-test/.claude-zai")
    if resolved != expected:
        raise AssertionError(f"resolved {resolved}, expected {expected}")


async def main_run() -> int:
    tests = [
        ("silent receive times out", t_silent_receive_times_out),
        ("periodic progress is not timed out", t_periodic_progress_is_not_timed_out),
        ("default watchdog is bounded", t_default_watchdog_is_bounded),
        ("loopback activity keeps watchdog alive", t_loopback_activity_keeps_watchdog_alive),
        ("background activity keeps watchdog alive", t_background_activity_keeps_watchdog_alive),
        ("background activity stop rearms watchdog timeout", t_background_activity_stop_rearms_watchdog_timeout),
        ("detached background work keeps turn receive alive", t_detached_background_work_keeps_turn_receive_alive),
        ("outstanding task counts as background activity", t_outstanding_task_counts_as_background_activity),
        ("background probe failure fails closed", t_background_probe_failure_fails_closed),
        ("watchdog times out after loopback activity stops", t_watchdog_times_out_after_loopback_activity_stops),
        ("loopback retry marks activity", t_loopback_retry_marks_activity),
        ("claude config dir expands home vars", t_claude_config_dir_expands_home_vars),
    ]
    failed = 0
    for name, fn in tests:
        try:
            await fn()
            print(f"{PASS} {name}")
        except Exception as exc:
            failed += 1
            print(f"{FAIL} {name}: {exc}")
    return failed


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main_run()))
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

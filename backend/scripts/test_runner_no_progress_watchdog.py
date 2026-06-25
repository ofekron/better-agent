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
    # Non-zero usage: a zero-usage success with no assistant output is
    # flagged `prompt_not_executed` by runner_guard's ghost-completion
    # check — these fakes model a REAL executed turn.
    usage = {"input_tokens": 10, "output_tokens": 5}
    model_usage = None


class _FakeAssistantMessage:
    usage = None
    error = None
    stop_reason = None

    def __init__(self, content: list) -> None:
        self.content = content


class _FakeUserMessage:
    def __init__(self, content) -> None:
        self.content = content


class _ToolCallResponse:
    """tool_use → long silence (>> watchdog timeout) → tool_result → result.
    The outstanding tool call must hold the watchdog open."""

    def __init__(self, silence_s: float) -> None:
        self._silence_s = silence_s
        self._step = 0

    async def __anext__(self):
        self._step += 1
        if self._step == 1:
            return _FakeAssistantMessage(
                [{"type": "tool_use", "id": "tu-1", "name": "mcp__x__y", "input": {}}]
            )
        if self._step == 2:
            await asyncio.sleep(self._silence_s)
            return _FakeUserMessage(
                [{"type": "tool_result", "tool_use_id": "tu-1", "is_error": True}]
            )
        if self._step == 3:
            return _FakeResultMessage()
        raise StopAsyncIteration


class _ToolResultThenSilence:
    """tool_use → tool_result → eternal silence. With no outstanding call the
    watchdog must fire again."""

    def __init__(self) -> None:
        self._step = 0

    async def __anext__(self):
        self._step += 1
        if self._step == 1:
            return _FakeAssistantMessage(
                [{"type": "tool_use", "id": "tu-1", "name": "mcp__x__y", "input": {}}]
            )
        if self._step == 2:
            return _FakeUserMessage([{"type": "tool_result", "tool_use_id": "tu-1"}])
        await asyncio.Event().wait()


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


class _IdleProcessControl:
    def has_detached_descendants(self, *_args, **_kwargs) -> bool:
        return False


    def kill_detached_descendant_groups(self, *_args, **_kwargs) -> int:
        return 0


class _QuietLog:
    def __init__(self) -> None:
        self.exceptions = 0

    def exception(self, *_args, **_kwargs) -> None:
        self.exceptions += 1


async def _run_turn(run_dir: Path, response, *, timeout_s: float) -> dict:
    state: dict = {}
    holder = [None]
    original_result_message = runner.ResultMessage
    original_assistant_message = runner.AssistantMessage
    original_user_message = runner.UserMessage
    runner.ResultMessage = _FakeResultMessage
    runner.AssistantMessage = _FakeAssistantMessage
    runner.UserMessage = _FakeUserMessage
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
        runner.AssistantMessage = original_assistant_message
        runner.UserMessage = original_user_message
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


async def t_default_watchdog_requires_explicit_stop() -> None:
    expected = 0
    actual = runner._RESPONSE_NO_PROGRESS_TIMEOUT_S
    if actual != expected:
        raise AssertionError(f"default watchdog is {actual}s, expected {expected}s")


async def t_unbounded_receive_waits_for_explicit_cancel() -> None:
    task = asyncio.create_task(
        runner._receive_response_message(_SilentResponse(), timeout_s=0),
    )
    await asyncio.sleep(0.05)
    if task.done():
        raise AssertionError("silent live response stopped without explicit cancellation")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError("explicit cancellation did not stop the receive")


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


async def t_outstanding_tool_call_holds_watchdog_open() -> None:
    original_poll = runner._RESPONSE_ACTIVITY_POLL_S
    original_process_control = proc_control.process_control
    runner._RESPONSE_ACTIVITY_POLL_S = 0.01
    proc_control.process_control = lambda: _IdleProcessControl()  # type: ignore[assignment]
    try:
        run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        result = await _run_turn(run_dir, _ToolCallResponse(0.2), timeout_s=0.05)
    finally:
        proc_control.process_control = original_process_control  # type: ignore[assignment]
        runner._RESPONSE_ACTIVITY_POLL_S = original_poll
    if not result["final_success"]:
        raise AssertionError(f"in-flight tool call was killed: {result['error']!r}")


async def t_tool_result_rearms_watchdog() -> None:
    original_poll = runner._RESPONSE_ACTIVITY_POLL_S
    original_process_control = proc_control.process_control
    runner._RESPONSE_ACTIVITY_POLL_S = 0.01
    proc_control.process_control = lambda: _IdleProcessControl()  # type: ignore[assignment]
    try:
        run_dir = Path(tempfile.mkdtemp(dir=_TMP_HOME))
        result = await _run_turn(run_dir, _ToolResultThenSilence(), timeout_s=0.05)
    finally:
        proc_control.process_control = original_process_control  # type: ignore[assignment]
        runner._RESPONSE_ACTIVITY_POLL_S = original_poll
    if result["final_success"]:
        raise AssertionError("silence after tool_result did not time out")
    if "no response progress" not in (result["error"] or ""):
        raise AssertionError(f"missing watchdog error: {result['error']!r}")


async def t_outstanding_tool_calls_unit() -> None:
    class ToolUseBlock:
        def __init__(self, id: str) -> None:
            self.id = id

    class ToolResultBlock:
        def __init__(self, tool_use_id: str) -> None:
            self.tool_use_id = tool_use_id

    log = _QuietLog2()
    original_assistant_message = runner.AssistantMessage
    original_user_message = runner.UserMessage
    runner.AssistantMessage = _FakeAssistantMessage
    runner.UserMessage = _FakeUserMessage
    try:
        await _outstanding_tool_calls_unit_body(log, ToolUseBlock, ToolResultBlock)
    finally:
        runner.AssistantMessage = original_assistant_message
        runner.UserMessage = original_user_message


async def _outstanding_tool_calls_unit_body(log, ToolUseBlock, ToolResultBlock) -> None:
    calls = runner._OutstandingToolCalls()
    if calls.busy(log):
        raise AssertionError("empty tracker reported busy")

    # Typed-object blocks: two parallel calls.
    calls.apply(_FakeAssistantMessage([ToolUseBlock("a"), ToolUseBlock("b")]))
    if not calls.busy(log):
        raise AssertionError("outstanding calls not busy")
    # One result arrives (typed block) — still busy on the other.
    calls.apply(_FakeUserMessage([ToolResultBlock("a")]))
    if not calls.busy(log):
        raise AssertionError("one remaining call should still be busy")
    # Dict-shaped result clears the second.
    calls.apply(_FakeUserMessage([{"type": "tool_result", "tool_use_id": "b"}]))
    if calls.busy(log):
        raise AssertionError("cleared tracker still busy")

    # str content is a no-op, not a crash.
    calls.apply(_FakeUserMessage("plain text"))

    # Backstop: an entry older than the cap no longer counts as busy.
    calls.apply(_FakeAssistantMessage([{"type": "tool_use", "id": "old"}]))
    calls._started["old"] = time.monotonic() - runner._TOOL_CALL_BUSY_BACKSTOP_S - 1
    if calls.busy(log):
        raise AssertionError("backstopped call still holds watchdog open")

    # Warn once past the warn threshold.
    calls.apply(_FakeAssistantMessage([{"type": "tool_use", "id": "slow"}]))
    calls._started["slow"] = time.monotonic() - runner._TOOL_CALL_BUSY_WARN_S - 1
    if not calls.busy(log) or not calls.busy(log):
        raise AssertionError("slow call should stay busy")
    if log.warnings != 1:
        raise AssertionError(f"expected exactly one warning, got {log.warnings}")


class _QuietLog2:
    def __init__(self) -> None:
        self.warnings = 0

    def warning(self, *_args, **_kwargs) -> None:
        self.warnings += 1


async def main_run() -> int:
    tests = [
        ("silent receive times out", t_silent_receive_times_out),
        ("periodic progress is not timed out", t_periodic_progress_is_not_timed_out),
        ("default watchdog requires explicit stop", t_default_watchdog_requires_explicit_stop),
        ("unbounded receive waits for explicit cancel", t_unbounded_receive_waits_for_explicit_cancel),
        ("loopback activity keeps watchdog alive", t_loopback_activity_keeps_watchdog_alive),
        ("background activity keeps watchdog alive", t_background_activity_keeps_watchdog_alive),
        ("background activity stop rearms watchdog timeout", t_background_activity_stop_rearms_watchdog_timeout),
        ("detached background work keeps turn receive alive", t_detached_background_work_keeps_turn_receive_alive),
        ("outstanding task counts as background activity", t_outstanding_task_counts_as_background_activity),
        ("background probe failure fails closed", t_background_probe_failure_fails_closed),
        ("watchdog times out after loopback activity stops", t_watchdog_times_out_after_loopback_activity_stops),
        ("loopback retry marks activity", t_loopback_retry_marks_activity),
        ("outstanding tool call holds watchdog open", t_outstanding_tool_call_holds_watchdog_open),
        ("tool result rearms watchdog", t_tool_result_rearms_watchdog),
        ("outstanding tool calls unit", t_outstanding_tool_calls_unit),
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

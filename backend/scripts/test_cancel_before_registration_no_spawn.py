"""Regression: a Stop/cancel that arrives while a provider start is still
blocked BEFORE the runner is registered (spawned) must NOT let a detached
runner spawn afterward.

Bug (card 17ae2fb054ea): `provider._start_authorized_execution` only checked
`execution.cancel_after_admission_requested` AFTER `_persist_and_start_execution`
had already spawned the subprocess (Popen). A cancel landing in the window
between commit (`_try_commit_spawn`) and spawn therefore spawned a runner
that the UI had already decided to cancel — a late, untracked spawn.

The fix is a state/event gate with no sleeps/timeouts:
  1. `execution_template._try_commit_spawn` honours the cancel Event: if it
     is set, admission resolves False and commit returns False.
  2. `provider._start_authorized_execution` re-checks the cancel signal after
     commit and BEFORE spawn; if set, it aborts (returns False, no spawn).
  3. `lifecycle_state_machines.AdmissionLifecycleMachine.cancel` records the
     cancel via the single authoritative Event in every branch, so a cancel
     that loses the admission-resolution race still leaves the Event set and
     is caught by gates 1/2 (or the post-spawn teardown safety net).

Pre-fix, Case B and Case C spawn a runner (FAIL). Post-fix none of the
cancel cases spawn and the positive control still launches normally (PASS).
"""
import asyncio
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import _test_home
_test_home.isolate("bc_test_cancel_pre_spawn_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provider import Provider  # noqa: E402
from execution_template import PreparedExecution  # noqa: E402
from runs_dir import runs_root  # noqa: E402


@dataclass
class _FakeRunState:
    run_id: str
    run_dir: Path
    turn_run_id: Optional[str] = None
    cancelled: bool = False
    turn_cancelled: bool = False


_PROVIDER_RECORD = {
    "id": "provider-cancel-test",
    "kind": "test",
    "generation": "7e3b9b0c-2c4d-4f6a-9d77-1f0e2a3b4c5d",
    "revision": 0,
    "execution_revision": 0,
}


class _RecordingProvider(Provider):
    """Stub provider: `_start_run` RECORDS the spawn instead of Popen, and
    the config-store authority hydration is bypassed so no disk provider
    record is required."""

    KIND = "test"

    def __init__(self, record: dict) -> None:
        super().__init__(record)
        self._runs: dict = {}
        self.spawned_run_ids: list[str] = []

    @classmethod
    @contextmanager
    def _bypass_authority(cls, execution, start_arguments):
        del execution, start_arguments
        yield _PROVIDER_RECORD

    def _start_run(self, *, run_id, _execution, **kwargs) -> bool:
        self.spawned_run_ids.append(run_id)
        self._runs[run_id] = _FakeRunState(run_id=run_id, run_dir=runs_root() / run_id)
        return True

    def _write_backend_state(self, rs) -> None:
        pass

    def build_env(self) -> dict[str, str]:
        return {}

    def recover_in_flight(self, loop=None, run_id_filter=None) -> list[dict]:
        return []

    def prune_old_runs(self, max_age_days: int = 7) -> int:
        return 0

    async def run_headless(self, **kwargs):
        return None

    async def rewind(self, agent_sid: str, message_uuid: str) -> None:
        pass


def _new_provider() -> _RecordingProvider:
    provider = _RecordingProvider(_PROVIDER_RECORD)
    # Bypass config_store authority hydration for the stub provider.
    provider._execution_authority_context = _RecordingProvider._bypass_authority.__get__(provider)  # type: ignore[assignment]
    return provider


def _make_execution(provider: _RecordingProvider, run_id: str) -> PreparedExecution:
    return provider.prepare_run(
        run_id=run_id,
        prompt="hi",
        cwd=tempfile.gettempdir(),
        model=None,
        reasoning_effort=None,
        session_id="sess-cancel-test",
        mode="native",
        app_session_id="sess-cancel-test",
    )


def _start(provider: _RecordingProvider, execution: PreparedExecution) -> bool:
    loop = asyncio.new_event_loop()
    try:
        queue = asyncio.Queue()
        return provider.start_run(execution=execution, loop=loop, queue=queue)
    finally:
        loop.close()


def test_case_a_cancel_while_admission_pending() -> None:
    """Cancel before commit (admission still pending): no spawn."""
    provider = _new_provider()
    execution = _make_execution(provider, "run-a")
    execution._mark_cancelled()  # lifecycle.cancel() while admission_pending
    result = _start(provider, execution)
    assert result is False, f"[A] expected start=False, got {result}"
    assert not provider.spawned_run_ids, f"[A] expected no spawn, got {provider.spawned_run_ids}"


def test_case_b_cancel_after_commit_before_start() -> None:
    """THE BUG WINDOW: commit succeeds, cancel arrives, then start must NOT
    spawn. Pre-fix this spawns (commit already resolved admission True)."""
    provider = _new_provider()
    execution = _make_execution(provider, "run-b")
    assert execution._try_commit_spawn(), "[B] setup commit failed"
    execution._request_cancel_after_admission()  # cancel lands after commit
    result = _start(provider, execution)
    assert result is False, f"[B] expected start=False, got {result}"
    assert not provider.spawned_run_ids, f"[B] expected no spawn, got {provider.spawned_run_ids}"


def test_case_c_cancel_lands_between_commit_and_spawn() -> None:
    """Cancel arrives during the commit->spawn span (after `_try_commit_spawn`
    passes but before spawn). Exercises the provider pre-spawn recheck (gate 2).
    We simulate by making commit set the cancel Event right after resolving
    admission, then returning True (proceeding past the commit gate)."""
    import execution_template as et

    provider = _new_provider()
    execution = _make_execution(provider, "run-c")

    original_commit = et.PreparedExecution._try_commit_spawn

    def _commit_then_cancel(self) -> bool:
        self._resolve_admission(result=True)
        self._cancel_after_admission.set()
        return True

    et.PreparedExecution._try_commit_spawn = _commit_then_cancel  # type: ignore[assignment]
    try:
        result = _start(provider, execution)
    finally:
        et.PreparedExecution._try_commit_spawn = original_commit  # type: ignore[assignment]

    assert result is False, f"[C] expected start=False, got {result}"
    assert not provider.spawned_run_ids, f"[C] expected no spawn, got {provider.spawned_run_ids}"


def test_positive_control_normal_launch() -> None:
    """No cancel: spawn happens, start returns True (normal launch unchanged)."""
    provider = _new_provider()
    execution = _make_execution(provider, "run-ok")
    result = _start(provider, execution)
    assert result is True, f"[+] expected start=True, got {result}"
    assert provider.spawned_run_ids == ["run-ok"], f"[+] expected spawn ['run-ok'], got {provider.spawned_run_ids}"


TESTS = [
    ("Case A: cancel while admission_pending -> no spawn", test_case_a_cancel_while_admission_pending),
    ("Case B: cancel after commit, before start -> no spawn", test_case_b_cancel_after_commit_before_start),
    ("Case C: cancel between commit and spawn -> no spawn", test_case_c_cancel_lands_between_commit_and_spawn),
    ("Positive control: no cancel -> normal spawn", test_positive_control_normal_launch),
]


def main() -> int:
    failures = []
    for name, fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            print(f"FAIL: {name}: {exc}")
            failures.append(name)
        else:
            print(f"PASS: {name}")
    if failures:
        print("Failures:", ", ".join(failures))
        return 1
    print("OK: pre-registration cancellation never spawns a runner")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

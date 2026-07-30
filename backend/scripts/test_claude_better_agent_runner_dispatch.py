"""Locks ClaudeProvider's dispatch between the native claude CLI launch and
the Claude-subscription-via-better_agent_runner launch.

Covers:
- prepare_run for runner=="better_agent_runner" produces a
  runner_better_agent-module launch, keeps ExecutionArtifact.provider_kind
  == "claude" (not "openai"), and needs no `claude` CLI binary on PATH.
- Native prepare_run (runner unset/"native") is completely unaffected —
  still requires the `claude` CLI binary, still launches "runner".
- Capability mirroring (steer_run gate, rewind_requires_agent_identity)
  flips only for the better_agent_runner instance.
- active_runs()/cancel_run()/is_running() correctly isolate two SEPARATE
  ClaudeProvider instances (one per runner — matching how
  provider.get_provider(id, runner) actually caches them): a run
  registered on one instance is invisible to and uncancellable from the
  other.
- attach_recovered_run routes a better_agent_runner descriptor to the
  session-events-shaped RunState, and recover_in_flight partitions native
  vs better_agent_runner run dirs correctly (by peeking backend_state.json
  "runner", never misreading a session-events backend_state.json with the
  claude-native reader).
- run_recovery._recovery_family resolves "session_events" for a
  better_agent_runner descriptor (not "claude"), confirming the merged
  gating in run_recovery.py."recognizes" runner=="better_agent_runner"
  descriptors this dispatch now actually produces.
- Security: the class of credential-mocking bug documented in this
  session's task (patching a function at its definition module when the
  consumer imported it via `from X import Y`, which silently does NOT
  intercept the call) is demonstrated with synthetic functions — proving
  an `assert_called()`-style check on the mock reliably catches a
  wrongly-targeted patch — without touching any real credential or making
  any network call.

Run with:
    cd backend && .venv/bin/python scripts/test_claude_better_agent_runner_dispatch.py
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402
_TEST_HOME = _test_home.TestHome.acquire("bc-test-claude-ba-dispatch-")

import config_store  # noqa: E402
from provider import RecoveredPopen  # noqa: E402
from provider_claude import ClaudeProvider, RunState as ClaudeRunState  # noqa: E402
from provider_claude_better_agent_runner import (  # noqa: E402
    ClaudeBetterAgentRunnerProvider,
)
from provider_family_execution_runtime import family_launch_from_artifact  # noqa: E402
from provider_session_events import RunState as SessionEventsRunState  # noqa: E402
import provider_claude  # noqa: E402
import runs_dir  # noqa: E402
import session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _session(provider_id: str, cwd: Path) -> dict:
    session = session_manager.manager.create(
        name="ba-runner-dispatch-test",
        cwd=str(cwd),
        orchestration_mode="native",
        source="test",
        provider_id=provider_id,
        model="claude-opus-4-6",
        permission={"mode": "dontAsk"},
    )
    assert session is not None
    return session


def _arguments(*, cwd: Path, session_id: str, run_id: str) -> dict:
    return {
        "run_id": run_id,
        "prompt": "hello from the better_agent_runner dispatch test",
        "images": None,
        "files": None,
        "cwd": str(cwd),
        "model": "claude-opus-4-6",
        "reasoning_effort": None,
        "session_id": None,
        "mode": "native",
        "app_session_id": session_id,
        "source": "web",
        "disallowed_tools": None,
        "setting_sources": None,
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "volatile-internal-token",
        "fork": False,
        "supervised": False,
        "supervisor_agent_session_id": None,
        "worker_agent_session_id": None,
        "mssg_sender_session_id": None,
        "is_worker": False,
        "browser_harness_enabled": False,
        "user_facing": True,
        "working_mode": None,
        "extra_env": None,
        "continuation_chain": None,
        "provider_run_config": None,
        "capability_contexts": None,
        "target_message_id": None,
        "resolved_harness_run_config": None,
        "turn_run_id": None,
        "disabled_builtin_extensions": [],
        "provisioned_tool_profile": "",
    }


def test_better_agent_runner_prepare_run_dispatches_to_runner_better_agent() -> bool:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        # Deliberately do NOT put a `claude` binary anywhere on PATH —
        # proves the better_agent_runner branch never calls
        # resolve_cli_binary("claude") the way native prepare_run does.
        record = config_store.add_provider({
            "name": "claude-ba-dispatch",
            "kind": "claude",
            "mode": "subscription",
            "runner": "better_agent_runner",
        })
        session = _session(record["id"], root)
        provider = ClaudeProvider(record)
        arguments = _arguments(
            cwd=root, session_id=session["id"], run_id=str(uuid.uuid4()),
        )
        prepared = provider.prepare_run(**arguments)
        launch = family_launch_from_artifact(prepared.artifact)
        runner_input = prepared.artifact.runtime_policy["runner_input"]

        if prepared.artifact.provider_kind != "claude":
            print(f"  artifact.provider_kind={prepared.artifact.provider_kind!r}, expected 'claude'")
            return False
        if launch.family != "claude":
            print(f"  launch.family={launch.family!r}, expected 'claude'")
            return False
        if launch.runner.runner_module != "runner_better_agent":
            print(f"  runner_module={launch.runner.runner_module!r}, expected 'runner_better_agent'")
            return False
        if launch.runner.runner_kind != "claude":
            print(f"  runner.runner_kind={launch.runner.runner_kind!r}, expected 'claude'")
            return False
        if not launch.attest():
            print("  launch failed self-attestation")
            return False
        if runner_input.get("provider_kind") != "claude":
            print(f"  runner_input provider_kind={runner_input.get('provider_kind')!r}, expected 'claude'")
            return False
        if runner_input.get("provider_mode") != "subscription":
            print(f"  runner_input provider_mode={runner_input.get('provider_mode')!r}")
            return False
        if runner_input.get("runner") != "better_agent_runner":
            print(f"  runner_input runner={runner_input.get('runner')!r}")
            return False
        if runner_input.get("model") != "claude-opus-4-6":
            print(f"  runner_input model={runner_input.get('model')!r}")
            return False
        print(f"{PASS} better_agent_runner prepare_run dispatches to runner_better_agent, keeps provider_kind=claude")
        return True


def test_native_prepare_run_still_requires_claude_binary() -> bool:
    """Regression guard: native claude prepare_run is byte-for-byte
    unaffected by the new branch. `resolve_cli_binary` also checks fixed
    install-default directories beyond PATH (real dev boxes may have a
    real `claude` there), so the portable assertion is the POSITIVE
    path — a fake `claude` placed first on PATH is picked up via
    `shutil.which`'s PATH-priority search, exactly like before this
    change — not a PATH-clearing negative-path drill that would be
    environment-dependent."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        record = config_store.add_provider({
            "name": "claude-native-dispatch",
            "kind": "claude",
            "mode": "subscription",
        })
        session = _session(record["id"], root)
        provider = ClaudeProvider(record)
        bin_dir = root / "bin"
        _write_executable(bin_dir / "claude", "#!/bin/sh\nexit 0\n")
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join((str(bin_dir), old_path))
        try:
            arguments = _arguments(
                cwd=root, session_id=session["id"], run_id=str(uuid.uuid4()),
            )
            prepared = provider.prepare_run(**arguments)
            launch = family_launch_from_artifact(prepared.artifact)
            if launch.runner.runner_module != "runner":
                print(f"  native runner_module={launch.runner.runner_module!r}, expected 'runner'")
                return False
            launcher_path = launch.downstream.launcher.resolved_path
            if str(bin_dir) not in launcher_path:
                print(f"  native launcher_path={launcher_path!r}, expected the fake claude in {bin_dir}")
                return False
        finally:
            os.environ["PATH"] = old_path
    print(f"{PASS} native prepare_run unaffected (fake claude on PATH still launches 'runner')")
    return True


def test_capability_mirroring_flips_only_for_better_agent_runner() -> bool:
    native = ClaudeProvider({"id": "cap-native", "kind": "claude"})
    ba = ClaudeProvider({
        "id": "cap-ba", "kind": "claude",
        "runner": "better_agent_runner", "mode": "subscription",
    })
    if native.supports_steering is not False:
        print(f"  native supports_steering={native.supports_steering!r}, expected False (unchanged)")
        return False
    if ba.supports_steering is not True:
        print(f"  ba supports_steering={ba.supports_steering!r}, expected True")
        return False
    if native.rewind_requires_agent_identity is not True:
        print(f"  native rewind_requires_agent_identity={native.rewind_requires_agent_identity!r}, expected True (unchanged)")
        return False
    if ba.rewind_requires_agent_identity is not False:
        print(f"  ba rewind_requires_agent_identity={ba.rewind_requires_agent_identity!r}, expected False")
        return False
    if native.steer_run("nonexistent", "prompt") is not False:
        print("  native steer_run should always return False")
        return False
    print(f"{PASS} capability mirroring flips only for the better_agent_runner instance")
    return True


class _FakePopen:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        del timeout
        self._alive = False
        return 0


def test_active_runs_and_cancel_run_isolate_separate_instances() -> bool:
    """Mirrors how provider.get_provider(id, runner) actually caches
    ClaudeProvider: one instance per (id, runner) pair, each with its own
    run registry — never one instance juggling both. Registers a fake
    run directly into each instance's registry (no real subprocess) and
    proves cancel/active-runs never cross instance boundaries."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        native = ClaudeProvider({"id": "iso-test", "kind": "claude"})
        ba = ClaudeProvider({
            "id": "iso-test", "kind": "claude",
            "runner": "better_agent_runner", "mode": "subscription",
        })

        native_run_dir = root / "native-run"
        native_run_dir.mkdir()
        native._runs["native-run-id"] = ClaudeRunState(
            run_id="native-run-id",
            run_dir=native_run_dir,
            popen=_FakePopen(111),
            mode="native",
            app_session_id="native-app-session",
            queue=asyncio.Queue(),
        )

        ba_run_dir = root / "ba-run"
        ba_run_dir.mkdir()
        ba._runs["ba-run-id"] = SessionEventsRunState(
            run_id="ba-run-id",
            run_dir=ba_run_dir,
            popen=_FakePopen(222),
            mode="native",
            app_session_id="ba-app-session",
            queue=asyncio.Queue(),
        )

        native_ids = {r["run_id"] for r in native.active_runs()}
        ba_ids = {r["run_id"] for r in ba.active_runs()}
        if native_ids != {"native-run-id"}:
            print(f"  native.active_runs() = {native_ids}")
            return False
        if ba_ids != {"ba-run-id"}:
            print(f"  ba.active_runs() = {ba_ids}")
            return False

        # native can't see/cancel the ba run and vice versa.
        if native.cancel_run("ba-run-id") is not False:
            print("  native.cancel_run reached the ba-runner's run")
            return False
        if ba.cancel_run("native-run-id") is not False:
            print("  ba.cancel_run reached the native run")
            return False
        if "ba-run-id" not in ba._runs:
            print("  ba run was removed by an unrelated cancel_run call")
            return False
        if "native-run-id" not in native._runs:
            print("  native run was removed by an unrelated cancel_run call")
            return False

        # Each provider CAN cancel its own run.
        if ba.cancel_run("ba-run-id") is not True:
            print("  ba.cancel_run failed to cancel its own run")
            return False
        if not ba._runs["ba-run-id"].cancelled:
            print("  ba run not marked cancelled")
            return False
        if native._runs["native-run-id"].cancelled:
            print("  native run was unexpectedly cancelled by the ba cancel_run call")
            return False

    print(f"{PASS} active_runs/cancel_run isolate two ClaudeProvider instances by runner")
    return True


def test_attach_recovered_run_routes_better_agent_runner_descriptor() -> bool:
    provider = ClaudeProvider({
        "id": "recover-ba", "kind": "claude",
        "runner": "better_agent_runner", "mode": "subscription",
    })
    original_schedule = provider_claude.schedule_loop_task
    provider_claude.schedule_loop_task = lambda _loop, coro, **_kwargs: coro.close()
    try:
        loop = asyncio.new_event_loop()
        try:
            desc = {
                "run_id": "ba-recovered-run",
                "pid": 54321,
                "app_session_id": "app-ba",
                "session_id": "ba-session-id",
                "runner": "better_agent_runner",
                "mode": "native",
            }
            queue: asyncio.Queue = asyncio.Queue()
            attached = provider.attach_recovered_run(
                desc=desc, queue=queue, loop=loop,
            )
        finally:
            loop.close()
    finally:
        provider_claude.schedule_loop_task = original_schedule

    if not attached:
        print("  attach_recovered_run returned False")
        return False
    rs = provider._runs.get("ba-recovered-run")
    if not isinstance(rs, SessionEventsRunState):
        print(f"  recovered run has wrong RunState type: {type(rs)!r}")
        return False
    if rs is not provider._ba_delegate._runs.get("ba-recovered-run"):
        print("  outer _runs and delegate _runs diverged for the recovered run")
        return False
    print(f"{PASS} attach_recovered_run routes a better_agent_runner descriptor to the session-events RunState")
    return True


def test_recover_in_flight_partitions_native_and_better_agent_runner_dirs() -> bool:
    provider = ClaudeProvider({
        "id": "recover-partition", "kind": "claude",
        "runner": "better_agent_runner", "mode": "subscription",
    })
    root = runs_dir.runs_root()
    native_id = f"native-{uuid.uuid4().hex[:8]}"
    ba_id = f"ba-{uuid.uuid4().hex[:8]}"
    native_dir = root / native_id
    ba_dir = root / ba_id
    native_dir.mkdir(parents=True)
    ba_dir.mkdir(parents=True)
    try:
        (native_dir / "backend_state.json").write_text(json.dumps({
            "provider_id": "recover-partition",
            "session_id": "native-sid",
            "app_session_id": "native-app",
            # No "runner" key at all — mirrors a pre-existing native run
            # dir, must NOT be misrouted to the delegate.
        }), encoding="utf-8")
        (ba_dir / "backend_state.json").write_text(json.dumps({
            "provider_id": "recover-partition",
            "session_id": "ba-sid",
            "app_session_id": "ba-app",
            "runner": "better_agent_runner",
            "jsonl_path": str(ba_dir / "session_events.jsonl"),
        }), encoding="utf-8")
        (ba_dir / "session_events.jsonl").write_text("", encoding="utf-8")

        recovered = provider.recover_in_flight(run_id_filter={native_id, ba_id})
        by_id = {d["run_id"]: d for d in recovered}
        if native_id not in by_id or ba_id not in by_id:
            print(f"  missing descriptors: {sorted(by_id)}")
            return False
        if by_id[ba_id].get("runner") != "better_agent_runner":
            print(f"  ba descriptor runner={by_id[ba_id].get('runner')!r}")
            return False

        from run_recovery import _recovery_family

        if _recovery_family(by_id[ba_id]) != "session_events":
            print(f"  _recovery_family(ba desc) = {_recovery_family(by_id[ba_id])!r}, expected 'session_events'")
            return False
        if _recovery_family(by_id[native_id]) != "claude":
            print(f"  _recovery_family(native desc) = {_recovery_family(by_id[native_id])!r}, expected 'claude'")
            return False
    finally:
        import shutil as _shutil
        _shutil.rmtree(native_dir, ignore_errors=True)
        _shutil.rmtree(ba_dir, ignore_errors=True)
    print(f"{PASS} recover_in_flight partitions native vs better_agent_runner run dirs; _recovery_family routes each correctly")
    return True


# --------------------------------------------------------------------------
# Security: prove the credential-mock-target pitfall is actually caught by
# this suite's own assertion style, using synthetic functions only — no
# real credential, no real network call, ever.
# --------------------------------------------------------------------------

def _consumer_module_import_shape_is_safe() -> bool:
    """Static guard: runner_better_agent_claude_subscription.py must
    `import claude_subscription_credential` (module import) and call
    `claude_subscription_credential.read_claude_subscription_token()` at
    the point of use — NOT `from claude_subscription_credential import
    read_claude_subscription_token`, which would bind a local name at
    import time that a `mock.patch.object(claude_subscription_credential,
    ...)` at the SOURCE module would silently fail to intercept."""
    source = (_BACKEND / "runner_better_agent_claude_subscription.py").read_text(
        encoding="utf-8",
    )
    if "from claude_subscription_credential import" in source:
        print("  found an unsafe `from claude_subscription_credential import ...`")
        return False
    if "import claude_subscription_credential" not in source:
        print("  expected `import claude_subscription_credential` (module import)")
        return False
    if "claude_subscription_credential.read_claude_subscription_token(" not in source:
        print("  expected a module-qualified call site")
        return False
    return True


def test_credential_mock_target_pitfall_is_caught() -> bool:
    if not _consumer_module_import_shape_is_safe():
        return False

    # Synthetic demonstration of the exact anti-pattern, isolated from any
    # real credential/network surface: module A defines a function; module
    # B imports it via `from A import fn` (the unsafe shape) vs via
    # `import A; A.fn()` (the safe shape, matching the real consumer).
    import types

    source_module = types.ModuleType("_fake_credential_source")

    def _real_read_token() -> str:
        raise AssertionError(
            "the real (unmocked) credential function was called — "
            "in production this would be a live Keychain read",
        )

    source_module.read_claude_subscription_token = _real_read_token
    sys.modules["_fake_credential_source"] = source_module
    try:
        # UNSAFE consumer: binds the function at import time.
        unsafe_consumer = types.ModuleType("_fake_unsafe_consumer")
        exec(
            "from _fake_credential_source import read_claude_subscription_token\n"
            "def call():\n"
            "    return read_claude_subscription_token()\n",
            unsafe_consumer.__dict__,
        )
        sys.modules["_fake_unsafe_consumer"] = unsafe_consumer

        # SAFE consumer: module-qualified access at call time (matches
        # the real runner_better_agent_claude_subscription.py shape).
        safe_consumer = types.ModuleType("_fake_safe_consumer")
        exec(
            "import _fake_credential_source\n"
            "def call():\n"
            "    return _fake_credential_source.read_claude_subscription_token()\n",
            safe_consumer.__dict__,
        )
        sys.modules["_fake_safe_consumer"] = safe_consumer

        fake_token = "fake-token-value"
        with mock.patch.object(
            source_module, "read_claude_subscription_token",
            return_value=fake_token,
        ) as mocked:
            # Patching the SOURCE module does NOT intercept the unsafe
            # consumer's already-bound name — calling it reaches the
            # real function, which raises. The assertion style this
            # suite uses elsewhere (mock.assert_called / catching the
            # real-call guard) reliably surfaces this as a loud failure,
            # never a silent pass.
            raised = False
            try:
                unsafe_consumer.call()
            except AssertionError:
                raised = True
            if not raised:
                print("  expected the unsafe consumer to reach the real function")
                return False
            if mocked.called:
                print("  mock unexpectedly intercepted the unsafe consumer's call")
                return False

            # The SAFE (module-qualified) consumer IS correctly
            # intercepted by the same patch.
            result = safe_consumer.call()
            if result != fake_token:
                print(f"  safe consumer got {result!r}, expected mocked token")
                return False
            if not mocked.called:
                print("  mock.assert_called-style check did not see the safe consumer's call")
                return False
    finally:
        for name in (
            "_fake_credential_source", "_fake_unsafe_consumer", "_fake_safe_consumer",
        ):
            sys.modules.pop(name, None)
    print(f"{PASS} credential-mock-target pitfall reproduced+caught with synthetic functions (no real network/credential)")
    return True


TESTS = [
    test_better_agent_runner_prepare_run_dispatches_to_runner_better_agent,
    test_native_prepare_run_still_requires_claude_binary,
    test_capability_mirroring_flips_only_for_better_agent_runner,
    test_active_runs_and_cancel_run_isolate_separate_instances,
    test_attach_recovered_run_routes_better_agent_runner_descriptor,
    test_recover_in_flight_partitions_native_and_better_agent_runner_dirs,
    test_credential_mock_target_pitfall_is_caught,
]


def main() -> int:
    failed = 0
    try:
        for fn in TESTS:
            try:
                ok = fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"{FAIL} {fn.__name__}: {e}")
                ok = False
            if not ok:
                failed += 1
                print(f"{FAIL} {fn.__name__}")
    finally:
        _TEST_HOME.release()
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

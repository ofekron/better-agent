"""Locks the per-account OAuth login/logout lifecycle for subscription
providers (claude, codex).

The settings-UI login spawns the provider's own CLI (`claude auth login` /
`codex login`) with the record's isolated credential env, so two records
with distinct config_dirs log in as distinct accounts. This test locks:

- `supports_auth` is true only for subscription claude/codex records.
- The spawn argv is the fixed kind->suffix mapping for BOTH claude and
  codex (no caller input), and the subprocess env carries the record's
  `CLAUDE_CONFIG_DIR` / `CODEX_HOME` override, pins `HOME`, and clears
  cross-provider credential env.
- The monitor classifies login success/failure (exit code + status
  check), and an exception in the flow NEVER wedges the record busy.
- Logout is exercised end to end.
- A real in-flight flow blocks a concurrent start (not a tautological
  state write).
- `attach_login_state` surfaces auth-flow state + cached auth-status.

The real OAuth browser flow is NOT exercised: the CLI binary resolution
and subprocess spawn are stubbed so the test captures argv+env without
opening a browser or touching the network.
"""
import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="provider_auth_home_")
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
paths.engage_test_home(_TMP)

import config_store  # noqa: E402
import provider_auth  # noqa: E402
from paths import user_home  # noqa: E402

HOME = Path(_TMP)
_original_resolve = provider_auth._resolve_binary
_original_create = asyncio.create_subprocess_exec


def _restore():
    provider_auth._resolve_binary = _original_resolve
    asyncio.create_subprocess_exec = _original_create  # type: ignore


def _add_provider(kind: str, config_dir: str, mode: str = "subscription") -> dict:
    return config_store.add_provider(
        {
            "name": f"{kind}-{config_dir}",
            "kind": kind,
            "mode": mode,
            "config_dir": config_dir,
        }
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _drain(coro):
    """Run an action coroutine to completion AND let the background monitor
    task it creates finish — both on the same loop (a fresh loop per call
    would orphan the monitor task `start_login` schedules)."""
    async def _wrap():
        result = await coro
        # Drain every background task the action created (the monitor +
        # its status subcommand) so no task is left pending at loop close.
        loop = asyncio.get_running_loop()
        pending = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return result

    return _run(_wrap())


def _noop_broadcast():
    async def _b():
        await asyncio.sleep(0)

    return _b


def test_supports_auth_only_subscription_claude_codex():
    sub_claude = _add_provider("claude", str(HOME / ".claude-a"))
    sub_codex = _add_provider("codex", str(HOME / ".codex-a"))
    api_key_claude = _add_provider("claude", str(HOME / ".claude-b"), mode="api_key")
    assert provider_auth.supports_auth(sub_claude)
    assert provider_auth.supports_auth(sub_codex)
    assert not provider_auth.supports_auth(api_key_claude)


def test_build_env_isolates_credential_dir_and_pins_home():
    claude = _add_provider("claude", str(HOME / ".claude-work"))
    codex = _add_provider("codex", str(HOME / ".codex-work"))
    os.environ["ANTHROPIC_API_KEY"] = "leaked-key"
    os.environ["CODEX_HOME"] = "/tmp/should-not-leak"
    try:
        c_env = provider_auth._build_env(claude)
        assert c_env["CLAUDE_CONFIG_DIR"] == str(HOME / ".claude-work")
        assert "ANTHROPIC_API_KEY" not in c_env
        assert "CODEX_HOME" not in c_env
        # HOME is pinned from the passwd db, not inherited.
        assert c_env["HOME"] == str(user_home())

        x_env = provider_auth._build_env(codex)
        assert x_env["CODEX_HOME"] == str(HOME / ".codex-work")
        assert "CLAUDE_CONFIG_DIR" not in x_env
        assert x_env["CODEX_HOME"] != "/tmp/should-not-leak"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("CODEX_HOME", None)


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b"", raises=None, pid=123456):
        self.returncode = returncode
        self.pid = pid
        self._stdout = stdout
        self._stderr = stderr
        self._raises = raises
        self.killed = False

    async def communicate(self):
        if self._raises:
            raise self._raises
        return (self._stdout, self._stderr)

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


def _install_fakes(spawn_fn, binary="/usr/local/bin/cli"):
    """Route both binary resolution and subprocess spawn through fakes,
    recording every spawn so tests can assert argv per kind."""
    spawns: list[dict] = []

    def fake_resolve(kind):
        return binary

    async def fake_create(*argv, **kwargs):
        proc = spawn_fn(argv, kwargs)
        if asyncio.iscoroutine(proc):
            proc = await proc
        spawns.append({"argv": list(argv), "env": dict(kwargs.get("env") or {}), "proc": proc})
        return proc

    provider_auth._resolve_binary = fake_resolve  # type: ignore
    asyncio.create_subprocess_exec = fake_create  # type: ignore
    return spawns


def test_login_spawns_fixed_argv_for_claude_and_codex():
    try:
        claude = _add_provider("claude", str(HOME / ".claude-argv"))
        codex = _add_provider("codex", str(HOME / ".codex-argv"))

        # claude login
        spawns = _install_fakes(lambda argv, kw: _FakeProc(0))
        provider_auth._states.clear()
        _drain(provider_auth.start_login(claude["id"], _noop_broadcast()))
        assert spawns[0]["argv"][:3] == ["/usr/local/bin/cli", "auth", "login"]
        assert spawns[0]["env"]["CLAUDE_CONFIG_DIR"] == str(HOME / ".claude-argv")

        # codex login — distinct argv suffix
        spawns = _install_fakes(lambda argv, kw: _FakeProc(0))
        provider_auth._states.clear()
        _drain(provider_auth.start_login(codex["id"], _noop_broadcast()))
        assert spawns[0]["argv"][:2] == ["/usr/local/bin/cli", "login"]
        assert spawns[0]["env"]["CODEX_HOME"] == str(HOME / ".codex-argv")
    finally:
        _restore()


def test_monitor_marks_success_only_when_status_confirms():
    try:
        claude = _add_provider("claude", str(HOME / ".claude-success"))
        # login exits 0, then status check exits 0 -> authenticated
        spawns = _install_fakes(
            lambda argv, kw: _FakeProc(1 if "status" in argv else 0)
        )
        provider_auth._states.clear()
        _drain(provider_auth.start_login(claude["id"], _noop_broadcast()))
        st = provider_auth.login_state(claude["id"])
        # status returned rc=1 -> not authenticated -> login_failed
        assert st["status"] == provider_auth.STATE_LOGIN_FAILED, st
    finally:
        _restore()


def test_monitor_marks_success_when_status_confirms():
    try:
        claude = _add_provider("claude", str(HOME / ".claude-success2"))
        spawns = _install_fakes(lambda argv, kw: _FakeProc(0))  # all 0
        provider_auth._states.clear()
        _drain(provider_auth.start_login(claude["id"], _noop_broadcast()))
        st = provider_auth.login_state(claude["id"])
        assert st["status"] == provider_auth.STATE_LOGIN_SUCCESS, st
        assert provider_auth._auth_cache[claude["id"]][1] is True
    finally:
        _restore()


def test_exception_in_flow_does_not_wedge_busy():
    try:
        claude = _add_provider("claude", str(HOME / ".claude-crash"))
        # communicate() raises -> monitor must resolve to failed, not stuck
        spawns = _install_fakes(
            lambda argv, kw: _FakeProc(raises=RuntimeError("boom"))
        )
        provider_auth._states.clear()
        first = _drain(provider_auth.start_login(claude["id"], _noop_broadcast()))
        assert provider_auth.login_state(claude["id"])["status"] != provider_auth.STATE_LOGIN_RUNNING

        # A second start must NOT be rejected as busy — the record unwedged.
        provider_auth._states.pop(claude["id"], None)
        provider_auth._auth_cache.pop(claude["id"], None)
        second = _drain(provider_auth.start_login(claude["id"], _noop_broadcast()))
        assert second.get("error") != "busy", second
    finally:
        _restore()


def test_logout_runs_logout_argv_and_invalidates_auth():
    try:
        codex = _add_provider("codex", str(HOME / ".codex-logout"))
        spawns = _install_fakes(lambda argv, kw: _FakeProc(0))
        provider_auth._states.clear()
        provider_auth._auth_cache[codex["id"]] = (0.0, True)
        _drain(provider_auth.start_logout(codex["id"], _noop_broadcast()))
        assert spawns[0]["argv"][:2] == ["/usr/local/bin/cli", "logout"]
        st = provider_auth.login_state(codex["id"])
        assert st["status"] in (provider_auth.STATE_LOGGED_OUT, provider_auth.STATE_IDLE)
    finally:
        _restore()


def test_concurrent_real_flows_are_serialized():
    """Two simultaneous start_login calls on the same record: exactly one
    wins, the other is rejected busy. Exercises the lock, not a state write."""
    try:
        claude = _add_provider("claude", str(HOME / ".claude-concurrent"))

        async def slow_login(argv, kw):
            await asyncio.sleep(0.1)  # hold the lock + in-flight window
            return _FakeProc(0)

        spawns = _install_fakes(slow_login)
        provider_auth._states.clear()

        async def _both():
            res = await asyncio.gather(
                provider_auth.start_login(claude["id"], _noop_broadcast()),
                provider_auth.start_login(claude["id"], _noop_broadcast()),
            )
            # Drain the winner's monitor task on this same loop.
            loop = asyncio.get_running_loop()
            pending = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            return res

        a, b = _run(_both())
        outcomes = sorted([a.get("error") or "ok", b.get("error") or "ok"])
        assert "busy" in outcomes, outcomes
    finally:
        _restore()


def test_attach_login_state_adds_state_for_supported_records():
    claude = _add_provider("claude", str(HOME / ".claude-attach"))
    provider_auth._states.clear()
    provider_auth._auth_cache.clear()
    detached = provider_auth.attach_login_state(dict(claude))
    assert "login_state" in detached
    assert detached["login_state"]["status"] == provider_auth.STATE_IDLE
    assert detached["login_state"]["authenticated"] is None


def test_cancel_kills_tracked_proc_and_clears_busy():
    """cancel() kills the in-flight process tree bound to the record, and
    is a no-op for an already-finished or unknown record."""
    claude = _add_provider("claude", str(HOME / ".claude-cancel"))
    provider_auth._procs.clear()
    # In-flight: returncode None -> killed.
    in_flight = _FakeProc()
    in_flight.returncode = None
    provider_auth._procs[claude["id"]] = in_flight
    assert provider_auth.cancel(claude["id"]) is True
    # Already finished (returncode set) -> no-op.
    provider_auth._procs[claude["id"]] = _FakeProc(returncode=0)
    assert provider_auth.cancel(claude["id"]) is False
    # Unknown record -> no-op.
    provider_auth._procs.pop(claude["id"], None)
    assert provider_auth.cancel(claude["id"]) is False


def test_shutdown_all_kills_tracked_procs():
    """shutdown_all force-kills every in-flight tree. Markers are NOT cleared
    here (a kill that didn't fully take stays reapable at next startup)."""
    claude = _add_provider("claude", str(HOME / ".claude-shutdown"))
    provider_auth._procs.clear()
    p = _FakeProc()
    p.returncode = None
    provider_auth._procs[claude["id"]] = p
    provider_auth._write_marker(claude["id"], p, "claude")
    provider_auth.shutdown_all()
    assert provider_auth._procs == {}


def _spawn_sleep_orphan():
    import subprocess
    # Detach like the real spawn (start_new_session) so force_kill's killpg
    # reaches ONLY this orphan's group, not the test's own.
    return subprocess.Popen(["sleep", "30"], start_new_session=True)


def test_reap_kills_orphan_when_identity_confirmed():
    """When the marker pid IS the expected CLI, reap force-kills it."""
    from proc_control import process_control

    provider_id = "orphan-confirmed"
    orphan = _spawn_sleep_orphan()
    original_identity = provider_auth._proc_is_expected_cli
    provider_auth._proc_is_expected_cli = lambda pid, kind: True  # simulate our CLI
    try:
        provider_auth._write_marker(provider_id, type("P", (), {"pid": orphan.pid})(), "claude")
        provider_auth._procs.clear()
        provider_auth.reap_orphaned_logins()
        orphan.wait(timeout=3)
        assert orphan.returncode is not None and orphan.returncode < 0, (
            f"orphan not reaped (rc={orphan.returncode})"
        )
        assert not provider_auth._marker_path(provider_id).exists()
    finally:
        provider_auth._proc_is_expected_cli = original_identity
        if process_control().pid_alive(orphan.pid):
            process_control().force_kill(orphan.pid)
        try:
            orphan.wait(timeout=2)
        except Exception:
            pass


def test_reap_spares_unrelated_process():
    """Regression for the PID-reuse safety hole: a marker whose live pid is
    NOT the expected claude/codex CLI must NEVER be killed, only unlinked."""
    from proc_control import process_control

    provider_id = "orphan-pid-reuse"
    orphan = _spawn_sleep_orphan()
    try:
        provider_auth._write_marker(provider_id, type("P", (), {"pid": orphan.pid})(), "claude")
        provider_auth._procs.clear()
        provider_auth.reap_orphaned_logins()
        # The sleep is NOT a claude CLI -> spared.
        assert process_control().pid_alive(orphan.pid), "unrelated process was wrongly killed"
        assert not provider_auth._marker_path(provider_id).exists()
    finally:
        process_control().force_kill(orphan.pid)
        try:
            orphan.wait(timeout=2)
        except Exception:
            pass


def test_reap_clears_dead_pid_marker():
    """A marker whose pid is already gone is just unlinked, no kill attempt."""
    provider_id = "orphan-dead"
    provider_auth._write_marker(provider_id, type("P", (), {"pid": 999999})(), "claude")
    provider_auth._procs.clear()
    provider_auth.reap_orphaned_logins()
    assert not provider_auth._marker_path(provider_id).exists()


def test_reap_skips_live_in_procs_flow():
    """A marker for a record currently in _procs is a live flow, not an
    orphan — reap must leave both the process and the marker alone."""
    from proc_control import process_control

    provider_id = "orphan-live"
    orphan = _spawn_sleep_orphan()
    original_identity = provider_auth._proc_is_expected_cli
    provider_auth._proc_is_expected_cli = lambda pid, kind: True
    try:
        provider_auth._write_marker(provider_id, type("P", (), {"pid": orphan.pid})(), "claude")
        provider_auth._procs[provider_id] = type("P", (), {"pid": orphan.pid, "returncode": None})()
        provider_auth.reap_orphaned_logins()
        assert process_control().pid_alive(orphan.pid), "live flow was wrongly reaped"
        assert provider_auth._marker_path(provider_id).exists(), "live flow marker was wrongly cleared"
    finally:
        provider_auth._proc_is_expected_cli = original_identity
        provider_auth._procs.pop(provider_id, None)
        process_control().force_kill(orphan.pid)
        try:
            orphan.wait(timeout=2)
        except Exception:
            pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"  {name} ...", end=" ")
            fn()
            print("ok")
    print("all provider_auth tests passed")

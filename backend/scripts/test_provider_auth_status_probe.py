"""Covers the async status-probe / auth-status half of provider_auth:
bounded admission semaphores, owned-subprocess reap/cleanup lifecycle,
the authoritative `_status_authenticated` probe and all its failure
branches, `shutdown_status_probes`, the durable auth-status commit/
refresh/schedule path, registered-flow discard, and the remaining
`_monitor` / `_start` / `cancel` branches the login happy-path test
does not reach.

Same isolation contract as test_provider_auth_reconcile_markers.py: an
isolated BETTER_AGENT_HOME tempdir, real config_store persistence, and
the subprocess surface stubbed at the binary-resolution / spawn seam.
No provider tokens, no network, no real subprocesses.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="provider_auth_status_")
import atexit  # noqa: E402

atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
paths.engage_test_home(_TMP)

import config_store  # noqa: E402
import provider_auth  # noqa: E402
import provider_transport  # noqa: E402

HOME = Path(_TMP)

# Original seams we patch; restored in _restore_all so no global leaks.
_orig_spawn = provider_auth._spawn
_orig_kill_proc = provider_auth._kill_proc
_orig_cancel_seconds = provider_auth._CANCEL_CONFIRM_SECONDS
_orig_status_timeout = provider_auth._STATUS_TIMEOUT_SECONDS
_orig_login_timeout = provider_auth.LOGIN_TIMEOUT_SECONDS
_orig_reap_owned = provider_auth._reap_owned_status
_orig_apply_transport = provider_transport.apply_provider_transport
_orig_status_authenticated = provider_auth._status_authenticated
_orig_commit_auth_status = provider_auth._commit_auth_status
_orig_refresh_auth_status = provider_auth.refresh_auth_status
_orig_invalidate_auth = provider_auth._invalidate_auth
_orig_acquire_admission = provider_auth._acquire_status_admission
_orig_apply_if_matches = config_store.apply_if_provider_matches


def _restore_all():
    provider_auth._spawn = _orig_spawn
    provider_auth._kill_proc = _orig_kill_proc
    provider_auth._CANCEL_CONFIRM_SECONDS = _orig_cancel_seconds
    provider_auth._STATUS_TIMEOUT_SECONDS = _orig_status_timeout
    provider_auth.LOGIN_TIMEOUT_SECONDS = _orig_login_timeout
    provider_auth._reap_owned_status = _orig_reap_owned
    provider_auth._status_authenticated = _orig_status_authenticated
    provider_auth._commit_auth_status = _orig_commit_auth_status
    provider_auth.refresh_auth_status = _orig_refresh_auth_status
    provider_auth._invalidate_auth = _orig_invalidate_auth
    provider_auth._acquire_status_admission = _orig_acquire_admission
    provider_transport.apply_provider_transport = _orig_apply_transport
    config_store.apply_if_provider_matches = _orig_apply_if_matches


def _add_provider(kind="claude", **extra):
    rec = {
        "name": f"{kind}-{os.urandom(3).hex()}",
        "kind": kind,
        "mode": "subscription",
        "config_dir": str(HOME / f".{os.urandom(3).hex()}"),
    }
    rec.update(extra)
    return config_store.add_provider(rec)


def _reset_status_globals():
    """Clear the loop-bound admission state between tests so each test
    runs on its own fresh loop without tripping the loop-change guard."""
    provider_auth._status_admission_loop = None
    provider_auth._status_global_semaphore = None
    provider_auth._status_kind_semaphores.clear()
    provider_auth._status_tasks.clear()
    provider_auth._status_procs.clear()
    provider_auth._status_cleanup_tasks.clear()
    provider_auth._status_shutting_down = False
    provider_auth._pending_refresh.clear()
    provider_auth._tasks.clear()
    provider_auth._config_change_loop = None
    provider_auth._config_reconcile_task = None
    provider_auth._config_reconcile_requested = False
    provider_auth._config_change_broadcast = None


class _FakeProc:
    """asyncio.subprocess.Process stand-in with controllable lifecycle."""

    def __init__(self, returncode=0, pid=99999, stdout=b"", stderr=b"",
                 communicate_fn=None, wait_fn=None):
        self.returncode = returncode
        self.pid = pid
        self._stdout = stdout
        self._stderr = stderr
        self._communicate_fn = communicate_fn
        self._wait_fn = wait_fn
        self.killed = False

    async def communicate(self):
        if self._communicate_fn is not None:
            return await self._communicate_fn(self)
        return self._stdout, self._stderr

    async def wait(self):
        if self._wait_fn is not None:
            return await self._wait_fn(self)
        return self.returncode

    def kill(self):
        self.killed = True


def _run(coro):
    """Drive a coroutine on a fresh loop AND cancel/drain any background
    tasks it created (monitors, scheduled refreshes, reconcile tasks) so
    loop teardown is clean and their finally/cleanup branches run."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _wrap():
        result = await coro
        # Stop the reconcile done-callback from re-arming a fresh task as we
        # cancel the in-flight one, then cancel/drain everything that remains.
        provider_auth._config_reconcile_requested = False
        for _ in range(5):
            pending = [t for t in asyncio.all_tasks(loop)
                       if t is not asyncio.current_task()]
            if not pending:
                break
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return result

    try:
        return loop.run_until_complete(_wrap())
    finally:
        loop.close()


async def _noop_broadcast():
    return None


# ============================================================= _status_admission


def test_status_admission_rejects_loop_change_while_proc_active():
    """Admission is loop-bound: running a probe on a loop that differs
    from the bound one while an owned proc is live is a programming
    error, not a recoverable state."""
    bound = asyncio.new_event_loop()
    runner = asyncio.new_event_loop()
    try:
        provider_auth._status_admission_loop = bound
        provider_auth._status_procs[object()] = _FakeProc()

        async def _on_runner():
            try:
                provider_auth._status_admission("claude")
                assert False, "expected RuntimeError on loop change"
            except RuntimeError as exc:
                assert "changed event loops" in str(exc)

        asyncio.set_event_loop(runner)
        runner.run_until_complete(_on_runner())
    finally:
        _reset_status_globals()
        bound.close()
        runner.close()


def test_status_admission_reinits_global_semaphore_when_none():
    """On a loop already bound, a None global semaphore is rebuilt lazily."""

    async def _scenario():
        provider_auth._status_admission_loop = asyncio.get_running_loop()
        provider_auth._status_global_semaphore = None
        kind_sem, glob_sem = provider_auth._status_admission("claude")
        assert glob_sem is not None
        assert kind_sem is not None
        kind_sem2, _ = provider_auth._status_admission("codex")
        assert kind_sem2 is not kind_sem

    try:
        _run(_scenario())
    finally:
        _reset_status_globals()


# ====================================================== _acquire_status_admission


async def _raise_cancelled():
    raise asyncio.CancelledError()


def test_acquire_status_admission_releases_kind_on_global_failure():
    """If the global semaphore acquire errors after the kind permit was
    acquired, the kind permit is released (no leak)."""

    async def _scenario():
        provider_auth._status_admission_loop = asyncio.get_running_loop()
        kind_real = asyncio.Semaphore(1)
        raising = type("S", (), {
            "acquire": staticmethod(_raise_cancelled),
            "release": staticmethod(lambda: None),
        })()
        provider_auth._status_global_semaphore = raising
        provider_auth._status_kind_semaphores["claude"] = kind_real
        try:
            await provider_auth._acquire_status_admission("claude")
            assert False, "expected CancelledError"
        except asyncio.CancelledError:
            pass
        assert kind_real._value == 1, "kind semaphore permit leaked"

    try:
        _run(_scenario())
    finally:
        _reset_status_globals()


# ======================================================== _kill_and_reap_status_proc


def test_kill_and_reap_noop_when_proc_already_dead():
    proc = _FakeProc(returncode=0)
    killed: list = []
    provider_auth._kill_proc = lambda p: killed.append(p)  # type: ignore
    try:
        _run(provider_auth._kill_and_reap_status_proc(proc))
        assert killed == [], "already-dead proc should not be killed"
    finally:
        _restore_all()


def test_kill_and_reap_kills_live_proc_and_waits():
    proc = _FakeProc(returncode=None)

    async def _wait_then_set(p):
        p.returncode = 0
        return 0
    proc._wait_fn = _wait_then_set
    killed: list = []
    provider_auth._kill_proc = lambda p: killed.append(p)  # type: ignore
    try:
        _run(provider_auth._kill_and_reap_status_proc(proc))
        assert proc in killed
        assert proc.returncode == 0
    finally:
        _restore_all()


def test_kill_and_reap_processlookuperror_reraised_when_still_live():
    proc = _FakeProc(returncode=None)

    async def _wait_missing(p):
        raise ProcessLookupError()
    proc._wait_fn = _wait_missing
    provider_auth._kill_proc = lambda p: None  # type: ignore
    try:
        try:
            _run(provider_auth._kill_and_reap_status_proc(proc))
            assert False, "expected ProcessLookupError re-raised"
        except ProcessLookupError:
            pass
    finally:
        _restore_all()


def test_kill_and_reap_processlookuperror_swallowed_when_dead():
    proc = _FakeProc(returncode=2)

    async def _wait_missing(p):
        raise ProcessLookupError()
    proc._wait_fn = _wait_missing
    provider_auth._kill_proc = lambda p: None  # type: ignore
    try:
        _run(provider_auth._kill_and_reap_status_proc(proc))  # no raise
    finally:
        _restore_all()


def test_kill_and_reap_timeout_raises_runtimeerror_when_unreaped():
    proc = _FakeProc(returncode=None)
    provider_auth._CANCEL_CONFIRM_SECONDS = 0.01  # type: ignore

    async def _hang(p):
        await asyncio.sleep(5)
        return 0
    proc._wait_fn = _hang
    provider_auth._kill_proc = lambda p: None  # type: ignore
    try:
        try:
            _run(provider_auth._kill_and_reap_status_proc(proc))
            assert False, "expected RuntimeError (unreaped)"
        except RuntimeError as exc:
            assert "did not reap" in str(exc)
    finally:
        _restore_all()


# ============================================================ _reap_owned_status


def test_reap_owned_status_pops_token_on_success():
    token = object()
    proc = _FakeProc(returncode=0)
    provider_auth._status_procs[token] = proc
    try:
        _run(provider_auth._reap_owned_status(token, proc))
        assert token not in provider_auth._status_procs
    finally:
        provider_auth._status_procs.pop(token, None)


def test_reap_owned_status_raises_when_proc_remains_live():
    token = object()
    proc = _FakeProc(returncode=None)
    provider_auth._status_procs[token] = proc
    provider_auth._kill_proc = lambda p: None  # type: ignore
    try:
        try:
            _run(provider_auth._reap_owned_status(token, proc))
            assert False, "expected RuntimeError for live proc"
        except RuntimeError as exc:
            assert "remains live" in str(exc)
    finally:
        provider_auth._status_procs.pop(token, None)
        _restore_all()


# ============================================================ _status_cleanup_task


def test_status_cleanup_task_reuses_existing_running_task():
    token = object()
    proc = _FakeProc()

    async def _scenario():
        existing = asyncio.create_task(asyncio.sleep(5))
        provider_auth._status_cleanup_tasks[token] = existing
        reused = provider_auth._status_cleanup_task(token, proc)
        assert reused is existing
        existing.cancel()
        try:
            await existing
        except asyncio.CancelledError:
            pass

    try:
        _run(_scenario())
    finally:
        _reset_status_globals()


def test_status_cleanup_task_removes_on_success_keeps_on_failure():
    token = object()
    proc = _FakeProc(returncode=0)

    async def _succeed(t, p):
        provider_auth._status_procs.pop(t, None)

    async def _fail(t, p):
        raise RuntimeError("reap failed")

    async def _drive(make_reap):
        provider_auth._reap_owned_status = make_reap  # type: ignore
        task = provider_auth._status_cleanup_task(token, proc)
        await task
        return task

    try:
        _run(_drive(_succeed))
        assert token not in provider_auth._status_cleanup_tasks

        try:
            _run(_drive(_fail))
        except RuntimeError:
            pass
        assert token in provider_auth._status_cleanup_tasks
    finally:
        _reset_status_globals()
        _restore_all()


# ============================================================ _await_status_cleanup


def test_await_status_cleanup_propagates_cancel_after_cleanup_finishes():
    """Cancelling the awaiter mid-shield must still let the cleanup task
    complete, then re-raise CancelledError to the caller."""
    token = object()
    proc = _FakeProc(returncode=0)
    cleanup_done = asyncio.Event()

    async def _slow_reap(t, p):
        await asyncio.sleep(0.05)
        cleanup_done.set()

    async def _scenario():
        provider_auth._reap_owned_status = _slow_reap  # type: ignore
        awaiter = asyncio.create_task(
            provider_auth._await_status_cleanup(token, proc)
        )
        await asyncio.sleep(0.01)
        awaiter.cancel()
        try:
            await awaiter
            assert False, "expected CancelledError"
        except asyncio.CancelledError:
            pass
        assert cleanup_done.is_set(), "cleanup task should still complete"

    try:
        _run(_scenario())
    finally:
        _reset_status_globals()
        _restore_all()


# ============================================================ _spawn_owned_status


def _install_spawn(spawn_fn):
    async def _fake(provider, suffix):
        result = spawn_fn()
        if asyncio.iscoroutine(result):
            result = await result
        return result
    provider_auth._spawn = _fake  # type: ignore


def test_spawn_owned_status_registers_proc_on_success():
    token = object()
    proc = _FakeProc(returncode=0)
    _install_spawn(lambda: proc)

    async def _scenario():
        got = await provider_auth._spawn_owned_status({"kind": "claude"}, token)
        assert got is proc
        assert provider_auth._status_procs[token] is proc

    try:
        _run(_scenario())
    finally:
        provider_auth._status_procs.clear()
        _restore_all()


def test_spawn_owned_status_reaps_proc_when_await_cancelled_mid_spawn():
    """If the awaiter is cancelled while the spawn task is in flight and
    the spawn nonetheless returns a proc, the proc is registered and
    reaped via the cleanup path before re-raising."""
    token = object()
    proc = _FakeProc(returncode=0)
    spawn_returned = asyncio.Event()

    async def _slow_spawn(provider, suffix):
        await asyncio.sleep(0.05)
        spawn_returned.set()
        return proc
    provider_auth._spawn = _slow_spawn  # type: ignore
    provider_auth._kill_proc = lambda p: None  # type: ignore

    async def _scenario():
        awaiter = asyncio.create_task(
            provider_auth._spawn_owned_status({"kind": "claude"}, token)
        )
        await asyncio.sleep(0.01)
        awaiter.cancel()
        try:
            await awaiter
            assert False, "expected CancelledError"
        except asyncio.CancelledError:
            pass
        assert spawn_returned.is_set()

    try:
        _run(_scenario())
    finally:
        provider_auth._status_procs.clear()
        _restore_all()


def test_spawn_owned_status_reraises_spawn_error_after_cancel():
    """If the awaiter is cancelled mid-spawn and the spawn itself then
    errors, the spawn error is re-raised (not masked as CancelledError)."""
    token = object()

    async def _raising_spawn(provider, suffix):
        await asyncio.sleep(0.05)
        raise ValueError("spawn internals broke")
    provider_auth._spawn = _raising_spawn  # type: ignore

    async def _scenario():
        awaiter = asyncio.create_task(
            provider_auth._spawn_owned_status({"kind": "claude"}, token)
        )
        await asyncio.sleep(0.01)
        awaiter.cancel()
        try:
            await awaiter
            assert False, "expected ValueError"
        except ValueError as exc:
            assert "spawn internals broke" in str(exc)

    try:
        _run(_scenario())
    finally:
        provider_auth._status_procs.clear()
        _restore_all()


# ============================================================ _status_authenticated


def test_status_authenticated_returns_false_when_shutting_down():

    async def _scenario():
        provider_auth._status_shutting_down = True
        return await provider_auth._status_authenticated({"kind": "claude"})

    try:
        assert _run(_scenario()) is False
    finally:
        _reset_status_globals()


def test_status_authenticated_returns_false_on_spawn_filenotfound():

    async def _boom():
        raise FileNotFoundError("no cli")
    _install_spawn(_boom)

    async def _scenario():
        return await provider_auth._status_authenticated({"kind": "claude"})

    try:
        assert _run(_scenario()) is False
    finally:
        _reset_status_globals()
        _restore_all()


def test_status_authenticated_returns_false_on_spawn_exception():

    async def _boom():
        raise RuntimeError("spawn broke")
    _install_spawn(_boom)

    async def _scenario():
        return await provider_auth._status_authenticated({"kind": "claude"})

    try:
        assert _run(_scenario()) is False
    finally:
        _reset_status_globals()
        _restore_all()


def test_status_authenticated_returns_false_when_spawn_returns_none():

    async def _none():
        return None
    _install_spawn(_none)

    async def _scenario():
        return await provider_auth._status_authenticated({"kind": "claude"})

    try:
        assert _run(_scenario()) is False
    finally:
        _reset_status_globals()
        _restore_all()


def test_status_authenticated_returns_false_on_shutting_down_after_admission():
    """The post-admission shutdown check (between acquiring permits and
    spawning) short-circuits to False so a shutdown isn't raced by a
    new probe."""
    _install_spawn(lambda: _FakeProc(returncode=0))

    async def _acquire_then_flag(kind):
        permits = await _orig_acquire_admission(kind)
        provider_auth._status_shutting_down = True
        return permits

    async def _scenario():
        provider_auth._acquire_status_admission = _acquire_then_flag  # type: ignore
        return await provider_auth._status_authenticated({"kind": "claude"})

    try:
        assert _run(_scenario()) is False
    finally:
        _reset_status_globals()
        _restore_all()


def test_status_authenticated_returns_false_on_fingerprint_drift():
    """If the stored provider changed between probe start and the
    expected-fingerprint check, the probe aborts (stale result)."""
    rec = _add_provider()
    fp = provider_auth._auth_fingerprint(rec)
    assert fp is not None
    _install_spawn(lambda: _FakeProc(returncode=0))
    stale = (fp[0], fp[1], fp[2] + 999, fp[3], fp[4], fp[5])

    async def _scenario():
        return await provider_auth._status_authenticated(rec, stale)

    try:
        assert _run(_scenario()) is False
    finally:
        _reset_status_globals()
        _restore_all()


def test_status_authenticated_true_on_zero_exit_false_on_nonzero():
    _install_spawn(lambda: _FakeProc(returncode=0))

    async def _ok():
        return await provider_auth._status_authenticated({"kind": "claude"})

    try:
        assert _run(_ok()) is True
        _reset_status_globals()

        _install_spawn(lambda: _FakeProc(returncode=1))

        async def _bad():
            return await provider_auth._status_authenticated({"kind": "claude"})

        assert _run(_bad()) is False
    finally:
        _reset_status_globals()
        _restore_all()


def test_status_authenticated_reaps_on_communicate_timeout():
    proc = _FakeProc(returncode=None)

    async def _hang(p):
        await asyncio.sleep(5)
        return b"", b""
    proc._communicate_fn = _hang

    async def _wait_sets_exit(p):
        p.returncode = 0
        return 0
    proc._wait_fn = _wait_sets_exit
    _install_spawn(lambda: proc)
    provider_auth._STATUS_TIMEOUT_SECONDS = 0.02  # type: ignore
    killed: list = []
    provider_auth._kill_proc = lambda p: killed.append(p)  # type: ignore

    async def _scenario():
        return await provider_auth._status_authenticated({"kind": "claude"})

    try:
        assert _run(_scenario()) is False
        assert proc in killed, "timed-out probe proc was not reaped"
    finally:
        _reset_status_globals()
        _restore_all()


def test_status_authenticated_cleans_up_live_proc_on_communicate_error():
    """A non-timeout error from communicate() propagates, but the finally
    block still reaps the live owned proc via the cleanup path before
    the error escapes."""
    proc = _FakeProc(returncode=None)

    async def _wait_sets_exit(p):
        p.returncode = 0
        return 0
    proc._wait_fn = _wait_sets_exit

    async def _crash(p):
        raise ValueError("transport framing error")
    proc._communicate_fn = _crash
    _install_spawn(lambda: proc)
    provider_auth._kill_proc = lambda p: None  # type: ignore

    async def _scenario():
        try:
            await provider_auth._status_authenticated({"kind": "claude"})
            assert False, "expected ValueError to propagate"
        except ValueError:
            pass

    try:
        _run(_scenario())
        # The live proc was registered then reaped by the finally cleanup.
        assert len(provider_auth._status_procs) == 0
    finally:
        provider_auth._status_procs.clear()
        _reset_status_globals()
        _restore_all()


# ============================================================ shutdown_status_probes


def test_shutdown_status_probes_noop_when_idle():

    async def _scenario():
        await provider_auth.shutdown_status_probes()

    try:
        _run(_scenario())
        assert provider_auth._status_shutting_down is True
    finally:
        _reset_status_globals()


def test_shutdown_status_probes_cancels_in_flight_tasks_and_reaps_procs():
    proc = _FakeProc(returncode=None)

    async def _wait_sets_exit(p):
        p.returncode = 0
        return 0
    proc._wait_fn = _wait_sets_exit
    token = object()
    provider_auth._status_procs[token] = proc
    provider_auth._kill_proc = lambda p: setattr(proc, "returncode", 0)  # type: ignore

    async def _child():
        await asyncio.sleep(5)

    async def _scenario():
        provider_auth._status_tasks.add(asyncio.current_task())
        child = asyncio.create_task(_child())
        provider_auth._status_tasks.add(child)
        await provider_auth.shutdown_status_probes()
        assert child.cancelled() or child.done()

    try:
        _run(_scenario())
        assert token not in provider_auth._status_procs
    finally:
        _reset_status_globals()
        _restore_all()


def test_shutdown_status_probes_raises_when_proc_remains_live():
    proc = _FakeProc(returncode=None)  # stays live
    token = object()
    provider_auth._status_procs[token] = proc
    provider_auth._kill_proc = lambda p: None  # type: ignore

    async def _scenario():
        await provider_auth.shutdown_status_probes()

    try:
        try:
            _run(_scenario())
            assert False, "expected RuntimeError for left-over owned proc"
        except RuntimeError as exc:
            assert "left owned processes" in str(exc)
    finally:
        _reset_status_globals()
        _restore_all()


def test_shutdown_status_probes_cancels_pending_reconcile_task():
    async def _scenario():
        provider_auth._status_shutting_down = False
        provider_auth._config_change_loop = asyncio.get_running_loop()
        provider_auth._config_reconcile_requested = True

        async def _hang_broadcast():
            await asyncio.sleep(5)
        provider_auth._config_change_broadcast = _hang_broadcast
        provider_auth._request_config_reconcile()
        rec_task = provider_auth._config_reconcile_task
        assert rec_task is not None and not rec_task.done()
        await provider_auth.shutdown_status_probes()
        return rec_task

    try:
        rec_task = _run(_scenario())
        assert rec_task.cancelled() or rec_task.done()
        assert provider_auth._config_change_loop is None
        assert provider_auth._config_reconcile_task is None
    finally:
        _reset_status_globals()
        _restore_all()


# ============================================================ _commit_auth_status


def test_commit_auth_status_false_and_requests_reconcile_on_drift():
    """A stale fingerprint (provider changed mid-probe) means the commit
    predicate fails -> not committed -> request a reconcile, return False."""
    rec = _add_provider()
    real_fp = provider_auth._auth_fingerprint(rec)
    assert real_fp is not None
    stale = (real_fp[0], real_fp[1], real_fp[2] + 999,
             real_fp[3], real_fp[4], real_fp[5])

    async def _scenario():
        provider_auth._config_change_loop = asyncio.get_running_loop()
        provider_auth._status_shutting_down = False
        return await provider_auth._commit_auth_status(rec["id"], stale, True)

    try:
        assert _run(_scenario()) is False
        assert provider_auth._config_reconcile_task is not None
    finally:
        _reset_status_globals()


def test_commit_auth_status_true_and_caches_on_match():
    rec = _add_provider()
    real_fp = provider_auth._auth_fingerprint(rec)
    assert real_fp is not None

    async def _scenario():
        return await provider_auth._commit_auth_status(rec["id"], real_fp, True)

    try:
        assert _run(_scenario()) is True
        assert provider_auth._auth_cache[rec["id"]][1] is True
        assert provider_auth._provider_auth_fingerprints[rec["id"]] == real_fp
    finally:
        provider_auth._auth_cache.pop(rec["id"], None)
        provider_auth._provider_auth_fingerprints.pop(rec["id"], None)


# ============================================================ refresh_auth_status


def test_refresh_auth_status_none_when_provider_missing():

    async def _scenario():
        return await provider_auth.refresh_auth_status("no-such-id", _noop_broadcast)

    try:
        assert _run(_scenario()) is None
    finally:
        _reset_status_globals()


def test_refresh_auth_status_none_when_unsupported():
    apikey = _add_provider(mode="api_key")

    async def _scenario():
        return await provider_auth.refresh_auth_status(apikey["id"], _noop_broadcast)

    try:
        assert _run(_scenario()) is None
    finally:
        _reset_status_globals()


def test_refresh_auth_status_broadcasts_false_on_probe_exception():
    """A failing probe resolves to authenticated=False; the record is
    unchanged so the commit lands and broadcasts the durable truth."""
    rec = _add_provider()

    async def _boom(provider, expected_fingerprint=None):
        raise RuntimeError("probe transport down")
    provider_auth._status_authenticated = _boom  # type: ignore
    fired: list = []

    async def _broadcast():
        fired.append(1)

    async def _scenario():
        return await provider_auth.refresh_auth_status(rec["id"], _broadcast)

    try:
        result = _run(_scenario())
        assert result is False
        assert fired == [1], "broadcast should fire once when durable status commits"
    finally:
        provider_auth._auth_cache.pop(rec["id"], None)
        provider_auth._provider_auth_fingerprints.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


def test_refresh_auth_status_none_when_commit_fails():
    rec = _add_provider()

    async def _ok(provider, expected_fingerprint=None):
        return True

    async def _commit_fail(pid, fp, auth):
        provider_auth._request_config_reconcile()
        return False
    provider_auth._status_authenticated = _ok  # type: ignore
    provider_auth._commit_auth_status = _commit_fail  # type: ignore
    fired: list = []

    async def _broadcast():
        fired.append(1)

    async def _scenario():
        provider_auth._config_change_loop = asyncio.get_running_loop()
        provider_auth._status_shutting_down = False
        return await provider_auth.refresh_auth_status(rec["id"], _broadcast)

    try:
        assert _run(_scenario()) is None
        assert fired == [], "no broadcast when commit did not land"
    finally:
        _reset_status_globals()
        _restore_all()


# ============================================================ maybe_schedule_refresh


def test_maybe_schedule_refresh_skips_when_already_pending():
    rec = _add_provider()
    provider_auth._pending_refresh.add(rec["id"])
    before = set(provider_auth._tasks)
    provider_auth.maybe_schedule_refresh(rec["id"], _noop_broadcast)
    assert provider_auth._tasks == before
    provider_auth._pending_refresh.discard(rec["id"])


def test_maybe_schedule_refresh_skips_when_cached():
    rec = _add_provider()
    fp = provider_auth._auth_fingerprint(rec)
    provider_auth._auth_cache[rec["id"]] = (time.monotonic(), True, fp)
    provider_auth._provider_auth_fingerprints[rec["id"]] = fp
    before = set(provider_auth._tasks)
    try:
        provider_auth.maybe_schedule_refresh(rec["id"], _noop_broadcast)
        assert provider_auth._tasks == before
        assert rec["id"] not in provider_auth._pending_refresh
    finally:
        provider_auth._auth_cache.pop(rec["id"], None)
        provider_auth._provider_auth_fingerprints.pop(rec["id"], None)


def test_maybe_schedule_refresh_skips_when_unsupported():
    apikey = _add_provider(mode="api_key")
    before = set(provider_auth._tasks)
    provider_auth.maybe_schedule_refresh(apikey["id"], _noop_broadcast)
    assert provider_auth._tasks == before
    assert apikey["id"] not in provider_auth._pending_refresh


def test_maybe_schedule_refresh_noop_without_running_loop():
    rec = _add_provider()
    provider_auth.maybe_schedule_refresh(rec["id"], _noop_broadcast)
    assert rec["id"] not in provider_auth._pending_refresh


def test_maybe_schedule_refresh_creates_task_and_publishes():
    rec = _add_provider()

    async def _ok(provider, expected_fingerprint=None):
        return True
    provider_auth._status_authenticated = _ok  # type: ignore
    fired: list = []

    async def _broadcast():
        fired.append(1)

    async def _scenario():
        provider_auth.maybe_schedule_refresh(rec["id"], _broadcast)
        # Let the scheduled task run before _run's drain cancels it.
        for _ in range(200):
            if fired:
                break
            await asyncio.sleep(0.005)

    try:
        _run(_scenario())
        assert fired, "scheduled refresh should have broadcast"
        assert rec["id"] not in provider_auth._pending_refresh
    finally:
        provider_auth._auth_cache.pop(rec["id"], None)
        provider_auth._provider_auth_fingerprints.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


# ============================================================ _discard_registered_flow


def test_discard_registered_flow_clears_current_flow():
    rec = _add_provider()
    fp = provider_auth._auth_fingerprint(rec)
    proc = _FakeProc(returncode=0)
    provider_auth._procs[rec["id"]] = proc
    provider_auth._flow_fingerprints[rec["id"]] = fp
    provider_auth._states[rec["id"]] = {"status": provider_auth.STATE_LOGIN_RUNNING}
    provider_auth._kill_proc = lambda p: None  # type: ignore
    try:
        _run(provider_auth._discard_registered_flow(rec["id"], fp, proc))
        assert rec["id"] not in provider_auth._procs
        assert rec["id"] not in provider_auth._flow_fingerprints
        assert rec["id"] not in provider_auth._states
    finally:
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._flow_fingerprints.pop(rec["id"], None)
        provider_auth._states.pop(rec["id"], None)
        _restore_all()


def test_discard_registered_flow_keeps_state_when_flow_superseded():
    rec = _add_provider()
    fp = provider_auth._auth_fingerprint(rec)
    proc = _FakeProc(returncode=0)
    provider_auth._procs[rec["id"]] = proc
    provider_auth._flow_fingerprints[rec["id"]] = fp
    provider_auth._states[rec["id"]] = {"status": provider_auth.STATE_LOGIN_RUNNING}
    provider_auth._kill_proc = lambda p: None  # type: ignore
    other = (fp[0], fp[1], fp[2] + 1, fp[3], fp[4], fp[5])
    try:
        _run(provider_auth._discard_registered_flow(rec["id"], other, proc))
        assert rec["id"] in provider_auth._procs
        assert rec["id"] in provider_auth._states
    finally:
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._flow_fingerprints.pop(rec["id"], None)
        provider_auth._states.pop(rec["id"], None)
        _restore_all()


# ============================================================ _notify_catalog_auth_changed


def test_notify_catalog_auth_changed_swallows_errors():
    import types
    fake = types.ModuleType("model_catalog_refresh")

    def _boom(provider_id):
        raise RuntimeError("catalog unavailable")
    fake.notify_provider_auth_changed = _boom  # type: ignore
    # Restore the ORIGINAL module, not just pop: a bare pop forces every later
    # `import model_catalog_refresh` in the process to re-import a fresh
    # (unpatched) module object, silently breaking other tests' stubs.
    original = sys.modules.get("model_catalog_refresh")
    sys.modules["model_catalog_refresh"] = fake
    try:
        provider_auth._notify_catalog_auth_changed("any-id")  # must not raise
    finally:
        if original is not None:
            sys.modules["model_catalog_refresh"] = original
        else:
            sys.modules.pop("model_catalog_refresh", None)


# ============================================================ _monitor branches


def _seed_flow(rec, proc, fingerprint=None):
    fp = fingerprint or provider_auth._auth_fingerprint(rec)
    provider_auth._flow_fingerprints[rec["id"]] = fp
    provider_auth._provider_auth_fingerprints[rec["id"]] = fp
    provider_auth._procs[rec["id"]] = proc
    return fp


def _clear_flow(provider_id):
    """Drop every projection entry a _monitor/_start test may have touched."""
    provider_auth._procs.pop(provider_id, None)
    provider_auth._flow_fingerprints.pop(provider_id, None)
    provider_auth._provider_auth_fingerprints.pop(provider_id, None)
    provider_auth._states.pop(provider_id, None)


def _supersede_flow(rec, fp):
    """Make `fp` no longer current so `_flow_is_current` returns False:
    both projection dicts must disagree with the in-flight fingerprint."""
    newer = (fp[0], fp[1], fp[2] + 1, fp[3], fp[4], fp[5])
    provider_auth._flow_fingerprints[rec["id"]] = newer
    provider_auth._provider_auth_fingerprints[rec["id"]] = newer


def test_monitor_login_timeout_reaps_and_refreshes():
    rec = _add_provider()
    proc = _FakeProc(returncode=None)
    fp = _seed_flow(rec, proc)
    refreshed: list = []

    async def _fake_refresh(pid, b):
        refreshed.append(pid)
    provider_auth.refresh_auth_status = _fake_refresh  # type: ignore
    provider_auth._kill_proc = lambda p: setattr(proc, "returncode", -9)  # type: ignore
    provider_auth.LOGIN_TIMEOUT_SECONDS = 0.02  # type: ignore

    async def _hang(p):
        await asyncio.sleep(5)
        return b"", b""
    proc._communicate_fn = _hang

    async def _scenario():
        await provider_auth._monitor(rec["id"], "login", proc, fp, _noop_broadcast)

    try:
        _run(_scenario())
        assert rec["id"] in refreshed
        assert provider_auth._states[rec["id"]]["status"] == provider_auth.STATE_LOGIN_FAILED
    finally:
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._flow_fingerprints.pop(rec["id"], None)
        provider_auth._provider_auth_fingerprints.pop(rec["id"], None)
        provider_auth._states.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


def test_monitor_login_exception_sets_failed_state():
    rec = _add_provider()
    proc = _FakeProc(returncode=None)
    fp = _seed_flow(rec, proc)
    fired: list = []

    async def _broadcast():
        fired.append(1)

    async def _crash(p):
        raise RuntimeError("transport broke")
    proc._communicate_fn = _crash
    provider_auth._kill_proc = lambda p: None  # type: ignore

    async def _scenario():
        await provider_auth._monitor(rec["id"], "login", proc, fp, _broadcast)

    try:
        _run(_scenario())
        assert fired, "exception path should broadcast"
        assert provider_auth._states[rec["id"]]["status"] == provider_auth.STATE_LOGIN_FAILED
    finally:
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._flow_fingerprints.pop(rec["id"], None)
        provider_auth._provider_auth_fingerprints.pop(rec["id"], None)
        provider_auth._states.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


def test_monitor_logout_success_and_failure():
    rec = _add_provider()

    proc_ok = _FakeProc(returncode=0, stderr=b"")
    fp_ok = _seed_flow(rec, proc_ok)
    invalidated: list = []
    orig_invalidate = provider_auth._invalidate_auth
    provider_auth._invalidate_auth = lambda pid: invalidated.append(pid)  # type: ignore
    refreshed: list = []

    async def _fake_refresh(pid, b):
        refreshed.append(pid)
    provider_auth.refresh_auth_status = _fake_refresh  # type: ignore

    async def _scenario_ok():
        await provider_auth._monitor(rec["id"], "logout", proc_ok, fp_ok, _noop_broadcast)

    try:
        _run(_scenario_ok())
        assert provider_auth._states[rec["id"]]["status"] == provider_auth.STATE_LOGGED_OUT
        assert rec["id"] in invalidated
        assert rec["id"] in refreshed

        proc_bad = _FakeProc(returncode=2, stderr=b"already logged out")
        provider_auth._procs[rec["id"]] = proc_bad
        fp_bad = _seed_flow(rec, proc_bad)

        async def _scenario_bad():
            await provider_auth._monitor(rec["id"], "logout", proc_bad, fp_bad, _noop_broadcast)

        _run(_scenario_bad())
        st = provider_auth._states[rec["id"]]
        assert st["status"] == provider_auth.STATE_LOGOUT_FAILED
        assert st["message"] == "already logged out"
    finally:
        provider_auth._invalidate_auth = orig_invalidate  # type: ignore
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._flow_fingerprints.pop(rec["id"], None)
        provider_auth._provider_auth_fingerprints.pop(rec["id"], None)
        provider_auth._states.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


def test_monitor_login_success_via_status_check():
    rec = _add_provider()
    proc = _FakeProc(returncode=0, stderr=b"")
    fp = _seed_flow(rec, proc)

    async def _authenticated(provider, expected_fingerprint=None):
        return True
    provider_auth._status_authenticated = _authenticated  # type: ignore
    fired: list = []

    async def _broadcast():
        fired.append(1)

    async def _scenario():
        await provider_auth._monitor(rec["id"], "login", proc, fp, _broadcast)

    try:
        _run(_scenario())
        assert provider_auth._states[rec["id"]]["status"] == provider_auth.STATE_LOGIN_SUCCESS
        assert fired
    finally:
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._flow_fingerprints.pop(rec["id"], None)
        provider_auth._provider_auth_fingerprints.pop(rec["id"], None)
        provider_auth._states.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


def test_monitor_login_failure_when_status_check_false():
    rec = _add_provider()
    proc = _FakeProc(returncode=0, stderr=b"oauth incomplete")
    fp = _seed_flow(rec, proc)

    async def _not_authenticated(provider, expected_fingerprint=None):
        return False
    provider_auth._status_authenticated = _not_authenticated  # type: ignore

    async def _scenario():
        await provider_auth._monitor(rec["id"], "login", proc, fp, _noop_broadcast)

    try:
        _run(_scenario())
        st = provider_auth._states[rec["id"]]
        assert st["status"] == provider_auth.STATE_LOGIN_FAILED
        assert st["message"] == "oauth incomplete"
    finally:
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._flow_fingerprints.pop(rec["id"], None)
        provider_auth._provider_auth_fingerprints.pop(rec["id"], None)
        provider_auth._states.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


def test_monitor_skips_state_update_when_flow_superseded():
    rec = _add_provider()
    proc = _FakeProc(returncode=0)
    fp = _seed_flow(rec, proc)
    newer = (fp[0], fp[1], fp[2] + 1, fp[3], fp[4], fp[5])
    provider_auth._flow_fingerprints[rec["id"]] = newer
    provider_auth._provider_auth_fingerprints[rec["id"]] = newer

    async def _scenario():
        await provider_auth._monitor(rec["id"], "login", proc, fp, _noop_broadcast)

    try:
        _run(_scenario())
        assert provider_auth._states.get(rec["id"], {}).get("status") != (
            provider_auth.STATE_LOGIN_SUCCESS
        )
    finally:
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._flow_fingerprints.pop(rec["id"], None)
        provider_auth._provider_auth_fingerprints.pop(rec["id"], None)
        provider_auth._states.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


def test_monitor_login_nonzero_exit_skips_status_probe_and_aborts():
    """A login subprocess that exits non-zero must not be probed for auth
    status (no exit_code==0 branch) and must not be committed: with
    status_fingerprint still None, the guard at the commit check
    short-circuits and the monitor returns without touching state, so a
    crashed login can never be misclassified as success."""
    rec = _add_provider()
    proc = _FakeProc(returncode=1, stderr=b"oauth denied")
    fp = _seed_flow(rec, proc)
    probed: list = []

    async def _must_not_probe(provider, expected_fingerprint=None):
        probed.append(provider)
        return True
    provider_auth._status_authenticated = _must_not_probe  # type: ignore

    async def _scenario():
        await provider_auth._monitor(rec["id"], "login", proc, fp, _noop_broadcast)

    try:
        _run(_scenario())
        assert not probed, "non-zero exit must skip the status probe"
        assert provider_auth._states.get(rec["id"], {}).get("status") != (
            provider_auth.STATE_LOGIN_SUCCESS
        )
    finally:
        _clear_flow(rec["id"])
        _reset_status_globals()
        _restore_all()


def test_monitor_login_fingerprint_drift_skips_status_probe():
    """If the provider record changed under the login (the config snapshot
    read after exit 0 no longer matches the in-flight fingerprint), the
    authoritative status probe is skipped — there is nothing authoritative
    to confirm against the stale flow."""
    rec = _add_provider()
    proc = _FakeProc(returncode=0, stderr=b"")
    fp = _seed_flow(rec, proc)
    probed: list = []

    async def _must_not_probe(provider, expected_fingerprint=None):
        probed.append(provider)
        return True
    provider_auth._status_authenticated = _must_not_probe  # type: ignore

    drifted = dict(rec)
    drifted["revision"] = rec["revision"] + 1
    orig_get = config_store.get_provider
    config_store.get_provider = lambda pid: drifted  # type: ignore

    async def _scenario():
        await provider_auth._monitor(rec["id"], "login", proc, fp, _noop_broadcast)

    try:
        _run(_scenario())
        assert not probed, "fingerprint drift must skip the status probe"
        assert provider_auth._states.get(rec["id"], {}).get("status") != (
            provider_auth.STATE_LOGIN_SUCCESS
        )
    finally:
        config_store.get_provider = orig_get  # type: ignore
        _clear_flow(rec["id"])
        _reset_status_globals()
        _restore_all()


def test_monitor_login_status_probe_raises_is_logged_as_failed():
    """A crashing status probe is swallowed, logged, and resolves to a
    failed login rather than wedging the record busy."""
    rec = _add_provider()
    proc = _FakeProc(returncode=0, stderr=b"")
    fp = _seed_flow(rec, proc)

    async def _crash(provider, expected_fingerprint=None):
        raise RuntimeError("probe transport broke")
    provider_auth._status_authenticated = _crash  # type: ignore

    async def _scenario():
        await provider_auth._monitor(rec["id"], "login", proc, fp, _noop_broadcast)

    try:
        _run(_scenario())
        assert provider_auth._states[rec["id"]]["status"] == (
            provider_auth.STATE_LOGIN_FAILED
        )
    finally:
        _clear_flow(rec["id"])
        _reset_status_globals()
        _restore_all()


def test_monitor_timeout_with_stale_flow_skips_state_and_refresh():
    """A timeout on a flow that was already superseded must still reap the
    proc (via finally) but must neither write a failed state nor trigger a
    refresh — the superseding flow owns the projection now."""
    rec = _add_provider()
    proc = _FakeProc(returncode=None)
    fp = _seed_flow(rec, proc)
    _supersede_flow(rec, fp)
    refreshed: list = []

    async def _fake_refresh(pid, b):
        refreshed.append(pid)
    provider_auth.refresh_auth_status = _fake_refresh  # type: ignore
    provider_auth._kill_proc = lambda p: setattr(proc, "returncode", -9)  # type: ignore
    provider_auth.LOGIN_TIMEOUT_SECONDS = 0.02  # type: ignore

    async def _hang(p):
        await asyncio.sleep(5)
        return b"", b""
    proc._communicate_fn = _hang

    async def _scenario():
        await provider_auth._monitor(rec["id"], "login", proc, fp, _noop_broadcast)

    try:
        _run(_scenario())
        assert not refreshed, "superseded timeout must not refresh"
        assert provider_auth._states.get(rec["id"], {}).get("status") != (
            provider_auth.STATE_LOGIN_FAILED
        )
    finally:
        _clear_flow(rec["id"])
        _reset_status_globals()
        _restore_all()


def test_monitor_exception_with_stale_flow_skips_state_and_broadcast():
    """A transport crash on a superseded flow must not broadcast a failed
    state — only the still-current flow may mutate the projection."""
    rec = _add_provider()
    proc = _FakeProc(returncode=None)
    fp = _seed_flow(rec, proc)
    _supersede_flow(rec, fp)
    fired: list = []

    async def _broadcast():
        fired.append(1)

    async def _crash(p):
        raise RuntimeError("transport broke")
    proc._communicate_fn = _crash
    provider_auth._kill_proc = lambda p: None  # type: ignore

    async def _scenario():
        await provider_auth._monitor(rec["id"], "login", proc, fp, _broadcast)

    try:
        _run(_scenario())
        assert not fired, "superseded exception must not broadcast"
        assert provider_auth._states.get(rec["id"], {}).get("status") != (
            provider_auth.STATE_LOGIN_FAILED
        )
    finally:
        _clear_flow(rec["id"])
        _reset_status_globals()
        _restore_all()


# ============================================================ _start branches


def test_start_returns_not_found_for_missing_provider():

    async def _scenario():
        return await provider_auth._start("nope", "login",
                                          provider_auth.STATE_LOGIN_RUNNING,
                                          _noop_broadcast)

    try:
        assert _run(_scenario()) == {"ok": False, "error": "not_found"}
    finally:
        _reset_status_globals()


def test_start_returns_unsupported_for_apikey():
    apikey = _add_provider(mode="api_key")

    async def _scenario():
        return await provider_auth._start(apikey["id"], "login",
                                          provider_auth.STATE_LOGIN_RUNNING,
                                          _noop_broadcast)

    try:
        assert _run(_scenario()) == {"ok": False, "error": "unsupported"}
    finally:
        _reset_status_globals()


def test_start_binary_missing_when_spawn_returns_none():
    rec = _add_provider()
    _install_spawn(lambda: None)
    fired: list = []

    async def _broadcast():
        fired.append(1)

    async def _scenario():
        return await provider_auth._start(rec["id"], "login",
                                          provider_auth.STATE_LOGIN_RUNNING,
                                          _broadcast)

    try:
        result = _run(_scenario())
        assert result == {"ok": False, "error": "binary_missing"}
        assert fired
        assert provider_auth._states[rec["id"]]["status"] == provider_auth.STATE_LOGIN_FAILED
    finally:
        provider_auth._states.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


def test_start_filenotfound_surfaces_binary_missing():
    rec = _add_provider()

    async def _raise(provider, action):
        raise FileNotFoundError("no cli")
    provider_auth._spawn = _raise  # type: ignore

    async def _scenario():
        return await provider_auth._start(rec["id"], "login",
                                          provider_auth.STATE_LOGIN_RUNNING,
                                          _noop_broadcast)

    try:
        assert _run(_scenario()) == {"ok": False, "error": "binary_missing"}
    finally:
        provider_auth._states.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


def test_start_spawn_exception_surfaces_spawn_failed():
    rec = _add_provider()

    async def _raise(provider, action):
        raise RuntimeError("spawn broke")
    provider_auth._spawn = _raise  # type: ignore

    async def _scenario():
        return await provider_auth._start(rec["id"], "login",
                                          provider_auth.STATE_LOGIN_RUNNING,
                                          _noop_broadcast)

    try:
        assert _run(_scenario()) == {"ok": False, "error": "spawn_failed"}
        assert provider_auth._states[rec["id"]]["status"] == provider_auth.STATE_LOGIN_FAILED
    finally:
        provider_auth._states.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


def test_start_obsolete_when_registration_predicate_fails():
    """apply_if_provider_matches returns False when the record changed
    between spawn and registration -> proc killed, 'obsolete' returned."""
    rec = _add_provider()
    proc = _FakeProc(returncode=0)
    _install_spawn(lambda: proc)
    killed: list = []
    provider_auth._kill_proc = lambda p: killed.append(p)  # type: ignore

    def _failing(pid, predicate, apply):
        return False
    config_store.apply_if_provider_matches = _failing  # type: ignore

    async def _scenario():
        return await provider_auth._start(rec["id"], "login",
                                          provider_auth.STATE_LOGIN_RUNNING,
                                          _noop_broadcast)

    try:
        assert _run(_scenario()) == {"ok": False, "error": "obsolete"}
        assert proc in killed
    finally:
        provider_auth._states.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()


# ============================================================ cancel


def _drive_start_cancel_during_registration(*, committed: bool):
    """Spawn _start, cancel it while it awaits shield(registration), and
    return (proc, killed, discarded). `committed` selects the branch: when
    True the registration commits the flow (still_current True -> discard);
    when False it reports the record obsolete (still_current False -> kill)."""
    rec = _add_provider()
    proc = _FakeProc(returncode=None)
    _install_spawn(lambda: proc)
    reached = threading.Event()
    release = threading.Event()

    def _blocking_apply(pid, predicate, apply):
        if committed:
            apply()  # register() commits the flow
        reached.set()
        release.wait(2.0)
        return committed

    config_store.apply_if_provider_matches = _blocking_apply  # type: ignore
    killed: list = []
    provider_auth._kill_proc = lambda p: killed.append(p)  # type: ignore
    discarded: list = []

    async def _discard_registered_flow(pid, fingerprint, p):
        discarded.append(p)

    provider_auth._discard_registered_flow = _discard_registered_flow  # type: ignore

    async def _scenario():
        task = asyncio.ensure_future(provider_auth._start(
            rec["id"], "login", provider_auth.STATE_LOGIN_RUNNING, _noop_broadcast))
        # Let _start progress to `await shield(registration)`.
        for _ in range(400):
            if reached.is_set():
                break
            await asyncio.sleep(0.005)
        assert reached.is_set(), "registration never reached the block point"
        task.cancel()
        release.set()  # registration completes -> resolves still_current
        raised = False
        try:
            await task
        except asyncio.CancelledError:
            raised = True
        assert raised

    try:
        _run(_scenario())
    finally:
        provider_auth._states.pop(rec["id"], None)
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._flow_fingerprints.pop(rec["id"], None)
        _reset_status_globals()
        _restore_all()
    return proc, killed, discarded


def test_start_cancel_during_registration_discards_committed_flow():
    """Cancelling _start while it awaits shield(registration), where the
    registration DID commit (still_current True), discards the registered
    flow. Covers the asyncio.CancelledError branch (lines 1063-1066)."""
    proc, killed, discarded = _drive_start_cancel_during_registration(committed=True)
    assert discarded == [proc]
    # A committed flow is torn down via _discard_registered_flow, not a bare kill.
    assert killed == []


def test_start_cancel_during_registration_kills_uncommitted_proc():
    """Cancelling _start while it awaits shield(registration), where the
    registration did NOT commit (still_current False), kills+reaps the proc
    without discarding a registered flow. Covers lines 1067-1069."""
    proc, killed, discarded = _drive_start_cancel_during_registration(committed=False)
    assert killed == [proc]
    assert discarded == []


def test_cancel_returns_false_when_no_proc():
    rec = _add_provider()

    async def _scenario():
        return await provider_auth.cancel(rec["id"])

    try:
        assert _run(_scenario()) is False
    finally:
        _reset_status_globals()


def test_cancel_returns_false_on_reap_timeout():
    rec = _add_provider()
    proc = _FakeProc(returncode=None)
    provider_auth._procs[rec["id"]] = proc
    provider_auth._CANCEL_CONFIRM_SECONDS = 0.01  # type: ignore
    provider_auth._kill_proc = lambda p: None  # type: ignore

    async def _hang(p):
        await asyncio.sleep(5)
        return 0
    proc._wait_fn = _hang

    async def _scenario():
        return await provider_auth.cancel(rec["id"])

    try:
        assert _run(_scenario()) is False
    finally:
        provider_auth._procs.pop(rec["id"], None)
        _restore_all()


# ============================================================ _build_env fail-closed


def test_build_env_fails_closed_on_transport_misconfig():
    rec = _add_provider()

    def _raise(*a, **kw):
        raise provider_transport.ProviderTransportError("bad proxy config")
    provider_transport.apply_provider_transport = _raise  # type: ignore
    try:
        try:
            provider_auth._build_env(rec)
            assert False, "expected ProviderTransportError (fail closed)"
        except provider_transport.ProviderTransportError:
            pass
    finally:
        _restore_all()


# ============================================================ self-runner

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"  {name} ...", end=" ")
            fn()
            print("ok")
    print("all provider_auth status-probe tests passed")

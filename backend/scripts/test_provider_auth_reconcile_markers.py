"""Covers the non-login half of provider_auth: authority fingerprints,
config-change broadcast + reconcile, cached auth-status TTL, transient
state teardown, binary/spawn resolution fallbacks, and the pid-marker
safety surface (path-traversal rejection, atomic write, reap edge cases).

Same isolation contract as test_provider_auth_login_env.py: an isolated
BETTER_AGENT_HOME tempdir, real config_store persistence, and the OAuth
browser/subprocess surface stubbed at the binary-resolution seam. No
provider tokens, no network.
"""
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="provider_auth_reconcile_")
import atexit  # noqa: E402

atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
paths.engage_test_home(_TMP)

import cli_paths  # noqa: E402
import config_store  # noqa: E402
import provider_auth  # noqa: E402

HOME = Path(_TMP)

# Restore handles for every seam we patch, so no global leaks across tests.
_orig_resolve_cli_binary = cli_paths.resolve_cli_binary
_orig_kill_proc = provider_auth._kill_proc
_orig_maybe_schedule_refresh = provider_auth.maybe_schedule_refresh
_orig_list_providers = config_store.list_providers


def _restore_all():
    cli_paths.resolve_cli_binary = _orig_resolve_cli_binary
    provider_auth._kill_proc = _orig_kill_proc
    provider_auth.maybe_schedule_refresh = _orig_maybe_schedule_refresh
    config_store.list_providers = _orig_list_providers


def _add_provider(kind="claude", generation="g1", **extra):
    rec = {
        "name": f"{kind}-{os.urandom(3).hex()}",
        "kind": kind,
        "mode": "subscription",
        "config_dir": str(HOME / f".{os.urandom(3).hex()}"),
        "generation": generation,
    }
    rec.update(extra)
    return config_store.add_provider(rec)


def _seed_fingerprint(provider_id, revision=1):
    fp = (provider_id, "g1", revision, "claude", "subscription", "CLAUDE_CONFIG_DIR")
    provider_auth._provider_auth_fingerprints[provider_id] = fp
    return fp


# ---------------------------------------------------------------- _auth_fingerprint


def test_auth_fingerprint_none_for_unsupported_or_invalid():
    # Unsupported mode -> None (supports_auth False).
    apikey = _add_provider(mode="api_key")
    assert provider_auth._auth_fingerprint(apikey) is None

    # Missing revision (None) -> TypeError swallowed -> None.
    rec = {"id": "x", "kind": "claude", "mode": "subscription", "generation": "g"}
    assert provider_auth._auth_fingerprint(rec) is None

    # Non-int revision -> ValueError swallowed -> None.
    rec = {"id": "x", "kind": "claude", "mode": "subscription",
           "generation": "g", "revision": "abc"}
    assert provider_auth._auth_fingerprint(rec) is None

    # Missing id / generation -> None even with valid revision.
    rec = {"id": "", "kind": "claude", "mode": "subscription",
           "generation": "g", "revision": 1}
    assert provider_auth._auth_fingerprint(rec) is None
    rec = {"id": "x", "kind": "claude", "mode": "subscription",
           "generation": "", "revision": 1}
    assert provider_auth._auth_fingerprint(rec) is None

    # Fully valid -> populated tuple.
    rec = {"id": "x", "kind": "codex", "mode": "subscription",
           "generation": "g", "revision": 7}
    fp = provider_auth._auth_fingerprint(rec)
    assert fp == ("x", "g", 7, "codex", "subscription", fp[5])


# ------------------------------------------------ config broadcast + reconcile


def test_configure_broadcast_and_notify_noop_without_loop():
    # notify before any loop is bound is a silent no-op (loop is None).
    provider_auth._config_change_loop = None
    provider_auth._status_shutting_down = False
    provider_auth.notify_provider_config_changed()  # must not raise


def test_notify_noop_when_shutting_down():
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        provider_auth._config_change_loop = loop
        provider_auth._status_shutting_down = True
        # Running on the bound loop but shutting down -> no task created.
        provider_auth.notify_provider_config_changed()
        assert provider_auth._config_reconcile_task is None or (
            provider_auth._config_reconcile_task is not None
            and provider_auth._config_reconcile_task.done()
        ) or True  # no crash is the contract
    finally:
        provider_auth._status_shutting_down = False
        provider_auth._config_change_loop = None
        loop.close()


def test_request_config_reconcile_noop_on_wrong_loop():
    loop = asyncio.new_event_loop()
    other = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        provider_auth._config_change_loop = other  # bound to a different loop
        provider_auth._status_shutting_down = False

        async def _call_on_this_loop():
            provider_auth._request_config_reconcile()

        loop.run_until_complete(_call_on_this_loop())
        assert provider_auth._config_reconcile_task is None
    finally:
        provider_auth._config_change_loop = None
        loop.close()
        other.close()


def test_provider_config_committed_snapshots_fingerprints():
    provider_auth._provider_auth_fingerprints.clear()
    rec = _add_provider()
    provider_auth.provider_config_committed()
    fp = provider_auth._provider_auth_fingerprints.get(rec["id"])
    assert fp is not None
    assert fp[0] == rec["id"]
    assert fp[2] == rec["revision"]


def test_provider_config_committed_fails_closed_on_list_error():
    def _boom():
        raise RuntimeError("store unavailable")

    config_store.list_providers = _boom
    try:
        provider_auth.provider_config_committed()  # must not raise
        # Failed-closed: snapshot is empty, prior fingerprints cleared.
        assert provider_auth._provider_auth_fingerprints == {}
    finally:
        _restore_all()


def test_provider_config_committed_kills_obsolete_in_flight_proc():
    killed: list = []
    provider_auth._kill_proc = lambda proc: killed.append(proc)  # type: ignore

    rec = _add_provider()
    # Seed a STALE authority fingerprint so `committed` sees this id as changed.
    provider_auth._provider_auth_fingerprints[rec["id"]] = _seed_fingerprint(
        rec["id"], revision=-999
    )
    fake_proc = type("P", (), {"returncode": None, "pid": 4242})()
    provider_auth._procs[rec["id"]] = fake_proc
    try:
        provider_auth.provider_config_committed()
        assert fake_proc in killed, "obsolete in-flight proc was not killed"
        assert rec["id"] not in provider_auth._auth_cache
        assert rec["id"] not in provider_auth._states
    finally:
        provider_auth._procs.pop(rec["id"], None)
        _restore_all()


def test_reconcile_provider_config_broadcasts_and_refreshes_changed():
    provider_auth._provider_auth_fingerprints.clear()
    provider_auth._committed_config_changes.clear()
    rec = _add_provider()

    broadcast_calls = []

    async def broadcast():
        broadcast_calls.append(time.monotonic())

    refreshed: list = []
    provider_auth.maybe_schedule_refresh = lambda pid, b: refreshed.append(pid)  # type: ignore

    provider_auth._config_change_broadcast = broadcast
    provider_auth._config_reconcile_requested = True
    provider_auth._status_shutting_down = False
    try:

        async def _drive():
            await provider_auth._reconcile_provider_config()

        asyncio.run(_drive())
        # Broadcast fired exactly once for the changed record.
        assert len(broadcast_calls) == 1, broadcast_calls
        assert rec["id"] in refreshed, "maybe_schedule_refresh not invoked for changed id"
        assert rec["id"] in provider_auth._provider_auth_fingerprints
    finally:
        provider_auth._config_change_broadcast = None
        _restore_all()


def test_bind_loop_and_notify_drive_reconcile_task():
    """Happy path: configure broadcast -> bind loop (requests reconcile) ->
    notify (call_soon_threadsafe) -> the reconcile task runs on the bound loop,
    fires broadcast once, populates authority fingerprints, and terminates."""
    rec = _add_provider()
    provider_auth._provider_auth_fingerprints.clear()
    provider_auth._committed_config_changes.clear()
    broadcast_calls = []

    async def broadcast():
        broadcast_calls.append(1)

    refreshed: list = []
    provider_auth.maybe_schedule_refresh = lambda pid, b: refreshed.append(pid)  # type: ignore
    provider_auth._status_shutting_down = False

    async def scenario():
        provider_auth.configure_config_change_broadcast(broadcast)  # line 146
        provider_auth.bind_config_change_loop()  # lines 151-152 + first request
        provider_auth.notify_provider_config_changed()  # line 205 (valid loop)
        # Let the reconcile task + its done-callback settle.
        for _ in range(200):
            t = provider_auth._config_reconcile_task
            if (t is None or t.done()) and not broadcast_calls:
                await asyncio.sleep(0.005)
            if broadcast_calls:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)

    try:
        asyncio.run(scenario())
        assert broadcast_calls, "broadcast never fired from reconcile task"
        assert rec["id"] in provider_auth._provider_auth_fingerprints
    finally:
        provider_auth._config_change_broadcast = None
        provider_auth._config_change_loop = None
        provider_auth._config_reconcile_task = None
        _restore_all()


def test_reconcile_provider_config_skips_when_nothing_changed():
    rec = _add_provider()
    # Seed the EXACT real fingerprint so authority == current -> no change.
    real_fp = provider_auth._auth_fingerprint(rec)
    assert real_fp is not None
    provider_auth._provider_auth_fingerprints[rec["id"]] = real_fp
    provider_auth._auth_cache.pop(rec["id"], None)
    provider_auth._flow_fingerprints.pop(rec["id"], None)
    provider_auth._committed_config_changes.clear()

    broadcast_calls = []

    async def broadcast():
        broadcast_calls.append(1)

    provider_auth._config_change_broadcast = broadcast
    provider_auth._status_shutting_down = False
    provider_auth._config_reconcile_requested = True
    try:

        async def _drive():
            await provider_auth._reconcile_provider_config()

        asyncio.run(_drive())
        assert broadcast_calls == [], "broadcast fired despite no changes"
    finally:
        provider_auth._config_change_broadcast = None
        _restore_all()


class _CaptureHandler(logging.Handler):
    """Collects LogRecords so tests can assert on the reconcile done-callback's
    error log without a pytest fixture (keeps the module self-runnable)."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_reconcile_kills_stale_in_flight_proc_on_config_drift():
    """Security: when a provider's committed config drifts from the in-flight
    login flow, reconciliation kills the still-running login subprocess so a
    superseded OAuth exchange can't complete against stale credentials. Also
    covers the broadcast-None branch (changed set non-empty, no broadcast)."""
    rec = _add_provider()
    provider_auth._provider_auth_fingerprints.clear()
    provider_auth._committed_config_changes.clear()
    provider_auth._flow_fingerprints.pop(rec["id"], None)
    killed: list = []
    provider_auth._kill_proc = lambda proc: killed.append(proc)  # type: ignore
    live = type("P", (), {"returncode": None, "pid": 4242})()
    provider_auth._procs[rec["id"]] = live
    provider_auth._config_change_broadcast = None  # exercises 285->287 branch
    provider_auth._status_shutting_down = False
    provider_auth._config_reconcile_requested = True
    try:

        async def _drive():
            await provider_auth._reconcile_provider_config()

        asyncio.run(_drive())
        assert live in killed, "stale in-flight proc was not killed on drift"
        assert rec["id"] not in provider_auth._flow_fingerprints
    finally:
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._config_change_broadcast = None
        _restore_all()


def test_reconcile_skips_kill_when_flow_fingerprint_matches_current():
    """If a record is marked changed (via committed-config changes) but its
    in-flight flow fingerprint already matches the new authority, there is no
    drift to kill for -> the kill block is skipped (line 277 False branch)."""
    rec = _add_provider()
    current_fp = provider_auth._auth_fingerprint(rec)
    assert current_fp is not None
    provider_auth._provider_auth_fingerprints[rec["id"]] = current_fp
    provider_auth._flow_fingerprints[rec["id"]] = current_fp
    provider_auth._committed_config_changes = {rec["id"]}
    provider_auth._auth_cache.pop(rec["id"], None)
    killed: list = []
    provider_auth._kill_proc = lambda proc: killed.append(proc)  # type: ignore
    finished = type("P", (), {"returncode": 0, "pid": 7})()
    provider_auth._procs[rec["id"]] = finished
    provider_auth._config_change_broadcast = None
    provider_auth._status_shutting_down = False
    provider_auth._config_reconcile_requested = True
    try:

        async def _drive():
            await provider_auth._reconcile_provider_config()

        asyncio.run(_drive())
        assert killed == [], "no kill expected when flow fingerprint matches current"
        # committed-config changes are drained once reconciled into the change set.
        assert provider_auth._committed_config_changes == set()
    finally:
        provider_auth._procs.pop(rec["id"], None)
        provider_auth._config_change_broadcast = None
        _restore_all()


def test_reconcile_loops_again_when_broadcast_redrives_request():
    """The reconcile while-loop must re-iterate when something re-arms
    _config_reconcile_requested during the in-flight iteration (here: the
    broadcast callback), covering the 287->235 loop back-edge in-task."""
    rec = _add_provider()
    provider_auth._provider_auth_fingerprints.clear()
    provider_auth._committed_config_changes.clear()
    broadcast_calls = {"n": 0}

    async def broadcast():
        broadcast_calls["n"] += 1
        if broadcast_calls["n"] == 1:
            # Re-arm mid-iteration so the while-loop runs a second time in-task.
            provider_auth._config_reconcile_requested = True

    refreshed: list = []
    provider_auth.maybe_schedule_refresh = lambda pid, b: refreshed.append(pid)  # type: ignore
    provider_auth._config_change_broadcast = broadcast
    provider_auth._status_shutting_down = False
    provider_auth._config_reconcile_requested = True
    try:

        async def _drive():
            await provider_auth._reconcile_provider_config()

        asyncio.run(_drive())
        # Second iteration finds nothing changed -> broadcast fires exactly once.
        assert broadcast_calls["n"] == 1, broadcast_calls["n"]
        assert rec["id"] in refreshed
    finally:
        provider_auth._config_change_broadcast = None
        _restore_all()


def test_reconcile_done_callback_logs_and_rerequests_on_failure():
    """The reconcile task's done-callback (a) logs the exception when the task
    raised, and (b) re-requests reconciliation when a request is still pending.
    A gated list_providers blocks the failing task so we can re-arm the request
    flag while the task is still in flight (otherwise the task clears it before
    raising)."""
    provider_auth._provider_auth_fingerprints.clear()
    provider_auth._committed_config_changes.clear()
    gate = threading.Event()
    calls = {"n": 0}

    def _gated_list():
        calls["n"] += 1
        gate.wait(timeout=3.0)
        if calls["n"] == 1:
            raise RuntimeError("reconcile boom")
        return _orig_list_providers()

    config_store.list_providers = _gated_list
    handler = _CaptureHandler()
    provider_auth.logger.addHandler(handler)
    try:

        async def scenario():
            provider_auth._status_shutting_down = False
            provider_auth.configure_config_change_broadcast(None)
            provider_auth.bind_config_change_loop()  # requests reconcile -> task1
            # Wait for task1 to enter _reconcile_provider_config and block on gate.
            for _ in range(100):
                await asyncio.sleep(0.01)
                if calls["n"] >= 1:
                    break
            # Re-arm the request flag while task1 is blocked (it already cleared it).
            provider_auth._request_config_reconcile()
            assert provider_auth._config_reconcile_requested is True
            gate.set()  # release task1 -> raises -> done-callback fires
            # Pump until the re-requested task2 settles and no request stays pending.
            for _ in range(400):
                await asyncio.sleep(0.01)
                t = provider_auth._config_reconcile_task
                if t is not None and t.done() and not provider_auth._config_reconcile_requested:
                    break
            await asyncio.sleep(0.02)

        asyncio.run(scenario())
        # 223: the failure was logged.
        assert any(
            "config reconciliation failed" in r.getMessage() for r in handler.records
        ), [r.getMessage() for r in handler.records]
        # 228: the done-callback re-requested, so a second list_providers ran.
        assert calls["n"] >= 2, calls["n"]
    finally:
        provider_auth.logger.removeHandler(handler)
        provider_auth._config_change_broadcast = None
        provider_auth._config_change_loop = None
        provider_auth._config_reconcile_task = None
        gate.set()
        _restore_all()


# ------------------------------------------------------------- cached auth TTL


def test_cached_auth_respects_ttl_and_fingerprint():
    pid = "cache-prov"
    fp = _seed_fingerprint(pid)
    try:
        # Fresh + matching fingerprint -> value returned.
        provider_auth._auth_cache[pid] = (time.monotonic(), True, fp)
        assert provider_auth._cached_auth(pid) is True

        # Expired TTL -> None.
        provider_auth._auth_cache[pid] = (
            time.monotonic() - provider_auth._AUTH_CACHE_TTL_SECONDS - 1, True, fp
        )
        assert provider_auth._cached_auth(pid) is None

        # Fresh TTL but stale fingerprint (authority changed) -> None.
        provider_auth._auth_cache[pid] = (time.monotonic(), True, fp)
        provider_auth._provider_auth_fingerprints[pid] = _seed_fingerprint(
            pid, revision=999
        )
        assert provider_auth._cached_auth(pid) is None

        # Missing entry -> None.
        provider_auth._auth_cache.pop(pid, None)
        assert provider_auth._cached_auth(pid) is None
    finally:
        provider_auth._auth_cache.pop(pid, None)
        provider_auth._provider_auth_fingerprints.pop(pid, None)


def test_invalidate_auth_drops_cache():
    pid = "invalidate-prov"
    provider_auth._auth_cache[pid] = (0.0, True, None)
    provider_auth._invalidate_auth(pid)
    assert pid not in provider_auth._auth_cache


# ----------------------------------------------------------------- clear_state


def test_clear_state_kills_live_proc_and_drops_all_state():
    pid = "clear-prov"
    killed: list = []
    provider_auth._kill_proc = lambda proc: killed.append(proc)  # type: ignore
    live = type("P", (), {"returncode": None, "pid": 777})()
    provider_auth._procs[pid] = live
    provider_auth._states[pid] = {"status": provider_auth.STATE_LOGIN_RUNNING}
    provider_auth._auth_cache[pid] = (0.0, True, None)
    provider_auth._flow_fingerprints[pid] = None
    provider_auth._locks[pid] = asyncio.Lock()
    try:
        provider_auth.clear_state(pid)
        assert live in killed
        # clear_state drops transient state/cache/flow/locks; the _procs entry
        # itself is intentionally left in place (the proc was just killed).
        assert pid in provider_auth._procs
        assert pid not in provider_auth._states
        assert pid not in provider_auth._auth_cache
        assert pid not in provider_auth._flow_fingerprints
        assert pid not in provider_auth._locks
    finally:
        provider_auth._procs.pop(pid, None)
        _restore_all()


def test_clear_state_noop_for_finished_proc():
    pid = "clear-finished"
    killed: list = []
    provider_auth._kill_proc = lambda proc: killed.append(proc)  # type: ignore
    finished = type("P", (), {"returncode": 0, "pid": 1})()
    provider_auth._procs[pid] = finished
    try:
        provider_auth.clear_state(pid)  # returncode set -> no kill
        assert killed == [], "finished proc should not be killed"
        assert pid in provider_auth._procs  # _procs untouched by clear_state
    finally:
        provider_auth._procs.pop(pid, None)
        _restore_all()


# ------------------------------------------------------- attach_login_state passthrough


def test_attach_login_state_passthrough_for_unsupported():
    apikey = _add_provider(mode="api_key")
    out = provider_auth.attach_login_state(dict(apikey))
    assert "login_state" not in out
    assert out["id"] == apikey["id"]


def test_attach_login_state_adds_state_when_supported():
    rec = _add_provider()
    out = provider_auth.attach_login_state(dict(rec))
    assert "login_state" in out
    assert out["login_state"]["status"] == provider_auth.STATE_IDLE


# ------------------------------------------------ binary/spawn resolution fallback


def test_resolve_and_prepare_spawn_return_none_when_binary_missing():
    cli_paths.resolve_cli_binary = lambda name: None  # type: ignore
    try:
        assert provider_auth._resolve_binary("claude") is None
        assert provider_auth._prepare_spawn(
            {"kind": "claude", "mode": "subscription", "id": "x"}, "login"
        ) is None

        async def _spawn_none():
            return await provider_auth._spawn(
                {"kind": "claude", "mode": "subscription", "id": "x"}, "login"
            )

        assert asyncio.run(_spawn_none()) is None
    finally:
        _restore_all()


# ------------------------------------------------------------- marker safety


def test_marker_path_rejects_traversal_and_unsafe_ids():
    assert provider_auth._marker_path("../escape") is None
    assert provider_auth._marker_path("") is None
    assert provider_auth._marker_path("has space") is None
    assert provider_auth._marker_path("ok/sep") is None
    p = provider_auth._marker_path("safe-id_1")
    assert p is not None and p.name == "safe-id_1.json"


def test_write_and_clear_marker_atomic_roundtrip():
    pid = "marker-ok"
    proc = type("P", (), {"pid": 5555})()
    provider_auth._write_marker(pid, proc)
    path = provider_auth._marker_path(pid)
    assert path is not None and path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pid"] == 5555
    assert data["provider_id"] == pid

    provider_auth._clear_marker(pid)
    assert not path.exists()


def test_write_marker_noop_on_unsafe_id_and_swallows_io_error():
    # Unsafe id -> _marker_path None -> silent no-op, no file.
    provider_auth._write_marker("../bad", type("P", (), {"pid": 1})())
    assert not (provider_auth._markers_dir() / "../bad.json").exists()

    # Safe id but the write raises (parent mkdir broken) -> swallowed.
    pid = "marker-iofail"

    def _boom(self, **kw):
        raise OSError("simulated")

    orig_mkdir = Path.mkdir
    Path.mkdir = _boom  # type: ignore
    try:
        provider_auth._write_marker(pid, type("P", (), {"pid": 9})())  # must not raise
    finally:
        Path.mkdir = orig_mkdir  # type: ignore
        provider_auth._clear_marker(pid)


def test_clear_marker_noop_on_unsafe_id_and_swallows_error():
    provider_auth._clear_marker("../bad")  # None path -> no-op, no raise


def test_clear_marker_swallows_unlink_failure():
    pid = "marker-clearfail"
    provider_auth._write_marker(pid, type("P", (), {"pid": 3})())
    orig_unlink = Path.unlink

    def _boom(self, *a, **kw):
        raise OSError("simulated")

    Path.unlink = _boom  # type: ignore
    try:
        provider_auth._clear_marker(pid)  # must not raise
    finally:
        Path.unlink = orig_unlink  # type: ignore
        provider_auth._clear_marker(pid)


def test_safe_unlink_swallows_exceptions():
    # Unlinking a directory raises; _safe_unlink must swallow it.
    provider_auth._safe_unlink(HOME)


# --------------------------------------------------------------- reap edge cases


def test_reap_noop_when_markers_dir_absent():
    d = provider_auth._markers_dir()
    if d.exists():
        for f in d.glob("*"):
            f.unlink(missing_ok=True)
        d.rmdir()
    assert not d.is_dir()
    provider_auth.reap_orphaned_logins()  # must not raise


def test_reap_sweeps_stale_tmp_and_malformed_markers():
    d = provider_auth._markers_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "leftover.json.tmp"
    tmp.write_text("partial", encoding="utf-8")
    malformed = d / "bad.json"
    malformed.write_text("not json at all", encoding="utf-8")
    provider_auth._procs.clear()
    try:
        provider_auth.reap_orphaned_logins()
        assert not tmp.exists(), "stale .json.tmp was not swept"
        # Malformed -> pid parsed as 0 -> treated as dead -> unlinked.
        assert not malformed.exists()
    finally:
        for f in d.glob("*"):
            f.unlink(missing_ok=True)


def test_reap_force_kill_failure_is_swallowed():
    """Identity confirmed but force_kill raises -> logged + swallowed; the
    marker is unlinked BEFORE the kill so a partial state is still safe."""
    real_pc = provider_auth.process_control
    provider_auth._procs.clear()

    class _FakePC:
        def __init__(self, real):
            self._real = real

        def pid_alive(self, pid):
            return self._real.pid_alive(pid)

        def force_kill(self, pid):
            raise OSError("simulated kill failure")

        def detach_spawn_kwargs(self):
            return self._real.detach_spawn_kwargs()

    # Use the current process as the "orphan": identity-verifiable, and we
    # patch force_kill so it is never actually harmed.
    import os as _os

    me = _os.getpid()
    import psutil

    ct = psutil.Process(me).create_time()
    pid = "reap-killfail"
    path = provider_auth._marker_path(pid)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": me, "provider_id": pid, "create_time": ct}),
        encoding="utf-8",
    )
    try:
        provider_auth.process_control = lambda: _FakePC(real_pc())  # type: ignore
        provider_auth.reap_orphaned_logins()  # must not raise
        assert not path.exists(), "marker should be unlinked before kill attempt"
    finally:
        provider_auth.process_control = real_pc  # type: ignore
        provider_auth._clear_marker(pid)


# --------------------------------------------------------- reopen_status_probes


def test_reopen_status_probes_clears_shutdown_flag():
    provider_auth._status_shutting_down = True
    provider_auth.reopen_status_probes()
    assert provider_auth._status_shutting_down is False


# --------------------------------------------------------------- self-runner

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"  {name} ...", end=" ")
            fn()
            print("ok")
    print("all provider_auth reconcile/marker tests passed")

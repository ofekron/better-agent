from __future__ import annotations

import signal
import threading

import pytest

import node_source_launcher


class FakeSupervisor:
    def __init__(
        self,
        *,
        exits: list[int],
        requested_restarts: list[bool],
        restart_results: list[bool],
        recovery_results: list[bool],
    ) -> None:
        self.exits = exits
        self.requested_restarts = requested_restarts
        self.restart_results = restart_results
        self.recovery_results = recovery_results
        self.calls: list[object] = []

    def start(self) -> None:
        self.calls.append("start")

    def wait_healthy(self, timeout: float | None = 30.0) -> bool:
        self.calls.append(("wait_healthy", timeout))
        return True

    def wait_exit(self) -> int:
        self.calls.append("wait_exit")
        return self.exits.pop(0)

    def restart_was_requested(self) -> bool:
        self.calls.append("restart_was_requested")
        return self.requested_restarts.pop(0)

    def record_requested_restart(self, exit_code: int) -> None:
        self.calls.append(("record_requested_restart", exit_code))

    def restart(self) -> bool:
        self.calls.append("restart")
        return self.restart_results.pop(0)

    def recover_unexpected_exit(self, exit_code: int) -> bool:
        self.calls.append(("recover_unexpected_exit", exit_code))
        return self.recovery_results.pop(0)

    def shutdown(self, *, kill_runners: bool) -> None:
        self.calls.append(("shutdown", kill_runners))


def test_default_launcher_injects_node_credential_authority(monkeypatch) -> None:
    store = object()
    broker = object()
    supervisor = FakeSupervisor(
        exits=[0],
        requested_restarts=[False],
        restart_results=[],
        recovery_results=[False],
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        node_source_launcher,
        "node_provider_credential_store",
        lambda: store,
    )

    def credential_broker(requested_store):
        observed["store"] = requested_store
        return broker

    def backend_supervisor(*, role, port, credential_broker):
        observed.update({
            "role": role,
            "port": port,
            "broker": credential_broker,
        })
        return supervisor

    monkeypatch.setattr(
        node_source_launcher,
        "ProviderCredentialBroker",
        credential_broker,
    )
    monkeypatch.setattr(
        node_source_launcher,
        "BackendSupervisor",
        backend_supervisor,
    )

    result = node_source_launcher.run(
        8002,
        stopping=threading.Event(),
        install_signal_handlers=False,
    )

    assert result == 1
    assert observed == {
        "store": store,
        "role": "node",
        "port": 8002,
        "broker": broker,
    }
    assert ("wait_healthy", None) in supervisor.calls


def test_requested_restart_uses_fresh_supervised_generation() -> None:
    supervisor = FakeSupervisor(
        exits=[0, 7],
        requested_restarts=[True, False],
        restart_results=[True],
        recovery_results=[False],
    )

    result = node_source_launcher.run(
        8002,
        supervisor=supervisor,
        stopping=threading.Event(),
        install_signal_handlers=False,
    )

    assert result == 7
    assert ("record_requested_restart", 0) in supervisor.calls
    assert "restart" in supervisor.calls
    assert ("recover_unexpected_exit", 7) in supervisor.calls
    assert supervisor.calls[-1] == ("shutdown", False)


def test_unhealthy_node_is_closed() -> None:
    supervisor = FakeSupervisor(
        exits=[],
        requested_restarts=[],
        restart_results=[],
        recovery_results=[],
    )
    supervisor.wait_healthy = lambda timeout=None: False

    try:
        node_source_launcher.run(
            8002,
            supervisor=supervisor,
            stopping=threading.Event(),
            install_signal_handlers=False,
        )
    except RuntimeError as exc:
        assert str(exc) == "worker node failed to become healthy"
    else:
        raise AssertionError("unhealthy worker node was accepted")

    assert supervisor.calls[-1] == ("shutdown", False)


# ---------------------------------------------------------------------------
# Supervisors that mutate the stopping Event from inside lifecycle hooks, to
# exercise the run() loop's stopping short-circuits (lines 44-45 and 52).
# ---------------------------------------------------------------------------

class _StoppingOnExit(FakeSupervisor):
    def __init__(self, stopping, **kwargs):
        super().__init__(**kwargs)
        self._stopping = stopping

    def wait_exit(self):
        code = super().wait_exit()
        self._stopping.set()
        return code


class _RecoverSetsStopping(FakeSupervisor):
    def __init__(self, stopping, **kwargs):
        super().__init__(**kwargs)
        self._stopping = stopping

    def recover_unexpected_exit(self, exit_code):
        result = super().recover_unexpected_exit(exit_code)
        self._stopping.set()
        return result


def test_stopping_set_during_wait_exit_returns_zero() -> None:
    stopping = threading.Event()
    supervisor = _StoppingOnExit(
        stopping, exits=[0], requested_restarts=[False],
        restart_results=[], recovery_results=[],
    )
    result = node_source_launcher.run(
        8002, supervisor=supervisor, stopping=stopping,
        install_signal_handlers=False,
    )
    assert result == 0
    assert ("wait_exit") in supervisor.calls
    assert supervisor.calls[-1] == ("shutdown", False)


def test_restart_false_falls_through_to_recover() -> None:
    supervisor = FakeSupervisor(
        exits=[0], requested_restarts=[True],
        restart_results=[False], recovery_results=[False],
    )
    result = node_source_launcher.run(
        8002, supervisor=supervisor, stopping=threading.Event(),
        install_signal_handlers=False,
    )
    assert result == 1
    assert "start" in supervisor.calls
    assert "restart" in supervisor.calls
    assert ("recover_unexpected_exit", 0) in supervisor.calls


def test_recover_true_loops_then_stopping_exits_zero() -> None:
    stopping = threading.Event()
    supervisor = _RecoverSetsStopping(
        stopping, exits=[0], requested_restarts=[False],
        restart_results=[], recovery_results=[True],
    )
    result = node_source_launcher.run(
        8002, supervisor=supervisor, stopping=stopping,
        install_signal_handlers=False,
    )
    assert result == 0
    assert ("recover_unexpected_exit", 0) in supervisor.calls


def test_signal_handlers_installed_restored_and_stop_closure(monkeypatch) -> None:
    """install_signal_handlers=True wires SIGINT/SIGTERM to a stop closure and
    restores the previous handlers in finally. Driving the captured closure
    sets the stopping Event and shuts the supervisor down (kill_runners=False)."""
    installs: list[tuple[int, object]] = []

    def fake_signal(signum, handler):
        installs.append((signum, handler))
        return f"prev-{signum}"

    monkeypatch.setattr(node_source_launcher.signal, "signal", fake_signal)

    stopping = threading.Event()
    supervisor = FakeSupervisor(
        exits=[0], requested_restarts=[False],
        restart_results=[], recovery_results=[False],
    )
    # wait_exit invokes the SIGINT closure so run returns 0 via the stop path
    # instead of the recover-False path.
    real_wait_exit = supervisor.wait_exit

    def wait_exit_then_stop():
        code = real_wait_exit()
        installs[0][1](signal.SIGINT, None)  # invoke the captured SIGINT handler
        return code

    supervisor.wait_exit = wait_exit_then_stop

    result = node_source_launcher.run(
        8002, supervisor=supervisor, stopping=stopping,
        install_signal_handlers=True,
    )

    assert result == 0
    # install(SIGINT,SIGTERM) then restore(SIGINT,SIGTERM).
    assert [s for s, _ in installs] == [
        signal.SIGINT, signal.SIGTERM, signal.SIGINT, signal.SIGTERM,
    ]
    assert installs[2][1] == f"prev-{signal.SIGINT}"
    assert installs[3][1] == f"prev-{signal.SIGTERM}"
    # The stop closure set the stopping Event and shut the supervisor down.
    assert stopping.is_set()
    assert supervisor.calls.count(("shutdown", False)) >= 2


def test_main_valid_port_invokes_run(monkeypatch) -> None:
    seen = {}

    def fake_run(port, **kw):
        seen["port"] = port
        return 42

    monkeypatch.setattr(node_source_launcher, "run", fake_run)
    assert node_source_launcher.main(["--port", "9000"]) == 42
    assert seen["port"] == 9000


def test_main_default_port_is_8002(monkeypatch) -> None:
    seen = {}

    def fake_run(port, **kw):
        seen["port"] = port
        return 0

    monkeypatch.setattr(node_source_launcher, "run", fake_run)
    node_source_launcher.main([])
    assert seen["port"] == 8002


def test_main_invalid_port_exits(monkeypatch) -> None:
    monkeypatch.setattr(node_source_launcher, "run", lambda port, **kw: 0)
    with pytest.raises(SystemExit):
        node_source_launcher.main(["--port", "0"])

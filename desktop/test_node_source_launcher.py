from __future__ import annotations

import signal
import threading
from pathlib import Path

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
    import credential_session
    import node_credential_store
    import supervisor as supervisor_module

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
        node_credential_store,
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
        credential_session,
        "ProviderCredentialBroker",
        credential_broker,
    )
    monkeypatch.setattr(
        supervisor_module,
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

    def fake_run(state_root, port, **kw):
        seen["state_root"] = state_root
        seen["port"] = port
        return 42

    monkeypatch.setattr(node_source_launcher, "_run_owned", fake_run)
    assert node_source_launcher.main(["--port", "9000"]) == 42
    assert seen["port"] == 9000


def test_main_default_port_is_8002(monkeypatch) -> None:
    seen = {}

    def fake_run(state_root, port, **kw):
        seen["port"] = port
        return 0

    monkeypatch.setattr(node_source_launcher, "_run_owned", fake_run)
    node_source_launcher.main([])
    assert seen["port"] == 8002


def test_main_invalid_port_exits(monkeypatch) -> None:
    monkeypatch.setattr(node_source_launcher, "_run_owned", lambda root, port: 0)
    with pytest.raises(SystemExit):
        node_source_launcher.main(["--port", "0"])


def test_main_defers_missing_topology_to_owned_launcher(monkeypatch, tmp_path) -> None:
    state_root = tmp_path / "missing-home"
    topology = tmp_path / "missing-topology.yaml"
    seen = {}

    def owned(root, port):
        seen["root"] = root
        seen["topology"] = Path(
            node_source_launcher.os.environ["BETTER_AGENT_TOPOLOGY_PATH"]
        )
        return 1

    monkeypatch.setattr(node_source_launcher, "_run_owned", owned)
    assert node_source_launcher.main([
        "--state-root", str(state_root),
        "--topology-path", str(topology),
    ]) == 1
    assert seen == {"root": state_root, "topology": topology}


def test_owned_launcher_publishes_lifecycle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import node_service
    import paths

    state_root = tmp_path / "home"
    state_root.mkdir(mode=0o700)
    paths.make_private_directory(state_root)
    states: list[str] = []
    monkeypatch.setattr(
        node_service, "require_durable_topology", lambda *args: "wss://primary"
    )
    monkeypatch.setattr(node_service, "begin_launcher_attempt", lambda root: True)
    monkeypatch.setattr(node_service, "mark_launcher_healthy", lambda root: None)
    monkeypatch.setattr(
        node_service,
        "publish_launcher_status",
        lambda root, state, **kwargs: states.append(state),
    )
    def run_node(port, *, stopping, on_healthy):
        on_healthy()
        return 0

    assert node_source_launcher._run_owned(
        state_root,
        8002,
        run_node=run_node,
    ) == 0
    assert states == ["starting", "running", "stopped"]


def test_owned_launcher_records_topology_failure_as_failed_attempt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import node_service
    import paths

    state_root = tmp_path / "home"
    state_root.mkdir(mode=0o700)
    paths.make_private_directory(state_root)
    states: list[str] = []
    monkeypatch.setattr(node_service, "begin_launcher_attempt", lambda root: True)
    monkeypatch.setattr(
        node_service,
        "require_durable_topology",
        lambda *args: (_ for _ in ()).throw(RuntimeError("bad topology")),
    )
    monkeypatch.setattr(
        node_service,
        "publish_launcher_status",
        lambda root, state, **kwargs: states.append(state),
    )

    with pytest.raises(RuntimeError, match="bad topology"):
        node_source_launcher._run_owned(state_root, 8002)
    assert states == ["starting", "failed"]


def test_owned_launcher_keeps_circuit_open_until_stopped(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import node_service
    import paths

    state_root = tmp_path / "home"
    state_root.mkdir(mode=0o700)
    paths.make_private_directory(state_root)
    stopping = threading.Event()
    stopping.set()
    states: list[str] = []
    monkeypatch.setattr(node_service, "require_durable_topology", lambda: "wss://primary")
    monkeypatch.setattr(node_service, "begin_launcher_attempt", lambda root: False)
    monkeypatch.setattr(
        node_service,
        "publish_launcher_status",
        lambda root, state, **kwargs: states.append(state),
    )

    assert node_source_launcher._run_owned(
        state_root,
        8002,
        stopping=stopping,
    ) == 1
    assert states == ["circuit_open"]

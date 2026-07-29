from __future__ import annotations

import threading

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

    def wait_healthy(self) -> bool:
        self.calls.append("wait_healthy")
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
    supervisor.wait_healthy = lambda: False

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

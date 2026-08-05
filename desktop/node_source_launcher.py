from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Protocol


class Supervisor(Protocol):
    def start(self) -> None: ...
    def wait_healthy(self, timeout: float | None = 30.0) -> bool: ...
    def wait_exit(self) -> int: ...
    def restart_was_requested(self) -> bool: ...
    def record_requested_restart(self, exit_code: int) -> None: ...
    def restart(self) -> bool: ...
    def recover_unexpected_exit(self, exit_code: int) -> bool: ...
    def shutdown(self, *, kill_runners: bool) -> None: ...


def _default_supervisor(port: int) -> Supervisor:
    from credential_session import ProviderCredentialBroker
    from node_credential_store import node_provider_credential_store
    from supervisor import BackendSupervisor

    return BackendSupervisor(
        role="node",
        port=port,
        credential_broker=ProviderCredentialBroker(
            node_provider_credential_store(),
        ),
    )


@contextmanager
def _signal_handlers(
    stopping: threading.Event,
    stop: Callable[[], None],
    *,
    enabled: bool,
) -> Iterator[None]:
    previous_handlers: dict[int, object] = {}

    def handle_stop(*_: object) -> None:
        stopping.set()
        stop()

    if enabled:
        previous_handlers = {
            signum: signal.signal(signum, handle_stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
    try:
        yield
    finally:
        if enabled:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def run(
    port: int,
    *,
    supervisor: Supervisor | None = None,
    stopping: threading.Event | None = None,
    install_signal_handlers: bool = True,
    on_healthy: Callable[[], None] | None = None,
) -> int:
    stopping = stopping or threading.Event()
    supervisor = supervisor or _default_supervisor(port)

    def shutdown() -> None:
        supervisor.shutdown(kill_runners=False)

    with _signal_handlers(
        stopping,
        shutdown,
        enabled=install_signal_handlers,
    ):
        try:
            supervisor.start()
            if not supervisor.wait_healthy(timeout=None):
                raise RuntimeError("worker node failed to become healthy")
            if on_healthy is not None:
                on_healthy()
            while not stopping.is_set():
                exit_code = supervisor.wait_exit()
                if stopping.is_set():
                    return 0
                if supervisor.restart_was_requested():
                    supervisor.record_requested_restart(exit_code)
                    if supervisor.restart():
                        continue
                if not supervisor.recover_unexpected_exit(exit_code):
                    return exit_code or 1
            return 0
        finally:
            shutdown()


def _run_owned(
    state_root: Path,
    port: int,
    *,
    stopping: threading.Event | None = None,
    run_node: Callable[..., int] = run,
) -> int:
    from node_launcher_lease import NodeLauncherLease
    from node_service import (
        begin_launcher_attempt,
        mark_launcher_healthy,
        publish_launcher_status,
        require_durable_topology,
    )

    stopping = stopping or threading.Event()
    checkout = Path(sys.executable).resolve(strict=False).parent
    with NodeLauncherLease.acquire(state_root, checkout=checkout):
        if not begin_launcher_attempt(state_root):
            publish_launcher_status(
                state_root,
                "circuit_open",
                detail="restart limit reached",
            )
            with _signal_handlers(stopping, lambda: None, enabled=True):
                stopping.wait()
            return 1

        def healthy() -> None:
            mark_launcher_healthy(state_root)
            publish_launcher_status(state_root, "running")

        publish_launcher_status(state_root, "starting")
        try:
            require_durable_topology(state_root)
            result = run_node(port, stopping=stopping, on_healthy=healthy)
        except Exception as exc:
            publish_launcher_status(state_root, "failed", detail=str(exc))
            raise
        if result != 0:
            publish_launcher_status(
                state_root,
                "failed",
                detail=f"node exited with status {result}",
            )
        else:
            publish_launcher_status(state_root, "stopped")
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=8002, type=int)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--topology-path", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")

    if args.state_root is not None:
        state_root = args.state_root.expanduser().resolve(strict=False)
        os.environ["BETTER_AGENT_HOME"] = str(state_root)
        os.environ["BETTER_CLAUDE_HOME"] = str(state_root)
    else:
        from paths import ba_home

        state_root = ba_home()
    if args.topology_path is not None:
        topology_path = args.topology_path.expanduser().resolve(strict=False)
        os.environ["BETTER_AGENT_TOPOLOGY_PATH"] = str(topology_path)
        os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(topology_path)
    return _run_owned(state_root, args.port)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

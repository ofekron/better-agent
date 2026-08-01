from __future__ import annotations

import argparse
import signal
import threading

from credential_session import ProviderCredentialBroker
from node_credential_store import node_provider_credential_store
from supervisor import BackendSupervisor


def run(
    port: int,
    *,
    supervisor: BackendSupervisor | None = None,
    stopping: threading.Event | None = None,
    install_signal_handlers: bool = True,
) -> int:
    stopping = stopping or threading.Event()
    supervisor = supervisor or BackendSupervisor(
        role="node",
        port=port,
        credential_broker=ProviderCredentialBroker(
            node_provider_credential_store(),
        ),
    )

    def stop(*_: object) -> None:
        stopping.set()
        supervisor.shutdown(kill_runners=False)

    previous_handlers = {}
    if install_signal_handlers:
        previous_handlers = {
            signum: signal.signal(signum, stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
    try:
        supervisor.start()
        if not supervisor.wait_healthy(timeout=None):
            raise RuntimeError("worker node failed to become healthy")
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
        supervisor.shutdown(kill_runners=False)
        if install_signal_handlers:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=8002, type=int)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")
    return run(args.port)


if __name__ == "__main__":  # pragma: no cover - entry-point guard; runs the real node supervisor + backend socket
    raise SystemExit(main())

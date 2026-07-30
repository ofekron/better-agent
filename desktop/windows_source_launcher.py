from __future__ import annotations

import argparse
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from backend_exit_journal import append_backend_exit
from backend_recovery_policy import decide_recovery
from browser_backend_supervisor import BrowserBackendSupervisor
from restart_request import clear_restart_request, consume_restart_request

_PRIMARY_ATTESTATION_TIMEOUT_SECONDS = 90.0


def _ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/readyz",
            timeout=0.2,
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def run(
    checkout: Path,
    host: str,
    port: int,
    *,
    supervisor: BrowserBackendSupervisor | None = None,
    state_root: Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    wall_time: Callable[[], float] = time.time,
    health_probe: Callable[[int], bool] = _ready,
    install_signal_handlers: bool = True,
    primary_attestation_timeout_seconds: float = _PRIMARY_ATTESTATION_TIMEOUT_SECONDS,
) -> int:
    from paths import bc_home

    state_root = state_root or bc_home()
    restart_path = state_root / "restart_requested"
    supervisor = supervisor or BrowserBackendSupervisor(checkout, dict(os.environ))
    stopping = threading.Event()

    def stop(*_: object) -> None:
        stopping.set()
        try:
            supervisor.handle({"op": "signal", "signal": "TERM"})
        except Exception:
            pass

    previous_handlers = {}
    if install_signal_handlers:
        previous_handlers = {
            signum: signal.signal(signum, stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
    attempts = 0
    try:
        while not stopping.is_set():
            clear_restart_request(restart_path)
            started_at = wall_time()
            healthy_at: float | None = None
            from daemonhost import pointer

            active_checkout = Path(pointer.resolve(str(checkout))).resolve()
            generation = supervisor.handle({
                "op": "start",
                "checkout": str(active_checkout),
                "host": host,
                "port": port,
            })
            generation_id = str(generation["generation_id"])
            pid = int(generation["pid"])
            activation_reverted = False
            while not stopping.wait(0.25):
                status = supervisor.handle({"op": "status"})
                if status.get("terminal") is True:
                    break
                if healthy_at is None and health_probe(port):
                    import repository_alignment

                    activation = repository_alignment.finalize_node_activation(
                        active_checkout
                    )
                    if activation == "active":
                        healthy_at = monotonic()
                    elif activation == "rejected" or (
                        monotonic() - started_at
                        >= primary_attestation_timeout_seconds
                    ):
                        if activation != "rejected":
                            repository_alignment.expire_node_activation(
                                active_checkout
                            )
                        supervisor.handle({"op": "signal", "signal": "TERM"})
                        activation_reverted = True
            if stopping.is_set():
                return 0
            status = supervisor.handle({"op": "status"})
            if (
                status.get("terminal") is not True
                or status.get("generation_id") != generation_id
                or not isinstance(status.get("returncode"), int)
            ):
                raise RuntimeError("backend generation ended without terminal acknowledgement")
            exit_code = int(status["returncode"])
            if activation_reverted:
                attempts = 0
                continue
            request_id = consume_restart_request(
                restart_path,
                not_before=started_at,
            )
            if request_id is not None:
                attempts = 0
                append_backend_exit(
                    state_root,
                    source="run_windows",
                    exit_code=exit_code,
                    classification="refresh",
                    decision="restart",
                    generation_id=generation_id,
                    pid=pid,
                    checkout=str(active_checkout),
                    request_id=request_id,
                )
                continue
            decision = decide_recovery(
                attempts=attempts,
                healthy_seconds=(
                    0 if healthy_at is None else int(monotonic() - healthy_at)
                ),
                limit=5,
                stability_seconds=60,
            )
            attempts = decision.attempts
            append_backend_exit(
                state_root,
                source="run_windows",
                exit_code=exit_code,
                classification="unexpected",
                decision=decision.action,
                restart_attempt=attempts,
                generation_id=generation_id,
                pid=pid,
                checkout=str(active_checkout),
            )
            if decision.action == "circuit_open":
                return exit_code or 1
            if stopping.wait(decision.backoff_seconds):
                return 0
        return 0
    finally:
        supervisor.shutdown()
        if install_signal_handlers:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args(argv)
    checkout = args.checkout.resolve()
    if args.host not in {"127.0.0.1", "0.0.0.0"}:
        parser.error("invalid host")
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")
    if not (checkout / "backend" / "main.py").is_file():
        parser.error("checkout is not runnable")
    return run(checkout, args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())

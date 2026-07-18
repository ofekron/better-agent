from __future__ import annotations

import signal
import sys
import threading

from .requests import service_tick


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return 0
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_args: stopped.set())
    while not stopped.wait(0.25):
        service_tick()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

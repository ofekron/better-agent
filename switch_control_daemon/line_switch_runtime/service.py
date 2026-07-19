from __future__ import annotations

import signal
import sys
import threading

from .requests import service_tick
from .web import create_server


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return 0
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_args: stopped.set())
    server = create_server()
    server_thread = threading.Thread(target=server.serve_forever, name="line-switch-web")
    server_thread.start()
    try:
        while not stopped.wait(0.25):
            service_tick()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

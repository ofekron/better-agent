"""CLI entry: run the daemon host (run.sh spawns this in prod mode).

``--once`` performs a single reconcile pass and exits (used by tests).
"""

from __future__ import annotations

import argparse
import signal
import sys

from daemonhost.host import DaemonHost


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="daemonhost")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    host = DaemonHost(poll_interval=args.poll_interval)
    if args.once:
        host.reconcile_once()
        host.stop()
        host.run()
        return 0
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: host.stop())
    host.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

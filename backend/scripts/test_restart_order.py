#!/usr/bin/env python3
from pathlib import Path


RUN_SH = Path(__file__).resolve().parents[2] / "run.sh"


def main() -> None:
    source = RUN_SH.read_text(encoding="utf-8")
    loop = source[source.index('PENDING_REFRESH_ID=""') :]

    backend_start = loop.index("start_backend")
    backend_ready = loop.index("wait_for_backend")
    initial_build = loop.index('start_frontend_build ""')
    refresh_build = loop.index('start_frontend_build "$PENDING_REFRESH_ID"')

    assert backend_start < initial_build < backend_ready, (
        "initial frontend build must start in parallel after backend spawn, before backend health wait"
    )
    assert backend_start < backend_ready < refresh_build, (
        "refresh must wait for the restarted backend before rebuilding frontend"
    )
    print("PASS: backend starts before frontend build and refresh build waits for health")


if __name__ == "__main__":
    main()

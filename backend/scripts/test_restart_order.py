#!/usr/bin/env python3
from pathlib import Path


RUN_SH = Path(__file__).resolve().parents[2] / "run.sh"


def main() -> None:
    source = RUN_SH.read_text(encoding="utf-8")
    loop = source[source.index('PENDING_REFRESH_ID=""') :]

    initial_build = source.index('build_frontend ""')
    loop_start = source.index('PENDING_REFRESH_ID=""')
    backend_start = loop.index("start_backend")
    backend_ready = loop.index("wait_for_backend")
    refresh_build = loop.index('build_frontend "$PENDING_REFRESH_ID"')

    assert initial_build < loop_start, "initial frontend build must precede the supervisor loop"
    assert backend_start < backend_ready < refresh_build, (
        "refresh must wait for the restarted backend before rebuilding frontend"
    )
    print("PASS: backend is healthy before the refresh-time frontend build")


if __name__ == "__main__":
    main()

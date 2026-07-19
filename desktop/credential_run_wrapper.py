from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from credential_session import ProviderCredentialSession


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        raise RuntimeError("run script path is required")
    run_script = Path(args[0]).resolve()
    session = ProviderCredentialSession()
    session.start()
    env = dict(os.environ)
    for key in (
        "BETTER_AGENT_CREDENTIAL_SESSION_ADDRESS",
        "BETTER_AGENT_CREDENTIAL_SESSION_AUTH",
        "BETTER_AGENT_CREDENTIAL_SESSION_FAMILY",
        "BETTER_AGENT_CREDENTIAL_SESSION_FD",
    ):
        env.pop(key, None)
    env.update(session.backend_env())
    env["BETTER_AGENT_CREDENTIAL_SESSION_WRAPPED"] = "1"
    proc = subprocess.Popen(
        ["bash", str(run_script), *args[1:]],
        cwd=run_script.parent,
        env=env,
        start_new_session=True,
        **session.backend_popen_kwargs(),
    )

    def forward(signum: int, _frame: object) -> None:
        if proc.poll() is None:
            proc.send_signal(signum)

    previous = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        return proc.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if proc.poll() is None:
            proc.terminate()
            proc.wait()
        session.stop()


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import os
import subprocess

from credential_broker.descriptor import coerce_secret_map, substitute_secrets
from credential_broker.executors.base import ExecResult, SinkExecutor


class ExecSinkExecutor(SinkExecutor):
    kind = "exec"

    def execute(self, descriptor: dict, secret: str | dict[str, str]) -> ExecResult:
        secrets = coerce_secret_map(secret)
        sink = descriptor["sink"]
        stdin = substitute_secrets(sink["stdin_template"], secrets)
        env = dict(os.environ)
        for key, value in list(env.items()):
            if any(secret_value and secret_value in value for secret_value in secrets.values()):
                del env[key]

        try:
            proc = subprocess.run(
                sink["argv"],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=sink["timeout_s"],
                shell=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(ok=False, error="command timed out")
        except OSError:
            return ExecResult(ok=False, error="failed to execute command")

        return ExecResult(
            ok=proc.returncode == 0,
            status=proc.returncode,
            body=proc.stdout,
            stderr=proc.stderr,
            error="" if proc.returncode == 0 else f"command exited {proc.returncode}",
        )

"""Single canonical runner for task-related scripts: trigger detectors,
pre/post run scripts, and script-based assessments.

Always runs an argv list (never a shell string) so task input cannot inject
commands. Inherits only a curated env (PATH/HOME/locale) — never the full
process env, which may carry provider tokens — so a user-authored task script
cannot exfiltrate backend secrets.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120
_MAX_OUTPUT = 1_000_000

_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "SHELL", "TZ",
)


@dataclass
class ScriptResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _curated_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    return env


def run_script(
    script: Optional[dict],
    *,
    fallback_cwd: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Optional[ScriptResult]:
    """Run {command: [...], cwd?: str}. Returns None if `script` is empty.
    Never raises on subprocess failure — encodes it in ScriptResult so callers
    (assessment, pipeline) can turn it into a verdict."""
    if not script or not isinstance(script, dict):
        return None
    command = script.get("command")
    if not isinstance(command, list) or not command:
        return None
    cwd = (script.get("cwd") or fallback_cwd or None)
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=_curated_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return ScriptResult(
            exit_code=proc.returncode,
            stdout=(proc.stdout or "")[:_MAX_OUTPUT],
            stderr=(proc.stderr or "")[:_MAX_OUTPUT],
            timed_out=False,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else ""
        return ScriptResult(
            exit_code=124,
            stdout=out[:_MAX_OUTPUT],
            stderr=(err or "timed out")[:_MAX_OUTPUT],
            timed_out=True,
        )
    except (OSError, ValueError) as e:
        return ScriptResult(exit_code=126, stdout="", stderr=str(e)[:_MAX_OUTPUT], timed_out=False)


def run_scripts(
    scripts: list[dict],
    *,
    fallback_cwd: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Run a list of scripts in order. Returns (all_ok, combined_stdout).
    Stops at the first failure (pre-scripts gate the run; post-scripts are
    best-effort and continue on failure). Callers choose stop-on-fail vs
    continue by splitting the list."""
    combined: list[str] = []
    for s in scripts:
        res = run_script(s, fallback_cwd=fallback_cwd, timeout=timeout)
        if res is None:
            continue
        if res.stdout:
            combined.append(res.stdout)
        if not res.ok:
            return False, "\n".join(combined)
    return True, "\n".join(combined)

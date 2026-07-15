"""run.sh port-resolution regression locks.

Locks:
- diagnostics in resolve_port_conflict's kill chain go to stderr, so the
  command substitution wrapping it captures exactly one numeric port
  (regression: a leaked "Stopping previous ... process(es): <pid>" stdout
  line produced BETTER_*_BACKEND_URL values like
  "http://127.0.0.1:Stopping previous Better Agent BFF process(es): 58127\n18765"
  which crashed every urllib consumer with "nonnumeric port")
- require_single_numeric_port fails closed on multi-line / whitespace /
  non-numeric / out-of-range values
- every resolve_port_conflict capture site in run.sh is immediately
  validated by require_single_numeric_port
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

_RUN_SH = Path(__file__).resolve().parents[2] / "run.sh"

_FUNCTIONS = (
    "kill_matching_processes",
    "kill_port_listeners",
    "port_in_use",
    "require_single_numeric_port",
    "resolve_port_conflict",
    "stop_known_better_agent_port_users",
)


def _extract_functions() -> str:
    text = _RUN_SH.read_text(encoding="utf-8")
    chunks: list[str] = []
    for name in _FUNCTIONS:
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", text, re.M | re.S)
        assert match, f"function {name}() not found in run.sh"
        chunks.append(match.group(0))
    return "\n\n".join(chunks)


def _write_shim(directory: Path, name: str, body: str) -> None:
    shim = directory / name
    shim.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_conflict_kill_path_captures_exactly_one_numeric_port() -> None:
    """Simulate a real port conflict resolved via the kill path ("k") and
    assert the command substitution captures only the port number."""
    with tempfile.TemporaryDirectory(prefix="ba-run-sh-port-") as tmp:
        tmpdir = Path(tmp)
        marker = tmpdir / "killed.marker"
        shims = tmpdir / "bin"
        shims.mkdir()
        # lsof/pgrep report a fake listener (pid 58127) until the kill shim
        # drops the marker; the kill shim never signals real processes.
        report = f'if [ -e "{marker}" ]; then exit 1; fi\necho 58127\n'
        _write_shim(shims, "lsof", report)
        _write_shim(shims, "pgrep", report)
        _write_shim(shims, "kill", f'touch "{marker}"\n')

        driver = tmpdir / "driver.sh"
        driver.write_text(
            "set -u\n"
            + _extract_functions()
            + "\n\nBACKEND_PORT=18765\n"
            'resolved="$(printf \'k\\n\' | resolve_port_conflict "$BACKEND_PORT" backend)"\n'
            'printf \'%s\' "$resolved"\n',
            encoding="utf-8",
        )
        env = {**os.environ, "PATH": f"{shims}:{os.environ['PATH']}"}
        result = subprocess.run(
            ["bash", str(driver)], capture_output=True, text=True, env=env, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "18765", (
            f"resolve_port_conflict leaked non-port output into the captured "
            f"value: {result.stdout!r}"
        )
        assert marker.exists(), "kill path was never exercised"
        assert "Stopping previous" in result.stderr


def _require_port(value: str) -> int:
    script = (
        _extract_functions()
        + '\nrequire_single_numeric_port "$1" test-label\n'
    )
    return subprocess.run(
        ["bash", "-c", script, "bash", value], capture_output=True, timeout=30,
    ).returncode


def test_require_single_numeric_port_fails_closed() -> None:
    assert _require_port("18765") == 0
    assert _require_port(" 58127\n18765") != 0
    assert _require_port("58127\n18765") != 0
    assert _require_port(" 18765") != 0
    assert _require_port("Stopping previous Better Agent BFF process(es): 58127") != 0
    assert _require_port("") != 0
    assert _require_port("0") != 0
    assert _require_port("65536") != 0


def test_every_resolve_port_conflict_capture_is_validated() -> None:
    lines = _RUN_SH.read_text(encoding="utf-8").splitlines()
    captures = [
        i for i, line in enumerate(lines)
        if re.search(r'="\$\(resolve_port_conflict ', line)
    ]
    assert captures, "no resolve_port_conflict capture sites found in run.sh"
    for i in captures:
        assert "require_single_numeric_port" in lines[i + 1], (
            f"run.sh line {i + 1} captures resolve_port_conflict without "
            "validating the result on the next line"
        )

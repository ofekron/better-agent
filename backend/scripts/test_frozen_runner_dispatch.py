"""Regression test: frozen-aware runner spawn + app entrypoint dispatch.

Pins two contracts for the PyInstaller-frozen macOS app:
  - `provider.runner_argv` re-execs the app binary (not `python script`)
    when `sys.frozen` is set, and keeps the byte-identical dev form
    otherwise.
  - `app_entry._dispatch` routes `--run-dir` argv to the right runner and
    bare argv to the server.

Run with:
    cd backend && .venv/bin/python scripts/test_frozen_runner_dispatch.py
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-frozen-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from provider import runner_argv          # noqa: E402
from app_entry import _dispatch, _env_port  # noqa: E402
from env_compat import agent_env_name  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_runner_argv_dev() -> None:
    """Dev (not frozen): argv is `<python> <script> --run-dir <dir>` —
    byte-identical to the pre-change spawn."""
    argv = runner_argv(
        Path("/runs/x"), dev_script=Path("/b/runner.py"), kind="claude",
    )
    assert argv == [sys.executable, "/b/runner.py", "--run-dir", "/runs/x"], \
        f"dev claude: got {argv}"
    argv_g = runner_argv(
        Path("/runs/y"), dev_script=Path("/b/runner_agy.py"), kind="agy",
    )
    assert argv_g == [sys.executable, "/b/runner_agy.py", "--run-dir", "/runs/y"], \
        f"dev agy: got {argv_g}"


def test_runner_argv_frozen() -> None:
    """Frozen: argv re-execs the app binary; agy carries --runner-kind,
    claude (the default) does not."""
    sys.frozen = True  # simulate PyInstaller
    try:
        argv = runner_argv(
            Path("/runs/x"), dev_script=Path("/b/runner.py"), kind="claude",
        )
        assert argv == [sys.executable, "--run-dir", "/runs/x"], f"frozen claude: got {argv}"
        argv_g = runner_argv(
            Path("/runs/y"), dev_script=Path("/b/runner_agy.py"),
            kind="agy",
        )
        assert argv_g == [
            sys.executable, "--run-dir", "/runs/y", "--runner-kind", "agy",
        ], f"frozen agy: got {argv_g}"
    finally:
        del sys.frozen


def test_dispatch() -> None:
    """`_dispatch` routes --run-dir to a runner (claude default / agy
    explicit) and bare argv to the server."""
    assert _dispatch([]) == ("server", None, None, None), f"bare argv: got {_dispatch([])}"
    assert _dispatch(["--serve"]) == ("server", None, None, None), \
        f"--serve argv: got {_dispatch(['--serve'])}"
    assert _dispatch(["--serve-node"]) == ("node_server", None, None, None), \
        f"--serve-node argv: got {_dispatch(['--serve-node'])}"
    assert _dispatch(["--communicate-mcp"]) == ("communicate_mcp", None, None, None), \
        f"--communicate-mcp: got {_dispatch(['--communicate-mcp'])}"
    assert _dispatch(["--open-file-panel-mcp"]) == ("open_file_panel_mcp", None, None, None), \
        f"--open-file-panel-mcp: got {_dispatch(['--open-file-panel-mcp'])}"
    assert _dispatch(["--frozen-artifact-smoke"]) == (
        "frozen_artifact_smoke", None, None, None,
    ), f"--frozen-artifact-smoke: got {_dispatch(['--frozen-artifact-smoke'])}"
    assert _dispatch(["--run-dir", "/runs/x"]) == (
        "runner", "claude", Path("/runs/x"), "",
    ), f"--run-dir: got {_dispatch(['--run-dir', '/runs/x'])}"
    got = _dispatch(["--run-dir", "/runs/y", "--runner-kind", "agy"])
    assert got == ("runner", "agy", Path("/runs/y"), ""), f"--run-dir agy: got {got}"
    got_claude_ba = _dispatch([
        "--run-dir", "/runs/z", "--runner-kind", "claude",
        "--runner-module", "runner_better_agent",
    ])
    assert got_claude_ba == (
        "runner", "claude", Path("/runs/z"), "runner_better_agent",
    ), f"--run-dir claude better_agent_runner: got {got_claude_ba}"


def test_env_port() -> None:
    legacy_name = "BETTER_CLAUDE_TEST_PORT"
    canonical_name = agent_env_name(legacy_name)
    original = {
        name: os.environ.get(name)
        for name in (canonical_name, legacy_name)
    }

    def available_ports(count: int) -> list[int]:
        sockets: list[socket.socket] = []
        try:
            for _ in range(count):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(sock)
                sock.bind(("127.0.0.1", 0))
            return [int(sock.getsockname()[1]) for sock in sockets]
        finally:
            for sock in sockets:
                sock.close()

    try:
        os.environ.pop(canonical_name, None)
        os.environ.pop(legacy_name, None)
        default, legacy_port, canonical_port = available_ports(3)
        assert _env_port(legacy_name, default) == default, "missing env should use default"

        os.environ[legacy_name] = str(legacy_port)
        os.environ[canonical_name] = str(canonical_port)
        assert _env_port(legacy_name, default) == canonical_port, \
            "canonical env override was not used"

        os.environ.pop(canonical_name, None)
        assert _env_port(legacy_name, default) == legacy_port, \
            "legacy env fallback was not used"

        os.environ.pop(legacy_name, None)
        for value in ("0", "70000", "abc"):
            os.environ[canonical_name] = value
            try:
                _env_port(legacy_name, default)
            except (RuntimeError, ValueError):
                continue
            raise AssertionError(f"expected invalid port to fail: {value}")
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


TESTS = [
    ("runner_argv dev form is byte-identical to pre-change", test_runner_argv_dev),
    ("runner_argv frozen form re-execs the app binary", test_runner_argv_frozen),
    ("app_entry._dispatch routes argv correctly", test_dispatch),
    ("app_entry._env_port validates env port overrides", test_env_port),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()
                print(f"{FAIL}  {name}")
                failed += 1
                continue
            print(f"{PASS}  {name}")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())

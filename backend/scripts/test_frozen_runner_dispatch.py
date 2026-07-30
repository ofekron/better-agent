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
import tempfile
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


def test_runner_argv_dev() -> bool:
    """Dev (not frozen): argv is `<python> <script> --run-dir <dir>` —
    byte-identical to the pre-change spawn."""
    argv = runner_argv(
        Path("/runs/x"), dev_script=Path("/b/runner.py"), kind="claude",
    )
    if argv != [sys.executable, "/b/runner.py", "--run-dir", "/runs/x"]:
        print(f"  dev claude: got {argv}")
        return False
    argv_g = runner_argv(
        Path("/runs/y"), dev_script=Path("/b/runner_agy.py"), kind="agy",
    )
    if argv_g != [sys.executable, "/b/runner_agy.py", "--run-dir", "/runs/y"]:
        print(f"  dev agy: got {argv_g}")
        return False
    return True


def test_runner_argv_frozen() -> bool:
    """Frozen: argv re-execs the app binary; agy carries --runner-kind,
    claude (the default) does not."""
    sys.frozen = True  # simulate PyInstaller
    try:
        argv = runner_argv(
            Path("/runs/x"), dev_script=Path("/b/runner.py"), kind="claude",
        )
        if argv != [sys.executable, "--run-dir", "/runs/x"]:
            print(f"  frozen claude: got {argv}")
            return False
        argv_g = runner_argv(
            Path("/runs/y"), dev_script=Path("/b/runner_agy.py"),
            kind="agy",
        )
        if argv_g != [
            sys.executable, "--run-dir", "/runs/y", "--runner-kind", "agy",
        ]:
            print(f"  frozen agy: got {argv_g}")
            return False
        return True
    finally:
        del sys.frozen


def test_dispatch() -> bool:
    """`_dispatch` routes --run-dir to a runner (claude default / agy
    explicit) and bare argv to the server."""
    if _dispatch([]) != ("server", None, None, None):
        print(f"  bare argv: got {_dispatch([])}")
        return False
    if _dispatch(["--serve"]) != ("server", None, None, None):
        print(f"  --serve argv: got {_dispatch(['--serve'])}")
        return False
    if _dispatch(["--serve-node"]) != ("node_server", None, None, None):
        print(f"  --serve-node argv: got {_dispatch(['--serve-node'])}")
        return False
    if _dispatch(["--communicate-mcp"]) != ("communicate_mcp", None, None, None):
        print(f"  --communicate-mcp: got {_dispatch(['--communicate-mcp'])}")
        return False
    if _dispatch(["--open-file-panel-mcp"]) != ("open_file_panel_mcp", None, None, None):
        print(f"  --open-file-panel-mcp: got {_dispatch(['--open-file-panel-mcp'])}")
        return False
    if _dispatch(["--frozen-artifact-smoke"]) != (
        "frozen_artifact_smoke", None, None, None,
    ):
        print(
            "  --frozen-artifact-smoke: got "
            f"{_dispatch(['--frozen-artifact-smoke'])}"
        )
        return False
    if _dispatch(["--run-dir", "/runs/x"]) != (
        "runner", "claude", Path("/runs/x"), "",
    ):
        print(f"  --run-dir: got {_dispatch(['--run-dir', '/runs/x'])}")
        return False
    got = _dispatch(["--run-dir", "/runs/y", "--runner-kind", "agy"])
    if got != ("runner", "agy", Path("/runs/y"), ""):
        print(f"  --run-dir agy: got {got}")
        return False
    got_claude_ba = _dispatch([
        "--run-dir", "/runs/z", "--runner-kind", "claude",
        "--runner-module", "runner_better_agent",
    ])
    if got_claude_ba != (
        "runner", "claude", Path("/runs/z"), "runner_better_agent",
    ):
        print(f"  --run-dir claude better_agent_runner: got {got_claude_ba}")
        return False
    return True


def test_env_port() -> bool:
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
        if _env_port(legacy_name, default) != default:
            print("  missing env should use default")
            return False

        os.environ[legacy_name] = str(legacy_port)
        os.environ[canonical_name] = str(canonical_port)
        if _env_port(legacy_name, default) != canonical_port:
            print("  canonical env override was not used")
            return False

        os.environ.pop(canonical_name, None)
        if _env_port(legacy_name, default) != legacy_port:
            print("  legacy env fallback was not used")
            return False

        os.environ.pop(legacy_name, None)
        for value in ("0", "70000", "abc"):
            os.environ[canonical_name] = value
            try:
                _env_port(legacy_name, default)
            except (RuntimeError, ValueError):
                continue
            print(f"  expected invalid port to fail: {value}")
            return False
        return True
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
                ok = fn()
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
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

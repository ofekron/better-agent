"""Regression: extension smoke-test subprocess must carry OS-essential env.

The python-module smoke test spawns a subprocess to verify declared modules
import. It previously passed only ``PYTHONPATH`` + ``PATH`` as the child env.
On Windows that strips ``SystemRoot``, so winsock's ``WSAStartup`` cannot load
its service-provider DLLs and any socket creation raises
``OSError [WinError 10106]``. ``import mcp`` (asyncio/anyio) creates a socket at
import, so installing the local ``ofek.testape`` MCP extension failed at smoke.
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402


def test_smoke_subprocess_env_forwards_os_essential_vars(monkeypatch):
    monkeypatch.setenv("PATH", "/host/bin")
    monkeypatch.setenv("SystemRoot", r"C:\WINDOWS")
    env = extension_store._smoke_subprocess_env(["/pkg", "/sdk"])
    assert env["PYTHONPATH"] == os.pathsep.join(["/pkg", "/sdk"])
    assert env["PATH"] == "/host/bin"
    # The var whose absence caused WinError 10106 must now be forwarded.
    assert env["SystemRoot"] == r"C:\WINDOWS"


@pytest.mark.skipif(
    sys.platform != "win32", reason="winsock WSAStartup / WinError 10106 is Windows-only"
)
def test_smoke_import_creating_socket_does_not_raise_winerror(tmp_path):
    """End-to-end: a module that inits winsock at import (like `import mcp`)
    smoke-imports cleanly. Before the fix this raised ExtensionError wrapping
    WinError 10106 because SystemRoot was stripped from the child env."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "winsock_probe.py").write_text(
        "import socket\n_s = socket.socket()\n_s.close()\n", encoding="utf-8"
    )
    # Must not raise — no assertion needed; ExtensionError would fail the test.
    extension_store._run_python_module_smoke(pkg, ["winsock_probe"])

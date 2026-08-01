"""Deterministic branch coverage for fd_limits.raise_fd_limit.

The regression test (test_fd_limit_raise.py) drives raise_fd_limit against the
real process, so which branch it hits depends on the host's current soft
RLIMIT_NOFILE — on a host whose soft limit is already adequate it never reaches
the setrlimit path, and the non-POSIX ImportError path can never run on a POSIX
host. These tests pin every branch independently of the host by stubbing
resource.getrlimit / resource.setrlimit and the resource import itself.
"""
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

import resource  # noqa: E402

import fd_limits  # noqa: E402
from fd_limits import raise_fd_limit  # noqa: E402


def test_returns_silently_when_resource_module_unavailable(monkeypatch) -> None:
    """Non-POSIX (Windows) path: `import resource` fails -> no-op return.

    Setting sys.modules['resource'] = None makes CPython's import machinery
    raise ModuleNotFoundError (an ImportError subclass) on `import resource`.
    """
    monkeypatch.setitem(sys.modules, "resource", None)
    assert raise_fd_limit() is None


def test_raises_soft_limit_and_logs_when_below_target(monkeypatch, caplog) -> None:
    setrlimit_calls: list = []

    monkeypatch.setattr(resource, "getrlimit", lambda _which: (256, 65536))
    monkeypatch.setattr(
        resource, "setrlimit", lambda which, limits: setrlimit_calls.append((which, limits))
    )

    with caplog.at_level(logging.INFO, logger="fd_limits"):
        raise_fd_limit(65536)

    # ceiling = min(65536, 65536) = 65536 > 256 -> set to 65536, hard preserved.
    assert setrlimit_calls == [(resource.RLIMIT_NOFILE, (65536, 65536))]
    assert any("raise_fd_limit" in r.message for r in caplog.records)


def test_never_lowers_and_skips_setrlimit_when_already_adequate(monkeypatch) -> None:
    setrlimit_calls: list = []

    monkeypatch.setattr(resource, "getrlimit", lambda _which: (65536, 65536))
    monkeypatch.setattr(
        resource, "setrlimit", lambda which, limits: setrlimit_calls.append((which, limits))
    )

    raise_fd_limit(65536)  # ceiling 65536 <= soft 65536 -> None -> no setrlimit

    assert setrlimit_calls == []

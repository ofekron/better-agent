from __future__ import annotations

import io
import os
import stat
import sys
from pathlib import Path

import pytest

import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_test_home.isolate("bc-test-private-diagnostics-unit-")

import private_diagnostics as mod  # noqa: E402


def _point_home(monkeypatch, tmp_path: Path) -> Path:
    """Route the diagnostic log into a per-test tmp_path, return its path."""
    monkeypatch.setattr(mod, "ba_home", lambda: tmp_path)
    return tmp_path / "faulthandler.log"


# --- open_private_diagnostics_log: secure append handle ----------------------


def test_open_creates_secure_append_handle(monkeypatch, tmp_path: Path) -> None:
    log = _point_home(monkeypatch, tmp_path)

    handle = mod.open_private_diagnostics_log()
    try:
        handle.write("first\n")
    finally:
        handle.close()

    assert log.read_text(encoding="utf-8") == "first\n"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600

    # O_APPEND: a second open leaves prior content in place.
    second = mod.open_private_diagnostics_log()
    try:
        second.write("second\n")
    finally:
        second.close()
    assert log.read_text(encoding="utf-8") == "first\nsecond\n"


def test_open_rejects_redirecting_target(monkeypatch, tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("O_NOFOLLOW redirect semantics differ on Windows")
    log = _point_home(monkeypatch, tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    log.symlink_to(victim)

    with pytest.raises(OSError):
        mod.open_private_diagnostics_log()

    assert victim.read_text(encoding="utf-8") == "unchanged"


# --- open_private_diagnostics_log: descriptor cleanup on failure (L24-26) -----


def _raise_oserror(*_args, **_kwargs) -> None:
    raise OSError("post-open setup failed")


def _assert_descriptor_cleaned(monkeypatch, tmp_path: Path) -> None:
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def spy_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def wrap_close(fd):
        closed.append(fd)
        return real_close(fd)

    _point_home(monkeypatch, tmp_path)
    monkeypatch.setattr(mod.os, "open", spy_open)
    monkeypatch.setattr(mod.os, "close", wrap_close)

    with pytest.raises(OSError):
        mod.open_private_diagnostics_log()

    assert opened, "os.open was never reached"
    descriptor = opened[-1]
    assert descriptor in closed
    # The descriptor os.open handed out must now be invalid (no fd leak).
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_open_closes_descriptor_when_fdopen_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mod.os, "fdopen", _raise_oserror)
    _assert_descriptor_cleaned(monkeypatch, tmp_path)


def test_open_closes_descriptor_when_make_private_file_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(mod, "make_private_file", _raise_oserror)
    _assert_descriptor_cleaned(monkeypatch, tmp_path)


# --- write_private_exception -------------------------------------------------


def test_write_private_exception_formats_header_chain_and_trailing_newline() -> None:
    handle = io.StringIO()
    try:
        raise ValueError("boom-detail")
    except ValueError as exc:
        mod.write_private_exception(
            handle,
            type(exc),
            exc,
            exc.__traceback__,
            context="probe",
        )

    text = handle.getvalue()
    assert text.startswith("=== probe ValueError: boom-detail ===\n")
    assert "ValueError: boom-detail" in text
    assert text.endswith("\n\n")


# --- append_private_exception ------------------------------------------------


def test_append_private_exception_writes_and_closes_handle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log = _point_home(monkeypatch, tmp_path)

    try:
        raise RuntimeError("append-detail")
    except RuntimeError as exc:
        mod.append_private_exception(exc, context="append-probe")

    text = log.read_text(encoding="utf-8")
    assert "=== append-probe RuntimeError: append-detail ===" in text
    assert "RuntimeError: append-detail" in text
    assert stat.S_IMODE(log.stat().st_mode) == 0o600

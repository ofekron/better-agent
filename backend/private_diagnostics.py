from __future__ import annotations

import os
import traceback
from types import TracebackType
from typing import TextIO

from paths import ba_home, make_private_file


def open_private_diagnostics_log() -> TextIO:
    path = ba_home() / "faulthandler.log"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        make_private_file(path)
        handle = os.fdopen(
            descriptor,
            "a",
            encoding="utf-8",
            buffering=1,
        )
    except Exception:
        os.close(descriptor)
        raise
    return handle


def write_private_exception(
    handle: TextIO,
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
    *,
    context: str = "uncaught",
) -> None:
    handle.write(f"=== {context} {exc_type.__name__}: {exc} ===\n")
    traceback.print_exception(exc_type, exc, tb, file=handle)
    handle.write("\n")


def append_private_exception(
    exc: Exception,
    *,
    context: str,
) -> None:
    with open_private_diagnostics_log() as handle:
        write_private_exception(
            handle,
            type(exc),
            exc,
            exc.__traceback__,
            context=context,
        )

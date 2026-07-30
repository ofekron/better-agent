from __future__ import annotations

import sys
import threading
from typing import Any, TextIO


class _ResilientTextStream:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._disabled = False
        self._lock = threading.Lock()

    def write(self, value: str) -> int:
        if self._disabled:
            return len(value)
        with self._lock:
            if self._disabled:
                return len(value)
            try:
                return self._stream.write(value)
            except (BrokenPipeError, ConnectionResetError):
                self._disabled = True
                return len(value)

    def flush(self) -> None:
        if self._disabled:
            return
        with self._lock:
            if self._disabled:
                return
            try:
                self._stream.flush()
            except (BrokenPipeError, ConnectionResetError):
                self._disabled = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def protect_standard_streams() -> None:
    if not isinstance(sys.stdout, _ResilientTextStream):
        sys.stdout = _ResilientTextStream(sys.stdout)
    if not isinstance(sys.stderr, _ResilientTextStream):
        sys.stderr = _ResilientTextStream(sys.stderr)
